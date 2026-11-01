# Verification

Local verification date: 2026-11-01. This report describes measured checks, with optional components distinguished from the portable core.

## Frozen local source

Two consecutive real-host passes completed **368 unittest tests** successfully, Ruff0.15.7 and raw strict MyPy1.9.0 across26production Python files. All production files are counted, including standalone scripts that were not imported. No coverage exclusions were added.

| Production Python measure | Result |
| --- | ---: |
| Lines | 87.5579% |
| Branches | 81.2096% |
| Functions with an executed body statement | 95.9538% |

| New module | Lines | Branches |
| --- | ---: | ---: |
| `lead_collector/batch.py` | 91.3208% | 88.8889% |
| `lead_collector/batch_safety.py` | 97.1154% | 86.6667% |
| `scripts/agent_tool.py` | 93.9130% | 87.5000% |

The installer also passed 95.3846% line coverage and 88.2353% branch coverage.

Raw line/branch data is in [coverage.json](coverage.json). Function execution uses at least one non-docstring body statement, not a claim that every path was executed. The full host has the optional local SDK environment and pinned native source; a fresh core checkout deliberately skips tests that need those absent optional artifacts.

## Public payload and dependencies

The explicit public source payload was checked with Gitleaks and Bandit. The release gate requires zero secrets and zero medium/high Python SAST findings. Reviewed low-severity reports concern explicit subprocess boundaries and invariant assertions. The runtime dependency audit queried only public package names and versions:100unique PyPI pins and298Rust crate pins, with zero known OSV advisories at the check time. This is a scoped audit, not a guarantee that no vulnerability exists. Rust received scoped source review, native tests and the Cargo dependency audit; no Rust SAST or Rust coverage percentage is claimed.

The exported lockfiles preserve every dependency pin and hash; only generation-comment paths are made portable. The native runtime, compilers, caches, private task records and real lead dataset are not repository payloads. The release manifest supplies per-file SHA-256 values.

## Actual local Reacher checks

The native Apple Silicon runner links official Reacher core v0.11.11, pinned to commit `2da147a38a11bda1f2e032615ef21c0ed55e9bd1`, with exact Hickory0.26.2 dependencies and source-checked API/macOS resolver compatibility patches. All provider variants use SMTP; Gravatar, HIBP, proxy, browser and account-recovery methods are disabled. Three native configuration/parser tests passed.

A prior real run checked48source-observed addresses:4safe,42risky,2invalid,0unknown. Only the4safe results had mailboxVerified=true. A fresh run of the new `enrich --verify-emails` command fetched one approved public business site, found one literal address and returned safe through the actual local native engine. No email was guessed and no email message body was sent. Raw contact data remains local; aggregate results are reported here.

Native executable SHA-256: `0dbcc0a9b94165e5870989d814d3e5c10cef0f4f1887a06ce9830a3957104a07`.

Reacher outcomes are observations at the check time. Risky/catch-all/unknown results remain unverified; a later mail-server response can differ. Missing local verifier capability is a named failure before work, not simulated success.

## Reproduce core checks

Use Python3.11+ and a user-owned virtual environment:

```sh
python3 scripts/setup.py --core
.venv/bin/python -m pip install --index-url https://pypi.org/simple ruff==0.15.7 mypy==1.9.0 bandit==1.9.4
LEAD_COLLECTOR_TMPDIR="$(mktemp -d "${HOME}/lead-collector-verify.XXXXXX")"
export TMPDIR="$LEAD_COLLECTOR_TMPDIR"
export PATH="$PWD/.venv/bin:$PATH"
command -v node
node --version
.venv/bin/python scripts/verify.py
.venv/bin/mypy --strict --follow-imports=skip scripts/reacher_smtp_runner/patch_reacher_core.py
.venv/bin/python -m bandit -r lead_collector scripts -ll
```

`node` is required on `PATH` for the EGO fixture subprocesses. `TMPDIR` must be a fresh owner-private directory below the real home directory; a shared `/tmp` ancestor is intentionally rejected by the production storage guard. The owner may inspect or remove the dedicated temporary folder after verification.

The [GitHub workflow](../.github/workflows/verify.yml) runs the portable offline unit/lint/type/SAST checks on a standard public Linux runner. It does not scrape live websites, perform SMTP, call AI, upload artifacts or create caches. Live workflow status is available in [Actions](https://github.com/GopalGB/lead-collector/actions/workflows/verify.yml); this source report does not pre-claim a workflow outcome.

## Optional AI and review

Deterministic collection, enrichment and verification need no AI. ScrapeGraphAI remains an explicit local option with separate SDK/model prerequisites. Jev request/replay contracts are tested; live Jev remains UNRUN under ZERO CHARGE. Claude review was waived by the user and was not run. Local behavior tests, parent review, security checks and artifact verification remain independent gates.

## Default verification in v0.1.1

CSV enrichment and `lead_collector_enrich` verify source-observed email addresses by default. Omitting the option uses the trusted local Reacher runtime; absence fails before any fetch or output write. `--skip-email-verification` or the explicit agent argument `verify_emails:false` selects the no-SMTP path. Raw directory collection and review retain their existing behavior. The default, explicit opt-out and missing-runtime boundaries have direct CLI and actual stdin regression tests. The output-limit test uses actual signal termination rather than a brittle one-second startup race; production deadline and byte limits are unchanged.

## FridayOutreach automatic safe exports

Every successful `fridayoutreach enrich` invocation writes the complete enriched CSV and a separate `<output-stem>.safe.csv`, including a header-only companion when no contacts qualify or verification was explicitly skipped. The safe export uses dedicated contact fields and requires literal publication evidence, native Reacher `safe`, mailbox confirmation, positive syntax/MX/SMTP flags, false catch-all/disabled/full-inbox flags, and no verification error. Duplicate exact business/email observations combine their source URLs while distinct addresses remain separate.

Regression checks cover case-insensitive input aliases, output collisions and unsafe links before fetch, unchecked input email exclusion, strict status/flag failures, source ownership, cold/idempotent installation, and preservation of unknown installation targets. Both clean extracted installations passed actual CLI and agent-stdin calls, including visible missing-native errors and empty safe companions for the explicit no-SMTP path.

The previously recorded real-directory dataset of22businesses and48native checks was replayed twice offline through the current agent runner. Both replays produced exactly4safe contacts with identical CSV bytes; the recorded outcomes were4safe,41risky,3invalid. These are replays of the original check snapshot, not fresh SMTP checks. The update performed no AI inference for extraction/verification, guessed no emails, and sent no messages. The public payload includes no real lead data.
