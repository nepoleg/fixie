import inspect

from fixie.dry_run import DryRunGateway, dry_run_orchestrator
from fixie.fake_gateway import FakeGateway
from fixie.gateway import AdoGateway
from fixie.model import Identity
from test_gateway_contract import FIXTURE_PATH, GATEWAY_METHODS
from test_render import SETTINGS

OWNER = Identity("Owner", "owner@example.com")


def _gw():
    fake = FakeGateway(FIXTURE_PATH, OWNER)
    return fake, DryRunGateway(fake, OWNER)


def test_dry_run_implements_every_method():
    _, dry = _gw()
    assert isinstance(dry, AdoGateway)
    for name in GATEWAY_METHODS:
        params = list(inspect.signature(getattr(DryRunGateway, name)).parameters)
        assert params[0] == "self"


def test_reads_pass_through():
    fake, dry = _gw()
    assert dry.find_tagged_tickets("fixie") == fake.find_tagged_tickets("fixie")
    assert dry.get_ticket(101) == fake.get_ticket(101)
    assert dry.get_comments(101) == fake.get_comments(101)


def test_writes_logged_not_applied():
    fake, dry = _gw()
    before = (fake.get_comments(101), fake.get_ticket(101).tags)
    c = dry.add_comment(101, "hi")
    dry.edit_comment(101, 5, "x")
    dry.remove_tag(101, "fixie")
    pr = dry.create_pull_request("fixie/101", "dev", "t", "d")
    dry.link_work_item(pr.id, 101)
    dry.attach_file(101, "log.gz", b"abc")
    assert c.text == "hi" and c.modified_by == OWNER
    assert [m for m, _ in dry.writes] == [
        "add_comment", "edit_comment", "remove_tag", "create_pull_request", "link_work_item", "attach_file",
    ]
    assert fake.writes == []
    assert (fake.get_comments(101), fake.get_ticket(101).tags) == before


def test_dry_run_orchestrator_saves_input_and_writes_nothing(tmp_path):
    fake, dry = _gw()
    orch = dry_run_orchestrator(dry, SETTINGS, tmp_path)
    try:
        orch.recover()
        for f in orch.poll_once():
            f.result()
    finally:
        orch.shutdown()
    assert fake.writes == []
    saved = sorted(p.name for p in tmp_path.iterdir())
    assert saved and all((tmp_path / t / "ticket.json").is_file() for t in saved)
