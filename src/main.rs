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

/// The show commands run per switch, in order.
const SHOW_COMMANDS: &[(&str, &str)] = &[
    ("version", "show version"),
    ("interfaces", "show interfaces status"),
    ("macs", "show mac address-table"),
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
}

#[derive(Clone)]
struct Switch {
    host: String,
    port: u16,
}

impl Config {
    fn load(path: &str) -> Result<Self, String> {
        let raw = fs::read_to_string(path)
            .map_err(|e| format!("read config {path}: {e}"))?;
        let v: serde_json::Value = serde_json::from_str(&raw)
            .map_err(|e| format!("parse config {path}: {e}"))?;
        let mut switches = HashMap::new();
        if let Some(sw) = v.get("switches").and_then(|s| s.as_object()) {
            for (sid, info) in sw {
                switches.insert(
                    sid.clone(),
                    Switch {
                        host: info.get("host").and_then(|h| h.as_str())
                            .unwrap_or("").to_string(),
                        port: info.get("port").and_then(|p| p.as_u64())
                            .unwrap_or(22) as u16,
                    },
                );
            }
        }
        Ok(Config {
            ha_token: v.get("ha_token").and_then(|t| t.as_str())
                .unwrap_or("").to_string(),
            exporter_token: v.get("exporter_token").and_then(|t| t.as_str())
                .unwrap_or("").to_string(),
            username: v.get("username").and_then(|u| u.as_str())
                .unwrap_or("admin").to_string(),
            enable_secret: v.get("enable_secret").and_then(|e| e.as_str())
                .unwrap_or("").to_string(),
            switches,
        })
    }
}

/// Run one `show` command on a switch via the system ssh (agent auth).
///
/// The key-only login lands at privilege 1; the show commands need
/// privileged EXEC. A pty (`-t`) lets us feed `enable` + the secret +
/// the command as a script, exactly like a console session.
fn ssh_show(cfg: &Config, sw: &Switch, cmd: &str) -> Result<String, String> {
    let mut ssh = Command::new("ssh");
    ssh.arg("-t").arg("-t")  // force pty (twice: no fallback to non-tty)
        // -F /dev/null + explicit IdentityAgent: the host's ~/.ssh/config
        // may set IdentityAgent, which OVERRIDES SSH_AUTH_SOCK — ssh would
        // then never consult the agent holding the cisco key. Ignore the
        // config and pin the agent socket so the cisco key is the FIRST
        // key offered (agent keys precede identity files).
        .arg("-F").arg("/dev/null")
        .arg("-o").arg("BatchMode=yes")
        .arg("-o").arg("StrictHostKeyChecking=no")
        .arg("-o").arg("UserKnownHostsFile=/dev/null")
        .arg("-o").arg("ConnectTimeout=10")
        // The 2960X predates rsa-sha2-256/512 and the modern kex list —
        // pin legacy ssh-rsa and re-add diffie-hellman-group14/group-
        // exchange-sha1 (dropped from OpenSSH 9.6 defaults).
        .arg("-o").arg("PubkeyAcceptedAlgorithms=ssh-rsa")
        .arg("-o").arg("HostKeyAlgorithms=ssh-rsa")
        .arg("-o").arg("KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1")
        .arg("-p").arg(sw.port.to_string())
        .arg(format!("{}@{}", cfg.username, sw.host))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Ok(sock) = std::env::var("SSH_AUTH_SOCK") {
        if !sock.is_empty() {
            ssh.arg("-o").arg(format!("IdentityAgent={sock}"));
        }
    }
    let mut child = ssh.spawn()
        .map_err(|e| format!("ssh spawn: {e}"))?;
    // Feed the enable dance + command through the pty.
    let mut script = String::from("enable\n");
    if !cfg.enable_secret.is_empty() {
        script.push_str(&cfg.enable_secret);
        script.push('\n');
    }
    script.push_str(cmd);
    script.push_str("\nexit\n");
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(script.as_bytes());
    }
    let out = child.wait_with_output()
        .map_err(|e| format!("ssh wait: {e}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        return Err(format!("ssh {}: {}", sw.host, err.trim()));
    }
    Ok(String::from_utf8_lossy(&out.stdout).to_string())
}

/// Collect every switch's raw show output.
fn collect(cfg: &Config) -> HashMap<String, serde_json::Value> {
    let mut out = HashMap::new();
    for (sid, sw) in &cfg.switches {
        let mut sw_out = serde_json::Map::new();
        for (key, cmd) in SHOW_COMMANDS {
            match ssh_show(cfg, sw, cmd) {
                Ok(text) => { sw_out.insert((*key).to_string(), serde_json::Value::String(text)); }
                Err(e) => { sw_out.insert((*key).to_string(), serde_json::Value::String(format!("__error__: {e}"))); }
            }
        }
        out.insert(sid.clone(), serde_json::Value::Object(sw_out));
    }
    out
}

