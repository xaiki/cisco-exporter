//! Unit tests: the IOS prompt protocol, the SSH argument set, the token
//! check, the command line, the `--dump` envelope and the config parser.
//!
//! Kept out of `main.rs` on purpose. What makes a new build is what
//! compiles into the deployed binary, and the deployment digests exactly
//! those files (`machines.host_cisco_exporter.version_id`). A test-only
//! edit must not cost a rebuild and a re-upload on every host.

use super::*;

fn test_config() -> Config {
    let mut switches = HashMap::new();
    switches.insert(
        "north".to_string(),
        Switch {
            host: "192.0.2.10".into(),
            port: 22,
        },
    );
    Config {
        ha_token: "ha-tok".into(),
        exporter_token: "ex-tok".into(),
        username: "admin".into(),
        enable_secret: "secret".into(),
        switches,
        ssh_control_dir: None,
    }
}

#[test]
fn ios_waits_for_password_and_privileged_prompt_before_show() {
    use std::cell::RefCell;
    let events = RefCell::new(Vec::new());
    let mut replies = [
        "switch>",
        "Password:",
        "switch#",
        "switch#",
        "Version 15.2(7)E11\nswitch#",
    ]
    .into_iter();
    let output = ios_exchange(
        "test-password",
        "show version",
        || {
            events.borrow_mut().push("read".to_string());
            Ok(replies.next().unwrap().to_string())
        },
        |line| {
            events.borrow_mut().push(line.to_string());
            Ok(())
        },
    )
    .unwrap();
    assert!(output.contains("Version"));
    assert!(!output.contains("test-password"));
    assert_eq!(
        *events.borrow(),
        vec![
            "read",
            "enable",
            "read",
            "test-password",
            "read",
            "terminal length 0",
            "read",
            "show version",
            "read"
        ]
    );
}

#[test]
fn ios_missing_password_does_not_send_show_as_password() {
    let mut replies = ["switch>", "Password:"].into_iter();
    let mut sent = Vec::new();
    let error = ios_exchange(
        "",
        "show version",
        || Ok(replies.next().unwrap().into()),
        |line| {
            sent.push(line.to_string());
            Ok(())
        },
    )
    .unwrap_err();
    assert!(error.contains("not configured"));
    assert_eq!(sent, vec!["enable"]);
}

#[test]
fn ios_rejects_cli_errors_even_with_a_successful_ssh_connection() {
    let mut replies = ["switch#", "switch#", "% Authorization failed\nswitch#"].into_iter();
    let error = ios_exchange(
        "",
        "show version",
        || Ok(replies.next().unwrap().into()),
        |_| Ok(()),
    )
    .unwrap_err();
    assert!(error.contains("rejected"));
}

#[test]
fn ssh_options_match_modern_and_legacy_switches() {
    let cfg = test_config();
    for legacy in [false, true] {
        let command = ssh_command(&cfg, &cfg.switches["north"], "/run/test-agent.sock", legacy);
        let args: Vec<_> = command.get_args().map(|a| a.to_str().unwrap()).collect();
        assert!(args.contains(&"IdentityFile=none"));
        assert!(args.contains(&"IdentityAgent=/run/test-agent.sock"));
        assert!(args.contains(&"PreferredAuthentications=publickey"));
        assert!(args.contains(&"HostKeyAlgorithms=+ssh-rsa"));
        assert!(args.contains(
            &"KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1"
        ));
        assert!(args.contains(&"ServerAliveInterval=5"));
        assert!(args.contains(&"ServerAliveCountMax=3"));
        assert_eq!(args.contains(&"PubkeyAcceptedAlgorithms=ssh-rsa"), legacy);
        assert_eq!(args.last(), Some(&"admin@192.0.2.10"));
    }
}

#[test]
fn ssh_options_are_never_repeated() {
    // OpenSSH keeps the *first* value of a repeated option, so a second copy
    // sits in the argument list looking like it changes something while doing
    // nothing at all.
    let cfg = test_config();
    let args: Vec<String> = ssh_command(&cfg, &cfg.switches["north"], "/run/test-agent.sock", true)
        .get_args()
        .map(|a| a.to_str().unwrap().to_string())
        .collect();
    let mut seen = std::collections::HashSet::new();
    for pair in args.windows(2) {
        if pair[0] == "-o" {
            assert!(seen.insert(pair[1].clone()), "repeated -o {}", pair[1]);
        }
    }
}

