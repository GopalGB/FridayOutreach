# Getting started

Clone the repository and prepare the local core with Python 3.11+:

```sh
git clone https://github.com/GopalGB/FridayOutreach.git
cd FridayOutreach
python3 scripts/setup.py --core
python3 scripts/setup.py --install
```

`fridayoutreach` is the installed branded launcher; `lead-collector` remains a compatible legacy name.

## Inspect, collect, and export a local directory

The checked-in fixture is fictional and requires no network, AI, SMTP, or native runner. The mapping assigns its observed `Name` column to `company`:

```sh
fridayoutreach preview --html example.html --source-url https://source.example.test/directory --mapping '{"Name":"company"}'
fridayoutreach collect --html example.html --source-url https://source.example.test/directory --mapping '{"Name":"company"}'
```

Copy the returned job ID, then review rows and export the row IDs you choose:

```sh
fridayoutreach review --job JOB_ID --sort company
fridayoutreach export --job JOB_ID --format csv --output selected.csv --row ROW_ID
```

`preview` does not create a job. `collect` does, and its JSON envelope supplies the job and row IDs needed by `review` and `export`.

## Offline enrichment demonstration

`--skip-email-verification` turns off SMTP verification only. A nonblank `website` is still fetched over HTTP. To run a local demonstration without either network or SMTP, make a new caller-owned CSV with a blank website:

```sh
FRIDAY_DEMO_DIR="$(mktemp -d "${HOME}/friday-demo.XXXXXX")"
printf '%s\n' 'company,website' 'Fictional Offline Co,' > "$FRIDAY_DEMO_DIR/offline-input.csv"
fridayoutreach enrich --input "$FRIDAY_DEMO_DIR/offline-input.csv" --output "$FRIDAY_DEMO_DIR/offline-results.csv" --skip-email-verification
```

The command writes a full CSV and a header-only `offline-results.safe.csv` in that new directory. It does not alter `examples/businesses.csv`.

## Use an approved public directory

Set `DIRECTORY_URL` to the source you are authorized to collect. Preview first, then replace the example column names below with the actual headers shown by that preview:

```sh
fridayoutreach preview --url "$DIRECTORY_URL"
fridayoutreach collect --url "$DIRECTORY_URL" --mapping '{"Name":"company","Website":"website"}'
fridayoutreach review --job JOB_ID --sort company
fridayoutreach export --job JOB_ID --format csv --output businesses.csv --row ROW_ID
```

Copy job and row IDs from the returned JSON, and repeat `--row ROW_ID` for each chosen record. Use a new output filename. For repeated-card layouts, inspect `fridayoutreach recipe --help` and save an explicit recipe rather than assuming a universal layout. Collection captures directory values; the enrichment command below visits the selected business websites.

For default verified enrichment, first build the optional trusted local runner using the [native runner README](../scripts/reacher_smtp_runner/README.md), then use your own business CSV:

```sh
fridayoutreach enrich --input businesses.csv --output businesses.enriched.csv
```

If that runner is missing, the default path fails before any fetch or output write. Deterministic collection and enrichment do not require AI. ScrapeGraphAI has separate local prerequisites; live Jev remains UNRUN and cost-guarded. See [Agent usage](AGENT-USAGE.md) for the trusted function-host contract, [output schema](OUTPUT-SCHEMA.md) for result fields, and [Verification](VERIFICATION.md) for pinned checks. Windows is not tested.
