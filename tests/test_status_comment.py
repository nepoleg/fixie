import base64
import hashlib
import hmac
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fixie.fake_gateway import FakeGateway
from fixie.model import Comment, FixieStatus, Identity, RunStatus
from fixie.status_comment import STATUS_HEADER, find_status_comment, format_status_comment, trusted_status

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ado" / "contract.json"

OWNER = Identity("Owner", "owner@example.com")
ALICE = Identity("Alice", "alice@example.com")
BOB = Identity("Bob", "bob@example.com")
SECRET = "secret"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

STATE_RE = re.compile(r"fixie-state:([A-Za-z0-9_=-]+)\.([0-9a-f]{64})")

FULL = FixieStatus(
    run_started=T0,
    last_finished_run_started=datetime(2026, 2, 28, 9, 30, tzinfo=UTC),
    result=RunStatus.DONE,
    pr_id=7,
    head="abc123",
    state="Code Review",
    last_build_id=56,
    attempts=2,
)


def _comment(text, created_by=OWNER, modified_by=OWNER, id=1):
    return Comment(id=id, text=text, created_by=created_by, created_date=T0, modified_by=modified_by, modified_date=T0)


def _signed(payload_bytes):
    p = base64.urlsafe_b64encode(payload_bytes).decode()
    sig = hmac.new(b"secret", f"101:{p}".encode(), hashlib.sha256).hexdigest()
    text = f"🤖 fixie status\n<code>fixie-state:{p}.{sig}</code>"
    return trusted_status(_comment(text), 101, "owner@example.com", SECRET)


def test_format_starts_with_header_and_lists_fields():
    text = format_status_comment(101, FULL, SECRET)
    assert text.startswith("🤖 fixie status\n<ul>")
    assert STATUS_HEADER == "🤖 fixie status"
    assert "<li>Run started: 2026-03-01T12:00:00+00:00</li>" in text
    assert "<li>Result: done</li>" in text
    assert "<li>PR: 7</li>" in text
    assert "<li>Head: abc123</li>" in text
    assert "<li>State set by fixie: Code Review</li>" in text
    assert "<li>Last seen build: 56</li>" in text
    assert "<li>Attempts: 2</li>" in text
    assert STATE_RE.search(text) is not None
    assert "<!--" not in text


def test_format_is_deterministic():
    a = format_status_comment(101, FULL, SECRET)
    b = format_status_comment(101, FULL, SECRET)
    assert a == b
    empty = format_status_comment(101, FixieStatus(), SECRET)
    assert "<li>PR: -</li>" in empty
    assert "<li>Result: -</li>" in empty


def test_valid_comment_round_trips():
    text = format_status_comment(101, FULL, SECRET)
    assert trusted_status(_comment(text), 101, "owner@example.com", SECRET) == FULL


def test_default_status_round_trips():
    text = format_status_comment(101, FixieStatus(), SECRET)
    assert trusted_status(_comment(text), 101, "owner@example.com", SECRET) == FixieStatus()


def test_edited_payload_untrusted():
    a = format_status_comment(101, FULL, SECRET)
    b = format_status_comment(101, replace(FULL, attempts=3), SECRET)
    a_payload, a_sig = STATE_RE.search(a).groups()
    b_payload, _ = STATE_RE.search(b).groups()
    forged = a.replace(f"fixie-state:{a_payload}.{a_sig}", f"fixie-state:{b_payload}.{a_sig}")
    assert trusted_status(_comment(forged), 101, "owner@example.com", SECRET) is None


def test_bad_signature_untrusted():
    a = format_status_comment(101, FULL, SECRET)
    payload, sig = STATE_RE.search(a).groups()
    flipped_last = "0" if sig[-1] == "1" else ("1" if sig[-1] == "0" else "0")
    bad_sig = sig[:-1] + flipped_last
    forged = a.replace(f"fixie-state:{payload}.{sig}", f"fixie-state:{payload}.{bad_sig}")
    assert trusted_status(_comment(forged), 101, "owner@example.com", SECRET) is None


