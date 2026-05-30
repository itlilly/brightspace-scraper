"""Authenticated Brightspace API client (API-first, HTML fallback).

Wraps the logged-in httpx.Client. Discovers the supported LP/LE API versions from
`/d2l/api/versions/` so we don't hardcode a version that MUN may not run.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Config


class APIError(RuntimeError):
    pass


class BrightspaceClient:
    def __init__(self, http: httpx.Client, config: Config):
        self.http = http
        self.config = config
        self.base = config.base_url
        self._lp_version: str | None = None
        self._le_version: str | None = None

    # -- version discovery -------------------------------------------------
    def _discover_versions(self) -> None:
        resp = self.http.get(f"{self.base}/d2l/api/versions/")
        resp.raise_for_status()
        products = {p["ProductCode"]: p["LatestVersion"] for p in resp.json()}
        self._lp_version = products.get("lp")
        self._le_version = products.get("le")
        if not self._lp_version or not self._le_version:
            raise APIError(f"Could not determine LP/LE API versions from {products!r}")

    @property
    def lp(self) -> str:
        if self._lp_version is None:
            self._discover_versions()
        return self._lp_version  # type: ignore[return-value]

    @property
    def le(self) -> str:
        if self._le_version is None:
            self._discover_versions()
        return self._le_version  # type: ignore[return-value]

    # -- low-level helpers -------------------------------------------------
    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        """GET an absolute-from-host path like '/d2l/api/lp/{ver}/...'."""
        url = path if path.startswith("http") else f"{self.base}{path}"
        return self.http.get(url, **kwargs)

    def get_json(self, path: str, **kwargs: Any) -> Any:
        resp = self.get(path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def get_paged(self, path: str, **kwargs: Any) -> list[Any]:
        """Follow Brightspace's bookmark paging (Objects + Next/PagingInfo)."""
        items: list[Any] = []
        next_path: str | None = path
        params = dict(kwargs.pop("params", {}) or {})
        while next_path:
            data = self.get_json(next_path, params=params, **kwargs)
            if isinstance(data, list):
                items.extend(data)
                break
            objects = data.get("Objects", data.get("Items"))
            if objects is None:
                items.append(data)
                break
            items.extend(objects)
            paging = data.get("PagingInfo") or {}
            if paging.get("HasMoreItems") and paging.get("Bookmark"):
                params = {**params, "bookmark": paging["Bookmark"]}
                next_path = path
            else:
                next_path = None
        return items

    # -- convenience -------------------------------------------------------
    def whoami(self) -> dict[str, Any]:
        return self.get_json(f"/d2l/api/lp/{self.lp}/users/whoami")
