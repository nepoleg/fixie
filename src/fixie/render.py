"""Render a ticket snapshot into the input files the agent reads."""

import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from markdownify import markdownify

from fixie.allowlist import is_allowed, is_owner
from fixie.gateway import AdoGateway
from fixie.model import Event, EventType, Identity, TicketSnapshot
from fixie.settings import Settings
from fixie.status_comment import is_bot_signed

_BLOCK_TAG_RE = re.compile(r"(?i)<br\s*/?>|</?(?:p|div|li|h[1-6]|tr)\b[^>]*>")
_ANY_TAG_RE = re.compile(r"<[^>]*>")
_BASE_LINE_RE = re.compile(r"\s*base(?:\s+branch)?\s*:\s*(\S+)\s*", re.IGNORECASE)
_VALID_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_IMG_SRC_RE = re.compile(r"(<img\b[^>]*?\bsrc\s*=\s*)([\"'])(.*?)\2", re.IGNORECASE | re.DOTALL)
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


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


def _key(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flat(s: str) -> str:
    return " ".join(s.split())


def _name(who: Identity) -> str:
    return f"{_flat(who.display_name)} <{who.unique_name}>"


def _who(who: Identity, settings: Settings) -> str:
    suffix = "" if is_allowed(who, settings.allowlist, settings.owner_email) else " (not allowlisted)"
    return _name(who) + suffix


def _fixie(who: Identity, text: str, settings: Settings) -> bool:
    return is_owner(who, settings.owner_email) and is_bot_signed(text)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _local_attachment_names(gateway: AdoGateway, snapshot: TicketSnapshot) -> tuple[list, dict[str, str]]:
    attachments = gateway.get_attachments(snapshot.ticket.id)
    used: set[str] = set()
    by_url: dict[str, str] = {}
    named: list[tuple[object, str]] = []
    for attachment in attachments:
        safe = _SANITIZE_RE.sub("_", attachment.name).lstrip(".") or "attachment"
        local = safe
        if local.lower() in used:
            stem, suffix = Path(safe).stem, Path(safe).suffix
            n = 2
            while f"{stem}-{n}{suffix}".lower() in used:
                n += 1
            local = f"{stem}-{n}{suffix}"
        used.add(local.lower())
        by_url[_key(attachment.url)] = local
        named.append((attachment, local))
    return named, by_url


def _md(text: str, image_map: dict[str, str]) -> str:
    def rewrite(match: re.Match) -> str:
        src = match.group(3)
        local = image_map.get(_key(html.unescape(src)))
        if local is None:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}attachments/{local}{match.group(2)}"

    rewritten = _IMG_SRC_RE.sub(rewrite, text)
    return markdownify(
        rewritten,
        heading_style="ATX",
        bullets="-",
        newline_style="backslash",
        escape_underscores=False,
        escape_asterisks=False,
    ).replace("\r\n", "\n").strip()


def _description_editor(snapshot: TicketSnapshot) -> Identity | None:
    editor = None
    for update in snapshot.updates:
        if update.description_changed:
            editor = update.revised_by
    return editor


def _ticket_fields(snapshot: TicketSnapshot, settings: Settings) -> str:
    t = snapshot.ticket
    lines = [
        f"- Type: {t.work_item_type}",
        f"- State: {t.state}",
        f"- Tags: {', '.join(t.tags)}",
        f"- Base branch: {parse_base_branch(t.description, settings.default_base_branch)}",
    ]
    editor = _description_editor(snapshot)
    if editor is not None:
        lines.append(f"- Description last edited by: {_who(editor, settings)}")
    return "\n".join(lines)


def _ticket_sections(snapshot: TicketSnapshot, image_map: dict[str, str]) -> list[str]:
    t = snapshot.ticket
    blocks = []
    for heading, value in (
        ("Description", t.description),
        ("Acceptance criteria", t.acceptance_criteria),
        ("Repro steps", t.repro_steps),
    ):
        rendered = _md(value, image_map)
        if rendered:
            blocks.append(f"## {heading}")
            blocks.append(_quote(rendered))
    return blocks


