# Session changelogs

One file per session that changed code, configuration, or documentation. Named
`YYYY-MM-DD-<short-slug>.md`. Committed alongside the change it describes, so history and
audit trail stay in lockstep. If several sessions land on one day, keep the slugs distinct —
never overwrite a prior session's file.

Each entry covers:

- **What** changed — the specific files, endpoints, models, services, or settings.
- **Why** — the request, bug, or requirement behind it.
- **How** — the approach taken, notable decisions, and any tradeoff made.
- **Affected modules and behaviors** — including downstream effects.
- **Intended outcome** — one line: what this was supposed to make true, and what signal would
  show it did or did not. Skip for changes with no behavioral goal.
- **Verification** — the commands run and their real results. Name anything left unverified.
- **Critical notes** — security implications, rollback considerations, breaking changes, and
  follow-ups an on-call engineer or auditor would need.

Never record secrets, API keys, credentials, or personal data in a changelog.
