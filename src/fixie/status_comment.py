"""Format, find and verify the signed status comment fixie keeps on a ticket."""

import base64
import hashlib
import hmac
import html
import json
import re
from datetime import datetime

from fixie.allowlist import is_owner
from fixie.model import BOT_SIGN, Comment, FixieStatus, RunStatus

STATUS_HEADER = f"{BOT_SIGN} fixie status"

_STATE_RE = re.compile(r"fixie-state:([A-Za-z0-9_=-]+)\.([0-9a-f]{64})")
_FIELDS = ("run_started", "last_finished_run_started", "result", "pr_id", "head", "state", "last_build_id", "attempts")


def _sign(ticket_id: int, payload: str, secret: str) -> str:
    message = f"{ticket_id}:{payload}".encode("ascii")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _encode(status: FixieStatus) -> str:
    d = {
        "run_started": status.run_started.isoformat() if status.run_started is not None else None,
        "last_finished_run_started": (
            status.last_finished_run_started.isoformat() if status.last_finished_run_started is not None else None
        ),
        "result": status.result.value if status.result is not None else None,
        "pr_id": status.pr_id,
        "head": status.head,
        "state": status.state,
        "last_build_id": status.last_build_id,
        "attempts": status.attempts,
    }
    payload = json.dumps(d, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode(payload: str) -> FixieStatus:
    d = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    if set(d) != set(_FIELDS):
        raise ValueError("unexpected payload keys")
    return FixieStatus(
        run_started=datetime.fromisoformat(d["run_started"]) if d["run_started"] is not None else None,
        last_finished_run_started=(
            datetime.fromisoformat(d["last_finished_run_started"])
            if d["last_finished_run_started"] is not None
            else None
        ),
        result=RunStatus(d["result"]) if d["result"] is not None else None,
        pr_id=d["pr_id"],
        head=d["head"],
        state=d["state"],
        last_build_id=d["last_build_id"],
        attempts=d["attempts"],
    )


def _visible(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, RunStatus):
        return value.value
    return str(value)


def format_status_comment(ticket_id: int, status: FixieStatus, secret: str) -> str:
    """Render the signed HTML status comment fixie posts or edits on a ticket."""
    payload = _encode(status)
    sig = _sign(ticket_id, payload, secret)
    lines = [
        STATUS_HEADER,
        "<ul>",
        f"<li>Run started: {html.escape(_visible(status.run_started))}</li>",
        f"<li>Last finished run started: {html.escape(_visible(status.last_finished_run_started))}</li>",
        f"<li>Result: {html.escape(_visible(status.result))}</li>",
        f"<li>PR: {html.escape(_visible(status.pr_id))}</li>",
        f"<li>Head: {html.escape(_visible(status.head))}</li>",
        f"<li>State set by fixie: {html.escape(_visible(status.state))}</li>",
        f"<li>Last seen build: {html.escape(_visible(status.last_build_id))}</li>",
        f"<li>Attempts: {html.escape(_visible(status.attempts))}</li>",
        "</ul>",
        f"<p>Machine state, do not edit: <code>fixie-state:{payload}.{sig}</code></p>",
    ]
    return "\n".join(lines)


def _plain(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", text)).split())


def _first_text_run(text: str) -> str:
    for part in re.split(r"<[^>]*>", text):
        run = " ".join(html.unescape(part).split())
        if run:
            return run
    return ""


def is_bot_signed(text: str) -> bool:
    """Return whether text, once HTML-stripped, starts with the bot sign."""
    return _plain(text).startswith(BOT_SIGN)


def is_status_text(text: str) -> bool:
    """Return whether text's first HTML text run is exactly the status header."""
    return _first_text_run(text) == STATUS_HEADER


def find_status_comment(comments: list[Comment], owner_email: str) -> Comment | None:
    """Return the oldest comment fixie itself created (its status comment), or None."""
    for comment in comments:
        if is_status_text(comment.text) and is_owner(comment.created_by, owner_email):
            return comment
    return None


def trusted_status(comment: Comment | None, ticket_id: int, owner_email: str, secret: str) -> FixieStatus | None:
    """Return the comment's FixieStatus if its signature and editor are trusted, else None."""
    if comment is None:
        return None
    if not is_owner(comment.modified_by, owner_email):
        return None
    match = _STATE_RE.search(comment.text)
    if match is None:
        return None
    payload, found_sig = match.group(1), match.group(2)
    expected_sig = _sign(ticket_id, payload, secret)
    if not hmac.compare_digest(expected_sig, found_sig):
        return None
    try:
        return _decode(payload)
    except (ValueError, KeyError, TypeError):
        return None
