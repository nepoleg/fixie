import subprocess
import sys
from pathlib import Path

import pytest

from fixie.__main__ import main
from fixie.orchestrator import Orchestrator

CONFIG_PATH = Path(__file__).parents[1] / "config.toml"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ado" / "orchestrator.json"


def test_main_without_fake_fixture_exits_2(capsys):
    assert main([]) == 2
    assert "real ADO gateway not implemented yet" in capsys.readouterr().err


def test_python_m_fixie_without_fake_fixture_exits_2():
    result = subprocess.run([sys.executable, "-m", "fixie"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "real ADO gateway not implemented yet" in result.stderr


def _write_config_and_env(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("FIXIE_OWNER_EMAIL=owner@example.com\nFIXIE_STATE_SECRET=s\n", encoding="utf-8")
    return config_path, env_path


def test_missing_fake_fixture_path_exits_2_not_traceback(tmp_path, capsys):
    config_path, env_path = _write_config_and_env(tmp_path)
    bad_fixture = tmp_path / "does-not-exist.json"

    result = main(
        ["--once", "--fake-fixture", str(bad_fixture), "--config", str(config_path), "--env", str(env_path)]
    )

    assert result == 2
    assert capsys.readouterr().err.startswith("fixie: ")


def test_once_shuts_down_even_when_poll_once_fails(tmp_path, monkeypatch):
    config_path, env_path = _write_config_and_env(tmp_path)

    shutdown_calls = []
    monkeypatch.setattr(Orchestrator, "recover", lambda self: None)

    def boom(self):
        raise RuntimeError("boom")

    def spy_shutdown(self):
        shutdown_calls.append(True)

    monkeypatch.setattr(Orchestrator, "poll_once", boom)
    monkeypatch.setattr(Orchestrator, "shutdown", spy_shutdown)

    with pytest.raises(RuntimeError):
        main(["--once", "--fake-fixture", str(FIXTURE_PATH), "--config", str(config_path), "--env", str(env_path)])

    assert shutdown_calls == [True]
