"""Harvest the content tree: pages, downloaded files, and external links.

Walks the course table of contents, downloads each File topic, and extracts its text
(via extract.py). Link topics point outside Brightspace, so we record the URL and flag
them external rather than pretend to harvest them.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..client import BrightspaceClient
from ..extract import extract_text
from ..models import CONTENT_FILE, CONTENT_PAGE, HarvestItem
from ..util import html_to_text, item_id

_PAGE_EXTS = {".html", ".htm", ".txt", ".md"}


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "file"


def _iter_topics(toc: dict):
    def walk(module):
        for t in module.get("Topics", []) or []:
            yield t
        for sm in module.get("Modules", []) or []:
            yield from walk(sm)

    for m in toc.get("Modules", []) or []:
        yield from walk(m)


def harvest_content(
    client: BrightspaceClient,
    org_unit_id: int,
    content_dir: Path,
) -> list[HarvestItem]:
    toc = client.get_json(f"/d2l/api/le/{client.le}/{org_unit_id}/content/toc")
    if not isinstance(toc, dict):
        return []

    course_dir = content_dir / str(org_unit_id)
    items: list[HarvestItem] = []

    for t in _iter_topics(toc):
        tid = t.get("TopicId")
        if tid is None:
            continue
        title = t.get("Title") or f"Topic {tid}"
        url = t.get("Url") or ""
        view_url = f"{client.base}/d2l/le/content/{org_unit_id}/viewContent/{tid}/View"
        last_modified = t.get("LastModifiedDate")

        # External link: outside Brightspace -> record, don't download (the ceiling).
        if t.get("TypeIdentifier") == "Link":
            items.append(
                HarvestItem(
                    id=item_id(CONTENT_PAGE, org_unit_id, tid),
                    org_unit_id=org_unit_id,
                    type=CONTENT_PAGE,
                    title=title,
                    body_text=html_to_text(t.get("Description")),
                    source_url=url or view_url,
                    raw={"topic": t, "external": True, "last_modified": last_modified},
                )
            )
            continue

        # File topic: download the bytes via the dedicated endpoint.
        try:
            resp = client.get(
                f"/d2l/api/le/{client.le}/{org_unit_id}/content/topics/{tid}/file"
            )
            resp.raise_for_status()
        except Exception as exc:  # broken/hidden/forbidden topic
            items.append(
                HarvestItem(
                    id=item_id(CONTENT_FILE, org_unit_id, tid),
                    org_unit_id=org_unit_id,
                    type=CONTENT_FILE,
                    title=title,
                    source_url=view_url,
                    raw={"topic": t, "download_error": str(exc)[:200],
                         "last_modified": last_modified},
                )
            )
            continue

        ext = Path(url).suffix.lower()
        ctype = resp.headers.get("content-type", "")
        if not ext and "html" in ctype:
            ext = ".html"

        course_dir.mkdir(parents=True, exist_ok=True)
        path = course_dir / f"{tid}_{_safe(Path(url).name or title)}{'' if ext and Path(url).suffix else ext}"
        if path.suffix.lower() != ext and ext:
            path = path.with_suffix(ext)
        path.write_bytes(resp.content)

        extracted = extract_text(path)
        is_page = ext in _PAGE_EXTS
        items.append(
            HarvestItem(
                id=item_id(CONTENT_PAGE if is_page else CONTENT_FILE, org_unit_id, tid),
                org_unit_id=org_unit_id,
                type=CONTENT_PAGE if is_page else CONTENT_FILE,
                title=title,
                body_text=extracted.text if is_page else html_to_text(t.get("Description")),
                extracted_text=None if is_page else extracted.text,
                needs_vision=extracted.needs_vision,
                source_url=view_url,
                content_ref=str(path),
                raw={"topic": t, "content_type": ctype, "bytes": len(resp.content),
                     "last_modified": last_modified},
            )
        )
    return items
