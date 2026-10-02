import concurrent.futures
import json
import logging
import threading
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fixie.fake_gateway import FakeGateway
from fixie.helper import MIRROR_LOCK
from fixie.model import BOT_SIGN, Comment, FixieStatus, Identity, RunResult, RunStatus
from fixie.orchestrator import Orchestrator
from fixie.runner import RunnerError, RunOutcome
from fixie.settings import Settings
from fixie.status_comment import find_status_comment, format_status_comment, is_bot_signed, trusted_status

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ado" / "orchestrator.json"
OWNER = Identity("Owner", "owner@example.com")
OWNER_EMAIL = "owner@example.com"
ALICE = Identity("Alice", "alice@example.com")
SECRET = "secret"
T1 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)

SETTINGS = Settings(
    ado_org_url="https://example.invalid",
    ado_project="P",
    ado_repo="R",
    ticket_tag="fixie",
    default_base_branch="dev",
    allowlist=(),
    claude_model="m",
    docker_image="i",
    mirror_volume="mv",
    gradle_ro_volume="gv",
    ticket_timeout_s=60,
    max_attempts=3,
    owner_email=OWNER_EMAIL,
    state_secret=SECRET,
    azure_token=None,
    claude_oauth_token=None,
    ado_feed_token=None,
)


class Fakes:
    def __init__(self, errors=None, running=None, volumes=None, gate_for=None):
        self.run_calls = []
        self.publish_calls = defaultdict(list)
        self.delete_calls = []
        self.kill_calls = []
        self.errors = dict(errors or {})
        self._running = set(running or ())
        self._volumes = set(volumes or ())
        self.gate_for = gate_for
        self.started = threading.Event()
        self.gate = threading.Event()

    def run_ticket(self, ticket_id, input_dir, mode, base_branch, settings):
        if ticket_id == self.gate_for:
            self.started.set()
            assert self.gate.wait(10), f"gate never opened for ticket {ticket_id}"
        names = sorted(p.name for p in input_dir.iterdir())
        new_json_text = (input_dir / "ticket.json").read_text(encoding="utf-8")
        self.run_calls.append((ticket_id, mode, base_branch, input_dir, names, new_json_text))
        if ticket_id in self.errors:
            raise self.errors[ticket_id]
        return RunOutcome(ticket_id, f"fixie-ticket-{ticket_id}", 0, False, RunResult(RunStatus.DONE, "ok"))

    def refresh_mirror(self, cfg):
        assert MIRROR_LOCK.locked()

    def publish(self, gw, s, snapshot, status, outcome, *, helper_config=None):
        self.publish_calls[snapshot.ticket.id].append(status)
        finished = replace(status, last_finished_run_started=status.run_started, result=RunStatus.DONE, attempts=0)
        tid = snapshot.ticket.id
        text = format_status_comment(tid, finished, s.state_secret)
        existing = find_status_comment(gw.get_comments(tid), s.owner_email)
        if existing is None:
            gw.add_comment(tid, text)
        else:
            gw.edit_comment(tid, existing.id, text)
        return finished

    def delete_volume(self, ticket_id):
        self.delete_calls.append(ticket_id)

    def kill_ticket(self, ticket_id):
        self.kill_calls.append(ticket_id)

    def running_tickets(self):
        return set(self._running)

    def ticket_volumes(self):
        return set(self._volumes)


def _make_orch(gw, settings, fakes, **overrides):
    kwargs = dict(
        run_ticket=fakes.run_ticket,
        refresh_mirror=fakes.refresh_mirror,
        publish=fakes.publish,
        delete_volume=fakes.delete_volume,
        kill_ticket=fakes.kill_ticket,
        running_tickets=fakes.running_tickets,
        ticket_volumes=fakes.ticket_volumes,
    )
    kwargs.update(overrides)
    return Orchestrator(gw, settings, **kwargs)


def test_new_ticket_runs_start_and_publishes():
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes()
    orch = _make_orch(gw, SETTINGS, fakes)

    futures = orch.poll_once()
    concurrent.futures.wait(futures)
    orch.shutdown()

    calls = [c for c in fakes.run_calls if c[0] == "601"]
    assert len(calls) == 1
    _, mode, base_branch, input_dir, names, new_json_text = calls[0]
    assert mode == "start"
    assert base_branch == "dev"
    assert names == ["ticket.json"]
    assert json.loads(new_json_text)["events"] == []
    assert not input_dir.exists()

    published = fakes.publish_calls[601]
    assert len(published) == 1
    assert published[0].run_started is not None
    assert published[0].attempts == 1
    assert orch._in_flight == set()


def test_resume_runs_resume_with_events():
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    status = FixieStatus(run_started=T1, last_finished_run_started=T1, result=RunStatus.DONE)
    gw.add_comment(605, format_status_comment(605, status, SECRET))
    gw.comments[605].append(
        Comment(id=2, text="<p>Please add tests</p>", created_by=ALICE, created_date=T2, modified_by=ALICE, modified_date=T2)
    )
    fakes = Fakes()
    orch = _make_orch(gw, SETTINGS, fakes)

    concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    calls = [c for c in fakes.run_calls if c[0] == "605"]
    assert len(calls) == 1
    assert calls[0][1] == "resume"
    events = json.loads(calls[0][5])["events"]
    assert [(e["type"], e["comment"], e["allowlisted"]) for e in events] == [("ticket_comment", 2, False)]


