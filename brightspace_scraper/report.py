"""Human-readable deadline report from the store's `deadlines` table.

    python -m brightspace_scraper.report            # print + write deadlines.md
    python -m brightspace_scraper.report --all      # include past deadlines too
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

from .config import load_config
from .util import parse_iso as _parse


def _fmt(dt: _dt.datetime | None) -> str:
    # Stored dates are UTC; show them in local time (matches the "generated" line).
    return dt.astimezone().strftime("%a %b %d, %Y  %H:%M") if dt else "—"


def build_report(db_path, *, include_past: bool = False) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    courses = {r["org_unit_id"]: r for r in conn.execute("SELECT * FROM courses")}
    rows = conn.execute("SELECT * FROM deadlines").fetchall()
    conn.close()

    now = _dt.datetime.now(_dt.timezone.utc)

    def course_label(oid):
        c = courses.get(oid)
        if not c:
            return str(oid)
        return (c["name"] or c["code"] or str(oid))

    dated, undated = [], []
    for r in rows:
        dt = _parse(r["final_due_date"])
        (dated if dt else undated).append((dt, r))
    dated.sort(key=lambda x: x[0])

    upcoming = [(dt, r) for dt, r in dated if dt >= now]
    past = [(dt, r) for dt, r in dated if dt < now]

    out: list[str] = []
    out.append("# 📅 Upcoming deadlines")
    out.append(f"_generated {now.astimezone().strftime('%a %b %d, %Y %H:%M')}_\n")

    if not upcoming:
        out.append("_No upcoming deadlines with a date._\n")
    for dt, r in upcoming:
        days = (dt - now).days
        soon = "  ⚠️" if days <= 7 else ""
        conf = "" if r["confidence"] == "high" else f"  ·  _{r['confidence']} confidence_"
        out.append(f"- **{_fmt(dt)}**  ({days}d){soon}  —  {r['title']}")
        out.append(f"    {course_label(r['org_unit_id'])}{conf}")
        if r["confidence"] != "high":
            out.append(f"    ↳ {r['reasoning']}")
        if r["source_url"]:
            out.append(f"    🔗 {r['source_url']}")

    if undated:
        out.append("\n## ❓ Mentioned but no firm date (check these)")
        for _, r in undated:
            out.append(f"- {r['title']}  ·  {course_label(r['org_unit_id'])}")
            out.append(f"    ↳ {r['reasoning']}")

    if include_past and past:
        out.append("\n## ✓ Past deadlines")
        for dt, r in past:
            out.append(f"- {_fmt(dt)}  —  {r['title']}  ·  {course_label(r['org_unit_id'])}")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="brightspace_scraper.report")
    p.add_argument("--all", action="store_true", help="include past deadlines")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    cfg = load_config()
    report = build_report(cfg.db_path, include_past=args.all)
    out_path = cfg.data_dir / "deadlines.md"
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[written to {out_path}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
