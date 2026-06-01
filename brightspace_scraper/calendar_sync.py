"""Sync the `deadlines` table to Google Calendar — pure httpx, no Google SDK.

Design (matches the house style in auth.py / client.py):
  * OAuth 2.0 "Desktop app" loopback flow. The student authorizes in their browser;
    we catch the ?code= on http://localhost:<port>, exchange it for a refresh token,
    and store ONLY the refresh token in the OS keychain (never on disk).
  * Least-privilege scope `calendar.app.created`: we can only ever touch a calendar
    THIS app created ("MUN Deadlines"), never the user's other events.
  * Idempotent reconcile. Each deadline maps to a deterministic event id
    (base32hex(sha1(stable-key))), so re-running updates in place instead of
    duplicating. A per-event content hash in extendedProperties lets us skip
    unchanged events. Future-dated events with no matching deadline are pruned.

CLI:
    python -m brightspace_scraper.calendar_sync auth            # one-time browser consent
    python -m brightspace_scraper.calendar_sync sync            # push deadlines -> calendar
    python -m brightspace_scraper.calendar_sync sync --dry-run  # show plan, call nothing
    python -m brightspace_scraper.calendar_sync sync --all      # include past deadlines
    python -m brightspace_scraper.calendar_sync sync --no-prune # never delete events
    python -m brightspace_scraper.calendar_sync status          # what's configured
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse, parse_qs
from zoneinfo import ZoneInfo

import httpx

from .config import Config, load_config
from .util import parse_iso

try:  # same guard as credentials.py — keyring may have no backend
    import keyring
    from keyring.errors import KeyringError
except Exception:  # pragma: no cover
    keyring = None  # type: ignore[assignment]

    class KeyringError(Exception):  # type: ignore[no-redef]
        pass


# --------------------------------------------------------------------------- const
SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CAL_API = "https://www.googleapis.com/calendar/v3"
CALENDAR_SUMMARY = "MUN Deadlines"
APP_MARKER = "brightspace-scraper"

_KC_SERVICE = "brightspace-scraper"
_KC_REFRESH = "__google_refresh_token__"
_KC_CALENDAR = "__google_calendar_id__"


class CalendarError(RuntimeError):
    pass


# --------------------------------------------------------------------------- keychain
def _kc_get(key: str) -> str | None:
    if keyring is None:
        return None
    try:
        return keyring.get_password(_KC_SERVICE, key)
    except KeyringError:
        return None


def _kc_set(key: str, value: str) -> None:
    if keyring is None:
        raise CalendarError(
            "No keyring backend available to store the Google token. Install/enable "
            "an OS secret store (GNOME Keyring / KWallet on Linux)."
        )
    keyring.set_password(_KC_SERVICE, key, value)


def _kc_delete(key: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(_KC_SERVICE, key)
    except KeyringError:
        pass


# --------------------------------------------------------------------------- OAuth client
class GoogleCalendar:
    """Thin authenticated httpx wrapper over the Calendar v3 REST API."""

    def __init__(self, http: httpx.Client, client_id: str, client_secret: str):
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: str | None = None

    # -- authorization (one-time, interactive) -----------------------------
    @classmethod
    def authorize(cls, cfg: Config, *, timeout: float = 30.0) -> None:
        """Run the loopback consent flow and persist the refresh token."""
        import webbrowser
        from http.server import BaseHTTPRequestHandler, HTTPServer

        if not cfg.google_client_id or not cfg.google_client_secret:
            raise CalendarError(
                "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set. Create a "
                "Desktop-app OAuth client in Google Cloud Console and add them to .env "
                "(see .env.example)."
            )

        redirect_uri = f"http://localhost:{cfg.oauth_port}/"
        auth_url = AUTH_ENDPOINT + "?" + urlencode(
            {
                "client_id": cfg.google_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",   # ask for a refresh token
                "prompt": "consent",         # force refresh-token issuance every time
            }
        )

        captured: dict[str, str] = {}

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                qs = parse_qs(urlparse(self.path).query)
                captured.update({k: v[0] for k, v in qs.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                ok = "code" in captured
                msg = (
                    "Authorized. You can close this tab and return to the terminal."
                    if ok else f"Authorization failed: {captured.get('error', 'unknown')}"
                )
                self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

            def log_message(self, *a):  # silence the default stderr logging
                return

        print(f"Opening browser for Google consent...\n  {auth_url}\n")
        webbrowser.open(auth_url)
        server = HTTPServer(("localhost", cfg.oauth_port), _Handler)
        try:
            server.handle_request()  # serve exactly one redirect
        finally:
            server.server_close()

        if "code" not in captured:
            raise CalendarError(
                f"No authorization code received (got {captured!r})."
            )

        with httpx.Client(timeout=timeout) as http:
            resp = http.post(
                TOKEN_ENDPOINT,
                data={
                    "code": captured["code"],
                    "client_id": cfg.google_client_id,
                    "client_secret": cfg.google_client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        if resp.status_code != 200:
            raise CalendarError(f"Token exchange failed: {resp.status_code} {resp.text}")
        tokens = resp.json()
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise CalendarError(
                "Google did not return a refresh token. Revoke this app's access at "
                "https://myaccount.google.com/permissions and run `auth` again."
            )
        _kc_set(_KC_REFRESH, refresh)
        print("Stored Google refresh token in the OS keychain.")

    # -- access token (refresh on demand) ----------------------------------
    def _refresh_access_token(self) -> None:
        refresh = _kc_get(_KC_REFRESH)
        if not refresh:
            raise CalendarError(
                "Not authorized yet. Run:\n"
                "    python -m brightspace_scraper.calendar_sync auth"
            )
        resp = self.http.post(
            TOKEN_ENDPOINT,
            data={
                "refresh_token": refresh,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            raise CalendarError(
                f"Token refresh failed: {resp.status_code} {resp.text}\n"
                "The refresh token may be expired/revoked — re-run `auth`."
            )
        self._access_token = resp.json()["access_token"]

    # -- request helper (auto-refresh on 401) ------------------------------
    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._access_token is None:
            self._refresh_access_token()
        url = path if path.startswith("http") else f"{CAL_API}{path}"
        headers = {**kwargs.pop("headers", {}),
                   "Authorization": f"Bearer {self._access_token}"}
        resp = self.http.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:  # access token expired mid-run — refresh once
            self._refresh_access_token()
            headers["Authorization"] = f"Bearer {self._access_token}"
            resp = self.http.request(method, url, headers=headers, **kwargs)
        return resp

    # -- calendar bootstrap ------------------------------------------------
    def ensure_calendar(self, timezone: str) -> str:
        """Return the id of our 'MUN Deadlines' calendar, creating it if needed."""
        cal_id = _kc_get(_KC_CALENDAR)
        if cal_id:
            # Verify it still exists (user may have deleted it).
            r = self.request("GET", f"/calendars/{cal_id}")
            if r.status_code == 200:
                return cal_id
        resp = self.request(
            "POST", "/calendars",
            json={"summary": CALENDAR_SUMMARY, "timeZone": timezone},
        )
        if resp.status_code not in (200, 201):
            raise CalendarError(f"Could not create calendar: {resp.status_code} {resp.text}")
        cal_id = resp.json()["id"]
        _kc_set(_KC_CALENDAR, cal_id)
        return cal_id

    def list_events(self, calendar_id: str) -> list[dict]:
        """All events on our calendar (paged), with the fields reconcile needs."""
        events: list[dict] = []
        params = {
            "maxResults": 250,
            "showDeleted": "false",
            "fields": "nextPageToken,items(id,start,extendedProperties)",
        }
        while True:
            resp = self.request(
                "GET", f"/calendars/{calendar_id}/events", params=params
            )
            if resp.status_code != 200:
                raise CalendarError(f"List events failed: {resp.status_code} {resp.text}")
            data = resp.json()
            events.extend(data.get("items", []))
            token = data.get("nextPageToken")
            if not token:
                return events
            params = {**params, "pageToken": token}

    def upsert_event(self, calendar_id: str, event_id: str, body: dict) -> None:
        """PATCH-first (cheap when it exists), fall back to insert with our id."""
        r = self.request(
            "PATCH", f"/calendars/{calendar_id}/events/{event_id}", json=body
        )
        if r.status_code == 200:
            return
        if r.status_code == 404:
            r = self.request(
                "POST", f"/calendars/{calendar_id}/events",
                json={**body, "id": event_id},
            )
            if r.status_code in (200, 201):
                return
        raise CalendarError(f"Upsert {event_id} failed: {r.status_code} {r.text}")

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        r = self.request("DELETE", f"/calendars/{calendar_id}/events/{event_id}")
        if r.status_code not in (200, 204, 410):  # 410 = already gone
            raise CalendarError(f"Delete {event_id} failed: {r.status_code} {r.text}")


# --------------------------------------------------------------------------- mapping
def _stable_key(d: dict) -> str:
    """Identity of a deadline that survives date changes (so updates move events).

    Keyed on the harvest item id (or course+title when absent) plus the title, so a
    course that yields several deadlines from one item still gets distinct events.
    """
    base = d.get("item_id") or f"{d.get('org_unit_id')}"
    return f"{base}::{d.get('title')}"


def _event_id(key: str) -> str:
    """Deterministic, Google-legal event id: base32hex(sha1(key)), lowercased.

    Google requires event ids in the base32hex alphabet (0-9a-v), length 5-1024.
    sha1 -> 20 bytes -> 32 base32hex chars (no padding); we prefix 'mun' to namespace.
    """
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    enc = base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()
    return "mun" + enc


def _content_hash(d: dict, assumed_time: bool) -> str:
    basis = "|".join(str(x) for x in (
        d.get("title"), d.get("final_due_date"), d.get("structured_due_date"),
        d.get("confidence"), d.get("reasoning"), d.get("source_url"), assumed_time,
    ))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


@dataclass
class _Resolved:
    start: _dt.datetime          # tz-aware, in the calendar timezone
    assumed_time: bool           # True when we defaulted a date-only deadline


def _resolve_time(raw: str, cfg: Config) -> _Resolved:
    """Turn a deadline's final_due_date string into a concrete local datetime."""
    tz = ZoneInfo(cfg.calendar_timezone)
    has_time = "T" in raw  # ISO-8601 separates date and time with 'T'
    if has_time:
        dt = parse_iso(raw)
        if dt is None:  # malformed — fall back to date-only handling
            has_time = False
        else:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return _Resolved(start=dt.astimezone(tz), assumed_time=False)
    # date-only: apply the (configurable) default due time, flagged as assumed
    day = _dt.date.fromisoformat(raw[:10])
    hh, mm = (int(x) for x in cfg.default_due_time.split(":"))
    return _Resolved(
        start=_dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz),
        assumed_time=True,
    )


