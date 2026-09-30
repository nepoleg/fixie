"""Loads fixie's configuration from config.toml plus secrets from a .env file."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


class SettingsError(Exception):
    """Raised when required secrets are missing from the .env file."""


@dataclass(frozen=True)
class Settings:
    """Fully resolved fixie configuration: non-secret values plus loaded secrets."""

    ado_org_url: str
    ado_project: str
    ado_repo: str
    ticket_tag: str
    default_base_branch: str
    allowlist: tuple[str, ...]
    claude_model: str
    docker_image: str
    mirror_volume: str
    gradle_ro_volume: str
    ticket_timeout_s: int
    max_attempts: int
    owner_email: str
    state_secret: str = field(repr=False)
    azure_token: str | None = field(repr=False)
    claude_oauth_token: str | None = field(repr=False)
    ado_feed_token: str | None = field(repr=False)
    poll_interval_s: int = 300
    max_parallel: int = 2
    helper_timeout_s: int = 3600


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def load_settings(config_path: Path, env_path: Path) -> Settings:
    """Load Settings from a TOML config file and a .env file; raises SettingsError if secrets are missing."""
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    env = dotenv_values(env_path, encoding="utf-8") if env_path.exists() else {}

    owner_email = _clean(env.get("FIXIE_OWNER_EMAIL"))
    state_secret = _clean(env.get("FIXIE_STATE_SECRET"))

    missing = [
        name
        for name, value in (("FIXIE_OWNER_EMAIL", owner_email), ("FIXIE_STATE_SECRET", state_secret))
        if value is None
    ]
    if missing:
        raise SettingsError(f"missing required settings: {', '.join(sorted(missing))}")

    ado = config["ado"]
    docker = config["docker"]
    orchestrator = config.get("orchestrator", {})
    max_attempts = int(orchestrator.get("max_attempts", 3))
    if max_attempts < 1:
        raise SettingsError(f"max_attempts must be at least 1, got {max_attempts}")
    poll_interval_s = int(orchestrator.get("poll_interval_s", 300))
    if poll_interval_s < 1:
        raise SettingsError(f"poll_interval_s must be at least 1, got {poll_interval_s}")
    max_parallel = int(orchestrator.get("max_parallel", 2))
    if max_parallel < 1:
        raise SettingsError(f"max_parallel must be at least 1, got {max_parallel}")
    helper_timeout_s = int(docker.get("helper_timeout_s", 3600))
    if helper_timeout_s < 1:
        raise SettingsError(f"helper_timeout_s must be at least 1, got {helper_timeout_s}")

    return Settings(
        ado_org_url=ado["org_url"],
        ado_project=ado["project"],
        ado_repo=ado["repo"],
        ticket_tag=ado["ticket_tag"],
        default_base_branch=ado["default_base_branch"],
        allowlist=tuple(a.strip().lower() for a in ado["allowlist"] if a.strip()),
        claude_model=config["claude"]["model"],
        docker_image=docker["image"],
        mirror_volume=docker["mirror_volume"],
        gradle_ro_volume=docker["gradle_ro_volume"],
        ticket_timeout_s=int(docker["ticket_timeout_s"]),
        max_attempts=max_attempts,
        owner_email=owner_email.lower(),
        state_secret=state_secret,
        azure_token=_clean(env.get("AZURE_TOKEN")),
        claude_oauth_token=_clean(env.get("CLAUDE_CODE_OAUTH_TOKEN")),
        ado_feed_token=_clean(env.get("ADO_FEED_TOKEN")),
        poll_interval_s=poll_interval_s,
        max_parallel=max_parallel,
        helper_timeout_s=helper_timeout_s,
    )
