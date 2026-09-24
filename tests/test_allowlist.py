from datetime import UTC, datetime, timedelta
from pathlib import Path

from fixie.allowlist import find_tagger, is_allowed, is_owner
from fixie.fake_gateway import FakeGateway
from fixie.model import Identity, TicketUpdate

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ado" / "contract.json"

OWNER = Identity("Owner", "owner@example.com")
ALICE = Identity("Alice", "alice@example.com")
BOB = Identity("Bob", "bob@example.com")
MALLORY = Identity("Mallory", "mallory@example.com")
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _update(id, who, added=(), removed=()):
    return TicketUpdate(
        id=id,
        revised_by=who,
        revised_date=T0 + timedelta(hours=id),
        tags_added=added,
        tags_removed=removed,
        state_from=None,
        state_to=None,
    )


def test_tagger_is_author_of_latest_add():
    updates = [_update(1, BOB), _update(2, ALICE, added=("backend", "fixie")), _update(3, BOB)]
    assert find_tagger(updates, "fixie") == ALICE


def test_allowed():
    assert is_allowed(ALICE, ("alice@example.com",), "owner@example.com") is True


def test_not_allowed():
    assert is_allowed(MALLORY, ("alice@example.com",), "owner@example.com") is False


def test_removed_and_readded_latest_add_wins():
    updates = [
        _update(1, ALICE, added=("fixie",)),
        _update(2, BOB, removed=("fixie",)),
        _update(3, MALLORY, added=("fixie",)),
    ]
    tagger = find_tagger(updates, "fixie")
    assert tagger == MALLORY
    assert is_allowed(tagger, ("alice@example.com",), "owner@example.com") is False

    updates2 = [
        _update(1, MALLORY, added=("fixie",)),
        _update(2, BOB, removed=("fixie",)),
        _update(3, ALICE, added=("fixie",)),
    ]
    tagger2 = find_tagger(updates2, "fixie")
    assert tagger2 == ALICE
    assert is_allowed(tagger2, ("alice@example.com",), "owner@example.com") is True


def test_empty_allowlist_owner_only():
    assert is_allowed(OWNER, (), "owner@example.com") is True
    assert is_allowed(ALICE, (), "owner@example.com") is False


def test_owner_always_allowed():
    assert is_allowed(OWNER, ("alice@example.com",), "owner@example.com") is True
    assert is_allowed(Identity("O", " Owner@Example.com "), ("alice@example.com",), "owner@example.com") is True


def test_emails_and_tags_trimmed():
    assert is_allowed(Identity("A", " alice@example.com "), (" Alice@example.com ",), "owner@example.com") is True
    assert is_allowed(Identity("E", ""), ("",), "owner@example.com") is False
    assert is_allowed(Identity("E", " "), (" ",), "owner@example.com") is False
    assert find_tagger([_update(1, ALICE, added=(" fixie ",))], "fixie") == ALICE


def test_is_owner():
    assert is_owner(OWNER, "OWNER@example.com ") is True
    assert is_owner(ALICE, "owner@example.com") is False
    assert is_owner(None, "owner@example.com") is False
    assert is_owner(Identity("E", ""), "") is False


def test_email_case_insensitive():
    assert is_allowed(Identity("A", "Alice@Example.COM"), ("alice@example.com",), "owner@example.com") is True
    assert is_allowed(ALICE, ("ALICE@example.com",), "owner@example.com") is True
    assert is_allowed(Identity("O", "OWNER@example.com"), (), "Owner@Example.com") is True


def test_tag_match_case_insensitive_and_exact():
    assert find_tagger([_update(1, ALICE, added=("Fixie",))], "fixie") == ALICE
    assert find_tagger([_update(1, ALICE, added=("fixie-2",))], "fixie") is None


def test_no_tag_add_means_not_allowed():
    assert find_tagger([_update(1, ALICE, added=("backend",))], "fixie") is None
    assert find_tagger([], "fixie") is None
    assert is_allowed(None, ("alice@example.com",), "owner@example.com") is False
    assert is_allowed(None, (), "owner@example.com") is False


def test_contract_fixture_ticket_101():
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    tagger = find_tagger(gw.get_updates(101), "fixie")
    assert tagger.unique_name == "alice@example.com"
    assert is_allowed(tagger, ("alice@example.com",), "owner@example.com") is True
    assert is_allowed(tagger, (), "owner@example.com") is False