def _build_event(d: dict, course_label: str, cfg: Config) -> tuple[str, str, dict]:
    """Return (stable_key, content_hash, event_body) for a dated deadline."""
    res = _resolve_time(d["final_due_date"], cfg)
    key = _stable_key(d)
    chash = _content_hash(d, res.assumed_time)

    confidence = (d.get("confidence") or "").lower()
    low = confidence and confidence != "high"
    title = f"{course_label}: {d.get('title') or 'Deadline'}"
    if low:
        title = "(?) " + title

    end = res.start + _dt.timedelta(minutes=30)

    lines: list[str] = []
    if d.get("reasoning"):
        lines.append(d["reasoning"])
        lines.append("")
    lines.append(f"Final due date: {d.get('final_due_date')}")
    if d.get("structured_due_date") and d.get("structured_due_date") != d.get("final_due_date"):
        lines.append(f"Brightspace structured date: {d.get('structured_due_date')}")
    if confidence:
        lines.append(f"Confidence: {confidence}")
    if res.assumed_time:
        lines.append(f"⏱ Time not specified — defaulted to {cfg.default_due_time} "
                     f"({cfg.calendar_timezone}).")
    if d.get("source_url"):
        lines.append(f"\nBrightspace: {d['source_url']}")
    lines.append(f"\n— synced by {APP_MARKER}")

    body = {
        "summary": title,
        "description": "\n".join(lines),
        "start": {"dateTime": res.start.isoformat(), "timeZone": cfg.calendar_timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": cfg.calendar_timezone},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 24 * 60},
                {"method": "popup", "minutes": 180},
            ],
        },
        "extendedProperties": {
            "private": {"itemKey": key, "hash": chash, "app": APP_MARKER}
        },
    }
    if d.get("source_url"):
        body["source"] = {"title": "Brightspace", "url": d["source_url"]}
    return key, chash, body


