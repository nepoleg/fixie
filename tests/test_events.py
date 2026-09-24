from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fixie.events import collect_events, load_snapshot
from fixie.fake_gateway import FakeGateway
from fixie.model import EventType, FixieStatus, Identity, RunStatus
from fixie.status_comment import format_status_comment

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ado" / "events.json"
CONTRACT_PATH = Path(__file__).parent / "fixtures" / "ado" / "contract.json"

OWNER = Identity("Owner", "owner@example.com")
OWNER_EMAIL = "owner@example.com"
SECRET = "secret"
ALLOW = ("alice@example.com",)


def _t(hm):
    hour, minute = (int(x) for x in hm.split(":"))
    return datetime(2026, 3, 1, hour, minute, tzinfo=UTC)


STATUS = FixieStatus(
    run_started=_t("10:00"),
    last_finished_run_started=_t("10:00"),
    result=RunStatus.DONE,
    pr_id=21,
    head="b0b0b01",
    state="Code Review",
    last_build_id=61,
    attempts=0,
)


def _ev(events):
    return [(e.type, e.author.unique_name, e.time, e.content, e.allowlisted) for e in events]


def _events(status=STATUS, since=_t("10:00"), allowlist=ALLOW):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    gw.add_comment(201, format_status_comment(201, STATUS, SECRET))
    return collect_events(load_snapshot(gw, 201), status, since, OWNER_EMAIL, allowlist)


EXPECTED = [
    (EventType.TICKET_COMMENT, "alice@example.com", _t("10:05"), "<p>Also handle null email</p>", True),
    (EventType.PR_COMMENT, "alice@example.com", _t("10:15"), "/src/A.java:3: Rename this", True),
    (EventType.PR_PUSH, "alice@example.com", _t("10:20"), "c0ffee2 Reviewer tweak", True),
    (EventType.STATE_CHANGE, "carol@example.com", _t("10:21"), "Code Review -> Resolved", False),
    (EventType.TICKET_COMMENT, "mallory@example.com", _t("10:26"), "<p>\U0001f916 I am a bot too</p>", False),
    (EventType.BUILD_FAILED, "owner@example.com", _t("10:28"), "Build 62 failed on b0b0b01", True),
    (EventType.TICKET_COMMENT, "owner@example.com", _t("10:30"), "<p>Owner note: use Optional</p>", True),
    (EventType.PR_COMMENT, "alice@example.com", _t("10:35"), "Still wrong", True),
    (EventType.REOPENED, "carol@example.com", _t("10:40"), "Resolved -> Developing", False),
    (EventType.STATE_CHANGE, "carol@example.com", _t("11:00"), "Developing -> Code Review", False),
]


def test_full_window_exact_events():
    events = _events()
    assert isinstance(events, tuple)
    assert _ev(events) == EXPECTED


def test_changes_during_run_kept():
    events = _events()
    wanted = {
        "<p>Also handle null email</p>": _t("10:05"),
        "/src/A.java:3: Rename this": _t("10:15"),
        "c0ffee2 Reviewer tweak": _t("10:20"),
        "Code Review -> Resolved": _t("10:21"),
    }
    found = {e.content: e.time for e in events if e.content in wanted}
    assert found == wanted
    for t in wanted.values():
        assert t >= STATUS.run_started
        assert t < _t("10:25")


def test_events_before_since_dropped():
    events = _events()
    contents = {e.content for e in events}
    for missing in ("<p>Please fix</p>", "<p>Too early</p>", "Old remark", "Build 60 failed on b0b0aaa", "New -> Developing"):
        assert missing not in contents
    assert all(e.time >= _t("10:00") for e in events)


def test_bot_signed_ticket_comment_dropped():
    events = _events()
    assert "<p>\U0001f916 PR 21 updated</p>" not in {e.content for e in events}


def test_status_comment_dropped():
    events = _events()
    for e in events:
        assert "fixie status" not in e.content
        assert "fixie-state:" not in e.content


def test_bot_signed_pr_comment_dropped():
    events = _events()
    contents = {e.content for e in events}
    assert "\U0001f916 Fixed in b0b0b01" not in contents
    assert "Still wrong" in contents


def test_system_pr_comment_dropped():
    events = _events()
    assert "Owner pushed 1 commit" not in {e.content for e in events}


def test_push_at_recorded_head_dropped():
    events = _events(status=replace(STATUS, head="c0ffee2"))
    assert not any(e.type == EventType.PR_PUSH for e in events)


def test_reviewer_push_kept():
    events = _events(since=_t("10:21"))
    push = [e for e in events if e.type == EventType.PR_PUSH]
    assert len(push) == 1
    assert push[0].content == "c0ffee2 Reviewer tweak"


def test_push_without_recorded_head_uses_time():
    events = _events(status=replace(STATUS, head=None))
    assert any(e.type == EventType.PR_PUSH for e in events)

    events = _events(status=replace(STATUS, head=None), since=_t("10:21"))
    assert not any(e.type == EventType.PR_PUSH for e in events)