/// Minimal HTTP/1.1 response.
fn http_response(status: u16, reason: &str, body: &[u8],
                 exporter_token: &str) -> Vec<u8> {
    let mut resp = format!(
        "HTTP/1.1 {status} {reason}\r\n\
         Content-Type: application/json\r\n\
         X-Exporter-Token: {token}\r\n\
         Content-Length: {len}\r\n\
         Connection: close\r\n\r\n",
        status = status, reason = reason,
        token = exporter_token, len = body.len(),
    ).into_bytes();
    resp.extend_from_slice(body);
    resp
}

fn handle(mut stream: TcpStream, cfg: Arc<Config>) {
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
        let _ = stream.write_all(&http_response(404, "Not Found", b"{\"error\":\"not found\"}", &cfg.exporter_token));
        return;
    }
    let authed = req.lines().any(|l| {
        l.to_ascii_lowercase().starts_with("authorization:")
            && l.trim_end().ends_with(&format!("Bearer {}", cfg.ha_token))
    });
    if !authed {
        let _ = stream.write_all(&http_response(401, "Unauthorized", b"{\"error\":\"unauthorized\"}", &cfg.exporter_token));
        return;
    }

    let snapshot = collect(&cfg);
    let body = serde_json::to_vec(&serde_json::json!({ "switches": snapshot }))
        .unwrap_or_else(|_| b"{\"error\":\"serialize\"}".to_vec());
    let _ = stream.write_all(&http_response(200, "OK", &body, &cfg.exporter_token));
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: cisco-exporter --config <path> [--host H] [--port P]");
        std::process::exit(2);
    }
    let mut config_path = String::new();
    let mut host = "0.0.0.0".to_string();
    let mut port: u16 = 8788;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--config" => { i += 1; config_path = args.get(i).cloned().unwrap_or_default(); }
            "--host" => { i += 1; host = args.get(i).cloned().unwrap_or_default(); }
            "--port" => { i += 1; port = args.get(i).and_then(|p| p.parse().ok()).unwrap_or(8788); }
            _ => {}
        }
        i += 1;
    }
    if config_path.is_empty() {
        eprintln!("--config required");
        std::process::exit(2);
    }
    let cfg = match Config::load(&config_path) {
        Ok(c) => c,
        Err(e) => { eprintln!("{e}"); std::process::exit(1); }
    };
    if cfg.ha_token.is_empty() || cfg.exporter_token.is_empty() {
        eprintln!("config missing ha_token/exporter_token");
        std::process::exit(1);
    }
    let cfg = Arc::new(cfg);
    let listener = TcpListener::bind((host.as_str(), port))
        .unwrap_or_else(|e| { eprintln!("bind {host}:{port}: {e}"); std::process::exit(1); });
    eprintln!("[exporter] serving on {host}:{port}");
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
mod tests {
    use super::*;

    fn test_config() -> Config {
        let mut switches = HashMap::new();
        switches.insert("north".to_string(), Switch { host: "10.0.0.2".into(), port: 22 });
        Config {
            ha_token: "ha-tok".into(),
            exporter_token: "ex-tok".into(),
            username: "admin".into(),
            enable_secret: "secret".into(),
            switches,
        }
    }

    #[test]
    fn response_has_exporter_token() {
        let body = b"{}";
        let resp = http_response(200, "OK", body, "ex-tok");
        let text = String::from_utf8(resp).unwrap();
        assert!(text.contains("HTTP/1.1 200 OK"));
        assert!(text.contains("X-Exporter-Token: ex-tok"));
        assert!(text.contains("Content-Length: 2"));
    }

    #[test]
    fn config_load_parses_switches() {
        let dir = std::env::temp_dir();
        let path = dir.join("cisco-exporter-test-config.json");
        std::fs::write(&path, r#"{
            "ha_token": "ha",
            "exporter_token": "ex",
            "username": "admin",
            "switches": {"north": {"host": "10.0.0.2", "port": 22}}
        }"#).unwrap();
        let cfg = Config::load(path.to_str().unwrap()).unwrap();
        assert_eq!(cfg.ha_token, "ha");
        assert_eq!(cfg.switches.len(), 1);
        assert_eq!(cfg.switches["north"].host, "10.0.0.2");
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn auth_rejects_bad_token() {
        // The auth check is: request must contain "Authorization: Bearer <ha_token>".
        let cfg = test_config();
        let req = "GET /api/status HTTP/1.1\r\nAuthorization: Bearer wrong\r\n\r\n";
        let authed = req.lines().any(|l| {
            l.to_ascii_lowercase().starts_with("authorization:")
                && l.trim_end().ends_with(&format!("Bearer {}", cfg.ha_token))
        });
        assert!(!authed);
    }

    #[test]
    fn auth_accepts_good_token() {
        let cfg = test_config();
        let req = "GET /api/status HTTP/1.1\r\nAuthorization: Bearer ha-tok\r\n\r\n";
        let authed = req.lines().any(|l| {
            l.to_ascii_lowercase().starts_with("authorization:")
                && l.trim_end().ends_with(&format!("Bearer {}", cfg.ha_token))
        });
        assert!(authed);
    }
}