#[test]
fn ssh_options_share_one_session_only_when_a_control_dir_is_set() {
    let dir = std::env::temp_dir().join("cisco-exporter-test-control");
    let mut cfg = test_config();
    let args_of = |cfg: &Config| -> Vec<String> {
        ssh_command(cfg, &cfg.switches["north"], "/run/test-agent.sock", false)
            .get_args()
            .map(|a| a.to_str().unwrap().to_string())
            .collect()
    };

    // off by default: an existing config must not need changing
    let plain = args_of(&cfg);
    assert!(!plain.iter().any(|a| a.starts_with("ControlMaster=")));

    cfg.ssh_control_dir = Some(dir.to_str().unwrap().to_string());
    let shared = args_of(&cfg);
    assert!(shared.contains(&"ControlMaster=auto".to_string()));
    assert!(shared.contains(&format!("ControlPath={}/cm-%C", dir.display())));
    assert!(shared.contains(&"ControlPersist=120".to_string()));
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn config_load_creates_the_optional_ssh_control_dir() {
    let base = std::env::temp_dir().join("cisco-exporter-test-controlcfg");
    std::fs::remove_dir_all(&base).ok();
    let dir = base.join("sockets");
    let path = base.join("config.json");
    std::fs::create_dir_all(&base).unwrap();
    std::fs::write(
        &path,
        format!(
            r#"{{"ha_token": "ha", "exporter_token": "ex",
                 "ssh_control_dir": "{}",
                 "switches": {{"north": {{"host": "192.0.2.10"}}}}}}"#,
            dir.display()
        ),
    )
    .unwrap();
    let cfg = Config::load(path.to_str().unwrap()).unwrap();
    assert_eq!(cfg.ssh_control_dir.as_deref(), Some(dir.to_str().unwrap()));
    assert!(
        dir.is_dir(),
        "the control dir has to exist before ssh needs it"
    );
    std::fs::remove_dir_all(&base).ok();
}

#[test]
fn binding_is_collected_with_the_other_show_commands() {
    // The one part a switch that is L2 for its vlans can still answer for its
    // ports: without it the payload has no per-port address for those.
    assert!(SHOW_COMMANDS.contains(&("binding", "show ip dhcp snooping binding")));
    assert_eq!(SHOW_COMMANDS.len(), 8);
}

#[test]
fn modern_success_does_not_retry() {
    let mut attempts = vec![];
    let result = with_ssh_fallback(|legacy| {
        attempts.push(legacy);
        Ok("show output".into())
    });
    assert_eq!(result.unwrap(), "show output");
    assert_eq!(attempts, vec![false]);
}

#[test]
fn old_firmware_gets_a_legacy_retry_and_both_failures_are_reported() {
    let mut attempts = vec![];
    let result = with_ssh_fallback(|legacy| {
        attempts.push(legacy);
        if legacy {
            Ok("legacy output".into())
        } else {
            Err("signature rejected".into())
        }
    });
    assert_eq!(result.unwrap(), "legacy output");
    assert_eq!(attempts, vec![false, true]);
    let err =
        with_ssh_fallback(|legacy| Err(if legacy { "old failed" } else { "new failed" }.into()))
            .unwrap_err();
    assert!(err.contains("new failed"));
    assert!(err.contains("old failed"));
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
    std::fs::write(
        &path,
        r#"{
            "ha_token": "ha",
            "exporter_token": "ex",
            "username": "admin",
            "switches": {"north": {"host": "192.0.2.10", "port": 22}}
        }"#,
    )
    .unwrap();
    let cfg = Config::load(path.to_str().unwrap()).unwrap();
    assert_eq!(cfg.ha_token, "ha");
    assert_eq!(cfg.switches.len(), 1);
    assert_eq!(cfg.switches["north"].host, "192.0.2.10");
    std::fs::remove_file(&path).ok();
}

