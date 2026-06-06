"""One-shot pipeline: harvest -> interpret -> report -> calendar sync.

The four stages each expose `main(argv) -> int`; this just sequences them for the
common single-user flow and stops on the first failure. Stage-specific behaviour
still lives in each module — this only routes flags to the right stage.

    python -m brightspace_scraper.run                 # full run, then push to calendar
    python -m brightspace_scraper.run --dry-run       # everything, but preview the calendar
    python -m brightspace_scraper.run --no-sync       # stop after deadlines.md
    python -m brightspace_scraper.run --skip-harvest  # re-interpret + report + sync only
"""

from __future__ import annotations

import argparse
import sys

from . import calendar_sync, cli, interpret, report


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="brightspace_scraper.run",
        description="Run the full pipeline: harvest -> interpret -> report -> calendar sync.",
    )
    # Stage 1 (harvest) scope — mirrors cli.py.
    p.add_argument("--active", action="store_true",
                   help="harvest all accessible courses, not just the current term")
    p.add_argument("--all-courses", action="store_true", help="harvest every enrollment ever")
    p.add_argument("--courses", help="comma-separated org_unit_ids to restrict to")
    p.add_argument("--no-content", action="store_true", help="skip content downloads")
    p.add_argument("--full", action="store_true", help="re-emit all items as new")
    # Report + sync scope.
    p.add_argument("--all", action="store_true",
                   help="include past deadlines in the report and calendar sync")
    # Stage 4 (calendar) controls.
    p.add_argument("--dry-run", action="store_true",
                   help="preview the calendar sync; make no Google API calls")
    p.add_argument("--no-prune", action="store_true",
                   help="never delete calendar events during sync")
    # Skip toggles.
    p.add_argument("--skip-harvest", action="store_true",
                   help="skip Stage 1; interpret what's already in the store")
    p.add_argument("--no-sync", action="store_true",
                   help="stop after writing deadlines.md; don't touch the calendar")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # Build each stage's argv from the shared flags.
    harvest_argv: list[str] = []
    if args.active:
        harvest_argv.append("--active")
    if args.all_courses:
        harvest_argv.append("--all-courses")
    if args.courses:
        harvest_argv += ["--courses", args.courses]
    if args.no_content:
        harvest_argv.append("--no-content")
    if args.full:
        harvest_argv.append("--full")

    interpret_argv = ["--courses", args.courses] if args.courses else ["--all"]
    report_argv = ["--all"] if args.all else []

    sync_argv = ["sync"]
    if args.dry_run:
        sync_argv.append("--dry-run")
    if args.all:
        sync_argv.append("--all")
    if args.no_prune:
        sync_argv.append("--no-prune")

    # (label, callable, argv) — backend for interpret comes from $LLM_BACKEND/.env.
    stages: list[tuple[str, object, list[str]]] = []
    if not args.skip_harvest:
        stages.append(("harvest", cli.main, harvest_argv))
    stages.append(("interpret", interpret.main, interpret_argv))
    stages.append(("report", report.main, report_argv))
    if not args.no_sync:
        stages.append(("calendar sync", calendar_sync.main, sync_argv))

    for i, (label, fn, stage_argv) in enumerate(stages, 1):
        print(f"\n=== [{i}/{len(stages)}] {label} "
              f"{'(' + ' '.join(stage_argv) + ')' if stage_argv else ''} ===",
              flush=True)
        try:
            rc = fn(stage_argv)
        except Exception as exc:  # surface which stage broke; don't run the rest
            print(f"\n! pipeline aborted: {label} raised "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if rc != 0:
            print(f"\n! pipeline aborted: {label} exited {rc}", file=sys.stderr)
            return rc

    print("\n✓ pipeline complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
