"""Backend ingestion seam (Stage 2, server-side).

Step 1 of the client/server split: the CLI harvests locally (MUN auth stays on the
client) and POSTs the normalized courses + items here; this service runs the *existing*
change-detection + interpret pipeline and persists deadlines. No accounts, no pooling,
no calendar sync yet — those are later build-order steps.

The wire format is `asdict()` of the `Course` / `HarvestItem` dataclasses; we rebuild them
and hand them straight to `changeset.process()` and `interpret.interpret_courses()`, so no
pipeline logic is duplicated here.

Run:  python -m brightspace_scraper.backend [--host H --port P]
  or: uvicorn brightspace_scraper.backend:app
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

from . import changeset, interpret
from .models import Course, HarvestItem
from .store import Store

load_dotenv()  # pick up LLM_BACKEND / LOCAL_LLM_* / BACKEND_DATA_DIR from .env


def _db_path() -> Path:
    """Server-side store, kept separate from the client's DATA_DIR so they never clash."""
    d = Path(
        os.environ.get("BACKEND_DATA_DIR", "~/.local/share/brightspace-backend")
    ).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d / "brightspace.sqlite"


def _open_store() -> Store:
    # Fresh connection per request: SQLite connections can't be shared across the
    # threadpool threads FastAPI runs sync endpoints in.
    return Store(_db_path())


app = FastAPI(title="Brightspace ingestion backend", version="0.1.0")


class IngestRequest(BaseModel):
    courses: list[dict] = []
    items: list[dict] = []
    full: bool = False


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/ingest")
def ingest(req: IngestRequest) -> dict:
    """Receive a harvest, run change detection + interpret on the affected courses, and
    persist the resulting deadlines. Sync `def` on purpose: the LLM call is blocking, so
    FastAPI runs this in its threadpool rather than stalling the event loop."""
    courses = [Course(**c) for c in req.courses]
    items = [HarvestItem(**i) for i in req.items]

    store = _open_store()
    try:
        run_id = store.start_run()
        cs = changeset.process(store, courses, items, run_id, full=req.full)
        # Only re-interpret courses with new/changed items — the change-detection seam.
        affected = sorted({it["org_unit_id"] for it in cs.new + cs.changed})

        interp = interpret.build_interpreter()
        deadlines = interpret.interpret_courses(store, affected, interp)
        store.save_deadlines(interpret.deadlines_to_dicts(deadlines), run_id)
        store.finish_run(run_id)

        by_conf: dict[str, int] = {}
        for d in deadlines:
            by_conf[d.confidence] = by_conf.get(d.confidence, 0) + 1

        return {
            "changeset": {
                "run_id": cs.run_id,
                "new": len(cs.new),
                "changed": len(cs.changed),
                "removed": len(cs.removed),
                "unchanged": cs.unchanged_count,
            },
            "interpreted_course_ids": affected,
            "deadlines": {"total": len(deadlines), "by_confidence": by_conf},
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
                "SELECT * FROM deadlines WHERE org_unit_id=? ORDER BY final_due_date",
                (org_unit,),
            ).fetchall()
        else:
            rows = store.conn.execute(
                "SELECT * FROM deadlines ORDER BY final_due_date"
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

    print(f"Backend store -> {_db_path()}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
