from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fixie.classify import classify
from fixie.events import load_snapshot
from fixie.fake_gateway import FakeGateway
from fixie.model import Action, Classification, EventType, FixieStatus, Identity, RunStatus
from fixie.settings import Settings
from fixie.status_comment import find_status_comment, format_status_comment, trusted_status

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ado" / "classify.json"

OWNER = Identity("Owner", "owner@example.com")
OWNER_EMAIL = "owner@example.com"
SECRET = "secret"
ALLOW = ("alice@example.com",)


def _t(hm):
    hour, minute = (int(x) for x in hm.split(":"))
    return datetime(2026, 3, 1, hour, minute, tzinfo=UTC)


def _ev(events):
    return [(e.type, e.author.unique_name, e.time, e.content) for e in events]


SETTINGS = Settings(
    ado_org_url="https://example.invalid",
    ado_project="P",
    ado_repo="R",
    ticket_tag="fixie",
    default_base_branch="dev",
    allowlist=ALLOW,
    claude_model="m",
    docker_image="i",
    mirror_volume="mv",
    gradle_ro_volume="gv",
    ticket_timeout_s=60,
    max_attempts=3,
    owner_email=OWNER_EMAIL,
    state_secret=SECRET,
    azure_token=None,
    claude_oauth_token=None,
    ado_feed_token=None,
)

FINISHED = FixieStatus(run_started=_t("10:00"), last_finished_run_started=_t("10:00"), result=RunStatus.DONE, attempts=0)


def _snapshot(ticket_id, status=None):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    if status is not None:
        gw.add_comment(ticket_id, format_status_comment(ticket_id, status, SECRET))
    return gw, load_snapshot(gw, ticket_id)


def _trusted(snap):
    return trusted_status(find_status_comment(list(snap.comments), OWNER_EMAIL), snap.ticket.id, OWNER_EMAIL, SECRET)


def _classify(ticket_id, status=None, settings=SETTINGS, state=None):
    gw, snap = _snapshot(ticket_id, status)
    if state is not None:
        snap = replace(snap, ticket=replace(snap.ticket, state=state))
    return classify(snap, _trusted(snap), settings)


def test_new_ticket():
    assert _classify(301) == Classification(Action.NEW, ())
    assert _classify(301, FixieStatus()).action == Action.NEW


def test_skip_no_new_events():
    assert _classify(302, replace(FINISHED, head="s302")) == Classification(Action.SKIP, ())


def test_skip_needs_info_without_reply():
    assert _classify(302, replace(FINISHED, result=RunStatus.NEEDS_INFO, head="s302")) == Classification(Action.SKIP, ())


def test_resume_on_new_events():
    r = _classify(303, replace(FINISHED, pr_id=31, head="s303"))
    assert r.action == Action.RESUME
    assert _ev(r.events) == [(EventType.PR_COMMENT, "alice@example.com", _t("11:00"), "Please rename")]


def test_rebuild_from_pr_and_summary():
    r = _classify(304)
    assert r.action == Action.REBUILD
    assert _ev(r.events) == [(EventType.TICKET_COMMENT, "alice@example.com", _t("11:00"), "<p>After summary</p>")]


def test_rebuild_from_branch_only():
    r = _classify(306)
    assert r.action == Action.REBUILD
    assert _ev(r.events) == [(EventType.PR_PUSH, "owner@example.com", _t("10:20"), "s306 WIP")]


def test_rebuild_untrusted_status():
    gw, snap = _snapshot(305)
    c = find_status_comment(list(snap.comments), OWNER_EMAIL)
    assert c.id == 3051
    assert trusted_status(c, 305, OWNER_EMAIL, SECRET) is None
    r = _classify(305)
    assert r.action == Action.REBUILD
    assert _ev(r.events) == [(EventType.TICKET_COMMENT, "bob@example.com", _t("10:00"), "<p>\U0001f916 fixie status</p>")]


def test_retry_unfinished_run():
    r = _classify(307, replace(FINISHED, run_started=_t("11:00"), attempts=1))
    assert r.action == Action.RETRY
    assert _ev(r.events) == [(EventType.TICKET_COMMENT, "alice@example.com", _t("10:15"), "<p>During crashed run</p>")]


def test_give_up_after_max_attempts():
    r = _classify(307, replace(FINISHED, run_started=_t("11:00"), attempts=3))
    assert r.action == Action.GIVE_UP
    assert _ev(r.events) == [(EventType.TICKET_COMMENT, "alice@example.com", _t("10:15"), "<p>During crashed run</p>")]


def test_retry_limit_from_settings():
    settings = replace(SETTINGS, max_attempts=5)
    r = _classify(307, replace(FINISHED, run_started=_t("11:00"), attempts=3), settings)
    assert r.action == Action.RETRY
    r = _classify(307, replace(FINISHED, run_started=_t("11:00"), attempts=5), settings)
    assert r.action == Action.GIVE_UP


def test_first_run_crash_retry_has_no_events():
    assert _classify(307, FixieStatus(run_started=_t("11:00"), attempts=1)) == Classification(Action.RETRY, ())


@pytest.mark.parametrize("state", ["Closed", "Removed", "Ready for testing", "Testing", "Ready for Release"])
def test_non_open_states_wait_whatever_the_pr(state):
    assert _classify(308, replace(FINISHED, pr_id=38), state=state) == Classification(Action.SKIP, ())
    assert _classify(309, replace(FINISHED, pr_id=39), state=state) == Classification(Action.SKIP, ())


def test_stop_tag_removed():
    assert _classify(310, FINISHED) == Classification(Action.STOP, ())


def test_tag_match_trimmed_case_insensitive():
    gw, snap = _snapshot(303, replace(FINISHED, pr_id=31, head="s303"))
    snap2 = replace(snap, ticket=replace(snap.ticket, tags=(" Fixie ",)))
    assert classify(snap2, _trusted(snap), SETTINGS).action == Action.RESUME


def test_classify_is_pure():
    gw, snap = _snapshot(303, replace(FINISHED, pr_id=31, head="s303"))
    n = len(gw.writes)
    a = classify(snap, _trusted(snap), SETTINGS)
    b = classify(snap, _trusted(snap), SETTINGS)
    assert a == b
    assert len(gw.writes) == n
