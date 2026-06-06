"""Integration tests for the hosted backend: accounts, pooling, and calendar fan-out.

Exercises the multi-user logic against a REAL local Postgres (uses a throwaway
`institution='pytest'` namespace + `pytest-*` users, cleaned up around the run), with NO
Google and NO LLM — the calendar layer is driven through a fake `GoogleCalendar`, and
deadlines are seeded directly so the interpret step isn't needed.

Run:  .venv/bin/python -m tests.test_backend     (from the repo root)
  or: .venv/bin/pytest tests/test_backend.py
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from brightspace_scraper import accounts, calendar_sync, changeset
from brightspace_scraper.config import load_config
from brightspace_scraper.models import Course, HarvestItem
from brightspace_scraper.store import Store

INST = "pytest"
_cfg = load_config(require_credentials=False)
# Ensure a key + web client for the crypto / sync_user tests, regardless of .env.
CFG = dataclasses.replace(
    _cfg,
    token_encryption_key=_cfg.token_encryption_key or Fernet.generate_key().decode(),
    google_web_client_id="test-web-client",
    google_web_client_secret="test-web-secret",
)
TZ = ZoneInfo(CFG.calendar_timezone)
NOW = _dt.datetime.now(_dt.timezone.utc)
FUTURE = "2099-12-01T23:59:00Z"
PAST = "2000-01-01T23:59:00Z"


def _store() -> Store:
    return Store(CFG.database_url, INST)


def _cleanup() -> None:
    s = _store()
    s.conn.execute("DELETE FROM users WHERE google_sub LIKE 'pytest-%'")  # cascades sessions/enrollments
    for t in ("courses", "items", "deadlines", "enrollment_items"):
        s.conn.execute(f"DELETE FROM {t} WHERE institution=%s", (INST,))
    s.conn.commit()
    s.close()


def _course(oid: int) -> Course:
    return Course(org_unit_id=oid, name=f"Course {oid}", code=f"C{oid}.202503",
                  type="Course Offering")


def _item(oid: int, n: int, *, body: str = "x") -> HarvestItem:
    return HarvestItem(id=f"assignment:{oid}:{n}", org_unit_id=oid, type="assignment",
                       title=f"Item {n}", structured_due_date=FUTURE, body_text=body)


def _deadline(oid: int, n: int, *, due: str = FUTURE) -> dict:
    return {"org_unit_id": oid, "item_id": f"assignment:{oid}:{n}", "title": f"DL {n}",
            "type": "assignment", "final_due_date": due, "structured_due_date": due,
            "confidence": "high", "source_url": None, "reasoning": "seeded"}


# --------------------------------------------------------------------------- crypto
def test_token_crypto_roundtrip_and_tamper():
    enc = accounts.encrypt_token(CFG, "refresh-xyz")
    assert enc != "refresh-xyz"
    assert accounts.decrypt_token(CFG, enc) == "refresh-xyz"
    try:
        accounts.decrypt_token(CFG, enc[:-4] + "AAAA")  # tampered
        raise AssertionError("tampered token should not decrypt")
    except Exception:
        pass


# --------------------------------------------------------------------------- users/sessions
def test_user_upsert_idempotent_and_token_coalesce():
    s = _store()
    enc = accounts.encrypt_token(CFG, "tok1")
    uid = s.upsert_user("pytest-U", "u@x.com", enc, "cal-1")
    uid2 = s.upsert_user("pytest-U", "new@x.com", None)   # re-login, no fresh token
    assert uid == uid2, "same google_sub must map to one user"
    u = s.get_user(uid)
    assert u["email"] == "new@x.com", "email updates"
    assert u["calendar_refresh_token"] == enc, "token must NOT be clobbered by None"
    assert u["calendar_id"] == "cal-1", "calendar_id preserved"
    s.close()


def test_sessions():
    s = _store()
    uid = s.upsert_user("pytest-S", "s@x.com", None)
    tok = s.create_session(uid)
    assert s.user_for_session(tok)["id"] == uid
    assert s.user_for_session("not-a-real-token") is None
    s.close()


# --------------------------------------------------------------------------- enrollment / fan-out targeting
def test_enrollment_and_fanout_targeting():
    s = _store()
    a = s.upsert_user("pytest-A", "a@x.com", None)
    b = s.upsert_user("pytest-B", "b@x.com", None)
    run = s.start_run()
    s.set_enrollment(a, [101, 102], run)
    s.set_enrollment(a, [101, 102], run)   # idempotent
    s.set_enrollment(b, [102], run)
    assert sorted(s.users_enrolled_in([101])) == [a], "c101 only A"
    assert sorted(s.users_enrolled_in([102])) == sorted([a, b]), "c102 both"
    assert s.users_enrolled_in([999]) == [], "nobody enrolled"
    s.close()


# --------------------------------------------------------------------------- pooling
def test_pooling_partial_scrape_does_not_evict():
    """prune=False: a user who scrapes only item 1 must NOT evict item 2 another contributed."""
    s = _store()
    c = 200001
    r1 = s.start_run()
    changeset.process(s, [_course(c)], [_item(c, 1), _item(c, 2)], r1, prune=False)
    r2 = s.start_run()
    cs = changeset.process(s, [_course(c)], [_item(c, 1)], r2, prune=False)  # partial
    items = s.get_items_for_courses([c])[c]
    ids = {it["id"] for it in items}
    assert f"assignment:{c}:2" in ids, "item 2 must survive a partial scrape"
    assert cs.unchanged_count == 1, "item 1 unchanged"
    s.close()


def test_pooling_interpret_once():
    """A second identical scrape produces no new/changed items → nothing to re-interpret."""
    s = _store()
    c = 200002
    r1 = s.start_run()
    cs1 = changeset.process(s, [_course(c)], [_item(c, 1)], r1, prune=False)
    r2 = s.start_run()
    cs2 = changeset.process(s, [_course(c)], [_item(c, 1)], r2, prune=False)
    assert len(cs1.new) == 1
    assert len(cs2.new) == 0 and len(cs2.changed) == 0, "identical re-scrape is all unchanged"
    s.close()


def test_deadlines_pooled_to_enrolled_users():
    s = _store()
    c = 300001
    run = s.start_run()
    s.save_deadlines([_deadline(c, 1), _deadline(c, 2)], run)
    a = s.upsert_user("pytest-DA", "da@x.com", None)
    b = s.upsert_user("pytest-DB", "db@x.com", None)
    other = s.upsert_user("pytest-DC", "dc@x.com", None)
    s.set_enrollment(a, [c], run)
    s.set_enrollment(b, [c], run)
    s.set_enrollment(other, [399999], run)  # different course
    assert len(s.deadlines_for_user(a)) == 2
    assert len(s.deadlines_for_user(b)) == 2, "pooled: B sees deadlines A's scrape produced"
    assert len(s.deadlines_for_user(other)) == 0, "not enrolled → no deadlines"
    s.close()


def test_institution_isolation():
    s = _store()
    c = 400001
    run = s.start_run()
    s.save_deadlines([_deadline(c, 1)], run)
    other = Store(CFG.database_url, "other-school")
    try:
        assert other.deadlines_for_institution() == [] or all(
            d["institution"] != INST for d in other.deadlines_for_institution()
        ), "another institution must not see pytest data"
    finally:
        other.close()
    s.close()


# --------------------------------------------------------------------------- calendar logic
def test_desired_skips_undated_and_filters_past():
    rows = [_deadline(1, 1, due=FUTURE), _deadline(1, 2, due=PAST),
            {**_deadline(1, 3), "final_due_date": None}]
    courses = {1: "Course 1"}
    desired, skipped = calendar_sync._desired_from_deadlines(
        rows, courses, CFG, include_past=False, now=NOW)
    assert skipped == 1, "the undated deadline is skipped"
    assert len(desired) == 1, "only the future dated deadline is desired"
    desired_all, _ = calendar_sync._desired_from_deadlines(
        rows, courses, CFG, include_past=True, now=NOW)
    assert len(desired_all) == 2, "include_past adds the past one"


class _FakeCalendar:
    def __init__(self, existing):
        self.existing = existing
        self.upserts: list[str] = []
        self.deletes: list[str] = []

    def list_events(self, cal_id):
        return self.existing

    def upsert_event(self, cal_id, event_id, body):
        self.upserts.append(event_id)

    def delete_event(self, cal_id, event_id):
        self.deletes.append(event_id)


def _desired_one():
    rows = [_deadline(1, 1, due=FUTURE)]
    return calendar_sync._desired_from_deadlines(rows, {1: "C1"}, CFG,
                                                 include_past=False, now=NOW)[0]


def test_reconcile_create_unchanged_update():
    desired = _desired_one()
    (event_id, (chash, body)), = desired.items()

    # nothing exists → created
    gc = _FakeCalendar([])
    c = calendar_sync._reconcile(gc, "cal", desired, prune=False, now=NOW, tz=TZ)
    assert c["created"] == 1 and gc.upserts == [event_id]

    # exists with same hash → unchanged, no write
    existing = [{"id": event_id, "start": body["start"],
                 "extendedProperties": {"private": {"hash": chash}}}]
    gc = _FakeCalendar(existing)
    c = calendar_sync._reconcile(gc, "cal", desired, prune=False, now=NOW, tz=TZ)
    assert c["unchanged"] == 1 and gc.upserts == []

    # exists with stale hash → updated
    existing[0]["extendedProperties"]["private"]["hash"] = "STALE"
    gc = _FakeCalendar(existing)
    c = calendar_sync._reconcile(gc, "cal", desired, prune=False, now=NOW, tz=TZ)
    assert c["updated"] == 1 and gc.upserts == [event_id]


def test_reconcile_prunes_future_orphans_only():
    desired = _desired_one()
    existing = [
        {"id": "orphan-future", "start": {"date": "2099-06-01"}},
        {"id": "orphan-past", "start": {"date": "2000-06-01"}},
    ]
    gc = _FakeCalendar(existing)
    c = calendar_sync._reconcile(gc, "cal", desired, prune=True, now=NOW, tz=TZ)
    assert gc.deletes == ["orphan-future"], "prune future orphans, keep history"
    assert c["deleted"] == 1


def test_sync_user_end_to_end_with_fake_calendar(monkeypatch=None):
    """sync_user: decrypt token → build desired from the user's pooled deadlines →
    reconcile → persist the calendar id. GoogleCalendar is faked (no Google)."""
    s = _store()
    c = 500001
    run = s.start_run()
    s.save_deadlines([_deadline(c, 1), _deadline(c, 2)], run)
    enc = accounts.encrypt_token(CFG, "user-refresh")
    uid = s.upsert_user("pytest-SU", "su@x.com", enc)   # no calendar_id yet
    s.set_enrollment(uid, [c], run)

    captured = {}

    class FakeGC:
        def __init__(self, *a, **k):
            pass

        def ensure_calendar(self, tz, calendar_id=None):
            captured["passed_cal_id"] = calendar_id
            return calendar_id or "new-cal-id"

        def list_events(self, cal_id):
            return []

        def upsert_event(self, cal_id, event_id, body):
            captured.setdefault("upserts", []).append(event_id)

        def delete_event(self, cal_id, event_id):
            pass

    orig = calendar_sync.GoogleCalendar
    calendar_sync.GoogleCalendar = FakeGC
    try:
        result = calendar_sync.sync_user(CFG, s, s.get_user(uid))
    finally:
        calendar_sync.GoogleCalendar = orig

    assert result["created"] == 2, f"both pooled deadlines synced, got {result}"
    assert captured["passed_cal_id"] is None, "no calendar id yet → creates one"
    assert s.get_user(uid)["calendar_id"] == "new-cal-id", "new calendar id persisted"
    s.close()


def test_sync_user_guard_no_token():
    s = _store()
    uid = s.upsert_user("pytest-NT", "nt@x.com", None)  # no refresh token
    assert calendar_sync.sync_user(CFG, s, s.get_user(uid)) == {"skipped": "no_refresh_token"}
    s.close()


# --------------------------------------------------------------------------- runner
def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main() -> int:
    _cleanup()
    passed = failed = 0
    try:
        for fn in _all_tests():
            try:
                fn()
                print(f"  PASS  {fn.__name__}")
                passed += 1
            except Exception as exc:
                print(f"  FAIL  {fn.__name__}: {type(exc).__name__}: {exc}")
                failed += 1
    finally:
        _cleanup()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
