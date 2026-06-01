# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A MUN (Memorial University) Brightspace/D2L scraper that collects course deadlines —
including ones buried in PDFs and announcements that have no structured due-date field —
and turns them into a readable deadline report. Single-user CLI, Python 3.12+.

## Commands

```bash
# one-time setup
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# store MUN credentials in the OS keychain (interactive — needs a real TTY, NOT the `!` box)
.venv/bin/python -m brightspace_scraper.credentials set
.venv/bin/python -m brightspace_scraper.credentials status   # check what's resolved

# the full pipeline (DATA_DIR + model default come from .env)
.venv/bin/python -m brightspace_scraper.cli            # Stage 1: harvest current-term courses
.venv/bin/python -m brightspace_scraper.interpret --all  # Stage 2: AI -> deadlines
.venv/bin/python -m brightspace_scraper.report           # human-readable deadlines.md

# optional Stage 3: push deadlines into Google Calendar (pure-httpx OAuth, no Google SDK)
.venv/bin/python -m brightspace_scraper.calendar_sync auth   # one-time browser consent (needs a real TTY)
.venv/bin/python -m brightspace_scraper.calendar_sync sync   # deadlines -> "MUN Deadlines" calendar

# useful flags
... cli --active | --all-courses | --courses 671215,671225 | --no-content | --full
... interpret --dry-run        # build bundles + print token sizes, no model call
... interpret --stream         # stream the model's output live to the terminal
... report --all               # include past deadlines
... calendar_sync sync --dry-run   # show the plan, make no Google API calls
... calendar_sync sync --all       # include past deadlines; --no-prune to never delete
```

There is **no automated test suite**. Verify changes by: `python -m py_compile` on changed
files, `interpret --dry-run` (exercises bundling without a model), and a scoped real run
(`cli --courses <id> --no-content` then `interpret --courses <id>`).

## Configuration

- Credentials live in the **OS keychain** (`keyring`), never in files. `.env` (gitignored)
  holds only non-secret config: `DATA_DIR` (default `~/.local/share/brightspace`),
  `LOCAL_LLM_MODEL`, `LOCAL_LLM_URL`. See `.env.example`.
- Resolution order for creds: keychain first, then `MUN_USERNAME`/`MUN_PASSWORD` env.

## Architecture

Two stages with a **change-detection seam** between them, so the AI only ever sees new or
changed content.

**Stage 1 — Harvest** (`cli.py` orchestrates):
`auth.py` (CAS login at login.mun.ca → session cookies, pure httpx, no browser) →
`client.py` (cookie'd httpx client; discovers LP/LE API versions; `get_paged` handles
bookmark paging) → `harvest/*.py` (one collector per type: courses, assignments, quizzes,
announcements, calendar, content) → `extract.py` (PDF/Word/PPT → text; model-free — images
& scanned PDFs get `needs_vision=True`, NOT OCR'd here; the raw file is preserved via
`content_ref`) → `store.py` (SQLite) + `changeset.py`
(SHA-256 per item, diff vs store, emit `changeset.json` of new/changed only).

**Stage 2 — Interpret** (`interpret.py`): an interpreter-side OCR pass (`ocr.py`,
Tesseract) reads any `needs_vision` items from their preserved raw file — so OCR runs
wherever Stage 2 runs (the GPU desktop, eventually), not on a thin client; it's a
graceful no-op if Tesseract is absent, leaving `needs_vision` as the escalation signal
for a future GPU vision model. Then each course's items (structured + unstructured
**together**, so the model can reconcile) go to an LLM that returns reconciled
`Deadline`s → stored in the `deadlines` table → `report.py` renders `deadlines.md`.

### Key design rules (don't break these)

- **Structured dates are ground truth, the model only adds/reconciles.** `_build_deadlines`
  in `interpret.py` deterministically *seeds* every assignment/quiz that has a
  `structured_due_date`, so a date the API already knows can never be lost even if the
  model omits it. The model's job is to find dates hidden in prose/PDFs and to override a
  structured date *only with clear textual evidence*. Output keeps both
  `structured_due_date` and `final_due_date` + confidence, so changes are auditable.
- **Adding an LLM backend = subclass `Interpreter` and implement `_complete(system, user)`
  only.** Bundling (`render_bundle`), JSON parsing (`_parse_model_json`), seeding/dedup
  (`_build_deadlines`) are shared in the base class. `LocalInterpreter` (OpenAI-compatible
  endpoint: Ollama/LM Studio/llama.cpp/vLLM) is the reference. A cloud backend slots in the
  same way — do not duplicate the pipeline.
- **Course-centric, multi-user-ready model** (`models.py`): a `Course` keyed by Brightspace
  `org_unit_id` (identical for everyone enrolled) owns shareable course-level items;
  per-user data goes in the separate `enrollment_items` table and is never shared. Stable
  item id format: `{type}:{org_unit_id}:{item_id}`.
- **Course scope** (`harvest/courses.py`): default `current` = the most recent term code
  (`.YYYYTT` suffix on the course code) among currently-accessible courses; `active` =
  IsActive + date window (includes evergreen/stale); `all` = everything ever.

### Gotchas

- **Hard ceiling:** work in external tools (Gradescope, Top Hat, Connect) isn't on
  Brightspace and can't be harvested — such links are recorded and flagged, not extracted.
- **Bundle budget:** `render_bundle` truncates per item and caps per course; syllabi/lab
  sheets (matched by `_HIGH_VALUE_RE`) get a larger char budget because their dated
  schedules sit deep in the document. Lecture slides/datasheets are deprioritized first.
- **Local GPU:** Arch's default `ollama` package is CPU-only; GPU needs `ollama-cuda`. A
  model only runs on the GPU if it fits in VRAM. Restart the running `ollama serve` after
  swapping packages (a package install doesn't restart the live server).
- **Re-downloads:** `store` records `last_modified` per item but the harvester does not yet
  use it to skip unchanged downloads — every run currently re-downloads all content.
- `DATA_DIR`, secrets, and `.venv` are gitignored. The store can accumulate stale courses
  if a broader-scope run is interrupted; the current-term filter only applies at harvest.
```
