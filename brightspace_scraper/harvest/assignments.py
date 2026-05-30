"""Harvest assignment (dropbox) folders for a course."""

from __future__ import annotations

from ..client import BrightspaceClient
from ..models import ASSIGNMENT, HarvestItem
from ..util import html_to_text, item_id


def harvest_assignments(client: BrightspaceClient, org_unit_id: int) -> list[HarvestItem]:
    folders = client.get_json(f"/d2l/api/le/{client.le}/{org_unit_id}/dropbox/folders/")
    items: list[HarvestItem] = []
    for f in folders or []:
        fid = f.get("Id")
        if fid is None:
            continue
        avail = f.get("Availability") or {}
        due = f.get("DueDate") or avail.get("EndDate")
        # Instruction attachments (e.g. a PDF spec) get downloaded in the content stage;
        # we record their presence so nothing is silently dropped.
        attachments = (f.get("Attachments") or []) + (f.get("LinkAttachments") or [])
        items.append(
            HarvestItem(
                id=item_id(ASSIGNMENT, org_unit_id, fid),
                org_unit_id=org_unit_id,
                type=ASSIGNMENT,
                title=f.get("Name") or f"Assignment {fid}",
                structured_due_date=due,
                body_text=html_to_text(f.get("CustomInstructions")),
                source_url=(
                    f"{client.base}/d2l/lms/dropbox/user/folder_submit_files.d2l"
                    f"?db={fid}&ou={org_unit_id}"
                ),
                raw={"folder": f, "attachment_count": len(attachments)},
            )
        )
    return items
