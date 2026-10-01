import inspect
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fixie.fake_gateway import FakeGateway
from fixie.gateway import AdoGateway
from fixie.model import Identity

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ado" / "contract.json"

GATEWAY_METHODS = (
    "find_tagged_tickets",
    "get_ticket",
    "get_updates",
    "get_comments",
    "get_attachments",
    "download_attachment",
    "get_pull_request",
    "find_pull_requests",
    "get_threads",
    "get_builds",
    "get_branch_head",
    "add_comment",
    "remove_tag",
    "set_state",
    "reply_to_thread",
    "edit_comment",
    "create_pull_request",
    "update_pull_request",
    "link_work_item",
    "attach_file",
)


@pytest.fixture
def gateway():
    return FakeGateway(FIXTURE_PATH, Identity("Owner", "owner@example.com"))


def test_fake_implements_every_method(gateway):
    assert AdoGateway.__abstractmethods__ == set(GATEWAY_METHODS)
    for name in GATEWAY_METHODS:
        fake_params = list(inspect.signature(getattr(FakeGateway, name)).parameters)
        abc_params = list(inspect.signature(getattr(AdoGateway, name)).parameters)
        assert fake_params == abc_params
    assert isinstance(gateway, AdoGateway)


def test_find_tagged_tickets_open_and_tagged_only(gateway):
    assert gateway.find_tagged_tickets("fixie") == [101, 104]
    assert gateway.find_tagged_tickets("FIXIE") == [101, 104]


def test_find_tagged_tickets_highest_priority_first(gateway):
    gateway.tickets[104] = replace(gateway.tickets[104], priority=1)
    assert gateway.find_tagged_tickets("fixie") == [104, 101]


def test_remove_tag_case_insensitive(gateway):
    gateway.remove_tag(104, "fixie")
    gateway.remove_tag(104, "fixie")
    assert gateway.get_ticket(104).tags == ()
    assert gateway.find_tagged_tickets("fixie") == [101]
    with pytest.raises(KeyError):
        gateway.remove_tag(999, "fixie")


def test_get_ticket(gateway):
    ticket = gateway.get_ticket(101)
    assert ticket.title == "Fix login NPE"
    assert ticket.state == "Developing"
    assert ticket.tags == ("fixie", "backend")
    assert ticket.repro_steps == "<p>Click login</p>"
    assert ticket.changed_date.tzinfo is not None


def test_get_unknown_ticket_raises_key_error(gateway):
    with pytest.raises(KeyError):
        gateway.get_ticket(999)


def test_get_updates_oldest_first_with_tag_diff(gateway):
    updates = gateway.get_updates(101)
    assert [u.id for u in updates] == [1, 2, 3]
    assert updates[1].revised_by.unique_name == "alice@example.com"
    assert updates[1].tags_added == ("fixie",)
    assert updates[2].state_to == "Developing"
    assert gateway.get_updates(103) == []


def test_get_comments_oldest_first_with_modifier(gateway):
    comments = gateway.get_comments(101)
    assert [c.id for c in comments] == [11, 12]
    c12 = comments[1]
    assert c12.created_by.unique_name == "owner@example.com"
    assert c12.modified_by.unique_name == "bob@example.com"
    assert c12.modified_date > c12.created_date


def test_attachments_downloadable(gateway):
    attachments = gateway.get_attachments(101)
    assert len(attachments) == 1
    assert attachments[0].name == "shot.png"
    assert gateway.download_attachment(attachments[0].url) == b"hello"
    with pytest.raises(KeyError):
        gateway.download_attachment("https://fake.ado/attachments/nope")


def test_find_pull_requests_by_branch(gateway):
    assert [p.id for p in gateway.find_pull_requests("fixie/101")] == [7]
    assert gateway.find_pull_requests("fixie/999") == []


def test_get_pull_request_and_unknown_raises(gateway):
    pr = gateway.get_pull_request(7)
    assert pr.target_branch == "dev"
    assert pr.status == "active"
    assert pr.work_item_ids == (101,)
    with pytest.raises(KeyError):
        gateway.get_pull_request(999)


def test_get_threads_with_file_line_status(gateway):
    threads = gateway.get_threads(7)
    t1, t2 = threads
    assert t1.status == "active"
    assert t1.file_path == "/src/Login.java"
    assert t1.line == 42
    assert t1.comments[0].comment_type == "text"
    assert t2.status is None
    assert t2.comments[0].comment_type == "system"


def test_get_builds_with_id_result_time(gateway):
    builds = gateway.get_builds(7)
    assert [(b.id, b.result) for b in builds] == [(55, "failed"), (56, None)]
    assert builds[0].finish_time.tzinfo is not None
    assert builds[1].finish_time is None


def test_get_branch_head_known_and_unknown(gateway):
    assert gateway.get_branch_head("fixie/101").sha == "abc123"
    assert gateway.get_branch_head("nope") is None


def test_reads_record_no_writes(gateway):
    gateway.find_tagged_tickets("fixie")
    gateway.get_ticket(101)
    gateway.get_updates(101)
    gateway.get_comments(101)
    gateway.get_attachments(101)
    gateway.download_attachment("https://fake.ado/attachments/a1")
    gateway.get_pull_request(7)
    gateway.find_pull_requests("fixie/101")
    gateway.get_threads(7)
    gateway.get_builds(7)
    gateway.get_branch_head("dev")
    assert gateway.writes == []


