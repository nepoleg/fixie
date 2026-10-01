import gzip
import logging
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fixie.events import load_snapshot
from fixie.fake_gateway import FakeGateway
from fixie.helper import HelperError
from fixie.model import BOT_SIGN, FixieStatus, Identity, RunResult, RunStatus
from fixie.publisher import mark_run_started, publish, record_give_up
from fixie.runner import RunOutcome
from fixie.settings import Settings
from fixie.status_comment import find_status_comment, format_status_comment, trusted_status

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ado" / "publish.json"
OWNER = Identity("Owner", "owner@example.com")
OWNER_EMAIL = "owner@example.com"
SECRET = "secret"
_T = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)

SETTINGS = Settings(
    ado_org_url="https://example.invalid",
    ado_project="P",
    ado_repo="R",
    ticket_tag="fixie",
    default_base_branch="dev",
    allowlist=(),
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

LOCK = threading.Lock()


def _gateway():
    return FakeGateway(FIXTURE_PATH, OWNER)


def _status(gw, ticket_id):
    return trusted_status(find_status_comment(gw.get_comments(ticket_id), OWNER_EMAIL), ticket_id, OWNER_EMAIL, SECRET)


def _outcome(status=RunStatus.DONE, summary="Fixed it", questions=(), has_bundle=True, session_log=b'{"a":1}\n', ticket_id="501"):
    result = RunResult(status, summary, questions)
    return RunOutcome(ticket_id, f"fixie-ticket-{ticket_id}", 0, False, result, session_log=session_log, has_bundle=has_bundle)


def _fake_push(calls, sha="a" * 40):
    def push(cfg, ticket_id, volume):
        assert LOCK.locked()
        calls.append((cfg.remote_url, ticket_id, volume))
        return sha

    return push


def _crashing_push(calls):
    state = {"raised": False}

    def push(cfg, ticket_id, volume):
        assert LOCK.locked()
        calls.append((cfg.remote_url, ticket_id, volume))
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("boom")
        return "a" * 40

    return push


def _crash_after(gw, method_name):
    original = getattr(gw, method_name)

    def wrapper(*args, **kwargs):
        setattr(gw, method_name, original)
        result = original(*args, **kwargs)
        raise RuntimeError("boom")

    setattr(gw, method_name, wrapper)


def _crash_before(gw, method_name):
    original = getattr(gw, method_name)

    def wrapper(*args, **kwargs):
        setattr(gw, method_name, original)
        raise RuntimeError("boom")

    setattr(gw, method_name, wrapper)


def test_mark_run_started_creates_status_comment():
    gw = _gateway()

    status = mark_run_started(gw, SETTINGS, 501, None)

    assert [w[0] for w in gw.writes] == ["add_comment", "edit_comment"]
    comments = gw.get_comments(501)
    assert len(comments) == 1
    assert gw.writes[1][1]["comment_id"] == comments[0].id
    assert status.run_started == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert status.attempts == 1
    assert _status(gw, 501) == status


def test_mark_run_started_edits_trusted_in_place():
    gw = _gateway()
    existing = FixieStatus(run_started=_T, last_finished_run_started=_T, result=RunStatus.DONE, attempts=0)
    gw.add_comment(501, format_status_comment(501, existing, SECRET))
    comment_id = gw.get_comments(501)[0].id
    gw.writes.clear()
    expected_run_started = gw.now

    status = mark_run_started(gw, SETTINGS, 501, existing)

    assert [w[0] for w in gw.writes] == ["edit_comment", "edit_comment"]
    assert all(w[1]["comment_id"] == comment_id for w in gw.writes)
    assert len(gw.get_comments(501)) == 1
    assert status.attempts == 1
    assert status.run_started == expected_run_started
    assert _status(gw, 501) == status


def test_mark_run_started_edits_untrusted_in_place():
    gw = _gateway()
    stale = FixieStatus(run_started=_T, attempts=2)
    gw.add_comment(501, format_status_comment(501, stale, "other-secret"))
    comment_id = gw.get_comments(501)[0].id
    gw.writes.clear()

    status = mark_run_started(gw, SETTINGS, 501, None)

    assert [w[0] for w in gw.writes] == ["edit_comment", "edit_comment"]
    assert all(w[1]["comment_id"] == comment_id for w in gw.writes)
    assert len(gw.get_comments(501)) == 1
    assert status.attempts == 1
    assert _status(gw, 501) == status


def test_publish_done_happy_path():
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 501, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 501)
    calls = []
    outcome = _outcome(summary="Fixed <login> & tests")

    finished = publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push(calls), mirror_lock=LOCK)

    assert [w[0] for w in gw.writes] == ["create_pull_request", "link_work_item", "add_comment", "attach_file", "edit_comment"]
    pr = gw.get_pull_request(finished.pr_id)
    assert pr.source_branch == "fixie/501"
    assert pr.target_branch == "dev"
    assert pr.title == "fixie: #501 Fix the login bug"
    assert pr.description == "Fixed <login> & tests"
    assert pr.is_draft is False
    assert pr.work_item_ids == (501,)

    comment_text = gw.get_comments(501)[-1].text
    assert comment_text.startswith(f"<p>{BOT_SIGN} fixie: done</p>")
    assert "Fixed &lt;login&gt; &amp; tests" in comment_text
    assert "https://example.invalid/P/_git/R/pullrequest/10" in comment_text
    assert f"fixie-run:{status.run_started.isoformat()}" in comment_text

    attachment = gw.get_attachments(501)[-1]
    assert attachment.name == "fixie-501-20260301T120000Z.jsonl.gz"
    assert gzip.decompress(gw.download_attachment(attachment.url)) == b'{"a":1}\n'

    assert calls == [("https://example.invalid/P/_git/R", "501", "fixie-ticket-501")]

    expected = FixieStatus(status.run_started, status.run_started, RunStatus.DONE, 10, "a" * 40, None, None, 0)
    assert finished == expected
    assert _status(gw, 501) == finished


