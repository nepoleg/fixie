"""Turn a finished agent run into ADO writes: status comment, PR, run summary, session log."""

import html
import io
import tarfile
import logging
from dataclasses import replace
from datetime import UTC
from urllib.parse import quote

from fixie.allowlist import is_owner
from fixie.events import threads_with_new_comments
from fixie.gateway import BEFORE_WORKING_STATES, WORKING_STATE, AdoGateway
from fixie.helper import MIRROR_LOCK, HelperConfig, HelperError, import_and_push
from fixie.model import BOT_SIGN, FixieStatus, RunStatus, TicketSnapshot
from fixie.render import parse_base_branch
from fixie.runner import RunOutcome
from fixie.settings import Settings
from fixie.status_comment import find_status_comment, format_status_comment, is_bot_signed

logger = logging.getLogger(__name__)

RESOLVED_THREAD_STATUS = "fixed"
_LABELS = {RunStatus.DONE: "done", RunStatus.NEEDS_INFO: "needs info", RunStatus.BLOCKED: "blocked"}


def _write_status(gateway: AdoGateway, settings: Settings, ticket_id: int, status: FixieStatus):
    text = format_status_comment(ticket_id, status, settings.state_secret)
    comment = find_status_comment(gateway.get_comments(ticket_id), settings.owner_email)
    if comment is None:
        return gateway.add_comment(ticket_id, text)
    if comment.text == text:
        return comment
    return gateway.edit_comment(ticket_id, comment.id, text)


def _redact(text: str, settings: Settings) -> str:
    for token in (settings.azure_token, settings.claude_oauth_token, settings.ado_feed_token):
        if token:
            text = text.replace(token, "[redacted]")
    return text


def _redact_bytes(data: bytes, settings: Settings) -> bytes:
    for token in (settings.azure_token, settings.claude_oauth_token, settings.ado_feed_token):
        if token:
            data = data.replace(token.encode("utf-8"), b"[redacted]")
    return data


def _logs_archive(outcome: RunOutcome, settings: Settings) -> bytes | None:
    members = [
        ("session.jsonl", outcome.session_log),
        ("gradle.log", outcome.gradle_log),
        ("claude.json", outcome.claude_json),
        ("container.log", outcome.container_log),
    ]
    members = [(name, _redact_bytes(data, settings)) for name, data in members if data]
    if not members:
        return None
    buf = io.BytesIO()
    # ponytail: logs are packed whole; cap or truncate if ADO's attachment size limit is ever hit.
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _marker(prefix: str, status: FixieStatus) -> str:
    run_started = status.run_started.isoformat() if status.run_started is not None else "-"
    return f"{prefix}:{run_started}"


def _posted(gateway: AdoGateway, settings: Settings, ticket_id: int, marker: str) -> bool:
    return any(
        is_owner(c.created_by, settings.owner_email) and is_bot_signed(c.text) and marker in c.text
        for c in gateway.get_comments(ticket_id)
    )


def _seen_build(builds, previous: int | None) -> int | None:
    pending = [b.id for b in builds if b.status != "completed"]
    limit = min(pending) if pending else None
    candidates = [b.id for b in builds if b.status == "completed" and (limit is None or b.id < limit)]
    if previous is not None:
        candidates.append(previous)
    return max(candidates) if candidates else None


def _pr_url(settings: Settings, pr_id: int) -> str:
    return f"{settings.ado_org_url.rstrip('/')}/{quote(settings.ado_project)}/_git/{quote(settings.ado_repo)}/pullrequest/{pr_id}"


def mark_run_started(
    gateway: AdoGateway,
    settings: Settings,
    ticket_id: int,
    status: FixieStatus | None,
    ticket_state: str | None = None,
) -> FixieStatus:
    """Bump attempts and record this run's start, using the first status write's ADO time as the clock.

    A ticket not yet in development is moved to the working state, recorded in the status first.
    """
    base = status or FixieStatus()
    move = ticket_state in BEFORE_WORKING_STATES
    bumped = replace(base, attempts=base.attempts + 1, state=WORKING_STATE if move else base.state)
    comment = _write_status(gateway, settings, ticket_id, bumped)
    started = replace(bumped, run_started=comment.modified_date)
    _write_status(gateway, settings, ticket_id, started)
    if move:
        gateway.set_state(ticket_id, WORKING_STATE)
        logger.info(f"publisher: ticket {ticket_id} moved {ticket_state} -> {WORKING_STATE}")
    logger.info(f"publisher: ticket {ticket_id} run started {started.run_started.isoformat()}")
    return started


def record_give_up(gateway: AdoGateway, settings: Settings, ticket_id: int, status: FixieStatus) -> FixieStatus:
    """Post a give-up comment (once) and record it in the status; safe to call again."""
    marker = _marker("fixie-giveup", status)
    if not _posted(gateway, settings, ticket_id, marker):
        gateway.add_comment(
            ticket_id,
            f"<p>{BOT_SIGN} fixie gave up after {status.attempts} attempts without a finished run.</p>\n"
            f"<p>Run <code>{marker}</code></p>",
        )
        logger.info(f"publisher: ticket {ticket_id} gave up after {status.attempts} attempts")
    gave_up = replace(status, result=RunStatus.GAVE_UP)
    _write_status(gateway, settings, ticket_id, gave_up)
    return gave_up


