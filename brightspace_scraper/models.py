"""Normalized, course-centric data model (multi-user-ready).

A `Course` is keyed by Brightspace's `org_unit_id`, which is identical for every user
enrolled in the same offering — the seam a future shared-instance backend needs.

Content splits into two tiers:
  * course-level items  -> identical for everyone in the offering (shareable)
  * enrollment items    -> per-user (grades / submission / overrides), never shared
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Item type constants (the `type` field on HarvestItem).
ASSIGNMENT = "assignment"
QUIZ = "quiz"
ANNOUNCEMENT = "announcement"
CALENDAR_EVENT = "calendar_event"
CONTENT_PAGE = "content_page"
CONTENT_FILE = "content_file"


@dataclass
class Course:
    org_unit_id: int
    name: str
    code: str | None = None
    type: str | None = None       # e.g. "Course Offering"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarvestItem:
    """A single harvested artifact, course-level unless `per_user` is True."""

    id: str                       # stable: "{type}:{org_unit_id}:{item_id}"
    org_unit_id: int
    type: str
    title: str
    source_url: str | None = None
    structured_due_date: str | None = None   # ISO-8601, if Brightspace had one
    body_text: str | None = None             # HTML-stripped instructions/body/page
    extracted_text: str | None = None        # text pulled from a downloaded file
    needs_vision: bool = False               # image/scanned file for the future AI
    per_user: bool = False                   # enrollment-tier (private) if True
    content_ref: str | None = None           # path to raw file on disk, if any
    raw: dict[str, Any] = field(default_factory=dict)
