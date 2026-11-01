# Business lead scraping and email extraction without AI

FridayOutreach helps you turn a bounded public business directory into a reviewable lead CSV. It is useful when you already have an approved directory, want to follow each listed website, and need literal contact addresses published by those businesses. It is not a contact database or a discovery service for every result on the web.

## A small, reviewable workflow

Start with a CSV containing `company` and `website` columns. Follow the [getting-started workflow](GETTING-STARTED.md) for directory collection, review, and export. For normal enrichment, build the local Reacher runner using the [native runner build instructions](../scripts/reacher_smtp_runner/README.md), then run:

```sh
fridayoutreach enrich --input businesses.csv --output leads.csv
```

The input must contain a company name and website for each business you want to process. The bounded flow is public directories to websites to literal published addresses to local checks to CSV. FridayOutreach does not guess an address from a name or domain. It records source observations and verification outcomes so you can review them before using the data.

The requested `leads.csv` is the full output. It keeps non-qualifying and error outcomes as well as successful observations. The companion `leads.safe.csv` is narrower: it contains only source-observed addresses that pass the native safe criteria. See the [output schema](OUTPUT-SCHEMA.md) for the fields and the [agent usage contract](AGENT-USAGE.md) for the fixed local function-host path. The core extraction and verifier work without AI; a model host may invoke the fixed runner, but it does not infer contacts.

## FAQ

**Do I need AI?** No. Deterministic extraction and the local verification path do not need AI.

**Are addresses guessed?** Never. An address must be literally published in the captured source evidence.

**What does safe mean?** Safe output requires a strict native-safe result, a verified mailbox, true syntax, MX, and SMTP checks, false catch-all, disabled, and full-inbox flags, and no error. Unsupported, limited, or uncertain results are excluded from safe output.

**Does it send email?** No. Enrichment sends no email messages and does not activate a paid provider.

**What does `--skip-email-verification` do?** It disables SMTP verification only. Websites are still fetched over HTTP. The safe CSV contains its header and no contact rows because verification is required for inclusion.

**Does it discover Google Maps or LinkedIn results automatically?** No. Those discovery paths are unsupported here. Use an approved, bounded public directory and keep actual lead and email data private. Do not use real contacts as indexed examples.

Continue with the [getting-started workflow](GETTING-STARTED.md) for detailed collection commands and the [output schema](OUTPUT-SCHEMA.md) for the full CSV field reference.
