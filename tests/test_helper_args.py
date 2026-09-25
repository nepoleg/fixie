from pathlib import Path

import pytest

import fixie.helper as helper
from fixie.helper import HelperConfig, auth_env
from fixie.settings import Settings


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    def _boom():
        raise AssertionError("docker called")

    monkeypatch.setattr(helper.docker, "from_env", _boom)


def test_auth_env_format():
    assert auth_env("abc") == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic OmFiYw==",
    }


def test_auth_env_none_is_empty():
    assert auth_env(None) == {}


def _settings(**overrides):
    fields = dict(
        ado_org_url="https://dev.azure.com/org/",
        ado_project="My Project",
        ado_repo="EETS",
        ticket_tag="fixie",
        default_base_branch="dev",
        allowlist=(),
        claude_model="haiku",
        docker_image="fixie-agent",
        mirror_volume="fixie-mirror",
        gradle_ro_volume="fixie-gradle-ro",
        ticket_timeout_s=3600,
        max_attempts=3,
        owner_email="owner@example.com",
        state_secret="s",
        azure_token="sekrit-123",
        claude_oauth_token=None,
        ado_feed_token=None,
    )
    fields.update(overrides)
    return Settings(**fields)


def test_config_from_settings():
    cfg = HelperConfig.from_settings(_settings())

    assert cfg.remote_url == "https://dev.azure.com/org/My%20Project/_git/EETS"
    assert cfg.token == "sekrit-123"
    assert cfg.mirror_volume == "fixie-mirror"
    assert cfg.gradle_ro_volume == "fixie-gradle-ro"
    assert "sekrit-123" not in repr(cfg)
    assert cfg.image == "fixie-helper:latest"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"remote_url": "https://u:p@dev.azure.com/x"},
        {"remote_url": "ssh://host/x"},
        {"mirror_volume": "C:\\evil"},
        {"gradle_ro_volume": "/etc"},
    ],
)
def test_config_rejects_bad_values(kwargs):
    base = dict(mirror_volume="fixie-mirror", gradle_ro_volume="fixie-gradle-ro", remote_url="https://dev.azure.com/x")
    base.update(kwargs)
    with pytest.raises(ValueError):
        HelperConfig(**base)


@pytest.mark.parametrize(
    "ticket_id,ticket_volume",
    [
        ("1;id", "fixie-ticket-1"),
        ("", "fixie-ticket-1"),
        ("12345678901", "fixie-ticket-1"),
        ("42", "C:\\evil"),
        ("42", "/home"),
    ],
)
def test_import_rejects_bad_args(ticket_id, ticket_volume):
    cfg = HelperConfig(mirror_volume="fixie-mirror", gradle_ro_volume="fixie-gradle-ro", remote_url="https://dev.azure.com/x")
    with pytest.raises(ValueError):
        helper.import_and_push(cfg, ticket_id, ticket_volume)


def _bad_gradle_paths(tmp_path):
    wrong_name = tmp_path / "caches"
    wrong_name.mkdir()
    return [Path("modules-2"), tmp_path / "missing" / "modules-2", wrong_name]


@pytest.mark.parametrize("index", range(3))
def test_gradle_rejects_bad_path(tmp_path, index):
    cfg = HelperConfig(mirror_volume="fixie-mirror", gradle_ro_volume="fixie-gradle-ro", remote_url="https://dev.azure.com/x")
    bad_path = _bad_gradle_paths(tmp_path)[index]

    with pytest.raises(ValueError):
        helper.refresh_gradle_cache(cfg, bad_path)
