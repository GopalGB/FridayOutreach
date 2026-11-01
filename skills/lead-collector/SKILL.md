---
name: lead-collector
description: Use for bounded public-directory collection, lead review and export, published business contact enrichment, and local Reacher verification.
---

# Lead Collector

From a clone, use Python 3.11+ and bootstrap only the offline core wheel set:

```sh
python3 scripts/setup.py --core
```

Use the installed `lead-collector` launcher or the clone's documented Python
entry point `.venv/bin/python lead.py`. Keep every source within the user's
approved hosts, paths and page budgets. The primary no-AI enrichment is:

```sh
lead-collector enrich --input INPUT.csv --output OUTPUT.csv
```

It is deterministic, serial, capped, source-bound, and AI-free. Use the
provider-neutral function schema and `scripts/agent_tool.py` when a host needs
an agent-callable operation. The model proposes arguments; the trusted host
executes the local runner. Never pass arbitrary commands or executable paths.

For directory collection, preview the chosen source first, then use its
actual column mapping or an explicit saved recipe:

```sh
lead-collector preview --url SOURCE_URL
lead-collector collect --url SOURCE_URL --mapping '{"Name":"company"}'
lead-collector review --job JOB_ID
lead-collector export --job JOB_ID --format csv --output businesses.csv --row ROW_ID
```

Copy job/row IDs from the returned JSON; repeat `--row` for each chosen row.
JSON export retains full evidence. CSV is a formula-safe projection. Existing
files need explicit `--overwrite`. `contacts` accepts approved URLs or chosen
job rows plus an explicit recipe. Keep missing, denied, failed and limited
outcomes distinct and retain source URLs/observation times.

For an agent tool host, bind `lead_collector_enrich` from
`integrations/function-tool.json` to the fixed local runner:

```sh
printf '%s\n' '{"input_csv":"businesses.csv","output_csv":"enriched.csv"}' | .venv/bin/python scripts/agent_tool.py --workspace "$PWD"
```

Email verification is optional and must be explicitly requested with
`--verify-emails`; it uses only a trusted local Reacher runtime. Never infer an
address, widen a domain, send mail, or treat uncertain verification as safe.

The optional runner is not downloaded or built automatically. See
`scripts/reacher_smtp_runner/README.md` for its local source/build prerequisites.

AI stays optional: `assist --ai off` needs only the core. ScrapeGraphAI uses an
already-running local Ollama service and installed `qwen3:0.6b`; no service or
model download starts automatically. Jev preview/replay are contract-tested;
live Jev remains UNRUN under ZERO CHARGE. Do not add `--allow-metered-jev`
without explicit spend authorization. The enrichment/function path performs
no AI inference and sends no outreach or email message body.

See `docs/AGENT-USAGE.md` for caps, path rules, and the end-to-end Mermaid flow.
