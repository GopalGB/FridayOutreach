# Output schema

The full enrichment CSV preserves input columns and adds `published_emails`, `email_evidence`, `email_verification`, `site_status`, and `site_evidence`. JSON evidence retains source URLs and observation timestamps; formula-sensitive CSV cells are escaped.

The safe CSV has exactly these 16 columns, in this order:

`company,email,website,phone,address,email_source_urls,observed_at,verification_status,mailbox_verified,syntax_valid,mx_accepts_mail,smtp_deliverable,catch_all,disabled,full_inbox,verification_error`

| Column | Meaning in a qualifying safe row |
| --- | --- |
| `company` | Business name from the input. |
| `email` | Exact published address, preserving its observed casing. |
| `website` | Input business website. |
| `phone` | Input phone when supplied; otherwise empty. |
| `address` | Input address when supplied; otherwise empty. |
| `email_source_urls` | Public pages where that business's address was observed. |
| `observed_at` | Page observation time(s), rather than a new verification timestamp. |
| `verification_status` | `safe`. |
| `mailbox_verified` | `true`. |
| `syntax_valid` | `true`. |
| `mx_accepts_mail` | `true`. |
| `smtp_deliverable` | `true`. |
| `catch_all` | `false`. |
| `disabled` | `false`. |
| `full_inbox` | `false`. |
| `verification_error` | Empty; errored results cannot qualify. |

Boolean values are lowercase strings (`true` or `false`). `email_source_urls` and `observed_at` preserve distinct source observations as semicolon-separated values. Safe rows are grouped by the exact business identity `(company, website, address, phone)` plus the raw email; distinct raw addresses remain separate.

A safe row requires literal source observation, native `safe` status, true mailbox confirmation and syntax/MX/SMTP flags, false catch-all/disabled/full-inbox flags, and an empty `verification_error`. The full CSV may instead record risky, invalid, unknown, not-checked-limit, no-website, fetch, or other named outcomes.

An empty result, explicit opt-out, or per-email failure simply excludes that contact and can leave a header-only safe CSV. A run-wide preflight or fatal error - such as a missing default verifier - returns `ok:false` and does not guarantee either output file. See [Agent usage](AGENT-USAGE.md) and [Verification](VERIFICATION.md) for the envelope and measured behavior.
