"""Dry-run mode: real ADO reads, logged writes, and no Docker, mirror or push."""

import abc
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fixie.gateway import AdoGateway
from fixie.model import Attachment, Comment, Identity, PrComment, PullRequest
from fixie.orchestrator import Orchestrator
from fixie.runner import RunOutcome
from fixie.settings import Settings

logger = logging.getLogger(__name__)

_READS = (
    "find_tagged_tickets", "get_ticket", "get_updates", "get_comments", "get_attachments",
    "get_pull_request", "find_pull_requests", "get_threads", "get_builds", "get_branch_head",
)


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 300 else f"{text[:300]}... ({len(text)} chars)"


class DryRunGateway(AdoGateway):
    """Wraps a gateway: reads go through, writes are only logged and answered with placeholder objects."""

    def __init__(self, inner: AdoGateway, owner: Identity):
        self.inner = inner
        self.owner = owner
        self.writes: list[tuple[str, dict[str, object]]] = []

    def _log(self, method: str, **params) -> datetime:
        self.writes.append((method, params))
        logger.info(f"dry-run: {method} " + ", ".join(f"{k}={_short(v)}" for k, v in params.items()))
        return datetime.now(UTC)

    def _comment(self, comment_id: int, text: str, now: datetime) -> Comment:
        return Comment(comment_id, text, self.owner, now, self.owner, now)

    def add_comment(self, ticket_id: int, text: str) -> Comment:
        return self._comment(-1, text, self._log("add_comment", ticket_id=ticket_id, text=text))

    def remove_tag(self, ticket_id: int, tag: str) -> None:
        self._log("remove_tag", ticket_id=ticket_id, tag=tag)

    def set_state(self, ticket_id: int, state: str) -> None:
        self._log("set_state", ticket_id=ticket_id, state=state)

    def reply_to_thread(self, pr_id: int, thread_id: int, text: str) -> PrComment:
        now = self._log("reply_to_thread", pr_id=pr_id, thread_id=thread_id, text=text)
        return PrComment(-1, self.owner, text, now, now, "text")

    def set_thread_status(self, pr_id: int, thread_id: int, status: str) -> None:
        self._log("set_thread_status", pr_id=pr_id, thread_id=thread_id, status=status)

    def delete_branch(self, branch: str, expected_sha: str) -> bool:
        self._log("delete_branch", branch=branch, expected_sha=expected_sha)
        return True

    def edit_comment(self, ticket_id: int, comment_id: int, text: str) -> Comment:
        return self._comment(comment_id, text, self._log("edit_comment", ticket_id=ticket_id, comment_id=comment_id, text=text))

    def create_pull_request(self, source_branch: str, target_branch: str, title: str, description: str) -> PullRequest:
        now = self._log("create_pull_request", source_branch=source_branch, target_branch=target_branch, title=title)
        return PullRequest(-1, title, description, source_branch, target_branch, "active", False, (), now)

    def update_pull_request(self, pr_id: int, title: str, description: str) -> PullRequest:
        self._log("update_pull_request", pr_id=pr_id, title=title)
        return self.inner.get_pull_request(pr_id)

    def link_work_item(self, pr_id: int, ticket_id: int) -> None:
        self._log("link_work_item", pr_id=pr_id, ticket_id=ticket_id)

    def attach_file(self, ticket_id: int, name: str, content: bytes) -> Attachment:
        self._log("attach_file", ticket_id=ticket_id, name=name, size=len(content))
        return Attachment("dry-run", name, f"dry-run://{name}")


def _delegate(name: str):
    return lambda self, *args: getattr(self.inner, name)(*args)


for _name in _READS:
    setattr(DryRunGateway, _name, _delegate(_name))
abc.update_abstractmethods(DryRunGateway)


def dry_run_orchestrator(gateway: AdoGateway, settings: Settings, out_dir: Path) -> Orchestrator:
    """An Orchestrator whose runs save their input files to out_dir/<ticket id> instead of starting a container."""

    def run_ticket(ticket_id, input_dir, mode, base_branch, _settings, **_):
        target = out_dir / ticket_id
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(input_dir, target)
        logger.info(f"dry-run: ticket {ticket_id} would {mode} on base {base_branch}, input saved to {target}")
        return RunOutcome(ticket_id, "dry-run", 0, False, None)

    def publish(*_, **__):
        logger.info("dry-run: publish skipped")
        return None

    def skip(name):
        return lambda *args, **__: logger.info(f"dry-run: {name}{args} skipped")

    return Orchestrator(
        gateway,
        settings,
        run_ticket=run_ticket,
        refresh_mirror=skip("refresh_mirror"),
        publish=publish,
        delete_volume=skip("delete_volume"),
        kill_ticket=skip("kill_ticket"),
        running_tickets=set,
        ticket_volumes=set,
    )
