import json
import os
import random
from dataclasses import replace
from pathlib import Path

import pytest

import fixie.runner as runner
from fixie.model import RunStatus
from fixie.runner import delete_ticket_volume, kill_ticket, run_ticket
from fixie.settings import load_settings
from test_agent_image import FLAGS, _docker_ok, _git, _run, _uuid8, image  # noqa: F401

REPO_ROOT = Path(__file__).parents[1]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("FIXIE_LIVE") != "1", reason="set FIXIE_LIVE=1 to run with the real Claude"),
    pytest.mark.skipif(not _docker_ok(), reason="Docker engine not reachable"),
]

CALC = """public class Calc {
    public static int add(int a, int b) {
        return a - b;
    }
}
"""
CALC_TEST = """public class CalcTest {
    public static void main(String[] args) {
        check(Calc.add(2, 3) == 5, "add(2, 3) == 5");
        System.out.println("OK");
    }

    static void check(boolean ok, String what) {
        if (!ok) {
            System.out.println("FAIL: " + what);
            System.exit(1);
        }
    }
}
"""
README = "# Calc\n\nBuild and test:\n\n    javac -d build *.java && java -cp build CalcTest\n"
TEST_CMD = "cd ~/work && rm -rf /tmp/b && javac -d /tmp/b *.java && java -cp /tmp/b CalcTest"

TICKET = """# 9001: Calc.add returns a wrong result

- Type: Bug
- State: Active
- Tags: fixie
- Base branch: dev

## Description

> `Calc.add(2, 3)` returns `-1`, it should return `5`. Fix it and keep `CalcTest` passing.
"""
REVIEW = [
    {
        "type": "pr_comment",
        "author": "Owner <owner@example.com>",
        "time": "2026-09-30T10:00:00Z",
        "content": "/Calc.java: please also add `static int multiply(int a, int b)` and a check for it in CalcTest.",
        "allowlisted": True,
    }
]


@pytest.fixture(scope="module")
def toy_mirror(image, tmp_path_factory):
    base = tmp_path_factory.mktemp("toy")
    bare, work = base / "monorepo.git", base / "work"
    _git("init", "-q", "--bare", "-b", "dev", str(bare))
    _git("init", "-q", "-b", "dev", str(work))
    for name, text in (("Calc.java", CALC), ("CalcTest.java", CALC_TEST), ("README.md", README)):
        (work / name).write_text(text, encoding="utf-8", newline="\n")
    (work / ".gitignore").write_text("build/\n", encoding="utf-8", newline="\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "toy calc", cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-q", "origin", "dev", cwd=work)

    vol, cname = f"fixie-test-toy-{_uuid8()}", f"fixie-test-{_uuid8()}"
    _run("docker", "volume", "create", vol)
    assert _run("docker", "create", "--name", cname, "-v", f"{vol}:/mirror", image).returncode == 0
    assert _run("docker", "cp", str(bare), f"{cname}:/mirror/monorepo.git").returncode == 0
    _run("docker", "rm", cname)
    yield vol
    _run("docker", "volume", "rm", "-f", vol)


@pytest.fixture(scope="module")
def live_settings(toy_mirror):
    s = load_settings(REPO_ROOT / "config.toml", REPO_ROOT / ".env")
    if not s.claude_oauth_token:
        pytest.skip("CLAUDE_CODE_OAUTH_TOKEN missing in .env")
    return replace(
        s,
        docker_image="fixie-agent:latest",
        mirror_volume=toy_mirror,
        gradle_ro_volume=f"fixie-test-nogradle-{_uuid8()}",
        ticket_timeout_s=900,
    )


@pytest.fixture(scope="module")
def ticket():
    old = runner.TICKET_VOLUME_PREFIX
    runner.TICKET_VOLUME_PREFIX = "fixie-test-ticket-"
    tid = str(random.randint(100_000_000, 999_999_999))
    yield tid
    kill_ticket(tid)
    delete_ticket_volume(tid)
    runner.TICKET_VOLUME_PREFIX = old


def _input(tmp_path, new_events):
    in_dir = tmp_path / f"in-{_uuid8()}"
    in_dir.mkdir()
    (in_dir / "ticket.md").write_text(TICKET, encoding="utf-8", newline="\n")
    (in_dir / "new.json").write_text(json.dumps(new_events, indent=2) + "\n", encoding="utf-8", newline="\n")
    return in_dir


def _assert_run_ok(o, tid):
    assert o.exit_code == 0, o
    assert not o.timed_out
    assert o.result is not None, "result.json missing or invalid"
    assert o.result.status == RunStatus.DONE, o.result
    assert o.result.summary.strip()
    assert o.has_bundle
    assert o.session_log
    claude = json.loads(o.claude_json)
    assert claude["is_error"] is False, claude
    assert claude["session_id"] == runner.session_id(tid)


def _sh(mirror, volume, script):
    result = _run(
        "docker", "run", "--rm", "--network", "none", "-e", "FIXIE_FIREWALL=off", *FLAGS,
        "-v", f"{mirror}:/mirror:ro", "-v", f"{volume}:/home/agent", "fixie-agent:latest", "bash", "-c", script,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _assert_tree(mirror, volume, tid):
    assert _sh(mirror, volume, TEST_CMD) == "OK"
    assert _sh(mirror, volume, "cd ~/work && git status --porcelain --untracked-files=no") == ""
    assert _sh(mirror, volume, "cd ~/work && git branch --show-current") == f"fixie/{tid}"


def test_real_claude_fixes_toy_bug_then_handles_review(live_settings, ticket, tmp_path):
    m = live_settings.mirror_volume
    o1 = run_ticket(ticket, _input(tmp_path, []), "start", "dev", live_settings)
    _assert_run_ok(o1, ticket)
    _assert_tree(m, o1.volume, ticket)
    assert "Calc.java" in _sh(m, o1.volume, f"cd ~/work && git diff --name-only origin/dev..fixie/{ticket}").splitlines()
    first = int(_sh(m, o1.volume, f"cd ~/work && git rev-list --count origin/dev..fixie/{ticket}"))
    assert first >= 1

    o2 = run_ticket(ticket, _input(tmp_path, REVIEW), "resume", "dev", live_settings)
    _assert_run_ok(o2, ticket)
    _assert_tree(m, o2.volume, ticket)
    assert "multiply" in _sh(m, o2.volume, "cat ~/work/Calc.java")
    assert "multiply" in _sh(m, o2.volume, "cat ~/work/CalcTest.java")
    assert int(_sh(m, o2.volume, f"cd ~/work && git rev-list --count origin/dev..fixie/{ticket}")) > first
    assert len(o2.session_log.splitlines()) > len(o1.session_log.splitlines()), "resume must extend the session"
