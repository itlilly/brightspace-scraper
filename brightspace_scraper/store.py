"""SQLite store: the source of truth for what has been seen, organized course-first.

Tables:
  courses           - one row per course offering (keyed by org_unit_id, global/shareable)
  items             - course-level content (shareable); hashed for change detection
  enrollment_items  - per-user data (grades/overrides); kept separate, never shared
  runs              - one row per harvest run

The normalized item (text + raw JSON) is stored too, so the harvested corpus is cached
and a future AI stage can be re-run without re-scraping.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .models import Course, HarvestItem
from .util import now_iso as _now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS courses (
    org_unit_id   INTEGER PRIMARY KEY,
    name          TEXT,
    code          TEXT,
    type          TEXT,
    last_seen_run INTEGER,
    raw_json      TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id                  TEXT PRIMARY KEY,
    org_unit_id         INTEGER NOT NULL,
    type                TEXT NOT NULL,
    title               TEXT,
    structured_due_date TEXT,
    source_url          TEXT,
    content_hash        TEXT NOT NULL,
    content_ref         TEXT,
    needs_vision        INTEGER DEFAULT 0,
    last_modified       TEXT,
    last_seen_run       INTEGER,
    status              TEXT,
    item_json           TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_ou ON items(org_unit_id);
CREATE TABLE IF NOT EXISTS enrollment_items (
    id            TEXT PRIMARY KEY,
    org_unit_id   INTEGER NOT NULL,
    type          TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    last_seen_run INTEGER,
    item_json     TEXT
);
CREATE TABLE IF NOT EXISTS deadlines (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    org_unit_id         INTEGER,
    item_id             TEXT,
    title               TEXT,
    type                TEXT,
    final_due_date      TEXT,
    structured_due_date TEXT,
    confidence          TEXT,
    source_url          TEXT,
    reasoning           TEXT,
    run_id              INTEGER,
    created_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_deadlines_ou ON deadlines(org_unit_id);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- runs --------------------------------------------------------------
    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs(started_at) VALUES (?)", (_now(),))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=? WHERE run_id=?", (_now(), run_id)
        )
        self.conn.commit()

    # -- courses -----------------------------------------------------------
    def upsert_course(self, course: Course, run_id: int) -> None:
        term = (course.code or "").rsplit(".", 1)[-1] if course.code else None
        self.conn.execute(
            """INSERT INTO courses(org_unit_id,name,code,type,last_seen_run,raw_json)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(org_unit_id) DO UPDATE SET
                 name=excluded.name, code=excluded.code, type=excluded.type,
                 last_seen_run=excluded.last_seen_run, raw_json=excluded.raw_json""",
            (course.org_unit_id, course.name, course.code, course.type, run_id,
             json.dumps(course.raw)),
        )

    # -- items -------------------------------------------------------------
    def get_hash(self, item_id: str, *, per_user: bool = False) -> str | None:
        table = "enrollment_items" if per_user else "items"
        row = self.conn.execute(
            f"SELECT content_hash FROM {table} WHERE id=?", (item_id,)
        ).fetchone()
        return row["content_hash"] if row else None

    def get_last_modified(self, item_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT last_modified FROM items WHERE id=?", (item_id,)
        ).fetchone()
        return row["last_modified"] if row else None

    def upsert_item(
        self, item: HarvestItem, content_hash: str, run_id: int, status: str
    ) -> None:
        last_modified = (item.raw or {}).get("last_modified")
        item_json = json.dumps(asdict(item), default=str)
        if item.per_user:
            self.conn.execute(
                """INSERT INTO enrollment_items(id,org_unit_id,type,content_hash,last_seen_run,item_json)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     content_hash=excluded.content_hash,
                     last_seen_run=excluded.last_seen_run, item_json=excluded.item_json""",
                (item.id, item.org_unit_id, item.type, content_hash, run_id, item_json),
            )
            return
        self.conn.execute(
            """INSERT INTO items(id,org_unit_id,type,title,structured_due_date,source_url,
                                 content_hash,content_ref,needs_vision,last_modified,
                                 last_seen_run,status,item_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, structured_due_date=excluded.structured_due_date,
                 source_url=excluded.source_url, content_hash=excluded.content_hash,
                 content_ref=excluded.content_ref, needs_vision=excluded.needs_vision,
                 last_modified=excluded.last_modified, last_seen_run=excluded.last_seen_run,
                 status=excluded.status, item_json=excluded.item_json""",
            (item.id, item.org_unit_id, item.type, item.title, item.structured_due_date,
             item.source_url, content_hash, item.content_ref, int(item.needs_vision),
             last_modified, run_id, status, item_json),
        )

    def mark_removed(self, org_unit_ids: set[int], run_id: int) -> list[str]:
        """Items in the scraped courses not seen this run are 'removed'. Returns their ids."""
        if not org_unit_ids:
            return []
        placeholders = ",".join("?" for _ in org_unit_ids)
        rows = self.conn.execute(
            f"""SELECT id FROM items
                WHERE org_unit_id IN ({placeholders})
                  AND (last_seen_run IS NULL OR last_seen_run < ?)
                  AND status != 'removed'""",
            (*org_unit_ids, run_id),
        ).fetchall()
        removed = [r["id"] for r in rows]
        if removed:
            self.conn.executemany(
                "UPDATE items SET status='removed' WHERE id=?",
                [(i,) for i in removed],
            )
        return removed

    def commit(self) -> None:
        self.conn.commit()

    # -- interpretation support -------------------------------------------
    def all_course_ids(self) -> list[int]:
        rows = self.conn.execute("SELECT org_unit_id FROM courses").fetchall()
        return [r["org_unit_id"] for r in rows]

    def course_name(self, org_unit_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT name FROM courses WHERE org_unit_id=?", (org_unit_id,)
        ).fetchone()
        return row["name"] if row else None

    def get_items_for_courses(self, org_unit_ids: list[int]) -> dict[int, list[dict]]:
        """Return current (non-removed) items grouped by course, as plain dicts."""
        out: dict[int, list[dict]] = {oid: [] for oid in org_unit_ids}
        if not org_unit_ids:
            return out
        placeholders = ",".join("?" for _ in org_unit_ids)
        rows = self.conn.execute(
            f"""SELECT org_unit_id, item_json FROM items
                WHERE org_unit_id IN ({placeholders})
                  AND (status IS NULL OR status != 'removed')""",
            tuple(org_unit_ids),
        ).fetchall()
        for r in rows:
            out[r["org_unit_id"]].append(json.loads(r["item_json"]))
        return out

    def save_deadlines(self, deadlines: list[dict], run_id: int) -> None:
        """Replace deadlines for the affected courses with the fresh set."""
        affected = {d["org_unit_id"] for d in deadlines}
        for oid in affected:
            self.conn.execute("DELETE FROM deadlines WHERE org_unit_id=?", (oid,))
        for d in deadlines:
            self.conn.execute(
                """INSERT INTO deadlines(org_unit_id,item_id,title,type,final_due_date,
                       structured_due_date,confidence,source_url,reasoning,run_id,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (d.get("org_unit_id"), d.get("item_id"), d.get("title"), d.get("type"),
                 d.get("final_due_date"), d.get("structured_due_date"),
                 d.get("confidence"), d.get("source_url"), d.get("reasoning"),
                 run_id, _now()),
            )
        self.conn.commit()