def test_retry_runs_resume():
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    status = FixieStatus(run_started=T2, last_finished_run_started=T1, result=RunStatus.DONE, attempts=1)
    gw.add_comment(605, format_status_comment(605, status, SECRET))
    fakes = Fakes()
    orch = _make_orch(gw, SETTINGS, fakes)

    concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    calls = [c for c in fakes.run_calls if c[0] == "605"]
    assert len(calls) == 1
    assert calls[0][1] == "resume"
    assert fakes.publish_calls[605][-1].attempts == 2


def test_rebuild_runs_start():
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    gw.add_comment(605, f"<p>{BOT_SIGN} fixie: done</p><p>old</p>")
    fakes = Fakes()
    orch = _make_orch(gw, SETTINGS, fakes)

    concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    calls = [c for c in fakes.run_calls if c[0] == "605"]
    assert len(calls) == 1
    assert calls[0][1] == "start"


def test_skip_does_nothing(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    status = FixieStatus(run_started=T1, last_finished_run_started=T1, result=RunStatus.DONE)
    gw.add_comment(605, format_status_comment(605, status, SECRET))
    gw.writes.clear()
    fakes = Fakes()
    orch = _make_orch(gw, SETTINGS, fakes)

    with caplog.at_level(logging.INFO, logger="fixie.orchestrator"):
        concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    assert not any(c[0] == "605" for c in fakes.run_calls)
    assert [w for w in gw.writes if w[1].get("ticket_id") == 605] == []
    assert "orchestrator: ticket 605 classified skip" in caplog.text


def test_give_up_recorded_once():
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    status = FixieStatus(run_started=T2, last_finished_run_started=T1, attempts=3)
    gw.add_comment(605, format_status_comment(605, status, SECRET))
    gw.writes.clear()
    fakes = Fakes()
    orch = _make_orch(gw, SETTINGS, fakes)

    concurrent.futures.wait(orch.poll_once())

    assert not any(c[0] == "605" for c in fakes.run_calls)
    add_comments = [w for w in gw.writes if w[0] == "add_comment" and w[1]["ticket_id"] == 605]
    assert len(add_comments) == 1
    assert "fixie gave up after 3 attempts" in add_comments[0][1]["text"]

    read_back = trusted_status(find_status_comment(gw.get_comments(605), OWNER_EMAIL), 605, OWNER_EMAIL, SECRET)
    assert read_back.result == RunStatus.GAVE_UP

    writes_before = len(gw.writes)
    concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    new_605_writes = [w for w in gw.writes[writes_before:] if w[1].get("ticket_id") == 605]
    assert new_605_writes == []


def test_clean_up_deletes_volume_for_closed_ticket(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes(volumes={"603"})
    orch = _make_orch(gw, SETTINGS, fakes)

    with caplog.at_level(logging.INFO, logger="fixie.orchestrator"):
        concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    assert fakes.delete_calls == ["603"]
    assert not any(c[0] == "603" for c in fakes.run_calls)
    assert "orchestrator: ticket 603 volume deleted" in caplog.text


def test_deleted_ticket_volume_deleted(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes(volumes={"999"})
    orch = _make_orch(gw, SETTINGS, fakes)

    with caplog.at_level(logging.INFO, logger="fixie.orchestrator"):
        concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    assert fakes.delete_calls == ["999"]
    assert "orchestrator: ticket 999 deleted in ADO, volume deleted" in caplog.text
    assert "poll failed" not in caplog.text


def test_stop_keeps_volume(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes(volumes={"604"})
    orch = _make_orch(gw, SETTINGS, fakes)

    with caplog.at_level(logging.INFO, logger="fixie.orchestrator"):
        concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    assert fakes.delete_calls == []
    assert not any(c[0] == "604" for c in fakes.run_calls)
    assert [w for w in gw.writes if w[1].get("ticket_id") == 604] == []
    assert "orchestrator: ticket 604 classified stop" in caplog.text


def test_unauthorized_tagger_tag_removed_and_commented_once(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes()
    orch = _make_orch(gw, SETTINGS, fakes)

    with caplog.at_level(logging.INFO, logger="fixie.orchestrator"):
        concurrent.futures.wait(orch.poll_once())
        concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    assert not any(c[0] == "602" for c in fakes.run_calls)
    comments_602 = gw.get_comments(602)
    assert len(comments_602) == 1
    assert is_bot_signed(comments_602[0].text)
    assert "fixie-unauthorized" in comments_602[0].text
    assert "removed the tag" in comments_602[0].text
    assert "fixie" not in gw.get_ticket(602).tags
    assert 602 not in gw.find_tagged_tickets("fixie")
    assert "orchestrator: ticket 602 tagged by non-allowlisted user, tag removed" in caplog.text


def test_second_stop_check_skips_run_after_mirror_refresh(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    for tid in list(gw.tickets):
        if tid != 601:
            del gw.tickets[tid]
    fakes = Fakes()

    def refresh_and_stop(cfg):
        assert MIRROR_LOCK.locked()
        orch.stop()

    orch = _make_orch(gw, SETTINGS, fakes, refresh_mirror=refresh_and_stop)

    with caplog.at_level(logging.INFO, logger="fixie.orchestrator"):
        concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    assert fakes.run_calls == []
    assert "orchestrator: ticket 601 stopping, run skipped" in caplog.text


def test_in_flight_ticket_skipped(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes(gate_for="601")
    orch = _make_orch(gw, SETTINGS, fakes)

    with caplog.at_level(logging.INFO, logger="fixie.orchestrator"):
        futures1 = orch.poll_once()
        assert fakes.started.wait(10)
        futures2 = orch.poll_once()
    fakes.gate.set()
    concurrent.futures.wait(futures1 + futures2)
    orch.shutdown()

    assert len([c for c in fakes.run_calls if c[0] == "601"]) == 1
    assert "orchestrator: ticket 601 in flight, skipped" in caplog.text


def test_pool_full_defers(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes(gate_for="601")
    settings = replace(SETTINGS, max_parallel=1)
    orch = _make_orch(gw, settings, fakes)

    with caplog.at_level(logging.INFO, logger="fixie.orchestrator"):
        futures1 = orch.poll_once()
        assert fakes.started.wait(10)
        assert "orchestrator: ticket 605 deferred, pool full" in caplog.text

    fakes.gate.set()
    concurrent.futures.wait(futures1)

    futures2 = orch.poll_once()
    concurrent.futures.wait(futures2)
    orch.shutdown()

    calls_605 = [c for c in fakes.run_calls if c[0] == "605"]
    assert len(calls_605) == 1
    assert calls_605[0][1] == "start"


def test_worker_error_does_not_stop_loop(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes(errors={"601": RunnerError("boom")})
    orch = _make_orch(gw, SETTINGS, fakes)

    with caplog.at_level(logging.WARNING, logger="fixie.orchestrator"):
        concurrent.futures.wait(orch.poll_once())

    assert "orchestrator: ticket 601 worker failed: RunnerError: boom" in caplog.text
    assert orch._in_flight == set()
    assert fakes.publish_calls[605]
    first_601_call = [c for c in fakes.run_calls if c[0] == "601"][0]
    assert not first_601_call[3].exists()

    concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    calls_601 = [c for c in fakes.run_calls if c[0] == "601"]
    assert len(calls_601) == 2
    assert calls_601[1][1] == "resume"


def test_ticket_poll_error_skips_ticket(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes(volumes={"999"})
    orch = _make_orch(gw, SETTINGS, fakes)
    real_get_ticket = gw.get_ticket

    def flaky(ticket_id):
        if ticket_id == 999:
            raise RuntimeError("ADO down")
        return real_get_ticket(ticket_id)

    gw.get_ticket = flaky
    with caplog.at_level(logging.WARNING, logger="fixie.orchestrator"):
        concurrent.futures.wait(orch.poll_once())
    orch.shutdown()

    assert "orchestrator: ticket 999 poll failed: RuntimeError" in caplog.text
    assert fakes.delete_calls == []
    assert any(c[0] == "601" for c in fakes.run_calls)


def test_recover_kills_running_and_leftover():
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes(running={"7"}, volumes={"7", "8", "x"})
    orch = _make_orch(gw, SETTINGS, fakes)

    orch.recover()
    orch.shutdown()

    assert sorted(fakes.kill_calls) == ["7", "8"]
    assert gw.writes == []


def test_run_forever_stops_and_kills_in_flight(caplog):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes(gate_for="601")

    def kill_and_release(ticket_id):
        fakes.kill_calls.append(ticket_id)
        fakes.gate.set()

    settings = replace(SETTINGS, poll_interval_s=1)
    orch = _make_orch(gw, settings, fakes, kill_ticket=kill_and_release)

    with caplog.at_level(logging.INFO, logger="fixie.orchestrator"):
        thread = threading.Thread(target=orch.run_forever)
        thread.start()
        assert fakes.started.wait(10)
        orch.stop()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert "601" in fakes.kill_calls
    assert "orchestrator: stopping, killing in-flight runs" in caplog.text


def test_run_forever_survives_poll_error(caplog, monkeypatch):
    gw = FakeGateway(FIXTURE_PATH, OWNER)
    fakes = Fakes()
    settings = replace(SETTINGS, poll_interval_s=1)
    orch = _make_orch(gw, settings, fakes)

    calls = []

    def fake_find_tagged_tickets(tag):
        calls.append(tag)
        if len(calls) == 1:
            raise RuntimeError("down")
        orch.stop()
        return []

    monkeypatch.setattr(gw, "find_tagged_tickets", fake_find_tagged_tickets)

    with caplog.at_level(logging.WARNING, logger="fixie.orchestrator"):
        orch.run_forever()

    assert "orchestrator: poll failed: RuntimeError: down" in caplog.text
    assert len(calls) == 2
