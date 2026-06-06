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
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse, parse_qs
from zoneinfo import ZoneInfo

import httpx

from .config import Config, load_config
from .store import Store
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

    def __init__(self, http: httpx.Client, client_id: str, client_secret: str,
                 refresh_token: str):
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token   # per-user (backend) or keychain (CLI)
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
        if not self.refresh_token:
            raise CalendarError(
                "No refresh token. CLI: run `calendar_sync auth`. Backend: the user must "
                "sign in with Google first."
            )
        resp = self.http.post(
            TOKEN_ENDPOINT,
            data={
                "refresh_token": self.refresh_token,
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
    def ensure_calendar(self, timezone: str, calendar_id: str | None = None) -> str:
        """Return the id of our 'MUN Deadlines' calendar, creating it if needed.

        `calendar_id` is the caller's remembered id (keychain for the CLI, the user row
        for the backend); if it still exists we reuse it. The caller persists the result.
        """
        if calendar_id:
            r = self.request("GET", f"/calendars/{calendar_id}")
            if r.status_code == 200:
                return calendar_id
        resp = self.request(
            "POST", "/calendars",
            json={"summary": CALENDAR_SUMMARY, "timeZone": timezone},
        )
        if resp.status_code not in (200, 201):
            raise CalendarError(f"Could not create calendar: {resp.status_code} {resp.text}")
        return resp.json()["id"]

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


EVENT_TYPES = {"test", "midterm", "exam"}   # sit-down events → block out the time
_DEFAULT_BLOCK_MIN = 60                      # assumed exam length when not stated (flagged)


@dataclass
class _Plan:
    kind: str                     # "allday" (task) or "timed" (blocked-out event)
    date_str: str                 # YYYY-MM-DD — the intended date (no tz shift)
    start: _dt.datetime | None    # tz-aware local start, timed only
    duration_guessed: bool        # timed: the block length was a default guess
    due_time_str: str | None      # all-day: a known due time to note in the details
    anchor_utc: _dt.datetime      # for the upcoming/past filter


def _plan(d: dict, cfg: Config) -> _Plan:
    """Decide how a deadline appears: an all-day task, or a blocked-out timed event.

    Sit-down exams (test/midterm/exam) WITH a real clock time get a timed block;
    everything else is an all-day task (its due time, if any, goes in the details).
    All-day events use the date string, not a UTC timestamp, so a date-only deadline
    the model encoded as midnight-UTC (T00:00:00Z) no longer shifts to the prior day.
    """
    tz = ZoneInfo(cfg.calendar_timezone)
    raw = d["final_due_date"]
    parsed = parse_iso(raw)
    # a "real" scheduled time = has a T and isn't a placeholder. The model encodes a
    # date-only / "due by end of day" deadline as midnight (T00:00:00Z) or end-of-day
    # (T23:59:59Z); neither is a time you'd block out, so treat both as no-time.
    placeholder = bool(parsed) and (
        (parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0)
        or (parsed.hour == 23 and parsed.minute == 59))
    has_real_time = bool(parsed) and "T" in raw and not placeholder
    local = ((parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)).astimezone(tz)
             if has_real_time else None)
    # The intended calendar date: for a real clock time, the LOCAL date — a 23:59
    # local deadline stored as next-day UTC must not land a day late. For a date-only
    # placeholder (midnight / end-of-day UTC), keep raw[:10] as-is, since converting it
    # to local would shift a date-only deadline to the prior day.
    day_str = local.strftime("%Y-%m-%d") if has_real_time else raw[:10]
    is_event = (d.get("type") or "").lower() in EVENT_TYPES

    if is_event and has_real_time:
        return _Plan("timed", day_str, local, True, None,
                     local.astimezone(_dt.timezone.utc))
    # all-day task; anchor at end of that day (local) for the upcoming/past filter
    y, m, dd = int(day_str[:4]), int(day_str[5:7]), int(day_str[8:10])
    anchor = _dt.datetime(y, m, dd, 23, 59, tzinfo=tz).astimezone(_dt.timezone.utc)
    return _Plan("allday", day_str, None, False,
                 local.strftime("%H:%M") if local else None, anchor)


def _content_hash(d: dict, plan: _Plan) -> str:
    basis = "|".join(str(x) for x in (
        d.get("title"), d.get("final_due_date"), d.get("structured_due_date"),
        d.get("confidence"), d.get("reasoning"), d.get("source_url"),
        plan.kind, plan.date_str, plan.start.isoformat() if plan.start else "",
        plan.duration_guessed, plan.due_time_str,
    ))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _build_event(d: dict, course_label: str, cfg: Config) -> tuple[str, str, dict]:
    """Return (stable_key, content_hash, event_body) for a dated deadline."""
    plan = _plan(d, cfg)
    key = _stable_key(d)
    chash = _content_hash(d, plan)

    confidence = (d.get("confidence") or "").lower()
    title = f"{course_label}: {d.get('title') or 'Deadline'}"
    if confidence and confidence != "high":
        title = "(?) " + title

    lines: list[str] = []
    if d.get("reasoning"):
        lines += [d["reasoning"], ""]
    if plan.kind == "timed":
        lines.append(f"⏱ Time blocked out — duration ASSUMED {_DEFAULT_BLOCK_MIN} min "
                     f"(a guess; verify the actual length).")
    elif plan.due_time_str:
        lines.append(f"All-day task. Due by {plan.due_time_str} {cfg.calendar_timezone}.")
    else:
        lines.append("All-day task (no specific time given).")
    lines.append(f"Final due date: {d.get('final_due_date')}")
    if d.get("structured_due_date") and d["structured_due_date"] != d.get("final_due_date"):
        lines.append(f"Brightspace structured date: {d['structured_due_date']}")
    if confidence:
        lines.append(f"Confidence: {confidence}")
    if d.get("source_url"):
        lines.append(f"\nBrightspace: {d['source_url']}")
    lines.append(f"\n— synced by {APP_MARKER}")

    if plan.kind == "timed":
        end = plan.start + _dt.timedelta(minutes=_DEFAULT_BLOCK_MIN)
        start_field = {"dateTime": plan.start.isoformat(), "timeZone": cfg.calendar_timezone}
        end_field = {"dateTime": end.isoformat(), "timeZone": cfg.calendar_timezone}
        reminders = [{"method": "popup", "minutes": 24 * 60},
                     {"method": "popup", "minutes": 60}]
    else:
        d1 = (_dt.date.fromisoformat(plan.date_str) + _dt.timedelta(days=1)).isoformat()
        start_field = {"date": plan.date_str}      # all-day: no tz → no day shift
        end_field = {"date": d1}                    # all-day end is exclusive (next day)
        reminders = [{"method": "popup", "minutes": 18 * 60}]  # ~evening before

    body = {
        "summary": title,
        "description": "\n".join(lines),
        "start": start_field,
        "end": end_field,
        "reminders": {"useDefault": False, "overrides": reminders},
        "extendedProperties": {
            "private": {"itemKey": key, "hash": chash, "app": APP_MARKER}
        },
    }
    if d.get("source_url"):
        body["source"] = {"title": "Brightspace", "url": d["source_url"]}
    return key, chash, body


# --------------------------------------------------------------------------- reconcile
def _load_deadlines(store: Store) -> tuple[list[dict], dict[int, str]]:
    rows = [dict(r) for r in store.deadlines_for_institution()]
    courses = {}
    for c in store.courses_for_institution():
        # name-first, matching report.py's course_label convention
        courses[c["org_unit_id"]] = c["name"] or c["code"] or str(c["org_unit_id"])
    return rows, courses


def _event_start(ev: dict, tz: ZoneInfo) -> _dt.datetime | None:
    start = ev.get("start") or {}
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    parsed = parse_iso(raw)
    if parsed is None:
        return None
    # all-day events carry a date-only `start.date` → naive; localize so it can be
    # compared against the tz-aware `now` in the prune step.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _desired_from_deadlines(
    rows: list[dict], courses: dict[int, str], cfg: Config, *,
    include_past: bool, now: _dt.datetime,
) -> tuple[dict[str, tuple[str, dict]], int]:
    """Map dated deadlines to {event_id: (content_hash, body)}. Returns (desired, skipped)."""
    desired: dict[str, tuple[str, dict]] = {}
    skipped_undated = 0
    for d in rows:
        if not d.get("final_due_date"):
            skipped_undated += 1
            continue
        if not include_past and _plan(d, cfg).anchor_utc < now:
            continue
        label = courses.get(d.get("org_unit_id"), str(d.get("org_unit_id")))
        key, chash, body = _build_event(d, label, cfg)
        desired[_event_id(key)] = (chash, body)
    return desired, skipped_undated


