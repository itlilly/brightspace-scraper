"""Orchestrate a full harvest run: log in, scrape everything, emit the changeset.

Usage:
    python -m brightspace_scraper.cli                 # active courses, incremental
    python -m brightspace_scraper.cli --full          # re-emit everything as new
    python -m brightspace_scraper.cli --courses 671225,671215
    python -m brightspace_scraper.cli --all-courses   # every enrollment, not just active
    python -m brightspace_scraper.cli --no-content    # skip file downloads (fast)
"""

from __future__ import annotations

import argparse
import sys

from .auth import login
from .changeset import process
from .client import BrightspaceClient
from .config import ConfigError, load_config
from .harvest.announcements import harvest_announcements
from .harvest.assignments import harvest_assignments
from .harvest.calendar import harvest_calendar
from .harvest.content import harvest_content
from .harvest.courses import harvest_courses
from .harvest.quizzes import harvest_quizzes
from .models import HarvestItem
from .store import Store


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="brightspace_scraper")
    p.add_argument("--full", action="store_true", help="re-emit all items as new")
    p.add_argument("--active", action="store_true",
                   help="all currently-accessible courses (incl. evergreen/stale), not just current term")
    p.add_argument("--all-courses", action="store_true", help="every enrollment ever")
    p.add_argument("--courses", help="comma-separated org_unit_ids to restrict to")
    p.add_argument("--no-content", action="store_true", help="skip content downloads")
    p.add_argument("--days-back", type=int, default=60)
    p.add_argument("--days-forward", type=int, default=240)
    return p.parse_args(argv)


def _harvest_course(bs, ou, args, content_dir) -> list[HarvestItem]:
    """Harvest one course; per-type failures are logged and skipped, not fatal."""
    items: list[HarvestItem] = []
    steps = [
        ("assignments", lambda: harvest_assignments(bs, ou)),
        ("quizzes", lambda: harvest_quizzes(bs, ou)),
        ("announcements", lambda: harvest_announcements(bs, ou)),
        ("calendar", lambda: harvest_calendar(
            bs, ou, days_back=args.days_back, days_forward=args.days_forward)),
    ]
    if not args.no_content:
        steps.append(("content", lambda: harvest_content(bs, ou, content_dir)))

    for name, fn in steps:
        try:
            items.extend(fn())
        except Exception as exc:  # keep going; one bad endpoint shouldn't sink the run
            print(f"    ! {name} failed for ou={ou}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
    return items


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    http = login(cfg)
    bs = BrightspaceClient(http, cfg)
    print(f"Logged in (creds from {cfg.cred_source}); API LP {bs.lp} / LE {bs.le}")

    only_ids = None
    if args.courses:
        only_ids = {int(x) for x in args.courses.split(",") if x.strip()}
    scope = "all" if args.all_courses else ("active" if args.active else "current")
    courses = harvest_courses(bs, scope=scope, only_ids=only_ids)
    print(f"Harvesting {len(courses)} course(s) [scope={scope}]...")

    all_items: list[HarvestItem] = []
    for c in courses:
        items = _harvest_course(bs, c.org_unit_id, args, cfg.content_dir)
        all_items.extend(items)
        print(f"  [{c.org_unit_id}] {c.code or c.name[:30]:30} -> {len(items)} items")

    store = Store(cfg.db_path)
    run_id = store.start_run()
    cs = process(store, courses, all_items, run_id, full=args.full)
    store.finish_run(run_id)
    store.close()

    out_path = cfg.data_dir / "changeset.json"
    out_path.write_text(cs.to_json(), encoding="utf-8")

    print(f"\n{cs.summary}")
    print(f"changeset -> {out_path}")
    print(f"store     -> {cfg.db_path}")
    print(f"content   -> {cfg.content_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