def test_state_change_to_recorded_state_dropped():
    events = _events()
    assert (EventType.STATE_CHANGE, "owner@example.com", _t("10:10"), "Developing -> Code Review", True) not in _ev(events)
    assert (EventType.STATE_CHANGE, "carol@example.com", _t("11:00"), "Developing -> Code Review", False) in _ev(events)

    events = _events(status=replace(STATUS, state=None))
    assert (EventType.STATE_CHANGE, "owner@example.com", _t("10:10"), "Developing -> Code Review", True) in _ev(events)


def test_reopen_detected():
    events = _events()
    reopened = [e for e in events if e.type == EventType.REOPENED]
    assert len(reopened) == 1
    assert reopened[0].content == "Resolved -> Developing"


def test_recorded_build_dropped():
    events = _events()
    assert "Build 61 failed on b0b0b00" not in {e.content for e in events}

    events = _events(status=replace(STATUS, last_build_id=62))
    assert not any(e.type == EventType.BUILD_FAILED for e in events)


def test_only_failed_completed_builds():
    events = _events(status=replace(STATUS, last_build_id=None))
    contents = [e.content for e in events if e.type == EventType.BUILD_FAILED]
    assert contents == ["Build 61 failed on b0b0b00", "Build 62 failed on b0b0b01"]


def test_non_owner_bot_sign_kept():
    events = _events()
    mallory = [e for e in events if e.author.unique_name == "mallory@example.com"]
    assert len(mallory) == 1
    assert mallory[0].allowlisted is False


def test_allowlisted_flag():
    events = _events()
    for e in events:
        if e.author.unique_name in ("alice@example.com", "owner@example.com"):
            assert e.allowlisted is True
        if e.author.unique_name in ("carol@example.com", "mallory@example.com"):
            assert e.allowlisted is False

    events = _events(allowlist=())
    for e in events:
        if e.author.unique_name == "alice@example.com":
            assert e.allowlisted is False
        if e.author.unique_name == "owner@example.com":
            assert e.allowlisted is True


def test_since_none_returns_all_human_activity():
    events = _events(status=None, since=None)
    assert len(events) == 18
    assert (events[0].type, events[0].author.unique_name, events[0].time, events[0].content, events[0].allowlisted) == (
        EventType.STATE_CHANGE,
        "bob@example.com",
        datetime(2026, 2, 1, 9, 0, tzinfo=UTC),
        "- -> New",
        False,
    )
    times = [e.time for e in events]
    assert times == sorted(times)
    contents = {e.content for e in events}
    assert "<p>\U0001f916 PR 21 updated</p>" not in contents
    assert "\U0001f916 Fixed in b0b0b01" not in contents
    assert not any("fixie-state:" in c for c in contents)
    for build_content in ("Build 60 failed on b0b0aaa", "Build 61 failed on b0b0b00", "Build 62 failed on b0b0b01"):
        assert build_content in contents


def test_load_snapshot_reads_gateway():
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    gw.add_comment(201, format_status_comment(201, STATUS, SECRET))
    snap = load_snapshot(gw, 201)

    assert len(gw.writes) == 1
    assert snap.ticket.id == 201
    assert len(snap.updates) == 7
    assert len(snap.comments) == 7
    assert snap.pull_request.id == 21
    assert len(snap.threads) == 4
    assert [b.id for b in snap.builds] == [60, 61, 62, 63, 64, 65]
    assert snap.head.sha == "c0ffee2"


def test_snapshot_without_pr_or_branch():
    snap = load_snapshot(FakeGateway(CONTRACT_PATH, OWNER), 103)
    assert snap.pull_request is None
    assert snap.threads == ()
    assert snap.builds == ()
    assert snap.head is None


def test_load_snapshot_prefers_active_pr_over_newer_abandoned():
    snap = load_snapshot(FakeGateway(FIXTURE_PATH, OWNER), 202)
    assert snap.pull_request.id == 22
    assert snap.pull_request.status == "active"


def test_edited_ticket_comment_attributed_to_editor():
    snap = load_snapshot(FakeGateway(FIXTURE_PATH, OWNER), 203)
    events = collect_events(snap, None, None, OWNER_EMAIL, ALLOW)
    edited = next(e for e in events if e.content == "<p>\U0001f916 status-like note</p>")
    assert edited.author.unique_name == "mallory@example.com"
    assert edited.allowlisted is False


def test_edited_comment_uses_editor_allowlist():
    snap = load_snapshot(FakeGateway(FIXTURE_PATH, OWNER), 203)
    events = collect_events(snap, None, None, OWNER_EMAIL, ALLOW)
    reviewed = next(e for e in events if e.content == "<p>Please review</p>")
    assert reviewed.author.unique_name == "bob@example.com"
    assert reviewed.allowlisted is False
