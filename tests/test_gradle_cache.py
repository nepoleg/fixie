import threading

import pytest

from fixie.helper import HelperConfig, HelperError, refresh_gradle_cache
from test_agent_image import FLAGS, INPUT, TICKET_ENV, _docker_ok, _run, _run_ticket, _uuid8, image  # noqa: F401
from test_git_helper import _sh, helper_image  # noqa: F401

pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _docker_ok(), reason="Docker engine not reachable")]


@pytest.fixture
def ro_volume():
    vol = f"fixie-test-ro-{_uuid8()}"
    _run("docker", "volume", "create", vol)
    yield vol
    _run("docker", "volume", "rm", "-f", vol)


@pytest.fixture
def cfg(ro_volume):
    return HelperConfig(
        mirror_volume="unused-mirror",
        gradle_ro_volume=ro_volume,
        remote_url="file:///unused",
    )


def _seed_modules2(tmp_path):
    src = tmp_path / "modules-2"
    files = {
        "files-2.1/g/a/1.0/h1/a-1.0.jar": "jar1",
        "metadata-2.106/descriptors/d.bin": "d",
        "modules-2.lock": "l",
        "gc.properties": "g",
        "metadata-2.106/metadata-2.106.lock": "l",
        "files-2.1/x.lock": "l",
    }
    for name, content in files.items():
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return src


def _ls(ro):
    return _sh([f"{ro}:/gradle-ro"], "cd /gradle-ro && readlink modules-2; ls -d v-*; cd modules-2 && find . -type f | sort")


def test_refresh_copies_without_lock_and_gc_files(helper_image, image, cfg, ro_volume, tmp_path):
    src = _seed_modules2(tmp_path)

    v = refresh_gradle_cache(cfg, src)

    assert v.startswith("v-") and len(v) == len("v-XXXXXX")
    listing = _ls(ro_volume)
    lines = listing.stdout.splitlines()
    assert lines[0] == f"{v}/modules-2"
    assert lines[2:] == ["./files-2.1/g/a/1.0/h1/a-1.0.jar", "./metadata-2.106/descriptors/d.bin"]

    result = _run(
        "docker", "run", "--rm", "--network", "none", "-e", "FIXIE_FIREWALL=off", *FLAGS,
        "-v", f"{ro_volume}:/gradle-ro:ro", "fixie-agent:latest", "bash", "-c",
        "cat /gradle-ro/modules-2/files-2.1/g/a/1.0/h1/a-1.0.jar",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "jar1"


def test_refresh_switches_and_prunes(helper_image, cfg, ro_volume, tmp_path):
    src = _seed_modules2(tmp_path)

    v1 = refresh_gradle_cache(cfg, src)

    (src / "files-2.1/g/b/2.0/h2/b-2.0.jar").parent.mkdir(parents=True)
    (src / "files-2.1/g/b/2.0/h2/b-2.0.jar").write_text("jar2", encoding="utf-8")

    v2 = refresh_gradle_cache(cfg, src)
    listing = _ls(ro_volume)
    lines = listing.stdout.splitlines()
    assert lines[0] == f"{v2}/modules-2"
    assert {line for line in lines[1:] if line.startswith("v-")} == {v1, v2}
    assert "./files-2.1/g/b/2.0/h2/b-2.0.jar" in lines

    link_count = _sh(
        [f"{ro_volume}:/gradle-ro"],
        f"stat -c %h /gradle-ro/{v2}/modules-2/files-2.1/g/a/1.0/h1/a-1.0.jar",
    ).stdout.strip()
    assert link_count == "2"

    v3 = refresh_gradle_cache(cfg, src)
    listing = _ls(ro_volume)
    out_lines = listing.stdout.splitlines()
    assert out_lines[0] == f"{v3}/modules-2"
    dirs = {line for line in out_lines[1:] if line.startswith("v-")}
    assert dirs == {v2, v3}

    tmp_check = _sh([f"{ro_volume}:/gradle-ro"], "test -e /gradle-ro/.modules-2.tmp && echo EXISTS || echo GONE")
    assert tmp_check.stdout.strip() == "GONE"


def test_concurrent_refresh_does_not_corrupt_cache(helper_image, cfg, ro_volume, tmp_path):
    src = _seed_modules2(tmp_path)

    results = []
    errors = []

    def _job():
        try:
            results.append(refresh_gradle_cache(cfg, src))
        except HelperError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_job) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) + len(errors) == 2
    assert len(results) >= 1
    for exc in errors:
        assert "busy" in str(exc)

    listing = _ls(ro_volume)
    lines = listing.stdout.splitlines()
    assert lines[0].split("/")[0] in set(results)
    check = _sh([f"{ro_volume}:/gradle-ro"], "cd /gradle-ro && test -d \"$(readlink -f modules-2)\" && echo OK")
    assert check.stdout.strip() == "OK"


