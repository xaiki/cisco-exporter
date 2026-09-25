//! Cisco switch exporter — serves raw `show` output to Home Assistant.
//!
//! Runs on the bastion (the only host that can reach the switches).
//! Security-critical parts live here in Rust:
//!
//! - **Key handling**: the cisco key is used via the system `ssh` client,
//!   which reads it from the ssh-agent — never a keyfile on disk.
//! - **Mutual auth**: the HA poller sends `Authorization: Bearer
//!   <ha_token>`; every response carries `X-Exporter-Token:
//!   <exporter_token>` so the poller can verify it talks to the real
//!   exporter.
//! - **HTTP**: a minimal stdlib HTTP/1.1 server (one endpoint), no
//!   framework, no async runtime — small attack surface.
//!
//! The exporter runs the `show` commands via the system `ssh` (agent
//! auth) and returns the RAW output; the HA-side poller (Python) parses
//! it with the existing tested parsers. Parsing stays out of the
//! critical path.

use std::collections::HashMap;
use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::thread;
use std::time::Instant;

/// The show commands run per switch, in order.
const SHOW_COMMANDS: &[(&str, &str)] = &[
    ("version", "show version"),
    ("interfaces", "show interfaces status"),
    ("macs", "show mac address-table"),
    // The one address source that is both per-port and switch-local: a switch
    // that is L2 for a vlan never learns those addresses in its own ARP table,
    // but every DHCP exchange a snooping port relays is recorded here.
    ("binding", "show ip dhcp snooping binding"),
    ("arp", "show ip arp"),
    ("counters", "show interfaces counters"),
    ("poe", "show power inline"),
    ("ip_brief", "show ip interface brief"),
];

/// Exporter config (0600 file on the bastion).
#[derive(Clone)]
struct Config {
    ha_token: String,
    exporter_token: String,
    username: String,
    enable_secret: String,
    switches: HashMap<String, Switch>,
    /// Optional directory for the SSH control sockets. When set, a switch's
    /// commands share one session instead of logging in per command; absent
    /// keeps the old behaviour, so an existing config needs no change.
    ssh_control_dir: Option<String>,
}

#[derive(Clone)]
struct Switch {
    host: String,
    port: u16,
}

impl Config {
    fn load(path: &str) -> Result<Self, String> {
        let raw = fs::read_to_string(path).map_err(|e| format!("read config {path}: {e}"))?;
        let v: serde_json::Value =
            serde_json::from_str(&raw).map_err(|e| format!("parse config {path}: {e}"))?;
        let mut switches = HashMap::new();
        if let Some(sw) = v.get("switches").and_then(|s| s.as_object()) {
            for (sid, info) in sw {
                switches.insert(
                    sid.clone(),
                    Switch {
                        host: info
                            .get("host")
                            .and_then(|h| h.as_str())
                            .unwrap_or("")
                            .to_string(),
                        port: info.get("port").and_then(|p| p.as_u64()).unwrap_or(22) as u16,
                    },
                );
            }
        }
        Ok(Config {
            ha_token: v
                .get("ha_token")
                .and_then(|t| t.as_str())
                .unwrap_or("")
                .to_string(),
            exporter_token: v
                .get("exporter_token")
                .and_then(|t| t.as_str())
                .unwrap_or("")
                .to_string(),
            username: v
                .get("username")
                .and_then(|u| u.as_str())
                .unwrap_or("admin")
                .to_string(),
            enable_secret: v
                .get("enable_secret")
                .and_then(|e| e.as_str())
                .unwrap_or("")
                .to_string(),
            // Opt-in, and created here so a missing directory is a startup
            // error with the path in it rather than an `ssh` failure per
            // command later. Keep it short: a unix socket path has a length
            // limit, and ControlPath lives inside it.
            ssh_control_dir: control_dir_of(v.get("ssh_control_dir"))?,
            switches,
        })
    }
}

/// The optional SSH control directory, created when configured.
///
/// A directory that cannot be created is a *startup* error naming it, rather
/// than an `ssh` failure per command later that reads as a switch problem.
/// Keep the path short: the control socket lives inside it, and a unix socket
/// path has a length limit.
fn control_dir_of(value: Option<&serde_json::Value>) -> Result<Option<String>, String> {
    let Some(raw) = value.and_then(|d| d.as_str()).filter(|d| !d.is_empty()) else {
        return Ok(None);
    };
    fs::create_dir_all(raw).map_err(|e| format!("ssh_control_dir {raw}: {e}"))?;
    Ok(Some(raw.to_string()))
}

