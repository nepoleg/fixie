import functools
from dataclasses import replace
from pathlib import Path

import pytest

import fixie.runner as runner
from fixie.fake_gateway import FakeGateway
from fixie.model import BOT_SIGN, Commit, Identity, PrComment, PrThread, TicketUpdate
from fixie.orchestrator import Orchestrator
from fixie.settings import Settings
from fixie.status_comment import find_status_comment, trusted_status
from test_agent_image import _docker_ok, _run, _uuid8, image  # noqa: F401
from test_git_helper import _ref, _sh, cfg, helper_image, mirror_volume, origin, ro_volume  # noqa: F401

pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _docker_ok(), reason="Docker engine not reachable")]

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ado" / "orchestrator.json"
OWNER = Identity("Owner", "owner@example.com")
ALICE = Identity("Alice", "alice@example.com")
QA = Identity("QA", "qa@example.com")


def _settings(cfg):
    return Settings(
        ado_org_url="https://dev.azure.com/org",
        ado_project="P",
        ado_repo="R",
        ticket_tag="fixie",
        default_base_branch="dev",
        allowlist=(),
        claude_model="haiku",
        docker_image="fixie-agent:latest",
        mirror_volume=cfg.mirror_volume,
        gradle_ro_volume=f"fixie-test-nogradle-{_uuid8()}",
        ticket_timeout_s=300,
        max_attempts=3,
        owner_email="owner@example.com",
        state_secret="s",
        azure_token=None,
        claude_oauth_token=None,
        ado_feed_token=None,
        poll_interval_s=1,
        max_parallel=1,
    )


def _assert_no_leftover_container():
    assert _run("docker", "ps", "-a", "-q", "--filter", "label=fixie.ticket=601").stdout.strip() == ""


def test_orchestrator_end_to_end(monkeypatch, helper_image, origin, mirror_volume, ro_volume, cfg, image):
    monkeypatch.setattr(runner, "TICKET_VOLUME_PREFIX", "fixie-test-ticket-")
    runner.kill_ticket("601")
    runner.delete_ticket_volume("601")

    gw = FakeGateway(FIXTURE_PATH, OWNER)
    for tid in list(gw.tickets):
        if tid != 601:
            del gw.tickets[tid]
    s = _settings(cfg)
    orch = Orchestrator(
        gw,
        s,
        helper_config=cfg,
        run_ticket=functools.partial(runner.run_ticket, extra_env={"FIXIE_FAKE_AGENT": "1", "FIXIE_FIREWALL": "off"}),
        ticket_volumes=lambda: runner.ticket_volumes() & {"601"},
        running_tickets=lambda: runner.running_tickets() & {"601"},
    )
    ORIGIN = (f"{origin}:/origin", "/origin/origin.git")

    def poll():
        return [f.result() for f in orch.poll_once()]

    def sync():
        sha = _ref(*ORIGIN, "refs/heads/fixie/601")
        gw.branches["fixie/601"] = Commit(sha, OWNER, gw._tick(), "fake")

    def origin_file():
        return _sh([f"{origin}:/origin"], "git -C /origin/origin.git show fixie/601:FIXIE_FAKE.md").stdout

    try:
        orch.recover()
        assert len(poll()) == 1
        _assert_no_leftover_container()

        sha1 = _ref(*ORIGIN, "refs/heads/fixie/601")
        assert len(sha1) == 40

        prs = gw.find_pull_requests("fixie/601")
        assert len(prs) == 1
        pr = prs[0]
        assert pr.status == "active"
        assert pr.target_branch == "dev"
        assert pr.work_item_ids == (601,)

        comments = gw.get_comments(601)
        assert any(c.text.startswith(f"<p>{BOT_SIGN} fixie: done</p>") for c in comments)

        status = trusted_status(find_status_comment(gw.get_comments(601), s.owner_email), 601, s.owner_email, s.state_secret)
        assert status.result.value == "done"
        assert status.last_finished_run_started == status.run_started
        assert status.head == sha1
        assert status.attempts == 0

        assert origin_file() == "fake run start\n"

        sync()
        writes_before = len(gw.writes)
        assert poll() == []
        assert len(gw.writes) == writes_before
        _assert_no_leftover_container()

        t = gw._tick()
        gw.threads[pr.id].append(
            PrThread(
                id=1,
                status="active",
                file_path="/FIXIE_FAKE.md",
                line=1,
                comments=(PrComment(1, ALICE, "Please say hello", t, t, "text"),),
                published_date=t,
                last_updated_date=t,
            )
        )
        assert len(poll()) == 1
        _assert_no_leftover_container()

        sha2 = _ref(*ORIGIN, "refs/heads/fixie/601")
        assert sha2 != sha1
        assert origin_file() == "fake run start\nfake run resume\n"
        assert len(gw.find_pull_requests("fixie/601")) == 1
        status = trusted_status(find_status_comment(gw.get_comments(601), s.owner_email), 601, s.owner_email, s.state_secret)
        assert status.head == sha2
        sync()

        t = gw._tick()
        gw.tickets[601] = replace(gw.tickets[601], state="Testing")
        gw.updates[601].append(
            TicketUpdate(
                id=10, revised_by=QA, revised_date=t, tags_added=(), tags_removed=(), state_from="Developing", state_to="Testing"
            )
        )
        assert poll() == []
        _assert_no_leftover_container()

        t = gw._tick()
        gw.tickets[601] = replace(gw.tickets[601], state="Developing")
        gw.updates[601].append(
            TicketUpdate(
                id=11, revised_by=QA, revised_date=t, tags_added=(), tags_removed=(), state_from="Testing", state_to="Developing"
            )
        )
        assert len(poll()) == 1
        _assert_no_leftover_container()

        assert origin_file() == "fake run start\nfake run resume\nfake run resume\n"
        new_head = _ref(*ORIGIN, "refs/heads/fixie/601")
        status = trusted_status(find_status_comment(gw.get_comments(601), s.owner_email), 601, s.owner_email, s.state_secret)
        assert status.head == new_head
        sync()

        t = gw._tick()
        gw.tickets[601] = replace(gw.tickets[601], state="Closed")
        gw.updates[601].append(
            TicketUpdate(
                id=12, revised_by=QA, revised_date=t, tags_added=(), tags_removed=(), state_from="Developing", state_to="Closed"
            )
        )
        pr_now = gw.get_pull_request(pr.id)
        gw.pull_requests[pr.id] = replace(pr_now, status="completed")
        assert poll() == []
        _assert_no_leftover_container()

        assert _run("docker", "volume", "ls", "-q", "--filter", "name=fixie-test-ticket-601").stdout.strip() == ""
    finally:
        orch.shutdown()
        runner.kill_ticket("601")
        runner.delete_ticket_volume("601")