def test_refresh_rejects_empty_source(helper_image, cfg, tmp_path):
    src = tmp_path / "modules-2"
    src.mkdir()

    with pytest.raises(HelperError) as exc_info:
        refresh_gradle_cache(cfg, src)
    assert "empty modules-2 source" in str(exc_info.value)


def test_gradle_offline_uses_ro_cache(helper_image, image, cfg, ro_volume, tmp_path):
    probe = _run(
        "docker", "run", "--rm", "alpine:3.22", "wget", "-q", "-O", "/dev/null",
        "https://repo.maven.apache.org/maven2/org/slf4j/slf4j-api/2.0.16/slf4j-api-2.0.16.pom",
    )
    if probe.returncode != 0:
        pytest.skip("no internet from containers")

    gradle = (
        "/home/agent/.gradle/wrapper/dists/gradle-8.12-bin/cetblhg4pflnnks72fxwobvgv/gradle-8.12/bin/gradle"
    )
    proj = (
        "mkdir -p /tmp/p && cd /tmp/p && printf \"rootProject.name = 'p'\\n\" > settings.gradle && "
        "printf '%s\\n' "
        "\"repositories { mavenCentral() }\" "
        "\"configurations { deps }\" "
        "\"dependencies { deps 'org.slf4j:slf4j-api:2.0.16' }\" "
        "\"tasks.register('resolve') { def f = configurations.deps; doLast { println 'RESOLVED ' + f.files*.name.join(',') } }\" "
        "> build.gradle"
    )

    gh_vol = f"fixie-test-gh-{_uuid8()}"
    _run("docker", "volume", "create", gh_vol)
    try:
        seed = _run(
            "docker", "run", "--rm", "--entrypoint", "bash", "-e", f"GRADLE_USER_HOME=/gh",
            "-v", f"{gh_vol}:/gh", "fixie-agent:latest", "-c", f"{proj} && {gradle} -q --no-daemon resolve",
        )
        assert seed.returncode == 0, seed.stderr
        assert "RESOLVED slf4j-api-2.0.16.jar" in seed.stdout

        cname = f"fixie-test-{_uuid8()}"
        _run("docker", "create", "--name", cname, "-v", f"{gh_vol}:/gh", "alpine:3.22")
        _run("docker", "cp", f"{cname}:/gh/caches/modules-2", str(tmp_path / "modules-2"))
        _run("docker", "rm", cname)
    finally:
        _run("docker", "volume", "rm", "-f", gh_vol)

    refresh_gradle_cache(cfg, tmp_path / "modules-2")

    positive = _run(
        "docker", "run", "--rm", "--network", "none", "-e", "FIXIE_FIREWALL=off", *FLAGS,
        "-e", "GRADLE_USER_HOME=/tmp/gh", "-v", f"{ro_volume}:/gradle-ro:ro", "fixie-agent:latest", "bash", "-c",
        f"{proj} && {gradle} -q --offline --no-daemon resolve",
    )
    assert positive.returncode == 0, positive.stderr
    assert "RESOLVED slf4j-api-2.0.16.jar" in positive.stdout

    negative = _run(
        "docker", "run", "--rm", "--network", "none", "-e", "FIXIE_FIREWALL=off", *FLAGS,
        "-e", "GRADLE_USER_HOME=/tmp/gh", "fixie-agent:latest", "bash", "-c",
        f"{proj} && {gradle} -q --offline --no-daemon resolve",
    )
    assert negative.returncode != 0
