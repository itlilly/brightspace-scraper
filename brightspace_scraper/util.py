"""Small shared helpers."""

from __future__ import annotations

import datetime as _dt
import re
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs):  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str):
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # Collapse runs of whitespace but keep paragraph breaks.
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*", "\n\n", joined)
        return joined.strip()


def html_to_text(html) -> str | None:
    """Strip HTML to readable plain text. Returns None for empty input.

    Tolerates D2L RichText blocks: a dict like {Html, Text} or nested {Text: {Html}}.
    """
    if html is None:
        return None
    if isinstance(html, dict):  # RichText block -> prefer Html, then Text
        inner = html.get("Html") or html.get("Text")
        if isinstance(inner, dict):
            inner = inner.get("Html") or inner.get("Text")
        html = inner
    if not isinstance(html, str) or not html:
        return None
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html).strip() or None
    return parser.text() or None


def item_id(item_type: str, org_unit_id: int, item_id_value) -> str:
    """Stable item key: '{type}:{org_unit_id}:{item_id}'."""
    return f"{item_type}:{org_unit_id}:{item_id_value}"


def parse_iso(ts: str | None) -> _dt.datetime | None:
    """Parse an ISO-8601 timestamp (tolerating a trailing 'Z'). None on empty/invalid."""
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