/// Run one `show` command on a switch via the system ssh (agent auth).
///
/// The key-only login lands at privilege 1; the show commands need
/// privileged EXEC. A pty (`-t`) lets us feed `enable` + the secret +
/// the command as a script, exactly like a console session.
fn ssh_command(cfg: &Config, sw: &Switch, agent: &str, legacy: bool) -> Command {
    let mut ssh = Command::new("ssh");
    ssh.arg("-t")
        .arg("-t")
        .arg("-F")
        .arg("/dev/null")
        .arg("-o")
        .arg("BatchMode=yes")
        .arg("-o")
        .arg("IdentityFile=none")
        .arg("-o")
        .arg(format!("IdentityAgent={agent}"))
        .arg("-o")
        .arg("PreferredAuthentications=publickey")
        .arg("-o")
        .arg("StrictHostKeyChecking=no")
        .arg("-o")
        .arg("UserKnownHostsFile=/dev/null")
        .arg("-o")
        .arg("ConnectTimeout=10")
        // Host-key negotiation is separate from the client's RSA signature.
        // Keep modern algorithms available while accepting older IOS hosts.
        .arg("-o")
        .arg("HostKeyAlgorithms=+ssh-rsa")
        .arg("-o")
        .arg("KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1");
    if legacy {
        // Older firmware disconnects on rsa-sha2: retry on a NEW connection.
        ssh.arg("-o").arg("PubkeyAcceptedAlgorithms=ssh-rsa");
    }
    // Bound the session, not only the connect. `ConnectTimeout` above covers
    // reaching the switch; once a session is established nothing makes ssh
    // notice that the path went away under it (a reboot, a dropped route) — it
    // waits on a socket that will never answer again, and the exporter's prompt
    // deadline is left to report the switch as merely quiet. Three unanswered
    // keepalives make ssh fail with its own message instead (`Timeout, server
    // not responding`), in about the time that deadline takes. They travel the
    // SSH channel, so they measure the transport and not IOS: a wedged CLI on a
    // live sshd still answers them, and that case stays a prompt timeout.
    ssh.arg("-o")
        .arg("ServerAliveInterval=5")
        .arg("-o")
        .arg("ServerAliveCountMax=3");
    if let Some(dir) = cfg.ssh_control_dir.as_deref() {
        // Share one SSH session for every `show` on a switch — and, while the
        // master lives, for the next request too. A poll used to cost one
        // login per command (TCP connect, key exchange, publickey auth,
        // seven times over); each multiplexed child still gets its own CLI
        // session on the switch, so `enable` and `terminal length 0` are still
        // sent per command — the *login* is what stops being paid again.
        // `%C` is a hash of (local host, remote user, host, port), so each
        // switch gets its own master.
        ssh.arg("-o")
            .arg("ControlMaster=auto")
            .arg("-o")
            .arg(format!("ControlPath={dir}/cm-%C"))
            .arg("-o")
            .arg("ControlPersist=120");
    }
    ssh.arg("-p")
        .arg(sw.port.to_string())
        .arg(format!("{}@{}", cfg.username, sw.host))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    ssh
}

fn with_ssh_fallback(
    mut attempt: impl FnMut(bool) -> Result<String, String>,
) -> Result<String, String> {
    match attempt(false) {
        Ok(output) => Ok(output),
        Err(modern) => {
            attempt(true).map_err(|legacy| format!("modern SSH: {modern}; legacy SSH: {legacy}"))
        }
    }
}

fn ssh_show(cfg: &Config, sw: &Switch, cmd: &str) -> Result<String, String> {
    let agent = std::env::var("SSH_AUTH_SOCK")
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or("Cisco SSH agent socket is not configured")?;
    with_ssh_fallback(|legacy| ssh_show_attempt(cfg, sw, cmd, &agent, legacy))
}

// Read until IOS has finished its response. Passwords are sent only after a
// password prompt; blind pipelining can echo them into the collected output.
fn ios_exchange(
    secret: &str,
    command: &str,
    mut prompt: impl FnMut() -> Result<String, String>,
    mut send: impl FnMut(&str) -> Result<(), String>,
) -> Result<String, String> {
    let mut reply = prompt()?;
    if reply.trim_end().ends_with('>') {
        send("enable")?;
        reply = prompt()?;
        if reply.trim_end().to_ascii_lowercase().ends_with("password:") {
            if secret.is_empty() {
                return Err("IOS enable password is not configured".into());
            }
            send(secret)?;
            reply = prompt()?;
        }
    }
    if !reply.trim_end().ends_with('#') {
        return Err("IOS privileged prompt was not reached".into());
    }
    send("terminal length 0")?;
    let reply = prompt()?;
    if !reply.trim_end().ends_with('#') || reply.contains("% ") {
        return Err("IOS paging could not be disabled".into());
    }
    send(command)?;
    let output = prompt()?;
    if !output.trim_end().ends_with('#') || output.contains("% ") {
        return Err("IOS rejected the status command".into());
    }
    Ok(output)
}

