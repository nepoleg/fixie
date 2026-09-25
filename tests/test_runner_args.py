import re

import pytest

import fixie.runner as runner
from fixie.model import RunResult, RunStatus
from fixie.runner import RunOutcome, delete_ticket_volume, ensure_ticket_volume, kill_ticket, parse_result, run_ticket, session_id
from fixie.settings import Settings

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    def _boom():
        raise AssertionError("docker called")

    monkeypatch.setattr(runner.docker, "from_env", _boom)


def test_session_id_deterministic():
    first = session_id("42")
    assert first == "5d93bb24-81cd-535d-a2b2-565076f96b85"
    assert session_id("42") == first
    assert session_id("43") != first
    assert UUID_RE.match(first)


def test_parse_result_valid():
    assert parse_result(b'{"status": "needs_info", "summary": "s", "questions": ["q1"]}') == RunResult(
        RunStatus.NEEDS_INFO, "s", ("q1",)
    )
    assert parse_result(b'{"status": "done", "summary": "s", "questions": [], "extra": 1}') == RunResult(
        RunStatus.DONE, "s", ()
    )


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json",
        b"[]",
        b'{"summary": "s", "questions": []}',
        b'{"status": "maybe", "summary": "s", "questions": []}',
        b'{"status": "done", "summary": 5, "questions": []}',
        b'{"status": "done", "summary": "s", "questions": "q"}',
        b'{"status": "done", "summary": "s", "questions": [1]}',
        b'{"status": "done", "summary": "s"}',
        b"\xff\xfe",
    ],
)
def test_parse_result_invalid(data):
    assert parse_result(data) is None


def _settings():
    return Settings(
        ado_org_url="https://dev.azure.com/org",
        ado_project="P",
        ado_repo="R",
        ticket_tag="fixie",
        default_base_branch="dev",
        allowlist=(),
        claude_model="haiku",
        docker_image="fixie-agent:latest",
        mirror_volume="fixie-mirror",
        gradle_ro_volume="fixie-gradle-ro",
        ticket_timeout_s=120,
        max_attempts=3,
        owner_email="owner@example.com",
        state_secret="s",
        azure_token=None,
        claude_oauth_token=None,
        ado_feed_token=None,
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda d, s: run_ticket("1;id", d, "start", "dev", s),
        lambda d, s: run_ticket("", d, "start", "dev", s),
        lambda d, s: run_ticket("12345678901", d, "start", "dev", s),
        lambda d, s: run_ticket("42", d, "fresh", "dev", s),
        lambda d, s: run_ticket("42", d / "missing", "start", "dev", s),
        lambda d, s: delete_ticket_volume("1;id"),
        lambda d, s: kill_ticket("1;id"),
        lambda d, s: ensure_ticket_volume("../x"),
    ],
)
def test_rejects_bad_args(tmp_path, call):
    with pytest.raises(ValueError):
        call(tmp_path, _settings())


def test_outcome_ok():
    assert RunOutcome("1", "v", 0, False, RunResult(RunStatus.DONE, "s")).ok is True
    assert RunOutcome("1", "v", 4, False, RunResult(RunStatus.DONE, "s")).ok is False
    assert RunOutcome("1", "v", 0, True, RunResult(RunStatus.DONE, "s")).ok is False
    assert RunOutcome("1", "v", 0, False, None).ok is False