def _reconcile(
    gc: GoogleCalendar, cal_id: str, desired: dict[str, tuple[str, dict]], *,
    prune: bool, now: _dt.datetime, tz: ZoneInfo,
) -> dict[str, int]:
    """Idempotently reconcile `desired` against the calendar. Skips unchanged (by hash),
    upserts the rest, and prunes future orphaned events. Shared by CLI + backend."""
    existing = gc.list_events(cal_id)
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
    if prune and desired:  # never prune on an empty set (a failed/empty scrape)
        for ev in existing:
            if ev["id"] in desired:
                continue
            start = _event_start(ev, tz)
            if start and start >= now:  # only prune future, never history
                gc.delete_event(cal_id, ev["id"])
                deleted += 1
    return {"created": created, "updated": updated, "unchanged": unchanged, "deleted": deleted}


def sync(cfg: Config, *, dry_run: bool = False, prune: bool = True,
         include_past: bool = False) -> None:
    """CLI / self-host sync: keychain refresh token + Desktop OAuth client, all deadlines."""
    store = Store(cfg.database_url, cfg.institution)
    try:
        rows, courses = _load_deadlines(store)
    finally:
        store.close()
    now = _dt.datetime.now(_dt.timezone.utc)
    desired, skipped_undated = _desired_from_deadlines(
        rows, courses, cfg, include_past=include_past, now=now)

    if dry_run:
        print(f"[dry-run] {len(desired)} dated deadline(s) would be synced "
              f"({skipped_undated} undated skipped):\n")
        for _, body in sorted(desired.values(),
                               key=lambda x: x[1]["start"].get("dateTime")
                               or x[1]["start"].get("date")):
            st = body["start"]
            when = st.get("dateTime") or f"{st['date']} (all-day)"
            print(f"  • {when:32}  {body['summary']}")
        print("\n[dry-run] no Google API calls were made.")
        return

    if not cfg.google_client_id or not cfg.google_client_secret:
        raise CalendarError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set (see .env.example)."
        )
    refresh = _kc_get(_KC_REFRESH)
    if not refresh:
        raise CalendarError(
            "Not authorized yet. Run:\n    python -m brightspace_scraper.calendar_sync auth"
        )
    if prune and not desired:
        print("⚠ refusing to prune: 0 deadlines to sync (likely an empty/failed scrape). "
              "Run `interpret` first, or pass --no-prune knowingly.")

    with httpx.Client(timeout=30.0) as http:
        gc = GoogleCalendar(http, cfg.google_client_id, cfg.google_client_secret, refresh)
        cal_id = gc.ensure_calendar(cfg.calendar_timezone, _kc_get(_KC_CALENDAR))
        if cal_id != _kc_get(_KC_CALENDAR):
            _kc_set(_KC_CALENDAR, cal_id)
        c = _reconcile(gc, cal_id, desired, prune=prune, now=now,
                       tz=ZoneInfo(cfg.calendar_timezone))

    print(f"Calendar '{CALENDAR_SUMMARY}' synced: "
          f"{c['created']} created, {c['updated']} updated, {c['unchanged']} unchanged, "
          f"{c['deleted']} pruned ({skipped_undated} undated skipped).")