fn ssh_show_attempt(
    cfg: &Config,
    sw: &Switch,
    cmd: &str,
    agent: &str,
    legacy: bool,
) -> Result<String, String> {
    use std::sync::mpsc;
    use std::time::{Duration, Instant};
    let mut child = ssh_command(cfg, sw, agent, legacy)
        .spawn()
        .map_err(|e| format!("ssh spawn: {e}"))?;
    let mut stdout = child.stdout.take().ok_or("ssh stdout missing")?;
    let mut stdin = child.stdin.take().ok_or("ssh stdin missing")?;
    let (tx, rx) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut buf = [0u8; 4096];
        loop {
            match stdout.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    if tx.send(buf[..n].to_vec()).is_err() {
                        break;
                    }
                }
            }
        }
    });
    let mut prompt = || {
        let deadline = Instant::now() + Duration::from_secs(15);
        let mut bytes = Vec::new();
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            let chunk = rx
                .recv_timeout(remaining)
                .map_err(|_| "SSH closed or timed out waiting for IOS prompt")?;
            bytes.extend(chunk);
            if bytes.len() > 2 * 1024 * 1024 {
                return Err("IOS response exceeds size limit".into());
            }
            let text = String::from_utf8_lossy(&bytes);
            let tail = text.trim_end();
            if tail.ends_with('#')
                || tail.ends_with('>')
                || tail.to_ascii_lowercase().ends_with("password:")
            {
                return Ok(text.into_owned());
            }
        }
    };
    let result = ios_exchange(&cfg.enable_secret, cmd, &mut prompt, |line| {
        stdin
            .write_all(format!("{line}\n").as_bytes())
            .map_err(|e| format!("ssh write: {e}"))?;
        stdin.flush().map_err(|e| format!("ssh flush: {e}"))
    });
    // The show response is complete. Bound cleanup even if IOS ignores EOF.
    drop(stdin);
    let _ = child.kill();
    let out = child
        .wait_with_output()
        .map_err(|e| format!("ssh wait: {e}"))?;
    let _ = reader.join();
    result.map_err(|error| {
        let stderr = String::from_utf8_lossy(&out.stderr);
        format!("ssh {}: {error}: {}", sw.host, stderr.trim())
    })
}

/// The response envelope, shared by `GET /api/status` and `--dump`.
fn snapshot_json(switches: HashMap<String, serde_json::Value>) -> serde_json::Value {
    serde_json::json!({ "switches": switches })
}

/// Collect every switch's raw show output, or one switch's when `only` is set.
fn collect(cfg: &Config, only: Option<&str>) -> HashMap<String, serde_json::Value> {
    let mut out = HashMap::new();
    for (sid, sw) in &cfg.switches {
        if only.is_some_and(|id| id != sid) {
            continue;
        }
        let started = Instant::now();
        let sessions_before = live_sessions(cfg);
        let mut sw_out = serde_json::Map::new();
        let mut failed = 0usize;
        for (key, cmd) in SHOW_COMMANDS {
            match ssh_show(cfg, sw, cmd) {
                Ok(text) => {
                    sw_out.insert((*key).to_string(), serde_json::Value::String(text));
                }
                Err(e) => {
                    failed += 1;
                    // The payload carries this too (`__error__:`), but a
                    // failure a consumer has to dig out of JSON is not a
                    // debugging trail: say it here, as it happens, with the
                    // ssh error still in it.
                    eprintln!("[exporter] {sid}: {key} failed: {e}");
                    sw_out.insert(
                        (*key).to_string(),
                        serde_json::Value::String(format!("__error__: {e}")),
                    );
                }
            }
        }
        eprintln!(
            "[exporter] {sid}: {} commands in {:.2}s, {failed} failed{}",
            SHOW_COMMANDS.len(),
            started.elapsed().as_secs_f64(),
            session_note(cfg, sessions_before),
        );
        out.insert(sid.clone(), serde_json::Value::Object(sw_out));
    }
    out
}

