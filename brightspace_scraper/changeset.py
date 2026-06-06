"""Change detection: hash each item, diff against the store, emit the delta.

The delta (new + changed items, plus removed ids) is exactly what the future AI stage
consumes — so unchanged content is never re-sent downstream.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import Course, HarvestItem
from .store import Store
from .util import now_iso


def _file_hash(path_str: str | None) -> str | None:
    if not path_str:
        return None
    p = Path(path_str)
    if not p.exists():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash(item: HarvestItem) -> str:
    """SHA-256 over the fields that matter for interpretation.

    Files with no extractable text (needs_vision) are hashed by their bytes, so a
    re-uploaded identical image/scan is a no-op rather than a spurious change.
    """
    payload = {
        "title": item.title,
        "due": item.structured_due_date,
        "body": item.body_text,
        "extracted": item.extracted_text,
        "source": item.source_url,
        "needs_vision": item.needs_vision,
    }
    if item.needs_vision and not item.extracted_text:
        payload["file"] = _file_hash(item.content_ref)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class Changeset:
    run_id: int
    generated_at: str
    new: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged_count: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    @property
    def summary(self) -> str:
        return (
            f"run {self.run_id}: {len(self.new)} new, {len(self.changed)} changed, "
            f"{len(self.removed)} removed, {self.unchanged_count} unchanged"
        )


def process(
    store: Store,
    courses: list[Course],
    items: list[HarvestItem],
    run_id: int,
    *,
    full: bool = False,
    prune: bool = True,
) -> Changeset:
    """Record courses + items, classify each item, and build the delta.

    full=True re-emits every item (ignores prior hashes) for a from-scratch export.
    prune=False skips removal — required under multi-user pooling, where one user's
    partial scrape must not evict items another user contributed (see store.mark_removed).
    """
    cs = Changeset(
        run_id=run_id,
        generated_at=now_iso(),
    )

    for course in courses:
        store.upsert_course(course, run_id)

    for item in items:
        h = content_hash(item)
        prior = None if full else store.get_hash(item.id, per_user=item.per_user)
        if prior is None:
            status = "new"
        elif prior != h:
            status = "changed"
        else:
            status = "unchanged"

        store.upsert_item(item, h, run_id, status)

        if status == "new":
            cs.new.append(asdict(item))
        elif status == "changed":
            cs.changed.append(asdict(item))
        else:
            cs.unchanged_count += 1

    if prune:
        scraped_ous = {c.org_unit_id for c in courses}
        cs.removed = store.mark_removed(scraped_ous, run_id)
    store.commit()
    return cs
