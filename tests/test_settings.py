import os
from pathlib import Path

import pytest

from fixie.settings import SettingsError, load_settings

CONFIG_PATH = Path(__file__).parents[1] / "config.toml"


def _write_env(tmp_path, lines):
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_path


def test_loads_with_only_required_keys(tmp_path):
    env_path = _write_env(
        tmp_path,
        ["FIXIE_STATE_SECRET=s", "FIXIE_OWNER_EMAIL=owner@example.com"],
    )
    settings = load_settings(CONFIG_PATH, env_path)

    assert settings.ado_project == "EETS"
    assert settings.ticket_tag == "fixie"
    assert settings.default_base_branch == "dev"
    assert settings.ticket_timeout_s == 3600
    assert settings.max_attempts == 3
    assert settings.allowlist == ()
    assert settings.azure_token is None
    assert settings.claude_oauth_token is None
    assert settings.ado_feed_token is None


def test_missing_required_keys_raises(tmp_path):
    env_path = _write_env(tmp_path, [])
    with pytest.raises(SettingsError) as exc_info:
        load_settings(CONFIG_PATH, env_path)
    assert str(exc_info.value) == "missing required settings: FIXIE_OWNER_EMAIL, FIXIE_STATE_SECRET"


def test_empty_required_value_counts_as_missing(tmp_path):
    env_path = _write_env(
        tmp_path,
        ["FIXIE_STATE_SECRET=", "FIXIE_OWNER_EMAIL=owner@example.com"],
    )
    with pytest.raises(SettingsError) as exc_info:
        load_settings(CONFIG_PATH, env_path)
    assert str(exc_info.value).endswith("FIXIE_STATE_SECRET")


def test_missing_env_file_raises(tmp_path):
    env_path = tmp_path / "absent.env"
    with pytest.raises(SettingsError):
        load_settings(CONFIG_PATH, env_path)


def test_optional_tokens_loaded(tmp_path):
    env_path = _write_env(
        tmp_path,
        [
            "FIXIE_STATE_SECRET=s",
            "FIXIE_OWNER_EMAIL=owner@example.com",
            "AZURE_TOKEN=t1",
            "CLAUDE_CODE_OAUTH_TOKEN=t2",
            "ADO_FEED_TOKEN=t3",
        ],
    )
    settings = load_settings(CONFIG_PATH, env_path)

    assert settings.azure_token == "t1"
    assert settings.claude_oauth_token == "t2"
    assert settings.ado_feed_token == "t3"


def test_secrets_not_in_repr(tmp_path):
    env_path = _write_env(
        tmp_path,
        [
            "FIXIE_STATE_SECRET=s3cret-value",
            "FIXIE_OWNER_EMAIL=owner@example.com",
            "AZURE_TOKEN=tok-a",
            "CLAUDE_CODE_OAUTH_TOKEN=tok-b",
            "ADO_FEED_TOKEN=tok-c",
        ],
    )
    settings = load_settings(CONFIG_PATH, env_path)
    r = repr(settings)

    for secret in ("s3cret-value", "tok-a", "tok-b", "tok-c"):
        assert secret not in r


def test_allowlist_and_owner_lowercased(tmp_path):
    config_text = CONFIG_PATH.read_text(encoding="utf-8")
    config_text = config_text.replace("allowlist = []", 'allowlist = ["Alice@Example.com"]')
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_text, encoding="utf-8")

    env_path = _write_env(
        tmp_path,
        ["FIXIE_STATE_SECRET=s", "FIXIE_OWNER_EMAIL=Owner@Example.com"],
    )
    settings = load_settings(config_path, env_path)

    assert settings.allowlist == ("alice@example.com",)
    assert settings.owner_email == "owner@example.com"


def test_allowlist_blank_entries_dropped(tmp_path):
    config_text = CONFIG_PATH.read_text(encoding="utf-8")
    config_text = config_text.replace("allowlist = []", 'allowlist = ["", "  ", " Alice@Example.com "]')
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_text, encoding="utf-8")

    env_path = _write_env(tmp_path, ["FIXIE_STATE_SECRET=s", "FIXIE_OWNER_EMAIL=owner@example.com"])
    settings = load_settings(config_path, env_path)

    assert settings.allowlist == ("alice@example.com",)


def test_max_attempts_default_and_config(tmp_path):
    config_text = CONFIG_PATH.read_text(encoding="utf-8")
    env_path = _write_env(tmp_path, ["FIXIE_STATE_SECRET=s", "FIXIE_OWNER_EMAIL=owner@example.com"])

    no_section_text = config_text.replace("[orchestrator]\nmax_attempts = 3\n", "")
    no_section_path = tmp_path / "no_section.toml"
    no_section_path.write_text(no_section_text, encoding="utf-8")
    assert load_settings(no_section_path, env_path).max_attempts == 3

    custom_text = config_text.replace("max_attempts = 3", "max_attempts = 5")
    custom_path = tmp_path / "custom.toml"
    custom_path.write_text(custom_text, encoding="utf-8")
    assert load_settings(custom_path, env_path).max_attempts == 5


def test_max_attempts_below_one_rejected(tmp_path):
    config_text = CONFIG_PATH.read_text(encoding="utf-8")
    config_text = config_text.replace("max_attempts = 3", "max_attempts = 0")
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_text, encoding="utf-8")

    env_path = _write_env(tmp_path, ["FIXIE_STATE_SECRET=s", "FIXIE_OWNER_EMAIL=owner@example.com"])
    with pytest.raises(SettingsError) as exc_info:
        load_settings(config_path, env_path)
    assert "max_attempts" in str(exc_info.value)


def test_does_not_modify_os_environ(tmp_path, monkeypatch):
    keys = [
        "FIXIE_STATE_SECRET",
        "FIXIE_OWNER_EMAIL",
        "AZURE_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ADO_FEED_TOKEN",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    env_path = _write_env(
        tmp_path,
        [
            "FIXIE_STATE_SECRET=s",
            "FIXIE_OWNER_EMAIL=owner@example.com",
            "AZURE_TOKEN=t1",
            "CLAUDE_CODE_OAUTH_TOKEN=t2",
            "ADO_FEED_TOKEN=t3",
        ],
    )
    load_settings(CONFIG_PATH, env_path)

    for key in keys:
        assert key not in os.environ