def test_publish_needs_info_posts_questions():
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 501, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 501)
    calls = []
    outcome = _outcome(
        status=RunStatus.NEEDS_INFO,
        summary="Need clarification",
        questions=("Which page?", "Use <b>?"),
        has_bundle=False,
    )

    finished = publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push(calls), mirror_lock=LOCK)

    assert calls == []
    assert [w[0] for w in gw.writes if w[0] in ("create_pull_request", "link_work_item")] == []
    comment_text = gw.get_comments(501)[-1].text
    assert f"<p>{BOT_SIGN} fixie: needs info</p>" in comment_text
    assert "<li>Which page?</li>" in comment_text
    assert "<li>Use &lt;b&gt;?</li>" in comment_text
    assert "pullrequest/" not in comment_text
    assert finished.result == RunStatus.NEEDS_INFO
    assert finished.attempts == 0


def test_publish_blocked_posts_reason():
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 501, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 501)
    outcome = _outcome(status=RunStatus.BLOCKED, summary="Needs DB access", has_bundle=False)

    finished = publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push([]), mirror_lock=LOCK)

    comment_text = gw.get_comments(501)[-1].text
    assert f"<p>{BOT_SIGN} fixie: blocked</p>" in comment_text
    assert "Needs DB access" in comment_text
    assert finished.result == RunStatus.BLOCKED


def test_no_bundle_no_push_no_pr():
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 501, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 501)
    calls = []
    outcome = _outcome(has_bundle=False)

    finished = publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push(calls), mirror_lock=LOCK)

    assert calls == []
    assert [w[0] for w in gw.writes if w[0] in ("create_pull_request", "link_work_item")] == []
    assert finished.pr_id is None
    assert finished.head is None


def test_push_without_commits_no_pr():
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 501, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 501)
    outcome = _outcome(has_bundle=True)

    finished = publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push([], sha=None), mirror_lock=LOCK)

    assert [w[0] for w in gw.writes if w[0] == "create_pull_request"] == []
    assert finished.head is None


def test_existing_active_pr_not_duplicated():
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 502, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 502)
    calls = []
    outcome = _outcome(ticket_id="502", has_bundle=True)

    finished = publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push(calls), mirror_lock=LOCK)

    assert [w[0] for w in gw.writes if w[0] == "create_pull_request"] == []
    assert [w for w in gw.writes if w[0] == "link_work_item"] == [("link_work_item", {"pr_id": 9, "ticket_id": 502})]
    comment_text = gw.get_comments(502)[-1].text
    assert "pullrequest/9" in comment_text
    assert finished.pr_id == 9
    assert finished.last_build_id == 40