fn argv(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

#[test]
fn parse_args_uses_the_documented_defaults() {
    let parsed = parse_args(&argv(&["cisco-exporter", "--config", "/etc/cisco.json"])).unwrap();
    assert_eq!(parsed.config, "/etc/cisco.json");
    assert_eq!(parsed.host, "0.0.0.0");
    assert_eq!(parsed.port, 8788);
    assert!(!parsed.dump);
    assert_eq!(parsed.switch, None);
    assert!(!parsed.version);
}

#[test]
fn parse_args_answers_version_without_a_config() {
    // The deployment asks `--version` on hosts where the config is what is
    // broken, so it must not need one.
    let parsed = parse_args(&argv(&["cisco-exporter", "--version"])).unwrap();
    assert!(parsed.version);
    assert_eq!(parsed.config, "");
    // And it does not quietly turn into a missing-config usage error.
    assert_eq!(
        parse_args(&argv(&["cisco-exporter"])).unwrap_err(),
        "--config required"
    );
}

#[test]
fn the_build_stamp_names_the_binary_or_plainly_says_dev() {
    // Stamped by whoever built it for a host (`build.rs`), in the repo's shared
    // stamp grammar: release, a digest of the build inputs, feature tags —
    // `v0.2.0-<digest16>-core`, with a revision between release and digest when
    // the deployment chooses to name one. `dev` for a build nobody made for a
    // host, and never empty: the deployment reads this line to decide whether
    // the host already runs what this tree builds, and an empty answer would
    // read as a version and skip an upgrade that was due.
    assert!(!BUILD.is_empty());
    let unset = option_env!("CISCO_EXPORTER_VERSION").is_none();
    assert_eq!(BUILD == "dev", unset);
    if !unset {
        let fields: Vec<&str> = BUILD.split('-').collect();
        assert!(fields.len() == 3 || fields.len() == 4, "stamp: {BUILD}");
        assert!(fields[0].starts_with('v'), "release first: {BUILD}");
        assert!(
            !fields[fields.len() - 1].is_empty(),
            "features last: {BUILD}"
        );
    }
}

#[test]
fn parse_args_reads_host_port_dump_and_switch() {
    let parsed = parse_args(&argv(&[
        "cisco-exporter",
        "--host",
        "127.0.0.1",
        "--port",
        "9101",
        "--dump",
        "--switch",
        "north",
        "--config",
        "/c.json",
    ]))
    .unwrap();
    assert_eq!(parsed.host, "127.0.0.1");
    assert_eq!(parsed.port, 9101);
    assert!(parsed.dump);
    assert_eq!(parsed.switch.as_deref(), Some("north"));
}

#[test]
fn parse_args_falls_back_on_a_bad_port_but_not_on_an_empty_config_or_switch() {
    // A `--port` that does not parse is documented to fall back to 8788...
    let parsed = parse_args(&argv(&["p", "--config", "/c.json", "--port", "http"])).unwrap();
    assert_eq!(parsed.port, 8788);
    // ...but a flag that needs a value and got none is a usage error.
    assert_eq!(
        parse_args(&argv(&["p", "--dump"])).unwrap_err(),
        "--config required"
    );
    assert_eq!(
        parse_args(&argv(&["p", "--config"])).unwrap_err(),
        "--config required"
    );
    assert_eq!(
        parse_args(&argv(&["p", "--config", "/c.json", "--switch"])).unwrap_err(),
        "--switch requires a switch id"
    );
}

#[test]
fn parse_args_ignores_arguments_that_are_not_flags() {
    let parsed = parse_args(&argv(&["p", "junk", "--config", "/c.json", "--nope", "1"])).unwrap();
    assert_eq!(parsed.config, "/c.json");
    assert_eq!(parsed.port, 8788);
}

#[test]
fn dump_envelope_matches_the_http_contract() {
    let cfg = Config {
        switches: HashMap::new(),
        ..test_config()
    };
    let dumped: serde_json::Value = serde_json::from_str(&dump_body(&cfg, None).unwrap()).unwrap();
    assert_eq!(dumped, serde_json::json!({ "switches": {} }));
}

#[test]
fn dump_refuses_an_unknown_switch_id_instead_of_printing_nothing() {
    let cfg = test_config();
    let error = dump_body(&cfg, Some("south")).unwrap_err();
    assert!(error.contains("unknown switch: south"));
}

#[test]
fn auth_rejects_bad_token() {
    let cfg = test_config();
    let req = "GET /api/status HTTP/1.1\r\nAuthorization: Bearer wrong\r\n\r\n";
    assert!(!is_authorized(req, &cfg.ha_token));
}

#[test]
fn auth_accepts_good_token() {
    let cfg = test_config();
    let req = "GET /api/status HTTP/1.1\r\nAuthorization: Bearer ha-tok\r\n\r\n";
    assert!(is_authorized(req, &cfg.ha_token));
}

#[test]
fn auth_needs_the_authorization_header_name_and_the_exact_token() {
    let cfg = test_config();
    // The header name is matched case-insensitively...
    assert!(is_authorized(
        "authorization: Bearer ha-tok\r\n",
        &cfg.ha_token
    ));
    assert!(is_authorized(
        "AUTHORIZATION: Bearer ha-tok\r\n",
        &cfg.ha_token
    ));
    // ...but the token and the `Bearer` scheme are not.
    assert!(!is_authorized(
        "Authorization: Bearer HA-TOK\r\n",
        &cfg.ha_token
    ));
    assert!(!is_authorized(
        "Authorization: bearer ha-tok\r\n",
        &cfg.ha_token
    ));
    // A suffix match on some other header is not authentication.
    assert!(!is_authorized(
        "X-Comment: Bearer ha-tok\r\n",
        &cfg.ha_token
    ));
    assert!(!is_authorized("", &cfg.ha_token));
}