def _reply_to_threads(gateway, settings, snapshot, status, pr_id, result, marker) -> None:
    thread_ids = threads_with_new_comments(snapshot, status.last_finished_run_started, settings.owner_email)
    if not thread_ids and not result.resolved_threads:
        return
    current = {t.id: t for t in gateway.get_threads(pr_id)}
    replied = {
        t.id
        for t in current.values()
        if any(is_owner(c.author, settings.owner_email) and is_bot_signed(c.content) and marker in c.content
               for c in t.comments)
    }
    parts = [f"{BOT_SIGN} fixie: {_LABELS[result.status]}", _redact(result.summary, settings)]
    if result.questions:
        parts.append("\n".join(f"- {_redact(q, settings)}" for q in result.questions))
    parts.append(f"`{marker}`")
    text = "\n\n".join(parts)
    for thread_id in thread_ids:
        if thread_id not in replied:
            gateway.reply_to_thread(pr_id, thread_id, text)
            logger.info(f"publisher: ticket {snapshot.ticket.id} replied in PR {pr_id} thread {thread_id}")
    for thread_id in result.resolved_threads:
        thread = current.get(thread_id)
        if thread is None:
            logger.info(f"publisher: ticket {snapshot.ticket.id} thread {thread_id} not in PR {pr_id}, not resolved")
        elif thread.status != RESOLVED_THREAD_STATUS:
            gateway.set_thread_status(pr_id, thread_id, RESOLVED_THREAD_STATUS)
            logger.info(f"publisher: ticket {snapshot.ticket.id} resolved PR {pr_id} thread {thread_id}")


def publish(
    gateway: AdoGateway,
    settings: Settings,
    snapshot: TicketSnapshot,
    status: FixieStatus,
    outcome: RunOutcome,
    *,
    push=import_and_push,
    mirror_lock=MIRROR_LOCK,
    helper_config: HelperConfig | None = None,
) -> FixieStatus | None:
    """Push a finished run's bundle, create/link its PR, post its summary and log, and finish the status.

    Returns None (nothing written but a possible push) when the run is not finished; the next poll retries.
    """
    ticket = snapshot.ticket
    ticket_id = ticket.id
    if outcome.ticket_id != str(ticket_id):
        raise ValueError(f"outcome ticket_id {outcome.ticket_id!r} does not match snapshot ticket {ticket_id}")
    if status.run_started is None:
        raise ValueError("status.run_started is None")

    if not outcome.ok:
        logger.info(f"publisher: ticket {ticket_id} run not ok, nothing published")
        return None
    result = outcome.result
    cfg = helper_config or HelperConfig.from_settings(settings)

    pushed = None
    if outcome.has_bundle:
        try:
            with mirror_lock:
                pushed = push(cfg, outcome.ticket_id, outcome.volume)
        except HelperError as exc:
            logger.warning(f"publisher: ticket {ticket_id} push failed: {exc}")
            return None
        if pushed is not None:
            logger.info(f"publisher: ticket {ticket_id} pushed {pushed}")

    branch = f"fixie/{ticket_id}"
    active = [p for p in gateway.find_pull_requests(branch) if p.status == "active"]
    pr = max(active, key=lambda p: p.id) if active else None
    if pr is None and pushed is not None:
        pr = gateway.create_pull_request(
            branch,
            parse_base_branch(ticket.description, settings.default_base_branch),
            _redact(f"fixie: #{ticket_id} {ticket.title}", settings),
            _redact(result.summary, settings)[:4000],
        )
        logger.info(f"publisher: ticket {ticket_id} created PR {pr.id}")
    elif pr is not None and pushed is not None:
        description = _redact(result.summary, settings)[:4000]
        if pr.description != description:
            gateway.update_pull_request(pr.id, pr.title, description)
            logger.info(f"publisher: ticket {ticket_id} updated PR {pr.id}")
    if pr is not None and ticket_id not in pr.work_item_ids:
        gateway.link_work_item(pr.id, ticket_id)
    pr_id = pr.id if pr is not None else status.pr_id
    if pushed is None and pr is None:
        logger.info(f"publisher: ticket {ticket_id} no commits, no PR")

    marker = _marker("fixie-run", status)
    if not _posted(gateway, settings, ticket_id, marker):
        lines = [
            f"<p>{BOT_SIGN} fixie: {_LABELS[result.status]}</p>",
            f"<p>{html.escape(_redact(result.summary, settings)).replace(chr(10), '<br>')}</p>",
        ]
        if result.questions:
            items = "".join(f"<li>{html.escape(_redact(q, settings))}</li>" for q in result.questions)
            lines.append(f"<ul>{items}</ul>")
        if pr_id is not None:
            url = _pr_url(settings, pr_id)
            lines.append(f'<p>PR: <a href="{url}">{url}</a></p>')
        lines.append(f"<p>Run <code>{marker}</code></p>")
        gateway.add_comment(ticket_id, "\n".join(lines))

    if pr is not None and snapshot.pull_request is not None and pr.id == snapshot.pull_request.id:
        _reply_to_threads(gateway, settings, snapshot, status, pr.id, result, marker)

    name = f"fixie-{ticket_id}-{status.run_started.astimezone(UTC):%Y%m%dT%H%M%SZ}.logs.tar.gz"
    archive = _logs_archive(outcome, settings)
    if archive is None:
        logger.info(f"publisher: ticket {ticket_id} no logs, nothing attached")
    elif not any(a.name == name for a in gateway.get_attachments(ticket_id)):
        gateway.attach_file(ticket_id, name, archive)

    finished = replace(
        status,
        last_finished_run_started=status.run_started,
        result=result.status,
        pr_id=pr_id,
        head=pushed or status.head,
        last_build_id=_seen_build(snapshot.builds, status.last_build_id),
        attempts=0,
    )
    _write_status(gateway, settings, ticket_id, finished)
    logger.info(f"publisher: ticket {ticket_id} finished {finished.result}")
    return finished
