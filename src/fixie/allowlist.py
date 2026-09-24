"""Who added the fixie tag to a ticket, and whether that person may trigger a run."""

from fixie.model import Identity, TicketUpdate


def is_owner(who: Identity | None, owner_email: str) -> bool:
    """Return whether who is the owner account, trimmed and case-insensitive."""
    if who is None or not who.unique_name.strip():
        return False
    return who.unique_name.strip().lower() == owner_email.strip().lower()


def find_tagger(updates: list[TicketUpdate], tag: str) -> Identity | None:
    """Return who added tag in the most recent update that added it, or None."""
    tag_clean = tag.strip().lower()
    tagger = None
    for update in updates:
        if any(t.strip().lower() == tag_clean for t in update.tags_added):
            tagger = update.revised_by
    return tagger


def is_allowed(who: Identity | None, allowlist: tuple[str, ...], owner_email: str) -> bool:
    """Return whether who may trigger fixie: the owner always, else the allowlist."""
    if who is None or not who.unique_name.strip():
        return False
    if is_owner(who, owner_email):
        return True
    return who.unique_name.strip().lower() in {a.strip().lower() for a in allowlist if a.strip()}
