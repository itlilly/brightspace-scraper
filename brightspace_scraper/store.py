"""Postgres store: the source of truth, organized course-first and multi-tenant.

One schema serves both tiers (see memory `brightspace-product-direction`):
  * self-host  -> a single implicit user, institution='mun', no OAuth
  * hosted     -> many users via Google OAuth, pooled by org_unit_id

Shared / poolable (identical for everyone in an offering, institution-scoped):
  courses, items, deadlines, runs
Per-user (never shared):
  users, sessions, enrollments  (which sections a user is in + their calendar token)

The normalized item (text + raw JSON) is stored too, so the harvested corpus is cached
and the AI stage can re-run without re-scraping. SQL is hand-written (no ORM) per house
style; connect via a libpq URL (`DATABASE_URL`), so dev (local Postgres) and prod
(managed/containerized) differ only by that string.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict

import psycopg
from psycopg.rows import dict_row

from .models import Course, HarvestItem

# Executed on every connect; idempotent. Institution-scoped shared tables use a composite
# PK so other schools can't collide once multi-institution adapters arrive.
_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id                     SERIAL PRIMARY KEY,
        google_sub             TEXT UNIQUE,
        email                  TEXT,
        calendar_refresh_token TEXT,            -- Fernet-encrypted; never plaintext
        calendar_id            TEXT,
        created_at             TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT PRIMARY KEY,
        user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS enrollments (
        user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        institution   TEXT NOT NULL,
        org_unit_id   BIGINT NOT NULL,
        last_seen_run INTEGER,
        PRIMARY KEY (user_id, institution, org_unit_id)
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id      SERIAL PRIMARY KEY,
        user_id     INTEGER,
        started_at  TIMESTAMPTZ,
        finished_at TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS courses (
        institution   TEXT NOT NULL,
        org_unit_id   BIGINT NOT NULL,
        name          TEXT,
        code          TEXT,
        type          TEXT,
        last_seen_run INTEGER,
        raw_json      TEXT,
        PRIMARY KEY (institution, org_unit_id)
    )""",
    """CREATE TABLE IF NOT EXISTS items (
        institution         TEXT NOT NULL,
        id                  TEXT NOT NULL,
        org_unit_id         BIGINT NOT NULL,
        type                TEXT NOT NULL,
        title               TEXT,
        structured_due_date TEXT,
        source_url          TEXT,
        content_hash        TEXT NOT NULL,
        content_ref         TEXT,
        needs_vision        BOOLEAN DEFAULT FALSE,
        last_modified       TEXT,
        last_seen_run       INTEGER,
        status              TEXT,
        item_json           TEXT,
        PRIMARY KEY (institution, id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_items_ou ON items(institution, org_unit_id)",
    """CREATE TABLE IF NOT EXISTS enrollment_items (
        institution   TEXT NOT NULL,
        id            TEXT NOT NULL,
        org_unit_id   BIGINT NOT NULL,
        type          TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        last_seen_run INTEGER,
        item_json     TEXT,
        PRIMARY KEY (institution, id)
    )""",
    """CREATE TABLE IF NOT EXISTS deadlines (
        id                  SERIAL PRIMARY KEY,
        institution         TEXT NOT NULL,
        org_unit_id         BIGINT,
        item_id             TEXT,
        title               TEXT,
        type                TEXT,
        final_due_date      TEXT,
        structured_due_date TEXT,
        confidence          TEXT,
        source_url          TEXT,
        reasoning           TEXT,
        run_id              INTEGER,
        created_at          TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS idx_deadlines_ou ON deadlines(institution, org_unit_id)",
]


