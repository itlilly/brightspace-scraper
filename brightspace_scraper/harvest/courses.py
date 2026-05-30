"""Harvest the user's course list from enrollments.

`myenrollments` returns *everything* the user has ever been enrolled in. Scopes:
  "current" (default) - only the current term's courses (the most recent term code among
                        courses whose enrollment window contains "now")
  "active"            - all currently-accessible courses (IsActive + date window): includes
                        evergreen resources and stale open-ended enrollments
  "all"               - every enrollment ever
"""

from __future__ import annotations

import datetime as _dt
import re

from ..client import BrightspaceClient
from ..models import Course
from ..util import parse_iso as _parse

# Brightspace OrgUnit type id 3 == "Course Offering".
_COURSE_OFFERING_TYPE_ID = 3
# MUN course codes end with a 6-digit term, e.g. "80219.202503" or "chem1050--cl.202503".
_TERM_RE = re.compile(r"\.(\d{6})$")


def _is_accessible_now(access: dict, now: _dt.datetime) -> bool:
    if not access.get("IsActive"):
        return False
    start = _parse(access.get("StartDate"))
    end = _parse(access.get("EndDate"))
    return (start is None or start <= now) and (end is None or end >= now)


def _term_code(code: str | None) -> str | None:
    if not code:
        return None
    m = _TERM_RE.search(code)
    return m.group(1) if m else None


def harvest_courses(
    client: BrightspaceClient,
    *,
    scope: str = "current",
    only_ids: set[int] | None = None,
    now: _dt.datetime | None = None,
) -> list[Course]:
    """Return course offerings the user is enrolled in.

    scope: "current" (current term, default) | "active" | "all".
    only_ids: if given, restrict to these org_unit_ids (overrides scope).
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    entities = client.get_paged(
        f"/d2l/api/lp/{client.lp}/enrollments/myenrollments/"
    )

    parsed: list[tuple[int, dict, dict, dict]] = []  # (oid, org, otype, ent)
    for ent in entities:
        org = ent.get("OrgUnit") or {}
        otype = org.get("Type") or {}
        if otype.get("Id") != _COURSE_OFFERING_TYPE_ID:
            continue
        oid = org.get("Id")
        if oid is not None:
            parsed.append((int(oid), org, otype, ent))

    # Determine the current term = most recent term code among accessible-now courses
    # whose enrollment has actually started (so a pre-registered future term doesn't win).
    current_term: str | None = None
    if scope == "current" and only_ids is None:
        candidates = []
        for oid, org, _otype, ent in parsed:
            acc = ent.get("Access") or {}
            if not _is_accessible_now(acc, now):
                continue
            start = _parse(acc.get("StartDate"))
            if start is None:  # evergreen resources have no real term
                continue
            term = _term_code(org.get("Code"))
            if term:
                candidates.append(term)
        current_term = max(candidates) if candidates else None

    courses: list[Course] = []
    for oid, org, otype, ent in parsed:
        if only_ids is not None:
            if oid not in only_ids:
                continue
        elif scope == "current":
            # Fall back to "active" if no term could be determined.
            if current_term is not None:
                if _term_code(org.get("Code")) != current_term:
                    continue
            elif not _is_accessible_now(ent.get("Access") or {}, now):
                continue
        elif scope == "active":
            if not _is_accessible_now(ent.get("Access") or {}, now):
                continue
        # scope == "all": no filtering

        courses.append(
            Course(
                org_unit_id=oid,
                name=org.get("Name") or f"Course {oid}",
                code=org.get("Code"),
                type=otype.get("Name") or "Course Offering",
                raw=ent,
            )
        )
    courses.sort(key=lambda c: c.org_unit_id)
    return courses