/// How many SSH control sockets the configured directory holds.
///
/// Only ever used to say whether a switch's collection *opened* a session or
/// went over one that was already there. That difference is the whole point of
/// the shared-session option and is otherwise invisible from outside: without
/// this line, "reusing the login" and "logging in once per command" look
/// identical in the journal, the payload and the timing.
fn live_sessions(cfg: &Config) -> usize {
    cfg.ssh_control_dir
        .as_deref()
        .and_then(|dir| fs::read_dir(dir).ok())
        .map(|entries| entries.flatten().count())
        .unwrap_or(0)
}

/// The parenthetical that ends a switch's line: what its sessions did.
fn session_note(cfg: &Config, before: usize) -> String {
    if cfg.ssh_control_dir.is_none() {
        return String::new(); // sharing is off: nothing to report about it
    }
    if live_sessions(cfg) > before {
        ", opened an ssh session".to_string()
    } else {
        ", re-used the ssh session".to_string()
    }
}

/// The poller's half of the mutual auth: it must send
/// `Authorization: Bearer <ha_token>`.
///
/// Matched line-wise on the raw header name (case-insensitively) and an
/// exact, case-sensitive `Bearer <ha_token>` suffix on the value.
fn is_authorized(request: &str, ha_token: &str) -> bool {
    request.lines().any(|line| {
        line.to_ascii_lowercase().starts_with("authorization:")
            && line.trim_end().ends_with(&format!("Bearer {ha_token}"))
    })
}

/// Minimal HTTP/1.1 response.
fn http_response(status: u16, reason: &str, body: &[u8], exporter_token: &str) -> Vec<u8> {
    let mut resp = format!(
        "HTTP/1.1 {status} {reason}\r\n\
         Content-Type: application/json\r\n\
         X-Exporter-Token: {token}\r\n\
         Content-Length: {len}\r\n\
         Connection: close\r\n\r\n",
        status = status,
        reason = reason,
        token = exporter_token,
        len = body.len(),
    )
    .into_bytes();
    resp.extend_from_slice(body);
    resp
}

fn handle(mut stream: TcpStream, cfg: Arc<Config>) {
    let started = Instant::now();
    let mut buf = [0u8; 4096];
    let n = match stream.read(&mut buf) {
        Ok(n) => n,
        Err(_) => return,
    };
    let req = String::from_utf8_lossy(&buf[..n]);
    let mut lines = req.lines();
    let request_line = lines.next().unwrap_or("");
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let path = parts.next().unwrap_or("");

    // Auth: only GET /api/status with the correct bearer token.
    if method != "GET" || path != "/api/status" {
        let _ = stream.write_all(&http_response(
            404,
            "Not Found",
            b"{\"error\":\"not found\"}",
            &cfg.exporter_token,
        ));
        log_request(method, path, 404, started);
        return;
    }
    let authed = is_authorized(&req, &cfg.ha_token);
    if !authed {
        let _ = stream.write_all(&http_response(
            401,
            "Unauthorized",
            b"{\"error\":\"unauthorized\"}",
            &cfg.exporter_token,
        ));
        log_request(method, path, 401, started);
        return;
    }

    let snapshot = collect(&cfg, None);
    let body = serde_json::to_vec(&snapshot_json(snapshot))
        .unwrap_or_else(|_| b"{\"error\":\"serialize\"}".to_vec());
    let _ = stream.write_all(&http_response(200, "OK", &body, &cfg.exporter_token));
    log_request(method, path, 200, started);
}

/// One line per request: what was asked for, what it got, how long it took.
///
/// The duration is the reason the line exists. A poll is an ssh round trip per
/// switch, so from the consumer's side "Home Assistant is timing out" and "the
/// switch took ten seconds" are indistinguishable — and a 401 (wrong token) or
/// 404 (wrong URL) in the journal is the difference between a poller
/// misconfigured and a network problem, which the consumer only sees as an
/// error envelope.
fn log_request(method: &str, path: &str, status: u16, started: Instant) {
    eprintln!(
        "[exporter] {method} {path} -> {status} in {:.2}s",
        started.elapsed().as_secs_f64()
    );
}

const USAGE: &str =
    "usage: cisco-exporter --config <path> [--host H] [--port P] [--dump [--switch ID]] [--version]";

/// The build this binary was made from, stamped at build time by whoever built
/// it for a host (`build.rs` reads `CISCO_EXPORTER_VERSION`: a release, a digest
/// of the source and the feature tags — see `machines.host_cisco_exporter.version_id`).
///
/// A binary nobody stamped says `dev` rather than claiming a build it was not
/// made from, which is also what stops a hand-copied file from passing for a
/// version the deployment never built. The deployment asks this before it
/// pushes or compiles anything, so "does this host already run what this tree
/// builds?" costs one line rather than a build.
const BUILD: &str = match option_env!("CISCO_EXPORTER_VERSION") {
    Some(version) => version,
    None => "dev",
};