def test_existing_pr_description_updated_on_push():
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 502, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 502)
    outcome = _outcome(ticket_id="502", summary="Fixed it", has_bundle=True)

    publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push([]), mirror_lock=LOCK)

    update_writes = [w for w in gw.writes if w[0] == "update_pull_request"]
    assert update_writes == [("update_pull_request", {"pr_id": 9, "title": "fixie: #502 Add export", "description": "Fixed it"})]
    assert gw.get_pull_request(9).description == "Fixed it"


def test_existing_pr_not_updated_without_push():
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 502, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 502)
    outcome = _outcome(ticket_id="502", has_bundle=False)

    publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push([]), mirror_lock=LOCK)

    assert [w for w in gw.writes if w[0] == "update_pull_request"] == []


def test_push_failure_leaves_status_unfinished(caplog):
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 501, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 501)
    outcome = _outcome(has_bundle=True)

    def push(cfg, ticket_id, volume):
        raise HelperError("helper import failed (exit 4): fixie-helper: push failed")

    with caplog.at_level(logging.WARNING, logger="fixie.publisher"):
        result = publish(gw, SETTINGS, snapshot, status, outcome, push=push, mirror_lock=LOCK)

    assert result is None
    assert gw.writes == []
    assert _status(gw, 501).last_finished_run_started is None
    assert "publisher: ticket 501 push failed" in caplog.text


@pytest.mark.parametrize(
    "outcome",
    [
        RunOutcome("501", "fixie-ticket-501", 4, False, RunResult(RunStatus.DONE, "s")),
        RunOutcome("501", "fixie-ticket-501", 0, False, None),
    ],
)
def test_outcome_not_ok_publishes_nothing(outcome, caplog):
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 501, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 501)
    calls = []

    with caplog.at_level(logging.INFO, logger="fixie.publisher"):
        result = publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push(calls), mirror_lock=LOCK)

    assert result is None
    assert calls == []
    assert gw.writes == []
    assert "publisher: ticket 501 run not ok, nothing published" in caplog.text


@pytest.mark.parametrize("step", ["push", "create_pull_request", "link_work_item", "add_comment", "attach_file", "edit_comment"])
def test_crash_after_step_then_rerun(step):
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 501, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 501)
    outcome = _outcome(has_bundle=True)
    calls = []

    if step == "push":
        push = _crashing_push(calls)
    else:
        push = _fake_push(calls)
        _crash_after(gw, step)

    with pytest.raises(RuntimeError):
        publish(gw, SETTINGS, snapshot, status, outcome, push=push, mirror_lock=LOCK)

    finished = publish(gw, SETTINGS, snapshot, status, outcome, push=push, mirror_lock=LOCK)

    assert finished is not None
    assert len([w for w in gw.writes if w[0] == "create_pull_request"]) == 1
    assert len([w for w in gw.writes if w[0] == "add_comment"]) == 1
    assert len([w for w in gw.writes if w[0] == "attach_file"]) == 1
    assert len([w for w in gw.writes if w[0] == "edit_comment"]) == 1
    pr = gw.get_pull_request(finished.pr_id)
    assert pr.work_item_ids == (501,)
    assert _status(gw, 501) == finished


def test_give_up_idempotent():
    gw = _gateway()
    status = FixieStatus(run_started=_T, attempts=3)
    gw.add_comment(501, format_status_comment(501, status, SECRET))
    gw.writes.clear()

    _crash_before(gw, "edit_comment")
    with pytest.raises(RuntimeError):
        record_give_up(gw, SETTINGS, 501, status)

    second = record_give_up(gw, SETTINGS, 501, status)
    writes_after_second = list(gw.writes)
    third = record_give_up(gw, SETTINGS, 501, status)

    add_comments = [w for w in gw.writes if w[0] == "add_comment"]
    assert len(add_comments) == 1
    text = add_comments[0][1]["text"]
    assert "fixie gave up after 3 attempts" in text
    assert "fixie-giveup:" in text
    assert second.result == RunStatus.GAVE_UP
    assert second.attempts == 3
    assert _status(gw, 501) == second
    assert gw.writes == writes_after_second
    assert third == second


