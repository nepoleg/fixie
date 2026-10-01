"""Read a ticket's ADO activity and turn it into the events fixie reacts to."""

from datetime import datetime

from fixie.allowlist import is_allowed, is_owner
from fixie.gateway import OPEN_STATES, AdoGateway
from fixie.model import Event, EventType, FixieStatus, Identity, TicketSnapshot
from fixie.status_comment import is_bot_signed


def load_snapshot(gateway: AdoGateway, ticket_id: int) -> TicketSnapshot:
    """Read a ticket, its history, PR, threads, builds and branch head from the gateway."""
    branch = f"fixie/{ticket_id}"
    ticket = gateway.get_ticket(ticket_id)
    updates = tuple(gateway.get_updates(ticket_id))
    comments = tuple(gateway.get_comments(ticket_id))
    prs = gateway.find_pull_requests(branch)
    pull_request = max(prs, key=lambda p: (p.status == "active", p.id)) if prs else None
    if pull_request is not None:
        threads = tuple(gateway.get_threads(pull_request.id))
        builds = tuple(gateway.get_builds(pull_request.id))
    else:
        threads = ()
        builds = ()
    head = gateway.get_branch_head(branch)
    return TicketSnapshot(
        ticket=ticket,
        updates=updates,
        comments=comments,
        pull_request=pull_request,
        threads=threads,
        builds=builds,
        head=head,
    )


def threads_with_new_comments(snapshot: TicketSnapshot, since: datetime | None, owner_email: str) -> tuple[int, ...]:
    """Ids of PR threads with a non-fixie text comment published since `since`."""
    return tuple(
        thread.id
        for thread in snapshot.threads
        if any(
            c.comment_type == "text"
            and (since is None or c.published_date >= since)
            and not (is_owner(c.author, owner_email) and is_bot_signed(c.content))
            for c in thread.comments
        )
    )


def collect_events(
    snapshot: TicketSnapshot,
    status: FixieStatus | None,
    since: datetime | None,
    owner_email: str,
    allowlist: tuple[str, ...],
) -> tuple[Event, ...]:
    """Return the snapshot's events since `since`, oldest first, excluding fixie's own actions."""

    def in_window(t: datetime) -> bool:
        return since is None or t >= since

    def fixie_comment(author: Identity, text: str) -> bool:
        return is_owner(author, owner_email) and is_bot_signed(text)

    def allowlisted(author: Identity) -> bool:
        return is_allowed(author, allowlist, owner_email)

    recorded_head = status.head if status is not None else None
    events: list[Event] = []

    for comment in snapshot.comments:
        author = comment.modified_by
        if in_window(comment.created_date) and not fixie_comment(author, comment.text):
            events.append(
                Event(EventType.TICKET_COMMENT, author, comment.created_date, comment.text, allowlisted(author))
            )

    for thread in snapshot.threads:
        for pr_comment in thread.comments:
            if pr_comment.comment_type != "text":
                continue
            if not in_window(pr_comment.published_date):
                continue
            if fixie_comment(pr_comment.author, pr_comment.content):
                continue
            content = pr_comment.content
            if thread.file_path is not None and thread.line is not None:
                content = f"{thread.file_path}:{thread.line}: {content}"
            elif thread.file_path is not None:
                content = f"{thread.file_path}: {content}"
            events.append(
                Event(
                    EventType.PR_COMMENT,
                    pr_comment.author,
                    pr_comment.published_date,
                    content,
                    allowlisted(pr_comment.author),
                )
            )

    head = snapshot.head
    if head is not None and head.sha != recorded_head and (recorded_head is not None or in_window(head.date)):
        events.append(
            Event(EventType.PR_PUSH, head.author, head.date, f"{head.sha} {head.message}", allowlisted(head.author))
        )

    for build in snapshot.builds:
        if build.status != "completed" or build.result != "failed" or build.finish_time is None:
            continue
        if not in_window(build.finish_time):
            continue
        if status is not None and status.last_build_id is not None and build.id <= status.last_build_id:
            continue
        events.append(
            Event(
                EventType.BUILD_FAILED,
                build.requested_for,
                build.finish_time,
                f"Build {build.id} failed on {build.source_version}",
                allowlisted(build.requested_for),
            )
        )

    for update in snapshot.updates:
        if update.state_to is None or update.state_to == update.state_from:
            continue
        if not in_window(update.revised_date):
            continue
        if (
            status is not None
            and status.state is not None
            and update.state_to == status.state
            and is_owner(update.revised_by, owner_email)
        ):
            continue
        if update.state_from is not None and update.state_from not in OPEN_STATES and update.state_to in OPEN_STATES:
            event_type = EventType.REOPENED
        else:
            event_type = EventType.STATE_CHANGE
        content = f"{update.state_from or '-'} -> {update.state_to}"
        events.append(Event(event_type, update.revised_by, update.revised_date, content, allowlisted(update.revised_by)))

    return tuple(sorted(events, key=lambda e: e.time))
