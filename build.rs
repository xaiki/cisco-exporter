//! Stamp the binary with the build it was made from.
//!
//! The deployment compiles this crate on the host it will run on, and hands it
//! the identity it computed (`machines.host_cisco_exporter.version_id`: a
//! release, a digest of the source, and the feature tags) in
//! `CISCO_EXPORTER_VERSION`. `--version` prints it, and the deployment compares
//! that answer with what this tree would build — so "this host already runs it"
//! can skip the push and the compile entirely.
//!
//! Nothing else about the build changes, and a plain `cargo build` stamps
//! nothing: the binary says `dev`, which is the truth for one nobody made for a
//! host. Watching the variable is why this script exists at all — cargo would
//! otherwise reuse an object file stamped with the previous version, and the
//! host would keep answering with a build it no longer runs.
fn main() {
    println!("cargo:rerun-if-env-changed=CISCO_EXPORTER_VERSION");
    if let Ok(stamp) = std::env::var("CISCO_EXPORTER_VERSION") {
        println!("cargo:rustc-env=CISCO_EXPORTER_VERSION={stamp}");
    }
}
