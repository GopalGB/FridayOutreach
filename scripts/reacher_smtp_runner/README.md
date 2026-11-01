# SMTP-only Reacher runner

This optional native runner makes the `lead-collector verify-email` command use the official Reacher core v0.11.11 through direct SMTP only. It sets every provider variant, including consumer Outlook and Yahoo, to SMTP. It disables proxy use, Gravatar, Have I Been Pwned, browser/headless methods, and account recovery.

The Lead Collector CLI never builds or downloads this runner. Build it only as an explicit local setup action. First acquire the immutable official source archive, verify its SHA-256, and unpack it at `.tools/reacher-v0.11.11`:

```sh
(
  set -eu
  mkdir -p .tools
  curl --fail --location --output .tools/reacher-v0.11.11-source.tar.gz https://github.com/reacherhq/check-if-email-exists/archive/2da147a38a11bda1f2e032615ef21c0ed55e9bd1.tar.gz
  test "$(shasum -a 256 .tools/reacher-v0.11.11-source.tar.gz | awk '{print $1}')" = 66790fc0f12151e5d6f6a6a48aa0308b975340ec945df438767586bf7d733ad7
  tar -xzf .tools/reacher-v0.11.11-source.tar.gz -C .tools
  mv .tools/check-if-email-exists-2da147a38a11bda1f2e032615ef21c0ed55e9bd1 .tools/reacher-v0.11.11
)
```

Prerequisite: install Rust only in user space. This project does not bootstrap a compiler. The fixed-DNS build used Rust 1.99.1 for `aarch64-apple-darwin`, whose official bundle is `https://static.rust-lang.org/dist/2026-10-15/rust-1.99.1-aarch64-apple-darwin.tar.gz` with SHA-256 `b73b6a78c4e7171a8fe1b4bee5342273605c5c9a9a4997554c15bb3ce2753b6a`. Use the isolated toolchain for this checkout:

```sh
export RUSTUP_HOME="$PWD/.tools/rustup"
export CARGO_HOME="$PWD/.tools/cargo"
export PATH="$CARGO_HOME/bin:$PATH"
rustc --version
cargo --version
```

Then use the explicit setup action, which applies the source-hash-checked patch, runs `cargo build --locked --release`, and atomically rebinds `.tools/reacher-smtp-check`:

```sh
python3 scripts/setup.py --reacher
```

The equivalent manual build sequence is:

```sh
python3 scripts/reacher_smtp_runner/patch_reacher_core.py
CARGO_BUILD_JOBS=2 CARGO_TARGET_DIR=.tools/reacher-target cargo build --locked --release --manifest-path scripts/reacher_smtp_runner/Cargo.toml
cp .tools/reacher-target/release/lead-collector-reacher-smtp .tools/reacher-smtp-check
chmod 700 .tools/reacher-smtp-check
```

Use a user-owned toolchain and a user-controlled target directory if the checkout is packaged or shared. The `.tools/` directory is intentionally ignored: it holds local source, registry, build cache, and binary artifacts rather than application source. The runner accepts exactly `--email ADDRESS`, emits one JSON object on stdout, and does not send `DATA` or a message. It uses a reserved technical SMTP identity, never a user identity.

The patch helper refuses source that does not match the recorded official v0.11.11 core hashes: manifest `be6a14448932656dae05bd41deb4a21b21d7a3f498b17f5f830cba3475ac2812`, MX implementation `290f536405a6e7483b2f3d7033c80ec7d468ace3dba76e7f8c8e6b473e85f1a7`, and core library `aca4e1d0d856dc5ac172791f7347776cac03630d12e9545375e2727bb39cb910`. It pins hickory-proto and hickory-resolver to exact `0.26.2`, the first common release that addresses the audited resolver DNSSEC and protocol compression advisories, and makes the source-checked MX API adjustments required by that upgrade. Cargo regenerates `Cargo.lock`; do not hand-edit it.

The runner is linked to official Reacher source under the MIT license. See [LICENSE-REACHER-MIT.md](LICENSE-REACHER-MIT.md). Exact task-local source, toolchain, lockfile, and binary hashes are recorded in the email-enrichment receipt.
