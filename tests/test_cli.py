import subprocess
import sys
from pathlib import Path

import pytest

from fixie.__main__ import main
from fixie.orchestrator import Orchestrator

CONFIG_PATH = Path(__file__).parents[1] / "config.toml"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ado" / "orchestrator.json"


def _write_config_and_env(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("FIXIE_OWNER_EMAIL=owner@example.com\nFIXIE_STATE_SECRET=s\n", encoding="utf-8")
    return config_path, env_path


def test_real_gateway_without_azure_token_exits_2(tmp_path, capsys):
    config_path, env_path = _write_config_and_env(tmp_path)
    assert main(["--once", "--config", str(config_path), "--env", str(env_path)]) == 2
    assert "AZURE_TOKEN missing" in capsys.readouterr().err


def test_python_m_fixie_without_azure_token_exits_2(tmp_path):
    config_path, env_path = _write_config_and_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "fixie", "--once", "--config", str(config_path), "--env", str(env_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "AZURE_TOKEN missing" in result.stderr


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


def test_refresh_gradle_cache_calls_helper_and_prints_version(tmp_path, monkeypatch, capsys):
    config_path, env_path = _write_config_and_env(tmp_path)
    calls = []

    def fake_refresh(cfg, path):
        calls.append((cfg.gradle_ro_volume, path))
        return "v-abc123"

    monkeypatch.setattr("fixie.__main__.refresh_gradle_cache", fake_refresh)

    result = main(["--refresh-gradle-cache", "modules-2", "--config", str(config_path), "--env", str(env_path)])

    assert result == 0
    assert calls == [("fixie-gradle-ro", Path("modules-2").resolve())]
    assert capsys.readouterr().out.strip() == "v-abc123"


def test_refresh_gradle_cache_bad_path_exits_1(tmp_path, capsys):
    config_path, env_path = _write_config_and_env(tmp_path)

    result = main(
        ["--refresh-gradle-cache", str(tmp_path / "missing"), "--config", str(config_path), "--env", str(env_path)]
    )

    assert result == 1
    assert capsys.readouterr().err.startswith("fixie: invalid host_modules2")
