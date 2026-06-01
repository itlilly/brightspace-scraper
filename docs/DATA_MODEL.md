# Data Model

The model is **course-centric and multi-user-ready**. The organizing key is Brightspace's
`org_unit_id` — identical for *everyone* enrolled in the same course offering. That's the seam
a future shared/multi-user backend needs: course-level content is shareable; per-user content
is isolated.

```mermaid
flowchart TB
    subgraph shared["SHARED · keyed by org_unit_id · poolable across a section"]
        Course --> HarvestItem
        HarvestItem --> Deadline
    end
    subgraph private["PER-USER · never shared"]
        EnrollmentItem["enrollment_items<br/>grades / submissions / overrides"]
    end
    Course -.-> EnrollmentItem
```

---

## Stable item ID

Every harvested item has a stable, reproducible ID (`util.item_id`):

```
{type}:{org_unit_id}:{item_id}
        e.g.  assignment:671503:6148138
              content_file:671215:6146415
```

Because `org_unit_id` is identical across all enrolled students, this ID is the same for
everyone — so one student's scrape of a course can populate the shared pool for the whole
section. It's also the basis for idempotent Google Calendar event IDs in `calendar_sync.py`.

---

## In-memory types (`models.py`, `interpret.py`)

### `Course` (`models.py`)
| Field | Type | Notes |
|-------|------|-------|
| `org_unit_id` | int | **Primary key.** Identical for all enrolled. |
| `name` | str | e.g. "Mechanisms and Machines" |
| `code` | str \| None | e.g. `ME-4302-001`; the `.YYYYTT` suffix encodes the term |
| `type` | str \| None | e.g. "Course Offering" |
| `raw` | dict | Original API payload |

### `HarvestItem` (`models.py`)
A single harvested artifact — course-level unless `per_user` is True.
| Field | Type | Notes |
|-------|------|-------|
| `id` | str | Stable ID `{type}:{org_unit_id}:{item_id}` |
| `org_unit_id` | int | Owning course |
| `type` | str | One of the item-type constants below |
| `title` | str | |
| `source_url` | str \| None | Link back to the Brightspace item |
| `structured_due_date` | str \| None | ISO-8601 — **authoritative** if Brightspace provided one |
| `body_text` | str \| None | HTML-stripped instructions/body/page text |
| `extracted_text` | str \| None | Text pulled from a downloaded file (or OCR at interpret time) |
| `needs_vision` | bool | Image/scanned file OCR couldn't read → escalate to a vision model |
| `per_user` | bool | If True, lands in `enrollment_items` (private), not `items` |
| `content_ref` | str \| None | Path to the raw downloaded file on disk |
| `raw` | dict | Original API payload |

**Item-type constants:** `assignment`, `quiz`, `announcement`, `calendar_event`,
`content_page`, `content_file`.

### `Deadline` (`interpret.py`)
The Stage-2 output. Keeps *both* dates so any model change is auditable.
| Field | Type | Notes |
|-------|------|-------|
| `org_unit_id` | int | Owning course |
| `item_id` | str \| None | Source item's stable ID (may be null for a model-discovered date) |
| `title` | str | e.g. "Quiz 2", "Mid-Term Exam" |
| `type` | str | assignment / quiz / test / midterm / exam / lab / project / other |
| `final_due_date` | str \| None | The reconciled date the report/calendar uses |
| `structured_due_date` | str \| None | What Brightspace originally said (ground truth) |
| `confidence` | str | high / medium / low |
| `source_url` | str \| None | Link back to the item |
| `reasoning` | str | One line: where the date came from / why |

---

## SQLite schema (`store.py`)

`DATA_DIR/brightspace.sqlite` — five tables. Relationships:

```mermaid
erDiagram
    RUNS ||--o{ COURSES : "last_seen_run"
    RUNS ||--o{ ITEMS : "last_seen_run"
    RUNS ||--o{ DEADLINES : "run_id"
    COURSES ||--o{ ITEMS : "org_unit_id (shared)"
    COURSES ||--o{ ENROLLMENT_ITEMS : "org_unit_id (per-user)"
    COURSES ||--o{ DEADLINES : "org_unit_id"
    ITEMS ||--o{ DEADLINES : "item_id"

    RUNS {
        int run_id PK
        text started_at
        text finished_at
    }
    COURSES {
        int org_unit_id PK
        text name
        text code
        text type
        int last_seen_run
        text raw_json
    }
    ITEMS {
        text id PK "stable item ID"
        int org_unit_id "indexed"
        text type
        text title
        text structured_due_date
        text source_url
        text content_hash "SHA-256, change detection"
        text content_ref "path to raw file"
        int needs_vision
        text status "new / changed / removed"
        text item_json "full serialized HarvestItem"
    }
    ENROLLMENT_ITEMS {
        text id PK
        int org_unit_id
        text type
        text content_hash
        text item_json "grades / submissions (private)"
    }
    DEADLINES {
        int id PK
        int org_unit_id
        text item_id
        text title
        text type
        text final_due_date "reconciled"
        text structured_due_date "ground truth"
        text confidence
        text source_url
        text reasoning
        int run_id
        text created_at
    }
```

> `save_deadlines` replaces all deadlines for the affected courses with the fresh set each
> interpret run (delete-then-insert per course).

---

## Lifecycle of a deadline

```mermaid
flowchart LR
    item["HarvestItem<br/>items table<br/>(structured_due_date + text/OCR)"]
    item -->|"render_bundle → LLM → _build_deadlines"| dl["Deadline<br/>final_due_date reconciled<br/>structured_due_date preserved<br/>confidence + reasoning"]
    dl -->|"save_deadlines"| tbl[("deadlines table")]
    tbl --> md["report.py<br/>deadlines.md"]
    tbl --> gcal["calendar_sync.py<br/>Google Calendar event<br/>(idempotent ID from item_id+title)"]
```

## Why the shared / per-user split matters

Course-level deadlines are **identical for every student in a section** — so a multi-user
backend interprets each course **once** (deduped by `content_hash`) and serves the result to
everyone enrolled. AI work scales with the number of *unique courses*, not the number of
*students*. Per-user data (`enrollment_items`) stays private and is never pooled.