class Store:
    def __init__(self, database_url: str, institution: str = "mun"):
        self.institution = institution
        self.conn = psycopg.connect(database_url, row_factory=dict_row)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        for stmt in _SCHEMA:
            self.conn.execute(stmt)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # -- runs --------------------------------------------------------------
    def start_run(self, user_id: int | None = None) -> int:
        row = self.conn.execute(
            "INSERT INTO runs(user_id, started_at) VALUES (%s, now()) RETURNING run_id",
            (user_id,),
        ).fetchone()
        self.conn.commit()
        return int(row["run_id"])

    def finish_run(self, run_id: int) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=now() WHERE run_id=%s", (run_id,)
        )
        self.conn.commit()

    # -- courses -----------------------------------------------------------
    def upsert_course(self, course: Course, run_id: int) -> None:
        self.conn.execute(
            """INSERT INTO courses(institution,org_unit_id,name,code,type,last_seen_run,raw_json)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(institution,org_unit_id) DO UPDATE SET
                 name=excluded.name, code=excluded.code, type=excluded.type,
                 last_seen_run=excluded.last_seen_run, raw_json=excluded.raw_json""",
            (self.institution, course.org_unit_id, course.name, course.code,
             course.type, run_id, json.dumps(course.raw)),
        )

    # -- items -------------------------------------------------------------
    def get_hash(self, item_id: str, *, per_user: bool = False) -> str | None:
        table = "enrollment_items" if per_user else "items"
        row = self.conn.execute(
            f"SELECT content_hash FROM {table} WHERE institution=%s AND id=%s",
            (self.institution, item_id),
        ).fetchone()
        return row["content_hash"] if row else None

    def get_last_modified(self, item_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT last_modified FROM items WHERE institution=%s AND id=%s",
            (self.institution, item_id),
        ).fetchone()
        return row["last_modified"] if row else None

    def upsert_item(
        self, item: HarvestItem, content_hash: str, run_id: int, status: str
    ) -> None:
        last_modified = (item.raw or {}).get("last_modified")
        item_json = json.dumps(asdict(item), default=str)
        if item.per_user:
            self.conn.execute(
                """INSERT INTO enrollment_items(institution,id,org_unit_id,type,content_hash,last_seen_run,item_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(institution,id) DO UPDATE SET
                     content_hash=excluded.content_hash,
                     last_seen_run=excluded.last_seen_run, item_json=excluded.item_json""",
                (self.institution, item.id, item.org_unit_id, item.type,
                 content_hash, run_id, item_json),
            )
            return
        self.conn.execute(
            """INSERT INTO items(institution,id,org_unit_id,type,title,structured_due_date,
                                 source_url,content_hash,content_ref,needs_vision,
                                 last_modified,last_seen_run,status,item_json)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(institution,id) DO UPDATE SET
                 title=excluded.title, structured_due_date=excluded.structured_due_date,
                 source_url=excluded.source_url, content_hash=excluded.content_hash,
                 content_ref=excluded.content_ref, needs_vision=excluded.needs_vision,
                 last_modified=excluded.last_modified, last_seen_run=excluded.last_seen_run,
                 status=excluded.status, item_json=excluded.item_json""",
            (self.institution, item.id, item.org_unit_id, item.type, item.title,
             item.structured_due_date, item.source_url, content_hash, item.content_ref,
             bool(item.needs_vision), last_modified, run_id, status, item_json),
        )

    def mark_removed(self, org_unit_ids: set[int], run_id: int) -> list[str]:
        """Items in the scraped courses not seen this run are 'removed'. Returns their ids.

        NOTE (pooling, Stage C): this trusts the run to be a complete view of its courses.
        Safe for self-host (single user sees the whole course); under multi-user pooling a
        partial scrape must not evict another user's items — to be made conservative there.
        """
        if not org_unit_ids:
            return []
        rows = self.conn.execute(
            """SELECT id FROM items
               WHERE institution=%s AND org_unit_id = ANY(%s)
                 AND (last_seen_run IS NULL OR last_seen_run < %s)
                 AND status != 'removed'""",
            (self.institution, list(org_unit_ids), run_id),
        ).fetchall()
        removed = [r["id"] for r in rows]
        if removed:
            self.conn.execute(
                "UPDATE items SET status='removed' WHERE institution=%s AND id = ANY(%s)",
                (self.institution, removed),
            )
        return removed

    # -- interpretation support -------------------------------------------
    def all_course_ids(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT org_unit_id FROM courses WHERE institution=%s", (self.institution,)
        ).fetchall()
        return [r["org_unit_id"] for r in rows]

    def course_name(self, org_unit_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT name FROM courses WHERE institution=%s AND org_unit_id=%s",
            (self.institution, org_unit_id),
        ).fetchone()
        return row["name"] if row else None

    def get_items_for_courses(self, org_unit_ids: list[int]) -> dict[int, list[dict]]:
        """Return current (non-removed) items grouped by course, as plain dicts."""
        out: dict[int, list[dict]] = {oid: [] for oid in org_unit_ids}
        if not org_unit_ids:
            return out
        rows = self.conn.execute(
            """SELECT org_unit_id, item_json FROM items
               WHERE institution=%s AND org_unit_id = ANY(%s)
                 AND (status IS NULL OR status != 'removed')""",
            (self.institution, list(org_unit_ids)),
        ).fetchall()
        for r in rows:
            out[r["org_unit_id"]].append(json.loads(r["item_json"]))
        return out

    def save_deadlines(self, deadlines: list[dict], run_id: int) -> None:
        """Replace deadlines for the affected courses with the fresh set."""
        affected = {d["org_unit_id"] for d in deadlines}
        for oid in affected:
            self.conn.execute(
                "DELETE FROM deadlines WHERE institution=%s AND org_unit_id=%s",
                (self.institution, oid),
            )
        for d in deadlines:
            self.conn.execute(
                """INSERT INTO deadlines(institution,org_unit_id,item_id,title,type,
                       final_due_date,structured_due_date,confidence,source_url,reasoning,
                       run_id,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())""",
                (self.institution, d.get("org_unit_id"), d.get("item_id"), d.get("title"),
                 d.get("type"), d.get("final_due_date"), d.get("structured_due_date"),
                 d.get("confidence"), d.get("source_url"), d.get("reasoning"), run_id),
            )
        self.conn.commit()

    # -- reporting / calendar reads ---------------------------------------
    def deadlines_for_institution(self) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM deadlines WHERE institution=%s", (self.institution,)
        ).fetchall()

    def courses_for_institution(self) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM courses WHERE institution=%s", (self.institution,)
        ).fetchall()

    # -- accounts (hosted tier) -------------------------------------------
    def upsert_user(
        self, google_sub: str, email: str | None,
        calendar_refresh_token: str | None = None, calendar_id: str | None = None,
    ) -> int:
        """Create/update a user by Google subject id. The refresh token + calendar id are
        only overwritten when a fresh value is supplied (Google omits the refresh token on
        repeat consents), so we never clobber a stored token with NULL."""
        row = self.conn.execute(
            """INSERT INTO users(google_sub,email,calendar_refresh_token,calendar_id)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT(google_sub) DO UPDATE SET
                 email=excluded.email,
                 calendar_refresh_token=COALESCE(excluded.calendar_refresh_token,
                                                 users.calendar_refresh_token),
                 calendar_id=COALESCE(excluded.calendar_id, users.calendar_id)
               RETURNING id""",
            (google_sub, email, calendar_refresh_token, calendar_id),
        ).fetchone()
        self.conn.commit()
        return int(row["id"])

    def get_user(self, user_id: int) -> dict | None:
        return self.conn.execute(
            "SELECT * FROM users WHERE id=%s", (user_id,)
        ).fetchone()

    def set_user_calendar_id(self, user_id: int, calendar_id: str) -> None:
        self.conn.execute(
            "UPDATE users SET calendar_id=%s WHERE id=%s", (calendar_id, user_id)
        )
        self.conn.commit()

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "INSERT INTO sessions(token,user_id) VALUES (%s,%s)", (token, user_id)
        )
        self.conn.commit()
        return token

    def user_for_session(self, token: str) -> dict | None:
        return self.conn.execute(
            """SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id
               WHERE s.token=%s""",
            (token,),
        ).fetchone()

    def set_enrollment(self, user_id: int, org_unit_ids: list[int], run_id: int) -> None:
        """Record which sections this user is in (discovered from their harvest)."""
        for oid in org_unit_ids:
            self.conn.execute(
                """INSERT INTO enrollments(user_id,institution,org_unit_id,last_seen_run)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT(user_id,institution,org_unit_id)
                   DO UPDATE SET last_seen_run=excluded.last_seen_run""",
                (user_id, self.institution, oid, run_id),
            )
        self.conn.commit()

    def users_enrolled_in(self, org_unit_ids: list[int]) -> list[int]:
        if not org_unit_ids:
            return []
        rows = self.conn.execute(
            """SELECT DISTINCT user_id FROM enrollments
               WHERE institution=%s AND org_unit_id = ANY(%s)""",
            (self.institution, list(org_unit_ids)),
        ).fetchall()
        return [r["user_id"] for r in rows]

    def deadlines_for_user(self, user_id: int) -> list[dict]:
        return self.conn.execute(
            """SELECT d.* FROM deadlines d
               JOIN enrollments e
                 ON e.institution=d.institution AND e.org_unit_id=d.org_unit_id
               WHERE e.user_id=%s AND d.institution=%s""",
            (user_id, self.institution),
        ).fetchall()
