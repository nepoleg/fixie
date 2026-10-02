import json
import logging
import random
import re
import threading
import time
from dataclasses import replace
from pathlib import Path

import docker
import pytest

import fixie.runner as runner
from fixie.model import RunResult, RunStatus
from fixie.runner import (
    RunnerError,
    delete_ticket_volume,
    ensure_ticket_volume,
    kill_ticket,
    run_ticket,
    running_tickets,
    session_id,
    ticket_volumes,
)
from fixie.settings import Settings
from test_agent_image import INPUT, _docker_ok, _inspect, _run, _uuid8, image, mirror  # noqa: F401

pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _docker_ok(), reason="Docker engine not reachable")]

FAKE = {"FIXIE_FAKE_AGENT": "1", "FIXIE_FIREWALL": "off"}


def settings(mirror):
    return Settings(
        ado_org_url="https://dev.azure.com/org",
        ado_project="P",
        ado_repo="R",
        ticket_tag="fixie",
        default_base_branch="dev",
        allowlist=(),
        claude_model="haiku",
        docker_image="fixie-agent:latest",
        mirror_volume=mirror[0],
        gradle_ro_volume=f"fixie-test-nogradle-{_uuid8()}",
        ticket_timeout_s=120,
        max_attempts=3,
        owner_email="owner@example.com",
        state_secret="s",
        azure_token=None,
        claude_oauth_token=None,
        ado_feed_token=None,
    )


@pytest.fixture
def ticket(monkeypatch):
    monkeypatch.setattr(runner, "TICKET_VOLUME_PREFIX", "fixie-test-ticket-")
    tid = str(random.randint(100_000_000, 999_999_999))
    yield tid
    kill_ticket(tid)
    delete_ticket_volume(tid)


def _in(tmp_path, files):
    in_dir = tmp_path / f"in-{_uuid8()}"
    for name, content in files.items():
        path = in_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
    return in_dir


def _labeled(tid):
    return _run("docker", "ps", "-a", "--filter", f"label=fixie.ticket={tid}", "-q").stdout.strip()


def _in_thread(fn):
    box = {}

    def _target():
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=_target)
    thread.start()
    return thread, box