def _attachments_section(named: list) -> list[str]:
    if not named:
        return []
    lines = "\n".join(f"- [{local}](attachments/{local})" for _, local in named)
    return ["## Attachments", lines]


def _comments_section(snapshot: TicketSnapshot, settings: Settings, image_map: dict[str, str]) -> list[str]:
    blocks = ["## Comments"]
    kept = [c for c in snapshot.comments if not _fixie(c.modified_by, c.text, settings)]
    if not kept:
        blocks.append("(none)")
        return blocks
    for comment in kept:
        blocks.append(f"### {_who(comment.modified_by, settings)}, {_iso(comment.created_date)}")
        blocks.append(_quote(_md(comment.text, image_map) or "(empty)"))
    return blocks


def _write_ticket_md(
    snapshot: TicketSnapshot, settings: Settings, named: list, image_map: dict, target: Path
) -> None:
    t = snapshot.ticket
    blocks = [f"# {t.id}: {_flat(t.title)}", _ticket_fields(snapshot, settings)]
    blocks.extend(_ticket_sections(snapshot, image_map))
    blocks.extend(_attachments_section(named))
    blocks.extend(_comments_section(snapshot, settings, image_map))
    text = "\n\n".join(blocks) + "\n"
    (target / "ticket.md").write_text(text, encoding="utf-8", newline="\n")


def _write_new_json(events: tuple[Event, ...], image_map: dict[str, str], target: Path) -> None:
    items = []
    for e in events:
        content = _md(e.content, image_map) if e.type == EventType.TICKET_COMMENT else e.content
        items.append(
            {
                "type": e.type.value,
                "author": _name(e.author),
                "time": _iso(e.time),
                "content": content,
                "allowlisted": e.allowlisted,
            }
        )
    text = json.dumps(items, indent=2, ensure_ascii=False) + "\n"
    (target / "new.json").write_text(text, encoding="utf-8", newline="\n")


def _thread_location(thread) -> str:
    if thread.file_path is not None and thread.line is not None:
        return f"{_flat(thread.file_path)}:{thread.line}"
    if thread.file_path is not None:
        return _flat(thread.file_path)
    return "general"


def _pr_md_text(snapshot: TicketSnapshot, settings: Settings) -> str:
    pr = snapshot.pull_request
    blocks = [
        f"# PR {pr.id}: {_flat(pr.title)}",
        f"- Source branch: {pr.source_branch}\n- Target branch: {pr.target_branch}\n- Status: {pr.status}",
        "## Active threads",
    ]
    thread_blocks: list[str] = []
    for thread in snapshot.threads:
        if thread.status not in ("active", "pending"):
            continue
        kept = [c for c in thread.comments if c.comment_type == "text" and not _fixie(c.author, c.content, settings)]
        if not kept:
            continue
        thread_blocks.append(f"### Thread {thread.id}, {_thread_location(thread)}, {thread.status}")
        for comment in kept:
            thread_blocks.append(f"#### {_who(comment.author, settings)}, {_iso(comment.published_date)}")
            body = comment.content.replace("\r\n", "\n").strip() or "(empty)"
            thread_blocks.append(_quote(body))
    if thread_blocks:
        blocks.extend(thread_blocks)
    else:
        blocks.append("(none)")
    return "\n\n".join(blocks) + "\n"


def write_input(
    gateway: AdoGateway,
    snapshot: TicketSnapshot,
    events: tuple[Event, ...],
    settings: Settings,
    target: Path,
) -> None:
    """Write ticket.md, new.json, pr.md and attachments/ for one ticket into target."""
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise FileExistsError(str(target))
    attachments_dir = target / "attachments"
    attachments_dir.mkdir()

    named, image_map = _local_attachment_names(gateway, snapshot)
    # ponytail: no size limit on downloads, and Windows reserved names (e.g. CON) are not handled.
    for attachment, local in named:
        content = gateway.download_attachment(attachment.url)
        (attachments_dir / local).write_bytes(content)

    _write_ticket_md(snapshot, settings, named, image_map, target)
    _write_new_json(events, image_map, target)
    if snapshot.pull_request is not None:
        text = _pr_md_text(snapshot, settings)
        (target / "pr.md").write_text(text, encoding="utf-8", newline="\n")