def test_tokens_never_posted():
    settings = replace(
        SETTINGS,
        azure_token="dummy-azure-8472",
        claude_oauth_token="dummy-claude-8472",
        ado_feed_token="dummy-feed-8472",
    )
    gw = _gateway()
    status = mark_run_started(gw, settings, 501, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 501)
    tokens = (settings.azure_token, settings.claude_oauth_token, settings.ado_feed_token)
    summary = f"Fixed it using {settings.azure_token} and {settings.claude_oauth_token}"
    questions = (f"Need {settings.ado_feed_token}?",)
    session_log = f'{{"token":"{settings.azure_token}"}}\n'.encode()
    outcome = RunOutcome(
        "501",
        "fixie-ticket-501",
        0,
        False,
        RunResult(RunStatus.NEEDS_INFO, summary, questions),
        session_log=session_log,
        has_bundle=True,
    )

    publish(gw, settings, snapshot, status, outcome, push=_fake_push([]), mirror_lock=LOCK)

    for method, params in gw.writes:
        for value in params.values():
            if isinstance(value, str):
                for token in tokens:
                    assert token not in value
            if isinstance(value, bytes) and method == "attach_file":
                content = gzip.decompress(value).decode()
                for token in tokens:
                    assert token not in content

    comment_text = gw.get_comments(501)[-1].text
    assert "[redacted]" in comment_text
    attachment = gw.get_attachments(501)[-1]
    log = gzip.decompress(gw.download_attachment(attachment.url)).decode()
    assert "[redacted]" in log


@pytest.mark.parametrize("bad", ["ticket_id", "run_started"])
def test_publish_rejects_bad_args(bad):
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 501, None)
    gw.writes.clear()
    snapshot = load_snapshot(gw, 501)
    outcome = _outcome()
    if bad == "ticket_id":
        outcome = replace(outcome, ticket_id="999")
    else:
        status = replace(status, run_started=None)

    with pytest.raises(ValueError):
        publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push([]), mirror_lock=LOCK)
    assert gw.writes == []


def test_mark_run_started_moves_new_ticket_to_developing():
    gw = _gateway()
    gw.tickets[501] = replace(gw.tickets[501], state="New")
    status = mark_run_started(gw, SETTINGS, 501, None, "New")
    assert status.state == "Developing"
    assert gw.get_ticket(501).state == "Developing"
    methods = [w[0] for w in gw.writes]
    assert methods.index("set_state") > methods.index("add_comment")
    assert _status(gw, 501).state == "Developing"


@pytest.mark.parametrize("state", ["Developing", "Code Review"])
def test_mark_run_started_leaves_later_states(state):
    gw = _gateway()
    status = mark_run_started(gw, SETTINGS, 501, None, state)
    assert status.state is None
    assert "set_state" not in [w[0] for w in gw.writes]


def test_own_state_move_is_not_an_event():
    from fixie.events import collect_events

    gw = _gateway()
    gw.tickets[501] = replace(gw.tickets[501], state="New")
    status = mark_run_started(gw, SETTINGS, 501, None, "New")
    snapshot = load_snapshot(gw, 501)
    assert collect_events(snapshot, status, status.run_started, OWNER_EMAIL, ()) == ()


def _thread(thread_id, author, text, when):
    from fixie.model import PrComment, PrThread

    comment = PrComment(1, author, text, when, when, "text")
    return PrThread(thread_id, "active", None, None, (comment,), when, when)


def test_publish_replies_once_in_threads_with_new_comments():
    gw = _gateway()
    alice = Identity("Alice", "alice@example.com")
    old = datetime(2026, 2, 1, tzinfo=UTC)
    previous = FixieStatus(last_finished_run_started=datetime(2026, 2, 15, tzinfo=UTC), pr_id=9)
    gw.threads[9] = [
        _thread(70, alice, "please rename", datetime(2026, 2, 25, tzinfo=UTC)),
        _thread(71, alice, "old remark", old),
        _thread(72, OWNER, f"{BOT_SIGN} fixie: done", datetime(2026, 2, 26, tzinfo=UTC)),
    ]
    status = mark_run_started(gw, SETTINGS, 502, previous)
    snapshot = load_snapshot(gw, 502)
    outcome = _outcome(ticket_id="502", summary="Renamed it")

    publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push([]), mirror_lock=LOCK)
    publish(gw, SETTINGS, snapshot, status, outcome, push=_fake_push([]), mirror_lock=LOCK)

    replies = [w[1] for w in gw.writes if w[0] == "reply_to_thread"]
    assert [(r["pr_id"], r["thread_id"]) for r in replies] == [(9, 70)]
    text = replies[0]["text"]
    assert text.startswith(f"{BOT_SIGN} fixie: done") and "Renamed it" in text and "fixie-run:" in text
