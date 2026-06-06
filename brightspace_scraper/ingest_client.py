"""Client half of the ingestion seam: ship a harvest to the backend.

The CLI harvests locally (MUN auth stays on the client), then POSTs the normalized
courses + items to the backend, which runs interpretation and persists deadlines. The
wire format is just `asdict()` of the dataclasses — the backend rebuilds them.
"""

from __future__ import annotations

from dataclasses import asdict

import httpx

from .models import Course, HarvestItem


def push_harvest(
    base_url: str,
    courses: list[Course],
    items: list[HarvestItem],
    *,
    full: bool = False,
    timeout: float = 300.0,
) -> dict:
    """POST the harvest to `<base_url>/ingest` and return the backend's JSON summary.

    `timeout` is generous because the backend runs the LLM interpret pass synchronously.
    """
    payload = {
        "full": full,
        "courses": [asdict(c) for c in courses],
        "items": [asdict(i) for i in items],
    }
    resp = httpx.post(base_url.rstrip("/") + "/ingest", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
