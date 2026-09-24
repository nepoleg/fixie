import dataclasses

import pytest

from fixie.model import EventType, FixieStatus, RunStatus


def test_enum_values():
    assert [s.value for s in RunStatus] == ["done", "blocked", "needs_info"]
    assert [e.value for e in EventType] == [
        "ticket_comment",
        "pr_comment",
        "pr_push",
        "build_failed",
        "reopened",
        "state_change",
    ]


def test_fixie_status_defaults_and_frozen():
    s = FixieStatus()
    assert s.run_started is None
    assert s.last_finished_run_started is None
    assert s.result is None
    assert s.pr_id is None
    assert s.head is None
    assert s.state is None
    assert s.last_build_id is None
    assert s.attempts == 0

    with pytest.raises(dataclasses.FrozenInstanceError):
        s.attempts = 1

    assert dataclasses.replace(s, attempts=1).attempts == 1