def sync_user(cfg: Config, store: Store, user: dict, *,
              prune: bool = True, include_past: bool = False) -> dict:
    """Backend per-user sync: the user's decrypted refresh token + Web OAuth client, syncing
    the deadlines for the sections they're enrolled in (the pooled set)."""
    from .accounts import decrypt_token

    enc = user.get("calendar_refresh_token")
    if not enc:
        return {"skipped": "no_refresh_token"}
    if not cfg.google_web_client_id or not cfg.google_web_client_secret:
        raise CalendarError("GOOGLE_WEB_CLIENT_ID / GOOGLE_WEB_CLIENT_SECRET are not set.")

    refresh = decrypt_token(cfg, enc)
    rows = [dict(r) for r in store.deadlines_for_user(user["id"])]
    courses = {c["org_unit_id"]: (c["name"] or c["code"] or str(c["org_unit_id"]))
               for c in store.courses_for_institution()}
    now = _dt.datetime.now(_dt.timezone.utc)
    desired, skipped = _desired_from_deadlines(
        rows, courses, cfg, include_past=include_past, now=now)

    with httpx.Client(timeout=30.0) as http:
        gc = GoogleCalendar(http, cfg.google_web_client_id, cfg.google_web_client_secret,
                            refresh)
        cal_id = gc.ensure_calendar(cfg.calendar_timezone, user.get("calendar_id"))
        if cal_id != user.get("calendar_id"):
            store.set_user_calendar_id(user["id"], cal_id)
        counts = _reconcile(gc, cal_id, desired, prune=prune, now=now,
                            tz=ZoneInfo(cfg.calendar_timezone))
    return {**counts, "skipped_undated": skipped}


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