#[derive(Debug)]
struct Args {
    config: String,
    host: String,
    port: u16,
    dump: bool,
    switch: Option<String>,
    version: bool,
}

/// Parse the command line. Flags are positional; an argument that is not one
/// of them is ignored, and a flag with no following value is an empty string.
/// A `--port` that does not parse as a `u16` falls back to 8788, but an empty
/// `--config` or `--switch` is a usage error rather than a silent default.
fn parse_args(args: &[String]) -> Result<Args, String> {
    let mut parsed = Args {
        config: String::new(),
        host: "0.0.0.0".to_string(),
        port: 8788,
        dump: false,
        switch: None,
        version: false,
    };
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--config" => {
                i += 1;
                parsed.config = args.get(i).cloned().unwrap_or_default();
            }
            "--host" => {
                i += 1;
                parsed.host = args.get(i).cloned().unwrap_or_default();
            }
            "--port" => {
                i += 1;
                parsed.port = args.get(i).and_then(|p| p.parse().ok()).unwrap_or(8788);
            }
            "--dump" => parsed.dump = true,
            "--version" => parsed.version = true,
            "--switch" => {
                i += 1;
                parsed.switch = Some(args.get(i).cloned().unwrap_or_default());
            }
            _ => {}
        }
        i += 1;
    }
    if parsed.version {
        // `--version` describes the binary rather than a run, so it answers
        // without a config.
        return Ok(parsed);
    }
    if parsed.config.is_empty() {
        return Err("--config required".to_string());
    }
    if parsed.switch.as_deref() == Some("") {
        return Err("--switch requires a switch id".to_string());
    }
    Ok(parsed)
}

/// `--dump`: run the commands once against the selected switch and print the
/// snapshot `GET /api/status` would have returned. No listener is opened, and
/// no token is required — there is no request to authenticate. Command
/// failures stay in the payload as `__error__:` values, so a dump that found
/// the switch unreachable still exits 0; an unknown switch id exits 1.
fn dump_body(cfg: &Config, only: Option<&str>) -> Result<String, String> {
    if let Some(id) = only {
        if !cfg.switches.contains_key(id) {
            return Err(format!("unknown switch: {id}"));
        }
    }
    serde_json::to_string_pretty(&snapshot_json(collect(cfg, only)))
        .map_err(|e| format!("serialize: {e}"))
}

fn run_dump(cfg: &Config, only: Option<&str>) -> i32 {
    match dump_body(cfg, only) {
        Ok(text) => {
            println!("{text}");
            0
        }
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("{USAGE}");
        std::process::exit(2);
    }
    let args = match parse_args(&args) {
        Ok(parsed) => parsed,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    if args.version {
        // Answers before the config is read: it describes the binary, and the
        // deployment asks it on hosts where the config is what is broken.
        println!("{BUILD}");
        return;
    }
    let cfg = match Config::load(&args.config) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    };
    if args.dump {
        std::process::exit(run_dump(&cfg, args.switch.as_deref()));
    }
    if cfg.ha_token.is_empty() || cfg.exporter_token.is_empty() {
        eprintln!("config missing ha_token/exporter_token");
        std::process::exit(1);
    }
    let cfg = Arc::new(cfg);
    let listener = TcpListener::bind((args.host.as_str(), args.port)).unwrap_or_else(|e| {
        eprintln!("bind {}:{}: {e}", args.host, args.port);
        std::process::exit(1);
    });
    // The one line that says what this process *is*: which build, where it
    // listens, and — the part that is otherwise invisible — whether the
    // switches' commands share a session or log in one at a time. A restart
    // that silently dropped the sharing option reads very differently here
    // than a switch that got slow.
    eprintln!(
        "[exporter] {BUILD} serving on {}:{} for {} switch(es){}",
        args.host,
        args.port,
        cfg.switches.len(),
        match cfg.ssh_control_dir.as_deref() {
            Some(dir) => format!(", ssh sessions shared in {dir}"),
            None => ", one ssh login per show command".to_string(),
        },
    );
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let cfg = Arc::clone(&cfg);
                thread::spawn(move || handle(s, cfg));
            }
            Err(_) => continue,
        }
    }
}

#[cfg(test)]
mod tests;
