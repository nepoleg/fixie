import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fixie.classify import classify
from fixie.events import load_snapshot
from fixie.fake_gateway import FakeGateway
from fixie.model import Action, Classification, Comment, EventType, FixieStatus, Identity, PrComment, RunStatus
from fixie.render import parse_base_branch, write_input
from fixie.settings import Settings
from fixie.status_comment import find_status_comment, format_status_comment, trusted_status

FIXTURES = Path(__file__).parent / "fixtures" / "ado"

OWNER = Identity("Owner", "owner@example.com")
OWNER_EMAIL = "owner@example.com"
SECRET = "secret"
ALLOW = ("alice@example.com",)


def _t(hm):
    hour, minute = (int(x) for x in hm.split(":"))
    return datetime(2026, 3, 1, hour, minute, tzinfo=UTC)


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

STATUS = FixieStatus(
    run_started=_t("10:00"),
    last_finished_run_started=_t("10:00"),
    result=RunStatus.DONE,
    pr_id=42,
    head="c0ffee1",
    state="Code Review",
    attempts=0,
)


def _gw():
    return FakeGateway(FIXTURES / "render.json", OWNER)


def _resumed_gw():
    gw = _gw()
    gw.add_comment(402, format_status_comment(402, STATUS, SECRET))
    return gw


def _render(gw, ticket_id, target):
    snap = load_snapshot(gw, ticket_id)
    status = trusted_status(find_status_comment(list(snap.comments), OWNER_EMAIL), ticket_id, OWNER_EMAIL, SECRET)
    c = classify(snap, status, SETTINGS)
    write_input(snap, c.events, c.since, SETTINGS, target)
    return c, json.loads((target / "ticket.json").read_text(encoding="utf-8"))


def _files(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_new_ticket_manifest(tmp_path):
    target = tmp_path / "in"
    c, m = _render(_gw(), 401, target)
    assert c == Classification(Action.NEW, ())
    assert _files(target) == ["ticket.json"]
    assert m["ticket"]["id"] == 401
    assert m["ticket"]["tags"] == ["fixie", "backend"]
    assert m["ticket"]["description"]["format"] == "html"
    assert "Log failed logins" in m["ticket"]["description"]["text"]
    assert "repro_steps" not in m["ticket"]
    assert (m["branch"], m["base_branch"], m["pull_request"], m["since"], m["events"]) == (
        "fixie/401", "dev", None, None, [],
    )


def test_resumed_ticket_events_are_pointers(tmp_path):
    c, m = _render(_resumed_gw(), 402, tmp_path / "in")
    assert c.action == Action.RESUME
    assert m["since"] == "2026-03-01T10:00:00Z"
    assert m["pull_request"] == {"id": 42, "status": "active"}
    assert m["events"] == [
        {"type": "pr_comment", "time": "2026-03-01T10:30:00Z", "author": "alice@example.com",
         "allowlisted": True, "pr": 42, "thread": 1, "comment": 1},
        {"type": "ticket_comment", "time": "2026-03-01T10:40:00Z", "author": "carol@example.com",
         "allowlisted": False, "comment": 4023},
        {"type": "pr_comment", "time": "2026-03-01T10:45:00Z", "author": "alice@example.com",
         "allowlisted": True, "pr": 42, "thread": 1, "comment": 3},
    ]


def test_markdown_field_format(tmp_path):
    gw = _gw()
    gw.tickets[401] = replace(gw.tickets[401], description="a > b", markdown_fields=frozenset({"description"}))
    _, m = _render(gw, 401, tmp_path / "in")
    assert m["ticket"]["description"] == {"format": "markdown", "text": "a > b"}


def test_render_writes_nothing_to_ado(tmp_path):
    gw = _resumed_gw()
    n = len(gw.writes)
    _render(gw, 402, tmp_path / "a")
    _render(gw, 401, tmp_path / "b")
    assert len(gw.writes) == n


def test_parse_base_branch():
    cases = [
        ("<p>Base branch: release/2.0</p>", "release/2.0"),
        ("<div>base: feature/x-1</div>", "feature/x-1"),
        ("<p>BASE:   hotfix/7 </p>", "hotfix/7"),
        ("<p>base: <b>release/3</b></p>", "release/3"),
        ("<p>base:&nbsp;rel/4</p>", "rel/4"),
        ("line<br>base: rel/1<br/>more", "rel/1"),
        ("<p>base: one</p><p>base: two</p>", "one"),
        ("<p>base: -x</p><p>base: ok/1</p>", "ok/1"),
        ("<p>NPE</p>", "dev"),
        ("", "dev"),
        ("<p>the base: dev2 is wrong</p>", "dev"),
        ("<p>base: -x</p>", "dev"),
        ("<p>base: a..b</p>", "dev"),
        ("<p>base: a b</p>", "dev"),
        ("<p>base: rel/</p>", "dev"),
        ("<p>base: x.lock</p>", "dev"),
    ]
    for html, expected in cases:
        assert parse_base_branch(html, "dev") == expected
    assert parse_base_branch("<p>NPE</p>", "main") == "main"


def test_target_must_be_empty_or_missing(tmp_path):
    snap = load_snapshot(_gw(), 401)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x.txt").write_text("x")
    with pytest.raises(FileExistsError):
        write_input(snap, (), None, SETTINGS, busy)
    assert _files(busy) == ["x.txt"]
    nested = tmp_path / "a" / "b"
    write_input(snap, (), None, SETTINGS, nested)
    assert _files(nested) == ["ticket.json"]
