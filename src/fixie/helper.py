"""Runs the fixie-helper container jobs (mirror refresh, bundle import/push, Gradle RO cache) via the Docker SDK."""

import base64
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

import docker
from docker.errors import DockerException, ImageNotFound
from requests.exceptions import RequestException

from fixie.settings import Settings

HELPER_IMAGE = "fixie-helper:latest"
MIRROR_LOCK = threading.Lock()

_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TICKET_ID_RE = re.compile(r"^[0-9]{1,10}$")
_PUSHED_RE = re.compile(r"^fixie-helper: pushed ([0-9a-f]{40})$")
_GRADLE_CACHE_RE = re.compile(r"^fixie-helper: gradle cache (v-[A-Za-z0-9]+)$")


class HelperError(Exception):
    """Raised when a helper job exited non-zero, timed out, or printed no result line."""


@dataclass(frozen=True)
class HelperConfig:
    """Everything needed to run a fixie-helper job: volumes, remote URL and token."""

    mirror_volume: str
    gradle_ro_volume: str
    remote_url: str
    token: str | None = field(default=None, repr=False)
    image: str = HELPER_IMAGE
    extra_volumes: tuple[str, ...] = ()
    timeout_s: int = 3600

    def __post_init__(self):
        if not _VOLUME_RE.match(self.mirror_volume):
            raise ValueError(f"invalid mirror_volume: {self.mirror_volume!r}")
        if not _VOLUME_RE.match(self.gradle_ro_volume):
            raise ValueError(f"invalid gradle_ro_volume: {self.gradle_ro_volume!r}")
        parts = urlsplit(self.remote_url)
        if parts.scheme not in ("https", "http", "file"):
            raise ValueError(f"unsupported remote_url scheme: {self.remote_url!r}")
        if parts.username is not None or parts.password is not None:
            raise ValueError("remote_url must not contain credentials")

    @classmethod
    def from_settings(cls, settings: Settings) -> "HelperConfig":
        """Builds the ADO remote URL and volumes from resolved fixie Settings."""
        remote_url = (
            f"{settings.ado_org_url.rstrip('/')}/{quote(settings.ado_project)}/_git/{quote(settings.ado_repo)}"
        )
        return cls(
            mirror_volume=settings.mirror_volume,
            gradle_ro_volume=settings.gradle_ro_volume,
            remote_url=remote_url,
            token=settings.azure_token,
            timeout_s=settings.helper_timeout_s,
        )


def auth_env(token: str | None) -> dict[str, str]:
    """Builds the env vars that make git send an Authorization header, without ever passing the token as argv."""
    if token is None:
        return {}
    basic = base64.b64encode(f":{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic " + basic,
    }


def create_or_refresh_mirror(cfg: HelperConfig) -> None:
    """Creates or fetches the bare mirror. Caller must hold the global mirror lock (shared with import_and_push)."""
    _run_job(
        cfg,
        command=["mirror"],
        volumes=[f"{cfg.mirror_volume}:/mirror:rw", *cfg.extra_volumes],
        environment={"FIXIE_REMOTE_URL": cfg.remote_url, **auth_env(cfg.token)},
        cap_add=[],
    )


def import_and_push(cfg: HelperConfig, ticket_id: str, ticket_volume: str) -> str | None:
    """Imports a ticket's branch.bundle into the mirror and pushes it to origin. Caller must hold the mirror lock."""
    if not _TICKET_ID_RE.match(ticket_id):
        raise ValueError(f"invalid ticket_id: {ticket_id!r}")
    if not _VOLUME_RE.match(ticket_volume):
        raise ValueError(f"invalid ticket_volume: {ticket_volume!r}")

    stdout = _run_job(
        cfg,
        command=["import", ticket_id],
        volumes=[f"{cfg.mirror_volume}:/mirror:rw", f"{ticket_volume}:/ticket:ro", *cfg.extra_volumes],
        environment={"FIXIE_REMOTE_URL": cfg.remote_url, **auth_env(cfg.token)},
        cap_add=["DAC_READ_SEARCH"],
    )
    last = _last_line(stdout)
    match = _PUSHED_RE.match(last)
    if match:
        return match.group(1)
    if last == "fixie-helper: no bundle":
        return None
    raise HelperError("helper import printed no result")


def refresh_gradle_cache(cfg: HelperConfig, host_modules2: Path) -> str:
    """Copies host_modules2 into a new versioned RO cache slot and atomically switches the modules-2 symlink."""
    if not (host_modules2.is_absolute() and host_modules2.is_dir() and host_modules2.name == "modules-2"):
        raise ValueError(f"invalid host_modules2: {host_modules2!r}")

    stdout = _run_job(
        cfg,
        command=["gradle"],
        volumes=[f"{host_modules2}:/src:ro", f"{cfg.gradle_ro_volume}:/gradle-ro:rw"],
        environment={},
        cap_add=[],
    )
    match = _GRADLE_CACHE_RE.match(_last_line(stdout))
    if not match:
        raise HelperError("helper gradle printed no result")
    return match.group(1)


def _last_line(stdout):
    lines = [line for line in stdout.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _run_job(cfg, command, volumes, environment, cap_add):
    client = docker.from_env()
    try:
        client.images.get(cfg.image)
    except ImageNotFound:
        raise HelperError(f"helper image not found: {cfg.image}")
    except DockerException as exc:
        raise HelperError(f"helper image lookup failed: {type(exc).__name__}")

    container = client.containers.create(
        cfg.image,
        command,
        detach=True,
        cap_drop=["ALL"],
        cap_add=cap_add,
        security_opt=["no-new-privileges"],
        labels={"fixie.role": "helper"},
        volumes=volumes,
        environment=environment,
    )
    try:
        try:
            container.start()
            result = container.wait(timeout=cfg.timeout_s)
        except RequestException:
            try:
                container.kill()
            except DockerException:
                pass
            raise HelperError(f"helper {command[0]} timed out")
        except DockerException as exc:
            raise HelperError(f"helper {command[0]} failed to start: {type(exc).__name__}")

        logger = logging.getLogger(__name__)
        stderr_lines = [line for line in container.logs(stdout=False, stderr=True).decode(
            "utf-8", errors="replace"
        ).splitlines() if line.strip()]
        for line in stderr_lines:
            logger.info(line)

        rc = result["StatusCode"]
        if rc != 0:
            last = stderr_lines[-1] if stderr_lines else ""
            raise HelperError(f"helper {command[0]} failed (exit {rc}): {last}")
        return container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
    finally:
        container.remove(force=True)
