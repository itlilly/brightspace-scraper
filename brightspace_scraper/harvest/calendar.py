"""Harvest calendar events for a course.

The myEvents endpoint caps results (≈100), so we sweep the date range in windows and
dedupe by CalendarEventId rather than risk silently truncating a busy calendar.
"""

from __future__ import annotations

import datetime as _dt

from ..client import BrightspaceClient
from ..models import CALENDAR_EVENT, HarvestItem
from ..util import html_to_text, item_id

_WINDOW_DAYS = 28


def _iso(d: _dt.datetime) -> str:
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def harvest_calendar(
    client: BrightspaceClient,
    org_unit_id: int,
    *,
    days_back: int = 60,
    days_forward: int = 240,
    now: _dt.datetime | None = None,
) -> list[HarvestItem]:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    start = now - _dt.timedelta(days=days_back)
    end = now + _dt.timedelta(days=days_forward)

    seen: dict[int, dict] = {}
    cursor = start
    while cursor < end:
        window_end = min(cursor + _dt.timedelta(days=_WINDOW_DAYS), end)
        events = client.get_json(
            f"/d2l/api/le/{client.le}/{org_unit_id}/calendar/events/myEvents/",
            params={"startDateTime": _iso(cursor), "endDateTime": _iso(window_end)},
        )
        if isinstance(events, dict):  # some courses return a paged wrapper, not a list
            events = events.get("Objects") or events.get("Items") or []
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            eid = ev.get("CalendarEventId")
            if eid is not None:
                seen[eid] = ev
        cursor = window_end

    items: list[HarvestItem] = []
    for eid, ev in sorted(seen.items()):
        assoc = ev.get("AssociatedEntity") or {}
        items.append(
            HarvestItem(
                id=item_id(CALENDAR_EVENT, org_unit_id, eid),
                org_unit_id=org_unit_id,
                type=CALENDAR_EVENT,
                title=ev.get("Title") or f"Event {eid}",
                # The event's end is the most due-date-like field it carries.
                structured_due_date=ev.get("EndDateTime") or ev.get("StartDateTime"),
                body_text=html_to_text(ev.get("Description")),
                source_url=ev.get("CalendarEventViewUrl") or assoc.get("Link"),
                raw={"event": ev},
            )
        )
    return items
