"""Runs one agent container per ticket via the Docker SDK: seed input, wait, collect output."""

import io
import json
import logging
import re
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.errors import APIError, DockerException, NotFound
from requests.exceptions import ConnectionError, ReadTimeout, RequestException

from fixie.model import RunResult, RunStatus
from fixie.settings import Settings

TICKET_VOLUME_PREFIX = "fixie-ticket-"
LABEL = "fixie.ticket"
MEM_LIMIT = "8g"
NANO_CPUS = 4_000_000_000
PIDS_LIMIT = 4096
KILL_WAIT_S = 60

_TICKET_ID_RE = re.compile(r"^[0-9]{1,10}$")
_OUT_FILES = ("out/result.json", "out/claude.json", "out/session.jsonl", "out/gradle.log")

logger = logging.getLogger(__name__)


class RunnerError(Exception):
    """Raised on a Docker/infra failure while handling a ticket; never for a failed agent run."""


@dataclass(frozen=True)
class RunOutcome:
    """Outcome of one container run: exit status plus whatever ~/out it managed to collect."""

    ticket_id: str
    volume: str
    exit_code: int
    timed_out: bool
    result: RunResult | None
    claude_json: bytes | None = field(default=None, repr=False)
    session_log: bytes | None = field(default=None, repr=False)
    gradle_log: bytes | None = field(default=None, repr=False)
    has_bundle: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.result is not None


def _check_ticket_id(ticket_id: str) -> None:
    if not _TICKET_ID_RE.match(ticket_id):
        raise ValueError(f"invalid ticket_id: {ticket_id!r}")


def _volume_name(ticket_id: str) -> str:
    return TICKET_VOLUME_PREFIX + ticket_id


def _close(client) -> None:
    try:
        client.close()
    except Exception:
        pass


def session_id(ticket_id: str) -> str:
    """Deterministic Claude Code session id for a ticket, stable across orchestrator restarts."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fixie://ticket/{ticket_id}"))


def parse_result(data: bytes) -> RunResult | None:
    """Parses ~/out/result.json bytes into a RunResult; returns None if malformed, never raises."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    status, summary, questions = obj.get("status"), obj.get("summary"), obj.get("questions")
    if not isinstance(status, str) or not isinstance(summary, str) or not isinstance(questions, list):
        return None
    if not all(isinstance(q, str) for q in questions):
        return None
    try:
        run_status = RunStatus(status)
    except ValueError:
        return None
    if run_status == RunStatus.GAVE_UP:
        return None
    return RunResult(run_status, summary, tuple(questions))


def _create_ticket_volume(client, ticket_id: str) -> str:
    name = _volume_name(ticket_id)
    client.volumes.create(name, labels={LABEL: ticket_id})
    return name


def ensure_ticket_volume(ticket_id: str) -> str:
    """Creates the ticket's Docker volume if it does not already exist; idempotent."""
    _check_ticket_id(ticket_id)
    client = None
    try:
        client = docker.from_env()
        return _create_ticket_volume(client, ticket_id)
    except (DockerException, RequestException) as exc:
        raise RunnerError(f"ensure_ticket_volume {ticket_id} failed: {type(exc).__name__}") from None
    finally:
        if client is not None:
            _close(client)


def delete_ticket_volume(ticket_id: str) -> None:
    """Removes the ticket's Docker volume; a no-op if it is already gone."""
    _check_ticket_id(ticket_id)
    name = _volume_name(ticket_id)
    client = None
    try:
        client = docker.from_env()
        client.volumes.get(name).remove()
    except NotFound:
        return
    except (DockerException, RequestException) as exc:
        raise RunnerError(f"cannot delete volume {name}: {type(exc).__name__}") from None
    finally:
        if client is not None:
            _close(client)


def running_tickets() -> set[str]:
    """Ticket ids with a currently running container, observed right now (no locking)."""
    client = None
    try:
        client = docker.from_env()
        containers = client.containers.list(filters={"label": LABEL}, ignore_removed=True)
        return {c.labels[LABEL] for c in containers}
    except (DockerException, RequestException) as exc:
        raise RunnerError(f"running_tickets failed: {type(exc).__name__}") from None
    finally:
        if client is not None:
            _close(client)


def ticket_volumes() -> set[str]:
    """Ticket ids that still have a ticket volume (volumes labelled fixie.ticket)."""
    client = None
    try:
        client = docker.from_env()
        volumes = client.volumes.list(filters={"label": LABEL})
        return {v.attrs["Labels"][LABEL] for v in volumes}
    except (DockerException, RequestException) as exc:
        raise RunnerError(f"ticket_volumes failed: {type(exc).__name__}") from None
    finally:
        if client is not None:
            _close(client)


def kill_ticket(ticket_id: str) -> None:
    """Force-removes every container (running or a stopped leftover) for a ticket."""
    _check_ticket_id(ticket_id)
    client = None
    try:
        client = docker.from_env()
        containers = client.containers.list(
            all=True, filters={"label": f"{LABEL}={ticket_id}"}, ignore_removed=True
        )
        for container in containers:
            try:
                container.remove(force=True)
            except NotFound:
                pass
    except (DockerException, RequestException) as exc:
        raise RunnerError(f"kill_ticket {ticket_id} failed: {type(exc).__name__}") from None
    finally:
        if client is not None:
            _close(client)


