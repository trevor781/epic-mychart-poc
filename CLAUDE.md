# epic_api

Proof of concept: extract the user's health records via Epic's patient-access
FHIR APIs (SMART on FHIR OAuth), plus Apple Health export parsing, and make
the combined data LLM-usable.

## Working with the health data

**`data/` is a symlink** to the consolidated health-data vault at
`~/workspace/active_claude_projects/trevors_health` — the canonical home of
all of Trevor's health data (this repo's extractions, Apple Health, plus
provider downloads and an email archive from other tools). All the scripts
here keep working unchanged; their outputs land in the vault through the
symlink.

**Start at `data/LLM_GUIDE.md`** (the vault's guide) — it documents every
dataset (what each file is, FHIR parsing patterns, the SQLite schema for
wearable data, known data-quality issues, and how to refresh each artifact).
Read it before touching the data.

`data` is gitignored because it contains PHI (real medical records). Never
commit anything from it, never upload its contents to external services, and
never quote identifying details in committed files.

## Scripts (all stdlib Python 3, no dependencies)

- `extract_mychart.py` — SMART on FHIR OAuth (PKCE) + full FHIR extraction.
  Interactive: opens a browser for MyChart login. Client IDs in macOS Keychain.
- `compile_record.py` — extraction → `record.md` + `prompt.md` (LLM-ready).
- `generate_report.py` — extraction → interactive `report.html`.
- `parse_apple_health.py` / `apple_health_to_sqlite.py` — Apple Health
  export.xml → monthly summary / queryable SQLite.

The OAuth callback + terms pages are on Cloudflare Pages
(https://personal-health-2h9.pages.dev, source in `site/`,
deploy: `npx wrangler@latest pages deploy site --project-name personal-health`).

## Conventions

- When printing a local file path for the user, print a `file://` URL
  (clickable in their terminal).
- `data/<timestamp>/` dirs are extractions; highest timestamp = current.
  After a new extraction, rerun `compile_record.py` and `generate_report.py`.
