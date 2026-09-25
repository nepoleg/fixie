import json
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from fixie.model import RunResult, RunStatus

AGENT_DIR = Path(__file__).parents[1] / "docker" / "agent"

FLAGS = [
    "--cap-drop", "ALL",
    "--cap-add", "NET_ADMIN",
    "--cap-add", "SETUID",
    "--cap-add", "SETGID",
    "--security-opt", "no-new-privileges",
]
SESSION = "3f1c2a9e-0000-4000-8000-000000000042"
TICKET_ENV = {
    "TICKET_ID": "42",
    "BASE_BRANCH": "dev",
    "SESSION_ID": SESSION,
    "RUN_MODE": "start",
    "FIXIE_FAKE_AGENT": "1",
    "FIXIE_FIREWALL": "off",
}
INPUT = {"ticket.md": "# Ticket 42\n\nAdd a line.\n", "new.json": "[]\n"}


def _docker_ok():
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, encoding="utf-8", timeout=20
        )
        return result.returncode == 0
    except Exception:
        return False


pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _docker_ok(), reason="Docker engine not reachable")]


def _uuid8():
    return uuid.uuid4().hex[:8]


def _run(*args, **kwargs):
    return subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", **kwargs)


def _git(*args, cwd=None):
    result = _run("git", "-c", "user.name=t", "-c", "user.email=t@t", *args, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture(scope="session")
def image():
    result = _run("docker", "build", "-t", "fixie-agent:latest", str(AGENT_DIR), timeout=1800)
    assert result.returncode == 0, result.stderr
    return "fixie-agent:latest"


@pytest.fixture(scope="session")
def mirror(image, tmp_path_factory):
    base = tmp_path_factory.mktemp("mirror")
    bare = base / "monorepo.git"
    work = base / "work"
    _git("init", "-q", "--bare", "-b", "dev", str(bare))
    _git("init", "-q", "-b", "dev", str(work))
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=work)
    _git("commit", "-q", "-m", "init", cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-q", "origin", "dev", cwd=work)
    _git("checkout", "-q", "-b", "fixie/77", cwd=work)
    (work / "existing.txt").write_text("x\n", encoding="utf-8")
    _git("add", "existing.txt", cwd=work)
    _git("commit", "-q", "-m", "existing work", cwd=work)
    _git("push", "-q", "origin", "fixie/77", cwd=work)

    vol = f"fixie-test-mirror-{_uuid8()}"
    cname = f"fixie-test-{_uuid8()}"
    _run("docker", "volume", "create", vol)
    result = _run("docker", "create", "--name", cname, "-v", f"{vol}:/mirror", "fixie-agent:latest")
    assert result.returncode == 0, result.stderr
    result = _run("docker", "cp", str(bare), f"{cname}:/mirror/monorepo.git")
    assert result.returncode == 0, result.stderr
    _run("docker", "rm", cname)

    yield vol, bare

    _run("docker", "volume", "rm", "-f", vol)


@pytest.fixture
def ticket_volume():
    vol = f"fixie-test-ticket-{_uuid8()}"
    _run("docker", "volume", "create", vol)
    yield vol
    _run("docker", "volume", "rm", "-f", vol)


def _run_ticket(mirror_vol, ticket_vol, env, in_files, tmp_path, pre_out=None):
    cname = f"fixie-test-{_uuid8()}"
    create_cmd = ["docker", "create", "--name", cname, "--network", "none", *FLAGS]
    for key, value in env.items():
        create_cmd += ["-e", f"{key}={value}"]
    create_cmd += ["-v", f"{mirror_vol}:/mirror:ro", "-v", f"{ticket_vol}:/home/agent", "fixie-agent:latest"]
    result = _run(*create_cmd)
    assert result.returncode == 0, result.stderr
    try:
        if in_files is not None:
            in_dir = tmp_path / f"in-{_uuid8()}" / "in"
            in_dir.mkdir(parents=True)
            for name, content in in_files.items():
                (in_dir / name).write_text(content, encoding="utf-8", newline="\n")
            result = _run("docker", "cp", f"{in_dir}/.", f"{cname}:/home/agent/in")
            assert result.returncode == 0, result.stderr
        if pre_out is not None:
            out_seed = tmp_path / f"preout-{_uuid8()}" / "out"
            out_seed.mkdir(parents=True)
            for name, content in pre_out.items():
                (out_seed / name).write_text(content, encoding="utf-8", newline="\n")
            result = _run("docker", "cp", f"{out_seed}/.", f"{cname}:/home/agent/out")
            assert result.returncode == 0, result.stderr

        run_result = _run("docker", "start", "-a", cname)
        inspect = _run("docker", "inspect", "-f", "{{.State.ExitCode}}", cname)
        rc = int(inspect.stdout.strip())
        host_out = tmp_path / f"out-{_uuid8()}"
        _run("docker", "cp", f"{cname}:/home/agent/out", str(host_out))
        return rc, run_result.stdout, run_result.stderr, host_out
    finally:
        _run("docker", "rm", "-f", cname)


def _bundle_commits(host_bare, bundle, ref):
    tmp_ref = f"check-{_uuid8()}"
    _git("-C", str(host_bare), "fetch", "-q", str(bundle), f"refs/heads/{ref}:refs/heads/{tmp_ref}")
    count = _git("-C", str(host_bare), "rev-list", "--count", f"dev..{tmp_ref}")
    subject = _git("-C", str(host_bare), "log", "-1", "--format=%s", tmp_ref)
    return int(count.stdout.strip()), subject.stdout.strip()


def _inspect(ticket_vol, script):
    return _run(
        "docker", "run", "--rm", "--network", "none", "-e", "FIXIE_FIREWALL=off", *FLAGS,
        "-v", f"{ticket_vol}:/home/agent", "fixie-agent:latest", "bash", "-c", script,
    )


def test_image_tools(image):
    result = _run(
        "docker", "run", "--rm", "--network", "none", "-e", "FIXIE_FIREWALL=off", *FLAGS,
        "fixie-agent:latest", "bash", "-c",
        "java -version 2>&1; git --version; node --version; claude --version; cat ~/.gradle/gradle.properties",
    )
    assert result.returncode == 0, result.stderr
    assert 'version "17.' in result.stdout
    assert "git version" in result.stdout
    assert any(line.startswith("v24.") for line in result.stdout.splitlines())
    assert "Claude Code" in result.stdout
    assert "org.gradle.java.installations.auto-download=false" in result.stdout


def test_gradle_wrapper_uses_preinstalled_distribution(image):
    # --no-validate-url: the wrapper task otherwise needs network to check the URL (see progress/09).
    script = (
        "mkdir /tmp/w && cd /tmp/w && touch settings.gradle && "
        "~/.gradle/wrapper/dists/gradle-8.12-bin/cetblhg4pflnnks72fxwobvgv/gradle-8.12/bin/gradle "
        "-q --no-daemon wrapper --gradle-distribution-url "
        "https://services.gradle.org/distributions/gradle-8.12-bin.zip --no-validate-url && "
        "./gradlew -q --no-daemon --offline help && ./gradlew --no-daemon --version && "
        "test ! -e ~/.gradle/wrapper/dists/gradle-8.12-bin/cetblhg4pflnnks72fxwobvgv/gradle-8.12-bin.zip"
    )
    result = _run(
        "docker", "run", "--rm", "--network", "none", "-e", "FIXIE_FIREWALL=off", *FLAGS,
        "fixie-agent:latest", "bash", "-c", script,
    )
    assert result.returncode == 0, result.stderr
    assert "Gradle 8.12" in result.stdout


def test_agent_runs_unprivileged(image):
    result = _run(
        "docker", "run", "--rm", "--network", "none", "-e", "FIXIE_FIREWALL=off", *FLAGS,
        "fixie-agent:latest", "bash", "-c",
        'id -un; id -u; grep -E "^(CapPrm|CapEff|NoNewPrivs):" /proc/self/status',
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "agent"
    assert lines[1] != "0"
    assert "CapPrm:\t0000000000000000" in result.stdout
    assert "CapEff:\t0000000000000000" in result.stdout
    assert "NoNewPrivs:\t1" in result.stdout


def test_firewall_fails_closed_without_network(image):
    result = _run("docker", "run", "--rm", "--network", "none", *FLAGS, "fixie-agent:latest", "echo", "SHOULD_NOT_RUN")
    assert result.returncode != 0
    assert "SHOULD_NOT_RUN" not in result.stdout
    assert "fixie-firewall: cannot resolve api.anthropic.com" in result.stderr


def test_firewall_allows_and_blocks(image):
    probe = _run(
        "docker", "run", "--rm", "-e", "FIXIE_FIREWALL=off", *FLAGS,
        "fixie-agent:latest", "curl", "-sS", "-o", "/dev/null", "--max-time", "10", "https://api.anthropic.com",
    )
    if probe.returncode != 0:
        pytest.skip("no internet from containers")

    result = _run(
        "docker", "run", "--rm", *FLAGS, "fixie-agent:latest", "bash", "-c",
        "curl -sS -o /dev/null --max-time 10 https://api.anthropic.com && echo ALLOWED_OK; "
        "curl -sS -o /dev/null --max-time 10 https://example.com || echo BLOCKED_OK; "
        "iptables -P OUTPUT ACCEPT 2>/dev/null || echo AGENT_IPT_DENIED",
    )
    assert result.returncode == 0, result.stderr
    assert "ALLOWED_OK" in result.stdout
    assert "BLOCKED_OK" in result.stdout
    assert "AGENT_IPT_DENIED" in result.stdout
    assert "fixie-firewall: self-test ok" in result.stderr
    assert "fixie-firewall: ipv6 blocked" in result.stderr
    assert re.search(r"fixie-firewall: allowed [1-9][0-9]* addresses", result.stderr)


def test_firewall_setup_failure_maps_to_exit_1(image):
    result = _run(
        "docker", "run", "--rm", "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID",
        "--security-opt", "no-new-privileges", "fixie-agent:latest", "echo", "SHOULD_NOT_RUN",
    )
    assert result.returncode == 1
    assert "SHOULD_NOT_RUN" not in result.stdout
    assert "fixie-firewall:" in result.stderr


def test_start_run_produces_outputs(mirror, ticket_volume, tmp_path):
    mirror_vol, host_bare = mirror
    rc, _out, err, out_dir = _run_ticket(mirror_vol, ticket_volume, TICKET_ENV, INPUT, tmp_path)
    assert rc == 0, err
    assert sorted(p.name for p in out_dir.iterdir()) == ["branch.bundle", "claude.json", "result.json", "session.jsonl"]

    data = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    assert set(data) == {"status", "summary", "questions"}
    run_result = RunResult(status=RunStatus(data["status"]), summary=data["summary"], questions=tuple(data["questions"]))
    assert run_result.status == RunStatus.DONE

    claude_json = json.loads((out_dir / "claude.json").read_text(encoding="utf-8"))
    assert claude_json["session_id"] == SESSION

    session_lines = (out_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(session_lines) == 1
    assert json.loads(session_lines[0])["sessionId"] == SESSION

    assert _bundle_commits(host_bare, out_dir / "branch.bundle", "fixie/42") == (1, "fake: update FIXIE_FAKE.md")

    # --no-walk: avoids needing /mirror's object store for the parent commit (see progress/09).
    inspect = _inspect(
        ticket_volume,
        "test -f ~/.gradle/gradle.properties && git -C ~/work rev-parse --abbrev-ref HEAD && "
        "git -C ~/work log -1 --no-walk --format=%an",
    )
    assert inspect.returncode == 0, inspect.stderr
    lines = inspect.stdout.splitlines()
    assert lines[0] == "fixie/42"
    assert lines[1] == "fixie"


def test_resume_run_adds_commit_and_clears_out(mirror, ticket_volume, tmp_path):
    mirror_vol, host_bare = mirror
    _run_ticket(mirror_vol, ticket_volume, TICKET_ENV, INPUT, tmp_path)

    resume_env = {**TICKET_ENV, "RUN_MODE": "resume"}
    resume_in = {"ticket.md": INPUT["ticket.md"], "new.json": '[{"type": "ticket_comment", "content": "one more"}]\n'}
    rc, _out, err, out_dir = _run_ticket(
        mirror_vol, ticket_volume, resume_env, resume_in, tmp_path, pre_out={"stale.txt": "x"}
    )
    assert rc == 0, err
    assert sorted(p.name for p in out_dir.iterdir()) == ["branch.bundle", "claude.json", "result.json", "session.jsonl"]
    count, _subject = _bundle_commits(host_bare, out_dir / "branch.bundle", "fixie/42")
    assert count == 2
    session_lines = (out_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(session_lines) == 2
    assert "starting fresh" not in err


def test_resume_without_session_starts_fresh(mirror, ticket_volume, tmp_path):
    mirror_vol, host_bare = mirror
    env = {**TICKET_ENV, "RUN_MODE": "resume"}
    rc, _out, err, out_dir = _run_ticket(mirror_vol, ticket_volume, env, INPUT, tmp_path)
    assert rc == 0, err
    assert "run-ticket: nothing to resume, starting fresh" in err
    count, _subject = _bundle_commits(host_bare, out_dir / "branch.bundle", "fixie/42")
    assert count == 1


def test_start_continues_existing_fixie_branch(mirror, ticket_volume, tmp_path):
    mirror_vol, host_bare = mirror
    env = {**TICKET_ENV, "TICKET_ID": "77", "SESSION_ID": "3f1c2a9e-0000-4000-8000-000000000077"}
    rc, _out, err, out_dir = _run_ticket(mirror_vol, ticket_volume, env, INPUT, tmp_path)
    assert rc == 0, err
    assert _bundle_commits(host_bare, out_dir / "branch.bundle", "fixie/77") == (2, "fake: update FIXIE_FAKE.md")


def test_missing_base_branch_fails_git_setup(mirror, ticket_volume, tmp_path):
    mirror_vol, _host_bare = mirror
    env = {**TICKET_ENV, "BASE_BRANCH": "no-such-branch"}
    rc, _out, err, out_dir = _run_ticket(mirror_vol, ticket_volume, env, INPUT, tmp_path)
    assert rc == 3
    assert "run-ticket: git setup failed" in err
    assert list(out_dir.iterdir()) == []


def test_no_commits_no_bundle(mirror, ticket_volume, tmp_path):
    mirror_vol, _host_bare = mirror
    env = {**TICKET_ENV, "FIXIE_FAKE_COMMIT": "0"}
    rc, _out, err, out_dir = _run_ticket(mirror_vol, ticket_volume, env, INPUT, tmp_path)
    assert rc == 0, err
    assert sorted(p.name for p in out_dir.iterdir()) == ["claude.json", "result.json", "session.jsonl"]
    assert "run-ticket: no new commits, no bundle" in err


def test_agent_failure_still_collects_outputs(mirror, ticket_volume, tmp_path):
    mirror_vol, _host_bare = mirror
    env = {**TICKET_ENV, "FIXIE_FAKE_EXIT": "7"}
    rc, _out, err, out_dir = _run_ticket(mirror_vol, ticket_volume, env, INPUT, tmp_path)
    assert rc == 4
    assert "run-ticket: agent exited with 7" in err
    assert sorted(p.name for p in out_dir.iterdir()) == ["branch.bundle", "claude.json", "result.json", "session.jsonl"]


def test_tokens_not_persisted(mirror, ticket_volume, tmp_path):
    mirror_vol, _host_bare = mirror
    env = {
        **TICKET_ENV,
        "CLAUDE_CODE_OAUTH_TOKEN": "sentinel-oauth-3141",
        "AZURE_PERSONAL_ACCESS_TOKEN": "sentinel-pat-2718",
    }
    rc, out, err, out_dir = _run_ticket(mirror_vol, ticket_volume, env, INPUT, tmp_path)
    assert rc == 0, err
    assert "sentinel-" not in out
    assert "sentinel-" not in err

    inspect = _inspect(
        ticket_volume,
        "grep -rIl sentinel- /home/agent /etc/gitconfig; git -C ~/work config --list --show-origin; echo DONE",
    )
    assert "sentinel-" not in inspect.stdout
    assert "DONE" in inspect.stdout

    for path in out_dir.rglob("*"):
        if path.is_file():
            assert "sentinel-" not in path.read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize(
    "changes,in_files,message",
    [
        ({"TICKET_ID": "1;id"}, INPUT, "run-ticket: invalid TICKET_ID"),
        ({"BASE_BRANCH": "../etc"}, INPUT, "run-ticket: invalid BASE_BRANCH"),
        ({"SESSION_ID": "not-a-uuid"}, INPUT, "run-ticket: invalid SESSION_ID"),
        ({"RUN_MODE": "restart"}, INPUT, "run-ticket: invalid RUN_MODE"),
        ({}, None, "run-ticket: missing /home/agent/in/ticket.md"),
    ],
)
def test_invalid_input_rejected(mirror, ticket_volume, tmp_path, changes, in_files, message):
    mirror_vol, _host_bare = mirror
    env = {**TICKET_ENV, **changes}
    rc, _out, err, out_dir = _run_ticket(mirror_vol, ticket_volume, env, in_files, tmp_path)
    assert rc == 2
    assert message in err
    assert list(out_dir.iterdir()) == []
