"""Decide what the orchestrator should do with a ticket this poll."""

from fixie.allowlist import is_owner
from fixie.events import collect_events
from fixie.gateway import OPEN_STATES
from fixie.model import Action, Classification, FixieStatus, TicketSnapshot
from fixie.settings import Settings
from fixie.status_comment import is_bot_signed, is_status_text


def classify(snapshot: TicketSnapshot, status: FixieStatus | None, settings: Settings) -> Classification:
    """Classify a ticket into one orchestrator action, from its snapshot and trusted status."""
    ticket = snapshot.ticket

    if ticket.state not in OPEN_STATES:
        if snapshot.pull_request is not None and snapshot.pull_request.status == "active":
            return Classification(Action.SKIP, ())
        return Classification(Action.CLEAN_UP, ())

    tag = settings.ticket_tag.strip().lower()
    if not any(t.strip().lower() == tag for t in ticket.tags):
        return Classification(Action.STOP, ())

    if status is None:
        has_artifacts = snapshot.pull_request is not None or snapshot.head is not None or any(
            is_owner(c.created_by, settings.owner_email) and is_bot_signed(c.text) for c in snapshot.comments
        )
        if not has_artifacts:
            return Classification(Action.NEW, ())
        since = None
        for c in snapshot.comments:
            if is_owner(c.created_by, settings.owner_email) and is_bot_signed(c.text) and not is_status_text(c.text):
                if since is None or c.created_date > since:
                    since = c.created_date
        events = collect_events(snapshot, None, since, settings.owner_email, settings.allowlist)
        return Classification(Action.REBUILD, events, since)

    if status.run_started is None:
        return Classification(Action.NEW, ())

    if status.last_finished_run_started is None or status.run_started > status.last_finished_run_started:
        if status.last_finished_run_started is not None:
            events = collect_events(snapshot, status, status.last_finished_run_started, settings.owner_email, settings.allowlist)
        else:
            events = ()
        action = Action.GIVE_UP if status.attempts >= settings.max_attempts else Action.RETRY
        return Classification(action, events, status.last_finished_run_started)

    events = collect_events(snapshot, status, status.last_finished_run_started, settings.owner_email, settings.allowlist)
    if events:
        return Classification(Action.RESUME, events, status.last_finished_run_started)
    return Classification(Action.SKIP, ())