# --------------------------------------------------------------------------- reconcile
def _load_deadlines(db_path) -> tuple[list[dict], dict[int, str]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM deadlines")]
    courses = {}
    for c in conn.execute("SELECT org_unit_id, name, code FROM courses"):
        # name-first, matching report.py's course_label convention
        courses[c["org_unit_id"]] = c["name"] or c["code"] or str(c["org_unit_id"])
    conn.close()
    return rows, courses


def _event_start(ev: dict) -> _dt.datetime | None:
    start = ev.get("start") or {}
    raw = start.get("dateTime") or start.get("date")
    return parse_iso(raw) if raw else None


def sync(cfg: Config, *, dry_run: bool = False, prune: bool = True,
         include_past: bool = False) -> None:
    rows, courses = _load_deadlines(cfg.db_path)
    now = _dt.datetime.now(_dt.timezone.utc)

    # Build the desired set from dated deadlines (skip undated — they have no event).
    desired: dict[str, tuple[str, dict]] = {}   # event_id -> (content_hash, body)
    desired_keys: set[str] = set()
    skipped_undated = 0
    for d in rows:
        if not d.get("final_due_date"):
            skipped_undated += 1
            continue
        res = _resolve_time(d["final_due_date"], cfg)
        if not include_past and res.start.astimezone(_dt.timezone.utc) < now:
            continue
        label = courses.get(d.get("org_unit_id"), str(d.get("org_unit_id")))
        key, chash, body = _build_event(d, label, cfg)
        desired[_event_id(key)] = (chash, body)
        desired_keys.add(key)

    if dry_run:
        print(f"[dry-run] {len(desired)} dated deadline(s) would be synced "
              f"({skipped_undated} undated skipped):\n")
        for _, body in desired.values():
            print(f"  • {body['start']['dateTime']}  {body['summary']}")
        print("\n[dry-run] no Google API calls were made.")
        return

    if not cfg.google_client_id or not cfg.google_client_secret:
        raise CalendarError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set (see .env.example)."
        )

    with httpx.Client(timeout=30.0) as http:
        gc = GoogleCalendar(http, cfg.google_client_id, cfg.google_client_secret)
        cal_id = gc.ensure_calendar(cfg.calendar_timezone)
        existing = gc.list_events(cal_id)

        # Map existing events by their stored hash so we can skip unchanged ones.
        existing_hash: dict[str, str] = {}
        for ev in existing:
            priv = (ev.get("extendedProperties") or {}).get("private") or {}
            existing_hash[ev["id"]] = priv.get("hash", "")

        created = updated = unchanged = 0
        for event_id, (chash, body) in desired.items():
            if existing_hash.get(event_id) == chash:
                unchanged += 1
                continue
            gc.upsert_event(cal_id, event_id, body)
            if event_id in existing_hash:
                updated += 1
            else:
                created += 1

        deleted = 0
        if prune:
            if not desired:
                print("⚠ refusing to prune: 0 deadlines to sync (likely an empty/failed "
                      "scrape). Run `interpret` first, or pass --no-prune knowingly.")
            else:
                for ev in existing:
                    if ev["id"] in desired:
                        continue
                    start = _event_start(ev)
                    if start and start >= now:  # only prune future, never history
                        gc.delete_event(cal_id, ev["id"])
                        deleted += 1

    print(f"Calendar '{CALENDAR_SUMMARY}' synced: "
          f"{created} created, {updated} updated, {unchanged} unchanged, "
          f"{deleted} pruned ({skipped_undated} undated skipped).")


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="brightspace_scraper.calendar_sync")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("auth", help="run the one-time Google consent flow")

    sp = sub.add_parser("sync", help="push deadlines to Google Calendar")
    sp.add_argument("--dry-run", action="store_true", help="print the plan, call nothing")
    sp.add_argument("--all", action="store_true", help="include past deadlines too")
    sp.add_argument("--no-prune", action="store_true",
                    help="never delete calendar events")

    sub.add_parser("status", help="show what's configured")

    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config(require_credentials=False)

    if args.cmd == "auth":
        GoogleCalendar.authorize(cfg)
        return 0

    if args.cmd == "status":
        print(f"google client id:   {'set' if cfg.google_client_id else 'MISSING'}")
        print(f"refresh token:      {'stored' if _kc_get(_KC_REFRESH) else 'not authorized'}")
        print(f"calendar id:        {_kc_get(_KC_CALENDAR) or '(none yet)'}")
        print(f"timezone:           {cfg.calendar_timezone}")
        print(f"default due time:   {cfg.default_due_time}")
        return 0

    if args.cmd == "sync":
        sync(cfg, dry_run=args.dry_run, prune=not args.no_prune, include_past=args.all)
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
