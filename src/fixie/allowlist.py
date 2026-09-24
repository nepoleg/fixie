"""Who added the fixie tag to a ticket, and whether that person may trigger a run."""

from fixie.model import Identity, TicketUpdate


def find_tagger(updates: list[TicketUpdate], tag: str) -> Identity | None:
    """Return who added tag in the most recent update that added it, or None."""
    tag_lower = tag.lower()
    tagger = None
    for update in updates:
        if any(t.lower() == tag_lower for t in update.tags_added):
            tagger = update.revised_by
    return tagger


def is_allowed(who: Identity | None, allowlist: tuple[str, ...], owner_email: str) -> bool:
    """Return whether who may trigger fixie, per allowlist (empty means owner only)."""
    if who is None:
        return False
    email = who.unique_name.lower()
    if not allowlist:
        return email == owner_email.lower()
    return email in {a.lower() for a in allowlist}