def test_missing_signature_untrusted():
    a = format_status_comment(101, FULL, SECRET)
    payload, sig = STATE_RE.search(a).groups()
    forged = a.replace(f".{sig}", "")
    assert trusted_status(_comment(forged), 101, "owner@example.com", SECRET) is None
    assert trusted_status(_comment("🤖 fixie status"), 101, "owner@example.com", SECRET) is None


def test_wrong_secret_untrusted():
    a = format_status_comment(101, FULL, "other")
    assert trusted_status(_comment(a), 101, "owner@example.com", SECRET) is None


def test_signature_bound_to_ticket():
    a = format_status_comment(101, FULL, SECRET)
    assert trusted_status(_comment(a), 102, "owner@example.com", SECRET) is None
    assert trusted_status(_comment(a), 101, "owner@example.com", SECRET) == FULL


def test_wrong_editor_untrusted():
    a = format_status_comment(101, FULL, SECRET)
    assert trusted_status(_comment(a, created_by=OWNER, modified_by=BOB), 101, "owner@example.com", SECRET) is None


def test_owner_email_case_insensitive():
    a = format_status_comment(101, FULL, SECRET)
    upper_owner = Identity("Owner", "Owner@Example.COM")
    assert trusted_status(_comment(a, modified_by=upper_owner), 101, "owner@example.com", SECRET) == FULL
    assert trusted_status(_comment(a, modified_by=OWNER), 101, "OWNER@example.com", SECRET) == FULL


def test_missing_comment_untrusted():
    assert trusted_status(None, 101, "owner@example.com", SECRET) is None
    assert find_status_comment([], "owner@example.com") is None
    assert find_status_comment([_comment("<p>hi</p>")], "owner@example.com") is None


def test_find_status_comment_owner_created_only():
    text = format_status_comment(101, FULL, SECRET)
    comments = [
        _comment(text, created_by=ALICE, id=1),
        _comment("🤖 PR created", created_by=OWNER, id=2),
        _comment(text, created_by=OWNER, id=3),
        _comment(text, created_by=OWNER, id=4),
    ]
    assert find_status_comment(comments, "owner@example.com").id == 3
    assert find_status_comment(comments, "OWNER@EXAMPLE.COM").id == 3


def test_visible_edit_keeps_payload():
    a = format_status_comment(101, FULL, SECRET)
    edited = a.replace("<li>Attempts: 2</li>", "<li>Attempts: 9</li>")
    result = trusted_status(_comment(edited), 101, "owner@example.com", SECRET)
    assert result.attempts == 2


def test_visible_values_html_escaped():
    s = replace(FULL, state="A<b>&")
    text = format_status_comment(101, s, SECRET)
    assert "State set by fixie: A&lt;b&gt;&amp;" in text
    assert "A<b>&" not in text
    assert trusted_status(_comment(text), 101, "owner@example.com", SECRET) == s


def test_signed_garbage_payload_untrusted():
    assert _signed(b"not json") is None
    assert _signed(b'{"attempts":0}') is None
    full_dict = {
        "run_started": None,
        "last_finished_run_started": None,
        "result": None,
        "pr_id": None,
        "head": None,
        "state": None,
        "last_build_id": None,
        "attempts": 0,
    }
    assert _signed(json.dumps(full_dict).encode()) == FixieStatus()


def test_fake_gateway_add_and_edit_round_trip():
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    c = gw.add_comment(103, format_status_comment(103, FULL, SECRET))
    assert find_status_comment(gw.get_comments(103), "owner@example.com") == c
    assert trusted_status(c, 103, "owner@example.com", SECRET) == FULL

    gw.edit_comment(103, c.id, format_status_comment(103, replace(FULL, attempts=3), SECRET))
    found = find_status_comment(gw.get_comments(103), "owner@example.com")
    assert found.id == c.id
    assert trusted_status(found, 103, "owner@example.com", SECRET).attempts == 3


def test_contract_fixture_comment_12_untrusted():
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    found = find_status_comment(gw.get_comments(101), "owner@example.com")
    assert found.id == 12
    assert trusted_status(found, 101, "owner@example.com", SECRET) is None