def _wait_running(tid, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tid in running_tickets():
            return
        time.sleep(0.5)
    assert tid in running_tickets()


def test_start_run_returns_result_and_bundle(mirror, ticket, tmp_path):
    s = settings(mirror)
    o = run_ticket(ticket, _in(tmp_path, INPUT), "start", "dev", s, extra_env=FAKE)

    assert o.exit_code == 0
    assert o.timed_out is False
    assert o.ok is True
    assert o.result == RunResult(RunStatus.DONE, "Fake agent appended a line to FIXIE_FAKE.md.", ())
    assert o.has_bundle is True
    assert o.volume == f"fixie-test-ticket-{ticket}"
    assert json.loads(o.claude_json)["session_id"] == session_id(ticket)
    assert b'"mode":"start"' in o.session_log

    inspect = _inspect(o.volume, "cd ~/work && git bundle list-heads ~/out/branch.bundle")
    assert f"refs/heads/fixie/{ticket}" in inspect.stdout
    assert _labeled(ticket) == ""


def test_resume_run_on_same_volume(mirror, ticket, tmp_path):
    s = settings(mirror)
    o1 = run_ticket(ticket, _in(tmp_path, INPUT), "start", "dev", s, extra_env=FAKE)
    assert o1.ok

    o2 = run_ticket(ticket, _in(tmp_path, INPUT), "resume", "dev", s, extra_env=FAKE)
    assert o2.ok
    assert o2.has_bundle

    modes = [json.loads(line)["mode"] for line in o2.session_log.decode().splitlines()]
    assert modes == ["start", "resume"]

    inspect = _inspect(o2.volume, "cat ~/work/FIXIE_FAKE.md")
    assert inspect.stdout == "fake run start\nfake run resume\n"


def test_inputs_replaced_each_run(mirror, ticket, tmp_path):
    s = settings(mirror)
    in1 = {"ticket.json": "v1\n", "stale.txt": "stale\n", "sub/a.png": b"png"}
    o1 = run_ticket(ticket, _in(tmp_path, in1), "start", "dev", s, extra_env=FAKE)
    assert o1.ok

    inspect1 = _inspect(o1.volume, "cd ~/in && find . | sort")
    assert inspect1.stdout.splitlines() == [
        ".", "./stale.txt", "./sub", "./sub/a.png", "./ticket.json",
    ]

    in2 = {"ticket.json": "v2\n"}
    o2 = run_ticket(ticket, _in(tmp_path, in2), "resume", "dev", s, extra_env=FAKE)
    assert o2.ok

    inspect2 = _inspect(
        o2.volume, "cd ~/in && find . | sort; cat ticket.json; stat -c '%U %a' ~/in ~/in/ticket.json"
    )
    assert inspect2.stdout.splitlines() == [".", "./ticket.json", "v2", "agent 755", "agent 644"]


def test_missing_result_is_failed_run(mirror, ticket, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    s = settings(mirror)
    o = run_ticket(ticket, _in(tmp_path, INPUT), "start", "dev", s, extra_env={**FAKE, "FIXIE_FAKE_RESULT": "0"})

    assert o.exit_code == 0
    assert o.result is None
    assert o.ok is False
    assert o.claude_json is not None
    assert f"runner: ticket {ticket} result.json missing or invalid" in caplog.text


def test_invalid_input_reported(mirror, ticket, tmp_path):
    s = settings(mirror)
    o = run_ticket(ticket, _in(tmp_path, {"other.txt": "x\n"}), "start", "dev", s, extra_env=FAKE)

    assert o.exit_code == 2
    assert o.result is None
    assert o.has_bundle is False
    assert o.claude_json is None
    assert o.ok is False


def test_agent_exit_code_reported(mirror, ticket, tmp_path):
    s = settings(mirror)
    o = run_ticket(ticket, _in(tmp_path, INPUT), "start", "dev", s, extra_env={**FAKE, "FIXIE_FAKE_EXIT": "7"})

    assert o.exit_code == 4
    assert o.result.status == RunStatus.DONE
    assert o.has_bundle is True
    assert o.ok is False


def test_timeout_kills_and_removes_container(mirror, ticket, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    s = replace(settings(mirror), ticket_timeout_s=15)
    t0 = time.monotonic()

    thread, box = _in_thread(
        lambda: run_ticket(
            ticket, _in(tmp_path, INPUT), "start", "dev", s, extra_env={**FAKE, "FIXIE_FAKE_SLEEP": "300"}
        )
    )
    _wait_running(ticket)

    names = _run(
        "docker", "ps", "--filter", f"label=fixie.ticket={ticket}", "--format", "{{.Names}}"
    ).stdout.strip()
    assert re.match(rf"^fixie-{ticket}-[0-9a-f]{{8}}$", names)

    thread.join(timeout=120)
    assert "error" not in box, box.get("error")
    o = box["result"]

    assert o.timed_out is True
    assert o.exit_code == 137
    assert o.result is None
    assert o.ok is False
    assert time.monotonic() - t0 < 90
    assert _labeled(ticket) == ""
    assert ticket not in running_tickets()
    assert f"runner: ticket {ticket} timed out after 15s, killed" in caplog.text


def test_kill_ticket_stops_run(mirror, ticket, tmp_path):
    s = settings(mirror)
    thread, box = _in_thread(
        lambda: run_ticket(
            ticket, _in(tmp_path, INPUT), "start", "dev", s, extra_env={**FAKE, "FIXIE_FAKE_SLEEP": "300"}
        )
    )
    _wait_running(ticket)

    kill_ticket(ticket)
    thread.join(timeout=60)

    assert isinstance(box.get("error"), RunnerError)
    assert _labeled(ticket) == ""
    assert ticket not in running_tickets()


@pytest.mark.parametrize("case", ["gradle", "nogradle"])
def test_container_config(mirror, ticket, tmp_path, monkeypatch, caplog, case):
    caplog.set_level(logging.DEBUG)
    s = replace(settings(mirror), claude_oauth_token="dummy-claude-8472", ado_feed_token="dummy-feed-8472")

    gvol = None
    if case == "gradle":
        gvol = f"fixie-test-gradle-{_uuid8()}"
        _run("docker", "volume", "create", gvol)
        s = replace(s, gradle_ro_volume=gvol)

    calls = []
    real_create = docker.models.containers.ContainerCollection.create

    def wrapper(self, image, command=None, **kwargs):
        calls.append((image, command, kwargs))
        return real_create(self, image, command=command, **kwargs)

    monkeypatch.setattr(docker.models.containers.ContainerCollection, "create", wrapper)

    try:
        o = run_ticket(ticket, _in(tmp_path, INPUT), "start", "dev", s, extra_env=FAKE)

        assert len(calls) == 1
        image_arg, command_arg, kwargs = calls[0]
        assert image_arg == "fixie-agent:latest"
        assert command_arg is None
        assert re.match(rf"^fixie-{ticket}-[0-9a-f]{{8}}$", kwargs["name"])
        assert kwargs["labels"] == {"fixie.ticket": ticket}
        assert kwargs["cap_drop"] == ["ALL"]
        assert kwargs["cap_add"] == ["NET_ADMIN", "SETUID", "SETGID"]
        assert kwargs["security_opt"] == ["no-new-privileges"]
        assert kwargs["mem_limit"] == "8g"
        assert kwargs["memswap_limit"] == "8g"
        assert kwargs["nano_cpus"] == 4_000_000_000
        assert kwargs["pids_limit"] == 4096
        assert "privileged" not in kwargs
        assert "network_mode" not in kwargs

        expected_volumes = [f"{mirror[0]}:/mirror:ro", f"fixie-test-ticket-{ticket}:/home/agent:rw"]
        if case == "gradle":
            expected_volumes.append(f"{gvol}:/gradle-ro:ro")
        assert kwargs["volumes"] == expected_volumes

        assert kwargs["environment"] == {
            "TICKET_ID": ticket,
            "BASE_BRANCH": "dev",
            "SESSION_ID": session_id(ticket),
            "RUN_MODE": "start",
            "CLAUDE_MODEL": "haiku",
            "CLAUDE_CODE_OAUTH_TOKEN": "dummy-claude-8472",
            "AZURE_PERSONAL_ACCESS_TOKEN": "dummy-feed-8472",
            "ADO_ORG_URL": "https://dev.azure.com/org",
            "ADO_PROJECT": "P",
            "ADO_REPO": "R",
            "FIXIE_FAKE_AGENT": "1",
            "FIXIE_FIREWALL": "off",
        }
        assert o.ok

        assert "dummy-claude-8472" not in caplog.text
        assert "dummy-feed-8472" not in caplog.text
        assert "dummy-claude-8472" not in repr(o)
        assert "dummy-feed-8472" not in repr(o)

        if case == "nogradle":
            assert f"runner: gradle volume {s.gradle_ro_volume} missing, not mounted" in caplog.text
            inspect = _run("docker", "volume", "inspect", s.gradle_ro_volume)
            assert inspect.returncode != 0
    finally:
        if gvol is not None:
            _run("docker", "volume", "rm", "-f", gvol)


def test_volume_lifecycle(ticket):
    v = ensure_ticket_volume(ticket)
    assert v == f"fixie-test-ticket-{ticket}"
    assert ensure_ticket_volume(ticket) == v

    label = _run(
        "docker", "volume", "inspect", "-f", '{{index .Labels "fixie.ticket"}}', v
    ).stdout.strip()
    assert label == ticket

    delete_ticket_volume(ticket)
    assert _run("docker", "volume", "inspect", v).returncode != 0

    delete_ticket_volume(ticket)


def test_ticket_volumes_lists_labelled(ticket):
    ensure_ticket_volume(ticket)
    assert ticket in ticket_volumes()

    delete_ticket_volume(ticket)
    assert ticket not in ticket_volumes()


@pytest.mark.parametrize("case", ["nomirror", "noimage"])
def test_infra_error_raises(mirror, ticket, tmp_path, case):
    s = settings(mirror)
    s = replace(
        s,
        claude_oauth_token="dummy-claude-8472",
        ado_feed_token="dummy-feed-8472",
    )
    if case == "nomirror":
        name = f"fixie-test-nomirror-{_uuid8()}"
        s = replace(s, mirror_volume=name)
        with pytest.raises(RunnerError, match="mirror volume missing") as exc_info:
            run_ticket(ticket, _in(tmp_path, INPUT), "start", "dev", s, extra_env=FAKE)
        assert _run("docker", "volume", "inspect", name).returncode != 0
    else:
        s = replace(s, docker_image=f"fixie-test-noimage-{_uuid8()}:latest")
        with pytest.raises(RunnerError) as exc_info:
            run_ticket(ticket, _in(tmp_path, INPUT), "start", "dev", s, extra_env=FAKE)

    assert "dummy-claude-8472" not in str(exc_info.value)
    assert "dummy-feed-8472" not in str(exc_info.value)
    assert _labeled(ticket) == ""
