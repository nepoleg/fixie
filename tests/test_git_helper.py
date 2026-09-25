import logging
from dataclasses import replace
from pathlib import Path

import docker
import pytest

from fixie.helper import HelperConfig, HelperError, auth_env, create_or_refresh_mirror, import_and_push
from test_agent_image import FLAGS, INPUT, TICKET_ENV, _docker_ok, _run, _run_ticket, _uuid8, image  # noqa: F401

HELPER_DIR = Path(__file__).parents[1] / "docker" / "helper"
HELPER_IMAGE = "fixie-helper:latest"

pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _docker_ok(), reason="Docker engine not reachable")]


@pytest.fixture(scope="session")
def helper_image():
    result = _run("docker", "build", "-t", HELPER_IMAGE, str(HELPER_DIR), timeout=600)
    assert result.returncode == 0, result.stderr
    return HELPER_IMAGE


def _sh(volumes, script):
    cmd = ["docker", "run", "--rm", "--entrypoint", "sh"]
    for volume in volumes:
        cmd += ["-v", volume]
    cmd += [HELPER_IMAGE, "-c", script]
    return _run(*cmd)


def _git(*args, cwd=None):
    result = _run("git", "-c", "user.name=t", "-c", "user.email=t@t", *args, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def origin(helper_image, tmp_path):
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git("init", "-q", "--bare", "-b", "dev", str(bare))
    _git("init", "-q", "-b", "dev", str(work))
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=work)
    _git("commit", "-q", "-m", "init", cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-q", "origin", "dev", cwd=work)

    vol = f"fixie-test-origin-{_uuid8()}"
    cname = f"fixie-test-{_uuid8()}"
    _run("docker", "volume", "create", vol)
    result = _run("docker", "create", "--name", cname, "-v", f"{vol}:/origin", HELPER_IMAGE)
    assert result.returncode == 0, result.stderr
    result = _run("docker", "cp", str(bare), f"{cname}:/origin/origin.git")
    assert result.returncode == 0, result.stderr
    _run("docker", "rm", cname)
    _sh([f"{vol}:/origin"], "chown -R 0:0 /origin")

    yield vol

    _run("docker", "volume", "rm", "-f", vol)


@pytest.fixture
def mirror_volume():
    vol = f"fixie-test-mirror-{_uuid8()}"
    _run("docker", "volume", "create", vol)
    yield vol
    _run("docker", "volume", "rm", "-f", vol)


@pytest.fixture
def ticket_volume():
    vol = f"fixie-test-ticket-{_uuid8()}"
    _run("docker", "volume", "create", vol)
    yield vol
    _run("docker", "volume", "rm", "-f", vol)


@pytest.fixture
def ro_volume():
    vol = f"fixie-test-ro-{_uuid8()}"
    _run("docker", "volume", "create", vol)
    yield vol
    _run("docker", "volume", "rm", "-f", vol)


@pytest.fixture
def cfg(mirror_volume, ro_volume, origin):
    return HelperConfig(
        mirror_volume=mirror_volume,
        gradle_ro_volume=ro_volume,
        remote_url="file:///origin/origin.git",
        token="dummy-token-8472",
        extra_volumes=(f"{origin}:/origin:rw",),
    )


@pytest.fixture
def broken_image(tmp_path):
    tag = f"fixie-test-broken-{_uuid8()}:latest"
    build_dir = tmp_path / "broken-image"
    build_dir.mkdir()
    (build_dir / "Dockerfile").write_text('FROM alpine:3.22\nENTRYPOINT ["/no-such-binary"]\n', encoding="utf-8")
    result = _run("docker", "build", "-t", tag, str(build_dir))
    assert result.returncode == 0, result.stderr
    yield tag
    _run("docker", "rmi", "-f", tag)


def _origin_commit(origin, branch):
    script = (
        f"git clone -q /origin/origin.git /tmp/w && cd /tmp/w && "
        f"(git checkout -q {branch} 2>/dev/null || git checkout -q -b {branch} origin/dev) && "
        f"git -c user.name=t -c user.email=t@t commit -q --allow-empty -m more && "
        f"git push -q origin {branch} && git rev-parse HEAD"
    )
    result = _sh([f"{origin}:/origin"], script)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _agent_run(cfg, ticket_vol, tmp_path, **env):
    rc, out, err, out_dir = _run_ticket(cfg.mirror_volume, ticket_vol, {**TICKET_ENV, **env}, INPUT, tmp_path)
    assert rc == 0, err
    return rc, out, err, out_dir


def _ref(vol_mount, gitdir, ref):
    result = _sh([vol_mount], f"git -C {gitdir} rev-parse --verify -q {ref}")
    return result.stdout.strip()


def _spy(monkeypatch):
    calls = []
    orig_create = docker.models.containers.ContainerCollection.create

    def wrapper(self, image, command, **kwargs):
        calls.append({"image": image, "command": command, **kwargs})
        return orig_create(self, image, command, **kwargs)

    monkeypatch.setattr(docker.models.containers.ContainerCollection, "create", wrapper)
    return calls


def test_mirror_create_and_refresh(helper_image, cfg, origin, mirror_volume):
    _origin_commit(origin, "tmp")

    create_or_refresh_mirror(cfg)

    assert _ref(f"{mirror_volume}:/mirror", "/mirror/monorepo.git", "refs/heads/dev") == _ref(
        f"{origin}:/origin", "/origin/origin.git", "refs/heads/dev"
    )
    assert _sh([f"{mirror_volume}:/mirror"], "git -C /mirror/monorepo.git config gc.auto").stdout.strip() == "0"
    assert (
        _sh([f"{mirror_volume}:/mirror"], "git -C /mirror/monorepo.git config remote.origin.mirror").stdout.strip()
        == "true"
    )
    assert (
        _sh([f"{mirror_volume}:/mirror"], "git -C /mirror/monorepo.git config --get-regexp '^http\\.'").stdout.strip()
        == ""
    )

    sha = _origin_commit(origin, "dev")
    _sh([f"{origin}:/origin"], "git -C /origin/origin.git branch -D tmp")

    create_or_refresh_mirror(cfg)

    assert _ref(f"{mirror_volume}:/mirror", "/mirror/monorepo.git", "refs/heads/dev") == sha
    assert _ref(f"{mirror_volume}:/mirror", "/mirror/monorepo.git", "refs/heads/tmp") == ""
    assert (
        _sh([f"{mirror_volume}:/mirror"], "test -e /mirror/monorepo.git.tmp && echo EXISTS || echo GONE").stdout.strip()
        == "GONE"
    )


def test_import_and_push_publishes_bundle(helper_image, image, cfg, origin, mirror_volume, ticket_volume, tmp_path, monkeypatch):
    create_or_refresh_mirror(cfg)
    _agent_run(cfg, ticket_volume, tmp_path)

    expected = _sh(
        [f"{ticket_volume}:/ticket:ro"], "git bundle list-heads /ticket/out/branch.bundle"
    ).stdout.split()[0]

    calls = _spy(monkeypatch)

    sha = import_and_push(cfg, "42", ticket_volume)
    assert sha == expected

    assert _ref(f"{origin}:/origin", "/origin/origin.git", "refs/heads/fixie/42") == expected
    assert _ref(f"{mirror_volume}:/mirror", "/mirror/monorepo.git", "refs/heads/fixie/42") == expected

    import_calls = [c for c in calls if c["command"] == ["import", "42"]]
    assert len(import_calls) == 1
    call = import_calls[0]
    assert f"{ticket_volume}:/ticket:ro" in call["volumes"]
    assert call["cap_add"] == ["DAC_READ_SEARCH"]
    assert call["cap_drop"] == ["ALL"]

    sha2 = import_and_push(cfg, "42", ticket_volume)
    assert sha2 == sha


def test_import_ignores_agent_repo_config(helper_image, image, cfg, origin, mirror_volume, ticket_volume, tmp_path):
    create_or_refresh_mirror(cfg)
    _agent_run(cfg, ticket_volume, tmp_path)

    result = _run(
        "docker", "run", "--rm", "--network", "none", "-e", "FIXIE_FIREWALL=off", *FLAGS,
        "-v", f"{ticket_volume}:/home/agent", "fixie-agent:latest", "bash", "-c",
        "git -C ~/work config core.fsmonitor 'touch /mirror/PWNED' && git -C ~/work config core.hooksPath /tmp",
    )
    assert result.returncode == 0, result.stderr

    import_and_push(cfg, "42", ticket_volume)

    result = _sh([f"{mirror_volume}:/mirror"], "test ! -e /mirror/PWNED && echo CLEAN")
    assert result.stdout.strip() == "CLEAN"


def test_import_without_bundle_returns_none(helper_image, image, cfg, origin, ticket_volume, tmp_path):
    create_or_refresh_mirror(cfg)

    assert import_and_push(cfg, "42", ticket_volume) is None

    _agent_run(cfg, ticket_volume, tmp_path, FIXIE_FAKE_COMMIT="0")
    assert import_and_push(cfg, "42", ticket_volume) is None

    assert _ref(f"{origin}:/origin", "/origin/origin.git", "refs/heads/fixie/42") == ""


@pytest.mark.parametrize(
    "kind,expected_message",
    [
        ("extra_ref", "bundle refs rejected"),
        ("wrong_ref", "bundle refs rejected"),
        ("symlink", "bundle is not a regular file"),
        ("garbage", "bundle verify failed"),
    ],
)
def test_bad_bundle_rejected(
    helper_image, image, cfg, origin, mirror_volume, ticket_volume, tmp_path, kind, expected_message
):
    create_or_refresh_mirror(cfg)
    _agent_run(cfg, ticket_volume, tmp_path)

    scripts = {
        "extra_ref": (
            "git clone -q /mirror/monorepo.git /tmp/w && cd /tmp/w && "
            "git -c user.name=t -c user.email=t@t checkout -q -b fixie/42 origin/dev && "
            "git -c user.name=t -c user.email=t@t commit -q --allow-empty -m x && "
            "git -c user.name=t -c user.email=t@t checkout -q -b evil origin/dev && "
            "git -c user.name=t -c user.email=t@t commit -q --allow-empty -m y && "
            "rm -f /ticket/out/branch.bundle && "
            "git bundle create /ticket/out/branch.bundle dev..fixie/42 dev..evil"
        ),
        "wrong_ref": (
            "git clone -q /mirror/monorepo.git /tmp/w && cd /tmp/w && "
            "git -c user.name=t -c user.email=t@t checkout -q -b fixie/43 origin/dev && "
            "git -c user.name=t -c user.email=t@t commit -q --allow-empty -m x && "
            "rm -f /ticket/out/branch.bundle && "
            "git bundle create /ticket/out/branch.bundle dev..fixie/43"
        ),
        "symlink": "rm -f /ticket/out/branch.bundle && ln -s /mirror/monorepo.git/HEAD /ticket/out/branch.bundle",
        "garbage": "echo junk > /ticket/out/branch.bundle",
    }
    result = _sh([f"{ticket_volume}:/ticket:rw", f"{mirror_volume}:/mirror"], scripts[kind])
    assert result.returncode == 0, result.stderr

    with pytest.raises(HelperError) as exc_info:
        import_and_push(cfg, "42", ticket_volume)
    assert expected_message in str(exc_info.value)

    for ref in ("refs/heads/fixie/42", "refs/heads/fixie/43", "refs/heads/evil"):
        assert _ref(f"{origin}:/origin", "/origin/origin.git", ref) == ""


def test_push_not_forced(helper_image, image, cfg, origin, ticket_volume, tmp_path):
    create_or_refresh_mirror(cfg)
    _agent_run(cfg, ticket_volume, tmp_path)
    s1 = import_and_push(cfg, "42", ticket_volume)
    assert s1 is not None

    s2 = _origin_commit(origin, "fixie/42")

    with pytest.raises(HelperError) as exc_info:
        import_and_push(cfg, "42", ticket_volume)
    assert "push failed" in str(exc_info.value)

    assert _ref(f"{origin}:/origin", "/origin/origin.git", "refs/heads/fixie/42") == s2


def test_token_never_persisted_or_logged(
    helper_image, image, cfg, origin, mirror_volume, ticket_volume, tmp_path, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    calls = _spy(monkeypatch)

    create_or_refresh_mirror(cfg)
    _agent_run(cfg, ticket_volume, tmp_path)
    import_and_push(cfg, "42", ticket_volume)
    create_or_refresh_mirror(cfg)

    result = _sh(
        [f"{mirror_volume}:/mirror"],
        "grep -rl -e dummy-token-8472 -e OmR1bW15LXRva2VuLTg0NzI= /mirror; echo DONE",
    )
    assert result.stdout.strip() == "DONE"

    assert "dummy-token-8472" not in caplog.text
    assert "OmR1bW15LXRva2VuLTg0NzI=" not in caplog.text
    for call in calls:
        assert "dummy-token-8472" not in str(call["command"])
        assert "OmR1bW15LXRva2VuLTg0NzI=" not in str(call["command"])
        assert call["environment"]["GIT_CONFIG_VALUE_0"] == "Authorization: Basic OmR1bW15LXRva2VuLTg0NzI="


def test_auth_header_sent_not_in_argv(helper_image):
    env_pairs = auth_env("dummy-token-8472")
    cmd = ["docker", "run", "--rm", "--entrypoint", "sh"]
    for key, value in env_pairs.items():
        cmd += ["-e", f"{key}={value}"]
    script = (
        "sleep 8 | nc -l -p 8080 > /tmp/req & sleep 1; "
        "timeout 5 git ls-remote http://127.0.0.1:8080/r.git >/dev/null 2>&1 & sleep 2; "
        r'cat /proc/[0-9]*/cmdline | tr "\0" " " > /tmp/ps; wait; cat /tmp/req; echo ---ARGV---; cat /tmp/ps'
    )
    cmd += [HELPER_IMAGE, "-c", script]
    result = _run(*cmd)

    request_part, _, argv_part = result.stdout.partition("---ARGV---")
    assert "Authorization: Basic OmR1bW15LXRva2VuLTg0NzI=" in request_part
    assert "git-remote-http" in argv_part
    assert "dummy-token-8472" not in argv_part
    assert "OmR1bW15LXRva2VuLTg0NzI=" not in argv_part


def test_start_failure_cleans_up_container_and_does_not_leak_token(cfg, broken_image):
    broken_cfg = replace(cfg, image=broken_image)

    with pytest.raises(HelperError) as exc_info:
        create_or_refresh_mirror(broken_cfg)

    assert "dummy-token-8472" not in str(exc_info.value)
    assert "OmR1bW15LXRva2VuLTg0NzI=" not in str(exc_info.value)

    result = _run("docker", "ps", "-a", "--filter", "label=fixie.role=helper", "-q")
    assert result.stdout.strip() == ""


def test_missing_helper_image_raises(cfg):
    missing_cfg = replace(cfg, image=f"fixie-test-missing-{_uuid8()}:latest")

    with pytest.raises(HelperError):
        create_or_refresh_mirror(missing_cfg)

    result = _run("docker", "ps", "-a", "--filter", "label=fixie.role=helper", "-q")
    assert result.stdout.strip() == ""


def test_mirror_remote_url_is_kept_in_sync(helper_image, cfg, origin, mirror_volume):
    create_or_refresh_mirror(cfg)

    _sh([f"{origin}:/origin"], "git init -q --bare /origin/other.git")
    new_cfg = replace(cfg, remote_url="file:///origin/other.git")

    create_or_refresh_mirror(new_cfg)

    result = _sh([f"{mirror_volume}:/mirror"], "git -C /mirror/monorepo.git config remote.origin.url")
    assert result.stdout.strip() == "file:///origin/other.git"
