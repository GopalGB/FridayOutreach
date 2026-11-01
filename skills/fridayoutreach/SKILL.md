---
name: fridayoutreach
description: Use FridayOutreach for deterministic, AI-free enrichment with a strict safe-contact companion CSV.
---

# FridayOutreach

From the clone, bootstrap the bundled offline core and install FridayOutreach:

```sh
python3 scripts/setup.py --core
python3 scripts/setup.py --install
fridayoutreach enrich --input businesses.csv --output businesses.enriched.csv
```

Every successful run writes the requested CSV and
`businesses.enriched.safe.csv`. The safe file has a fixed per-contact schema and
contains only source-observed literal addresses whose native result is `safe`,
`mailboxVerified` is true, syntax/MX/SMTP are true, catch-all/disabled/full are
false, and no error is present; skipped verification produces a header-only file.

The trusted host maps `friday_outreach_enrich` from
`integrations/fridayoutreach-function-tool.json` to the fixed runner:

```sh
printf '%s\n' '{"input_csv":"businesses.csv","output_csv":"businesses.enriched.csv"}' | .venv/bin/python scripts/agent_tool.py --workspace "$PWD"
```

Models select arguments only: no AI inference, address guessing, sent messages,
outreach, credentials, or provider spend. Default verification needs the local
Reacher runner; if it is missing, follow
`scripts/reacher_smtp_runner/README.md` or use explicit `verify_emails:false`.
