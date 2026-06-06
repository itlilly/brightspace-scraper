"""Hosted backend: authenticated ingestion + pooling (Stage 2, server-side).

The CLI/extension harvests locally (MUN auth stays on the client) and POSTs normalized
courses + items here; this service authenticates the user (Google OAuth), pools course
content keyed by `org_unit_id`, runs the *existing* change-detection + interpret pipeline,
and persists deadlines. The wire format is `asdict()` of the `Course` / `HarvestItem`
dataclasses; we rebuild them and hand them to `changeset.process()` /
`interpret.interpret_courses()`, so no pipeline logic is duplicated here.

Run:  python -m brightspace_scraper.backend [--host H --port P]
  or: uvicorn brightspace_scraper.backend:app
"""

from __future__ import annotations

import os
import secrets

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from . import accounts, calendar_sync, changeset, interpret
from .accounts import AuthError
from .config import load_config
from .models import Course, HarvestItem
from .store import Store

load_dotenv()  # pick up DATABASE_URL / GOOGLE_WEB_* / TOKEN_ENCRYPTION_KEY / LLM_* from .env

# Backend doesn't hold MUN creds — those stay client-side.
_cfg = load_config(require_credentials=False)

# OAuth CSRF state. In-memory is fine for a single instance; a shared store is a follow-up
# when the backend scales horizontally.
_pending_states: set[str] = set()


def _open_store() -> Store:
    # Fresh connection per request: a connection can't be shared across the threadpool
    # threads FastAPI runs sync endpoints in.
    return Store(_cfg.database_url, _cfg.institution)


def _bg_sync_user(user_id: int) -> None:
    """Background fan-out worker: sync one enrolled user's calendar from pooled deadlines.
    Runs after the response; opens its own store (the request's is closed). A real job
    queue replaces this later."""
    store = _open_store()
    try:
        user = store.get_user(user_id)
        if user:
            calendar_sync.sync_user(_cfg, store, user)
    except Exception as exc:
        print(f"  ! calendar fan-out failed for user {user_id}: "
              f"{type(exc).__name__}: {exc}")
    finally:
        store.close()


def current_user(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency: resolve the `Authorization: Bearer <session>` header to a user."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    store = _open_store()
    try:
        user = store.user_for_session(token)
    finally:
        store.close()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


app = FastAPI(title="Brightspace backend", version="0.2.0")


class IngestRequest(BaseModel):
    courses: list[dict] = []
    items: list[dict] = []
    full: bool = False


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- auth
@app.get("/auth/login")
def auth_login():
    """Redirect the user to Google consent (identity + Calendar grant in one flow)."""
    state = secrets.token_urlsafe(24)
    _pending_states.add(state)
    try:
        return RedirectResponse(accounts.consent_url(_cfg, state))
    except AuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/auth/callback")
def auth_callback(code: str | None = None, state: str | None = None,
                  error: str | None = None) -> dict:
    """Google redirects here with a code; exchange it, upsert the user, issue a session."""
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")
    if state not in _pending_states:
        raise HTTPException(status_code=400, detail="Unknown or expired state")
    _pending_states.discard(state)
    try:
        tokens = accounts.exchange_code(_cfg, code)
        identity = accounts.fetch_identity(tokens["access_token"])
        refresh = tokens.get("refresh_token")
        enc = accounts.encrypt_token(_cfg, refresh) if refresh else None
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    store = _open_store()
    try:
        uid = store.upsert_user(identity["sub"], identity.get("email"), enc)
        session_token = store.create_session(uid)
    finally:
        store.close()
    # Dev: return the token as JSON. The extension flow will instead redirect back to the
    # client with this token.
    return {"session_token": session_token, "user_id": uid,
            "email": identity.get("email"),
            "got_refresh_token": bool(refresh)}


@app.get("/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"id": user["id"], "email": user["email"]}


# --------------------------------------------------------------------------- ingest
@app.post("/ingest")
def ingest(req: IngestRequest, background_tasks: BackgroundTasks,
           user: dict = Depends(current_user)) -> dict:
    """Receive a harvest, run change detection + interpret on the affected courses, and
    persist the resulting deadlines. Sync `def` on purpose: the LLM call is blocking, so
    FastAPI runs this in its threadpool rather than stalling the event loop."""
    courses = [Course(**c) for c in req.courses]
    items = [HarvestItem(**i) for i in req.items]

    store = _open_store()
    try:
        run_id = store.start_run(user_id=user["id"])

        # Pooling: content is shared (keyed by org_unit_id); prune=False so this user's
        # (possibly partial) scrape never evicts items another enrolled user contributed.
        cs = changeset.process(store, courses, items, run_id, full=req.full, prune=False)

        # Per-user: record which sections this user is in (discovered from their harvest).
        enrolled = sorted({c.org_unit_id for c in courses})
        store.set_enrollment(user["id"], enrolled, run_id)

        # Interpret-once: only courses with new/changed content reach the model, regardless
        # of how many users submit them (the shared `items` table makes repeats `unchanged`).
        affected = sorted({it["org_unit_id"] for it in cs.new + cs.changed})
        interp = interpret.build_interpreter()
        deadlines = interpret.interpret_courses(store, affected, interp)
        store.save_deadlines(interpret.deadlines_to_dicts(deadlines), run_id)
        store.finish_run(run_id)

        by_conf: dict[str, int] = {}
        for d in deadlines:
            by_conf[d.confidence] = by_conf.get(d.confidence, 0) + 1

        # Fan-out: when a section's deadlines changed, re-sync the calendars of EVERY user
        # enrolled in it — that's the pooling payoff (a classmate's scrape updates everyone).
        fanout_users = store.users_enrolled_in(affected) if affected else []
        scheduled = bool(fanout_users and _cfg.google_web_client_id)
        if scheduled:
            for uid in fanout_users:
                background_tasks.add_task(_bg_sync_user, uid)

        return {
            "changeset": {
                "run_id": cs.run_id,
                "new": len(cs.new),
                "changed": len(cs.changed),
                "unchanged": cs.unchanged_count,
            },
            "enrolled_course_ids": enrolled,
            "interpreted_course_ids": affected,
            "deadlines": {"total": len(deadlines), "by_confidence": by_conf},
            "fanout_users": fanout_users,
            "fanout_scheduled": scheduled,
        }
    finally:
        store.close()


@app.get("/deadlines")
def deadlines(org_unit: int | None = None) -> dict:
    """Return persisted deadlines (optionally for one course) — for verification."""
    store = _open_store()
    try:
        if org_unit is not None:
            rows = store.conn.execute(
                "SELECT * FROM deadlines WHERE institution=%s AND org_unit_id=%s "
                "ORDER BY final_due_date",
                (_cfg.institution, org_unit),
            ).fetchall()
        else:
            rows = store.conn.execute(
                "SELECT * FROM deadlines WHERE institution=%s ORDER BY final_due_date",
                (_cfg.institution,),
            ).fetchall()
        return {"count": len(rows), "deadlines": [dict(r) for r in rows]}
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    import uvicorn

    p = argparse.ArgumentParser(prog="brightspace_scraper.backend")
    p.add_argument("--host", default=os.environ.get("BACKEND_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("BACKEND_PORT", "8000")))
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    print(f"Backend store -> {_cfg.database_url} (institution={_cfg.institution})")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
