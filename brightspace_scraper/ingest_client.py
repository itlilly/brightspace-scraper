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
    token: str | None = None,
    full: bool = False,
    timeout: float = 900.0,
) -> dict:
    """POST the harvest to `<base_url>/ingest` and return the backend's JSON summary.

    `token` is the backend session bearer token (from the Google sign-in flow); the hosted
    backend requires it. `timeout` is generous because the backend runs the LLM interpret
    pass synchronously.
    """
    payload = {
        "full": full,
        "courses": [asdict(c) for c in courses],
        "items": [asdict(i) for i in items],
    }
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = httpx.post(base_url.rstrip("/") + "/ingest", json=payload,
                      headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
