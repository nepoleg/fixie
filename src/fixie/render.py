"""Render a ticket snapshot into ticket.json, the one input file the agent reads."""

import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from fixie.model import Event, EventType, Identity, TicketSnapshot
from fixie.settings import Settings

_BLOCK_TAG_RE = re.compile(r"(?i)<br\s*/?>|</?(?:p|div|li|h[1-6]|tr)\b[^>]*>")
_ANY_TAG_RE = re.compile(r"<[^>]*>")
_BASE_LINE_RE = re.compile(r"\s*base(?:\s+branch)?\s*:\s*(\S+)\s*", re.IGNORECASE)
_VALID_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_DETAIL_TYPES = (EventType.PR_PUSH, EventType.REOPENED, EventType.STATE_CHANGE, EventType.BUILD_FAILED)


def parse_base_branch(description: str, default: str) -> str:
    """Return the base branch a ticket description names, or default if none is valid."""
    text = _BLOCK_TAG_RE.sub("\n", description)
    text = _ANY_TAG_RE.sub("", text)
    text = html.unescape(text)
    for line in text.splitlines():
        match = _BASE_LINE_RE.fullmatch(line)
        if match is None:
            continue
        value = match.group(1)
        if _is_valid_branch(value):
            return value
    return default


def _is_valid_branch(value: str) -> bool:
    return (
        _VALID_BRANCH_RE.fullmatch(value) is not None
        and ".." not in value
        and not value.endswith("/")
        and not value.endswith(".lock")
    )


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if t is not None else None


def _who(who: Identity) -> str:
    return who.unique_name or who.display_name


def _field(snapshot: TicketSnapshot, name: str) -> dict[str, str] | None:
    value = getattr(snapshot.ticket, name)
    if not value.strip():
        return None
    return {"format": "markdown" if name in snapshot.ticket.markdown_fields else "html", "text": value}


def _event(e: Event) -> dict:
    item = {"type": e.type.value, "time": _iso(e.time), "author": _who(e.author)}
    for key in ("pr", "thread", "comment", "build"):
        value = getattr(e, key)
        if value is not None:
            item[key] = value
    if e.type in _DETAIL_TYPES:
        item["detail"] = e.content
    return item


def ticket_manifest(snapshot: TicketSnapshot, events: tuple[Event, ...], since: datetime | None, settings: Settings) -> dict:
    """The ticket body plus pointers to everything new; the agent reads the rest from ADO itself."""
    t = snapshot.ticket
    pr = snapshot.pull_request
    fields = {name: _field(snapshot, name) for name in ("description", "acceptance_criteria", "repro_steps")}
    return {
        "ticket": {
            "id": t.id,
            "title": t.title,
            "type": t.work_item_type,
            "state": t.state,
            "tags": list(t.tags),
            **{name: value for name, value in fields.items() if value is not None},
        },
        "branch": f"fixie/{t.id}",
        "base_branch": parse_base_branch(t.description, settings.default_base_branch),
        "pull_request": {"id": pr.id, "status": pr.status} if pr is not None else None,
        "since": _iso(since),
        "events": [_event(e) for e in events],
    }


def write_input(
    snapshot: TicketSnapshot, events: tuple[Event, ...], since: datetime | None, settings: Settings, target: Path
) -> None:
    """Write ticket.json for one ticket into target, which must be empty or missing."""
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise FileExistsError(str(target))
    text = json.dumps(ticket_manifest(snapshot, events, since, settings), indent=2, ensure_ascii=False) + "\n"
    (target / "ticket.json").write_text(text, encoding="utf-8", newline="\n")