def test_add_comment_recorded_and_readable(gateway):
    c = gateway.add_comment(101, "hi")
    assert c.id == 13
    assert c.created_by == gateway.owner
    assert c.created_date == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert gateway.get_comments(101)[-1] == c
    assert gateway.writes == [("add_comment", {"ticket_id": 101, "text": "hi"})]


def test_edit_comment_updates_text_and_modified(gateway):
    original = gateway.get_comments(101)[1]
    edited = gateway.edit_comment(101, 12, "new")

    assert edited.text == "new"
    assert edited.modified_by == gateway.owner
    assert edited.modified_date > original.modified_date
    assert edited.created_date == original.created_date

    comments = gateway.get_comments(101)
    assert comments[1] == edited
    assert gateway.writes[-1] == ("edit_comment", {"ticket_id": 101, "comment_id": 12, "text": "new"})


def test_edit_unknown_comment_raises_key_error(gateway):
    with pytest.raises(KeyError):
        gateway.edit_comment(101, 999, "x")
    assert gateway.writes == []


def test_create_pull_request_then_find(gateway):
    pr = gateway.create_pull_request("fixie/103", "dev", "T", "D")
    assert pr.id == 8
    assert pr.status == "active"
    assert pr.work_item_ids == ()
    assert [p.id for p in gateway.find_pull_requests("fixie/103")] == [8]
    assert gateway.get_threads(8) == []
    assert gateway.get_builds(8) == []


def test_update_pull_request(gateway):
    updated = gateway.update_pull_request(7, "T2", "D2")
    pr = gateway.get_pull_request(7)
    assert pr.title == "T2"
    assert pr.description == "D2"
    assert updated == pr
    assert gateway.writes[-1][0] == "update_pull_request"


def test_link_work_item_idempotent(gateway):
    pr = gateway.create_pull_request("fixie/103", "dev", "T", "D")
    gateway.link_work_item(pr.id, 103)
    gateway.link_work_item(pr.id, 103)

    assert gateway.get_pull_request(pr.id).work_item_ids == (103,)
    link_writes = [w for w in gateway.writes if w[0] == "link_work_item"]
    assert len(link_writes) == 2


def test_attach_file_listed_and_downloadable(gateway):
    a = gateway.attach_file(101, "log.gz", b"\x1f\x8b")
    assert a in gateway.get_attachments(101)
    assert gateway.download_attachment(a.url) == b"\x1f\x8b"


def test_attach_file_id_survives_blob_removal(gateway):
    a1 = gateway.attach_file(101, "one.bin", b"1")
    a2 = gateway.attach_file(101, "two.bin", b"2")
    del gateway.blobs[a1.url]

    a3 = gateway.attach_file(101, "three.bin", b"3")

    assert a3.id not in (a1.id, a2.id)
    assert gateway.download_attachment(a3.url) == b"3"


def test_write_times_strictly_increase(gateway):
    c1 = gateway.add_comment(101, "one")
    c2 = gateway.add_comment(101, "two")
    c3 = gateway.add_comment(101, "three")

    assert c1.created_date < c2.created_date < c3.created_date
    assert (c2.created_date - c1.created_date).total_seconds() == 1
    assert (c3.created_date - c2.created_date).total_seconds() == 1


def test_writes_to_unknown_ticket_rejected(gateway):
    with pytest.raises(KeyError):
        gateway.add_comment(999, "hi")
    with pytest.raises(KeyError):
        gateway.edit_comment(999, 1, "hi")
    with pytest.raises(KeyError):
        gateway.attach_file(999, "log.gz", b"1")
    with pytest.raises(KeyError):
        gateway.link_work_item(7, 999)
    assert gateway.writes == []


def test_second_active_pr_rejected(gateway):
    with pytest.raises(ValueError):
        gateway.create_pull_request("fixie/101", "dev", "T", "D")
    assert gateway.writes == []
    assert [p.id for p in gateway.find_pull_requests("fixie/101")] == [7]


def test_concurrent_writes_get_unique_ids(gateway):
    def add_many():
        for _ in range(25):
            gateway.add_comment(101, "c")

    threads = [threading.Thread(target=add_many) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    comments = gateway.get_comments(101)
    new_comments = [c for c in comments if c.text == "c"]
    assert len(new_comments) == 200
    assert len({c.id for c in new_comments}) == 200
    assert len({c.modified_date for c in new_comments}) == 200


def test_concurrent_reads_during_writes_no_error(gateway):
    errors = []
    stop = threading.Event()

    def create_many():
        prefix = threading.get_ident()
        for i in range(200):
            gateway.create_pull_request(f"branch-{prefix}-{i}", "dev", "T", "D")

    def find_many():
        while not stop.is_set():
            try:
                gateway.find_pull_requests("fixie/101")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    finder = threading.Thread(target=find_many)
    finder.start()
    creators = [threading.Thread(target=create_many) for _ in range(4)]
    for t in creators:
        t.start()
    for t in creators:
        t.join()
    stop.set()
    finder.join()

    assert errors == []


def test_naive_datetime_in_fixture_rejected(tmp_path):
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    data["tickets"]["101"]["changed_date"] = "2026-01-01T00:00:00"
    bad_fixture = tmp_path / "bad.json"
    bad_fixture.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError):
        FakeGateway(bad_fixture, Identity("Owner", "owner@example.com"))
