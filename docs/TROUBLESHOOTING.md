# Troubleshooting

Errors are intentionally named and work should stop at the boundary that failed.

**Core artifacts missing.** `scripts/setup.py --core` requires `requirements-core.lock` and `wheels/` in the checkout. Do not replace this with an online install; obtain the intended release payload or ask a maintainer.

**Python or developer tool missing.** Use Python 3.11+ for core. CI’s verification tools are pinned in the existing workflow. Node.js must be on `PATH` for EGO fixture tests. A user-created virtual environment may need `ensurepip` before pip-based tool setup; do not install globally.

**Native verifier unavailable.** Default enrichment fails before fetch or output write when the trusted local Reacher runner is absent. Follow the separate [native runner README](../scripts/reacher_smtp_runner/README.md) only when that optional capability is intended. For a no-SMTP local preview, pass `--skip-email-verification`; expect a header-only safe file.

**No safe rows.** This is expected for opt-out, empty, risky, invalid, unknown, contradictory, or errored results. Inspect the full CSV and evidence; do not infer or edit a safe row by hand.

**Unsafe path, host, or collision.** Keep inputs and outputs in allowed, non-protected paths. Use a new output name or explicit `--overwrite` only when you own the target. The collector rejects traversal, symlinks, protected names, unbounded hosts, and unsafe collisions by design.

**Issue or pull request data.** Use fictional fixtures and sanitized logs. Never attach `.env`, credentials, private leads, mailbox details, or native build artifacts. See [SECURITY.md](../SECURITY.md).
