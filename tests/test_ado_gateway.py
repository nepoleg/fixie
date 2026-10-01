import inspect
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from fixie.ado_gateway import RealAdoGateway, _comment, _ticket_from_json, _update, attachment_id, split_tags, wiql_quote
from fixie.allowlist import find_tagger
from fixie.gateway import AdoGateway
from fixie.settings import load_settings
from test_gateway_contract import GATEWAY_METHODS

REPO_ROOT = Path(__file__).parents[1]
LIVE_TICKET = 42591


def _who(name, email):
    return NS(display_name=name, unique_name=email)


def _field(old, new):
    return NS(old_value=old, new_value=new)


def test_real_gateway_matches_interface():
    for name in GATEWAY_METHODS:
        assert list(inspect.signature(getattr(RealAdoGateway, name)).parameters) == list(
            inspect.signature(getattr(AdoGateway, name)).parameters
        )
    assert not RealAdoGateway.__abstractmethods__


def test_split_tags_trims_and_drops_empty():
    assert split_tags(" fixie ;backend;; ") == ("fixie", "backend")
    assert split_tags(None) == ()


def test_attachment_id_only_from_ado_attachment_urls():
    guid = "0F6B7A3C-1111-2222-3333-444455556666"
    assert attachment_id(f"https://dev.azure.com/o/p/_apis/wit/attachments/{guid}?fileName=a.png") == guid.lower()
    assert attachment_id(f"https://dev.azure.com/o/_apis/wit/attachments/{guid}") == guid.lower()
    assert attachment_id("https://evil.example.com/x.png") is None


def test_wiql_quote():
    assert wiql_quote("o'brien") == "o''brien"


def test_update_tags_state_time_and_description():
    u = NS(
        id=3,
        revised_by=_who(" Bob ", " bob@example.com "),
        revised_date=datetime(9999, 1, 1, tzinfo=UTC),
        fields={
            "System.Tags": _field("backend; Fixie-2", "backend; fixie"),
            "System.State": _field("New", "Developing"),
            "System.ChangedDate": _field("2026-10-01T16:00:00Z", "2026-10-01T16:52:18.72Z"),
            "System.Description": _field("a", "b"),
        },
    )
    got = _update(u)
    assert got.revised_by.unique_name == "bob@example.com"
    assert got.revised_date == datetime(2026, 10, 1, 16, 52, 18, 720000, tzinfo=UTC)
    assert got.tags_added == ("fixie",)
    assert got.tags_removed == ("Fixie-2",)
    assert (got.state_from, got.state_to, got.description_changed) == ("New", "Developing", True)


def test_update_without_changed_date_skipped():
    assert _update(NS(id=1, revised_by=_who("a", "b"), revised_date=None, fields={})) is None


def test_ticket_markdown_fields_unescaped_html_fields_kept():
    data = {
        "id": 7,
        "fields": {
            "System.Title": "T",
            "System.WorkItemType": "Bug",
            "System.State": "New",
            "System.Tags": "fixie",
            "System.Description": "a &gt; b",
            "Microsoft.VSTS.TCM.ReproSteps": "<p>x &gt; y</p>",
            "System.ChangedDate": "2026-10-01T16:52:18.72Z",
            "Microsoft.VSTS.Common.Priority": 1,
        },
        "multilineFieldsFormat": {"System.Description": "markdown"},
    }
    t = _ticket_from_json(data)
    assert t.description == "a > b"
    assert t.repro_steps == "<p>x &gt; y</p>"
    assert t.acceptance_criteria == ""
    assert t.markdown_fields == frozenset({"description"})
    assert (t.tags, t.priority) == (("fixie",), 1)


def test_markdown_comment_uses_rendered_html():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    c = NS(id=1, text="**hi**", rendered_text="<p><strong>hi</strong></p>", format="markdown",
           created_by=_who("A", "a@x"), created_date=now, modified_by=None, modified_date=None)
    got = _comment(c)
    assert got.text == "<p><strong>hi</strong></p>"
    assert got.modified_by.unique_name == "a@x" and got.modified_date == now


@pytest.fixture(scope="module")
def live():
    if os.environ.get("FIXIE_LIVE") != "1":
        pytest.skip("set FIXIE_LIVE=1 to read real ADO")
    s = load_settings(REPO_ROOT / "config.toml", REPO_ROOT / ".env")
    if not s.azure_token:
        pytest.skip("AZURE_TOKEN missing in .env")
    return RealAdoGateway(s.ado_org_url, s.ado_project, s.ado_repo, s.azure_token), s


@pytest.mark.live
def test_live_reads_test_ticket(live):
    gw, s = live
    assert LIVE_TICKET in gw.find_tagged_tickets(s.ticket_tag)
    t = gw.get_ticket(LIVE_TICKET)
    assert "fixie" in t.tags and "description" in t.markdown_fields
    assert "Settings > Editor" in t.description
    assert t.changed_date.tzinfo is not None
    tagger = find_tagger(gw.get_updates(LIVE_TICKET), s.ticket_tag)
    assert tagger is not None and tagger.unique_name.lower() == s.owner_email
    assert isinstance(gw.get_comments(LIVE_TICKET), list)
    assert gw.get_attachments(LIVE_TICKET) == []
    assert gw.find_pull_requests(f"fixie/{LIVE_TICKET}") == []
    assert gw.get_branch_head(f"fixie/{LIVE_TICKET}") is None
    assert gw.get_branch_head(s.default_base_branch) is not None
    with pytest.raises(KeyError):
        gw.get_ticket(999_999_999)
