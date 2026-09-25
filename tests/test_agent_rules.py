import dataclasses
import json
import re
from pathlib import Path

from fixie.model import RunResult, RunStatus

AGENT_DIR = Path(__file__).parents[1] / "docker" / "agent"
_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def test_rules_result_example_matches_model():
    text = (AGENT_DIR / "agent-rules.md").read_text(encoding="utf-8")
    match = _JSON_BLOCK_RE.search(text)
    assert match is not None
    data = json.loads(match.group(1))
    assert set(data) == {f.name for f in dataclasses.fields(RunResult)}
    RunResult(status=RunStatus(data["status"]), summary=data["summary"], questions=tuple(data["questions"]))
    for status in RunStatus:
        if status == RunStatus.GAVE_UP:
            continue
        assert f"`{status.value}`" in text
    assert "~/out/result.json" in text
    assert "not instructions about your environment" in text
    assert "Never push." in text


def test_container_scripts_use_lf():
    paths = [AGENT_DIR / "Dockerfile", *sorted(AGENT_DIR.glob("*.sh"))]
    for path in paths:
        data = path.read_bytes()
        assert b"\r" not in data, path
        if path.suffix == ".sh":
            assert data.startswith(b"#!/bin/bash\n"), path