def _normalize(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if not (tarinfo.isfile() or tarinfo.isdir()):
        return None
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = "root"
    tarinfo.mode = 0o755 if tarinfo.isdir() else 0o644
    return tarinfo


def _build_input_tar(input_dir: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(input_dir, arcname="fixie-in", filter=_normalize)
    return buf.getvalue()


def _read_out(container) -> tuple[dict[str, bytes], bool]:
    # ponytail: out/ is read into memory whole (bundle included); stream to a temp file if out ever gets big.
    try:
        stream, _ = container.get_archive("/home/agent/out")
    except NotFound:
        return {}, False
    tar = tarfile.open(fileobj=io.BytesIO(b"".join(stream)))
    files: dict[str, bytes] = {}
    has_bundle = False
    for member in tar.getmembers():
        if not member.isfile():
            continue
        if member.name in _OUT_FILES:
            files[member.name] = tar.extractfile(member).read()
        elif member.name == "out/branch.bundle":
            has_bundle = True
    return files, has_bundle


def run_ticket(
    ticket_id: str,
    input_dir: Path,
    mode: str,
    base_branch: str,
    settings: Settings,
    *,
    extra_env: dict[str, str] | None = None,
) -> RunOutcome:
    """Runs one agent container for a ticket: seeds ~/in, waits for it, and collects ~/out."""
    _check_ticket_id(ticket_id)
    if mode not in ("start", "resume"):
        raise ValueError(f"invalid mode: {mode!r}")
    if not input_dir.is_dir():
        raise ValueError(f"invalid input_dir: {input_dir!r}")

    tar_bytes = _build_input_tar(input_dir)

    client = None
    try:
        try:
            client = docker.from_env()
            try:
                client.volumes.get(settings.mirror_volume)
            except NotFound:
                raise RunnerError(f"mirror volume missing: {settings.mirror_volume}") from None

            volume = _create_ticket_volume(client, ticket_id)

            volumes = [f"{settings.mirror_volume}:/mirror:ro", f"{volume}:/home/agent:rw"]
            try:
                client.volumes.get(settings.gradle_ro_volume)
                volumes.append(f"{settings.gradle_ro_volume}:/gradle-ro:ro")
            except NotFound:
                logger.info(f"runner: gradle volume {settings.gradle_ro_volume} missing, not mounted")

            run_id = uuid.uuid4().hex[:8]
            env = {
                "TICKET_ID": ticket_id,
                "BASE_BRANCH": base_branch,
                "SESSION_ID": session_id(ticket_id),
                "RUN_MODE": mode,
                "CLAUDE_MODEL": settings.claude_model,
                "CLAUDE_CODE_OAUTH_TOKEN": settings.claude_oauth_token or "",
                "AZURE_PERSONAL_ACCESS_TOKEN": settings.ado_feed_token or "",
                "ADO_ORG_URL": settings.ado_org_url.rstrip("/"),
                "ADO_PROJECT": settings.ado_project,
                "ADO_REPO": settings.ado_repo,
                **(extra_env or {}),
            }
            container = client.containers.create(
                settings.docker_image,
                name=f"fixie-{ticket_id}-{run_id}",
                labels={LABEL: ticket_id},
                environment=env,
                volumes=volumes,
                cap_drop=["ALL"],
                cap_add=["NET_ADMIN", "SETUID", "SETGID"],
                security_opt=["no-new-privileges"],
                mem_limit=MEM_LIMIT,
                memswap_limit=MEM_LIMIT,
                nano_cpus=NANO_CPUS,
                pids_limit=PIDS_LIMIT,
            )
        except (DockerException, RequestException) as exc:
            raise RunnerError(f"ticket {ticket_id} run failed: {type(exc).__name__}") from None

        try:
            container.put_archive("/tmp", tar_bytes)
            container.start()
            logger.info(f"runner: ticket {ticket_id} started {container.name}")

            timed_out = False
            try:
                rc = container.wait(timeout=settings.ticket_timeout_s)["StatusCode"]
            except (ConnectionError, ReadTimeout):
                timed_out = True
                try:
                    container.kill()
                except APIError:
                    pass
                rc = container.wait(timeout=KILL_WAIT_S)["StatusCode"]
                logger.info(f"runner: ticket {ticket_id} timed out after {settings.ticket_timeout_s}s, killed")

            for line in container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    logger.info(f"ticket {ticket_id}: {line}")
            logger.info(f"runner: ticket {ticket_id} exited {rc}")

            result = claude_json = session_log = gradle_log = None
            has_bundle = False
            if rc == 1 and not timed_out:
                logger.info(f"runner: ticket {ticket_id} firewall failed, out not read")
            else:
                files, has_bundle = _read_out(container)
                claude_json = files.get("out/claude.json")
                session_log = files.get("out/session.jsonl")
                gradle_log = files.get("out/gradle.log")
                result_bytes = files.get("out/result.json")
                result = parse_result(result_bytes) if result_bytes is not None else None
                if result is None:
                    logger.info(f"runner: ticket {ticket_id} result.json missing or invalid")
        except (DockerException, RequestException) as exc:
            raise RunnerError(f"ticket {ticket_id} run failed: {type(exc).__name__}") from None
        finally:
            try:
                container.remove(force=True)
            except NotFound:
                pass
            except (DockerException, RequestException):
                logger.warning(f"runner: ticket {ticket_id} container {container.name} not removed")

        return RunOutcome(
            ticket_id=ticket_id,
            volume=volume,
            exit_code=rc,
            timed_out=timed_out,
            result=result,
            claude_json=claude_json,
            session_log=session_log,
            gradle_log=gradle_log,
            has_bundle=has_bundle,
        )
    finally:
        if client is not None:
            _close(client)
