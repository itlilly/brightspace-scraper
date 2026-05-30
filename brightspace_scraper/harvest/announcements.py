"""Harvest announcements (news items) for a course."""

from __future__ import annotations

from ..client import BrightspaceClient
from ..models import ANNOUNCEMENT, HarvestItem
from ..util import html_to_text, item_id


def harvest_announcements(client: BrightspaceClient, org_unit_id: int) -> list[HarvestItem]:
    news = client.get_json(f"/d2l/api/le/{client.le}/{org_unit_id}/news/")
    items: list[HarvestItem] = []
    for n in news or []:
        nid = n.get("Id")
        if nid is None:
            continue
        attachments = n.get("Attachments") or []
        items.append(
            HarvestItem(
                id=item_id(ANNOUNCEMENT, org_unit_id, nid),
                org_unit_id=org_unit_id,
                type=ANNOUNCEMENT,
                title=n.get("Title") or f"Announcement {nid}",
                # Announcements have no due date; StartDate is the publish date.
                structured_due_date=None,
                body_text=html_to_text(n.get("Body")),
                source_url=f"{client.base}/d2l/le/news/{org_unit_id}/{nid}/view",
                raw={
                    "news": n,
                    "last_modified": n.get("LastModifiedDate"),
                    "attachment_count": len(attachments),
                },
            )
        )
    return items
