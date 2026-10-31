"""Settings: read config.toml and apply command-line overrides."""

import re
import tomllib
import zoneinfo
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "config.toml"

# chess.com usernames are letters, digits, "_" and "-". Checking this also
# keeps the username safe to use as a folder name.
_USERNAME_RE = re.compile(r"^[a-z0-9_-]+$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    username: str
    data_dir: Path
    timezone: str = "UTC"
    session_gap_minutes: float = 30

    @property
    def user_dir(self) -> Path:
        return self.data_dir / self.username

    @property
    def raw_dir(self) -> Path:
        return self.user_dir / "raw"


def find_config(start: Path | None = None) -> Path | None:
    """Find config.toml in `start` (default: the current directory) or any parent."""
    start = (start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def normalise_username(username: str) -> str:
    name = username.strip().lower()
    if not _USERNAME_RE.match(name):
        raise ConfigError(f"Invalid chess.com username: {username!r}")
    return name


def load_settings(username: str | None = None, config_path: Path | None = None) -> Settings:
    """Build settings. A `username` passed in (e.g. from --username) beats config.toml."""
    if config_path is not None and not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")
    path = config_path or find_config()

    values: dict = {}
    root = Path.cwd()
    if path is not None:
        with open(path, "rb") as f:
            values = tomllib.load(f)
        root = path.parent

    name = username or values.get("username")
    if not name:
        raise ConfigError(f"No chess.com username: pass --username or set username in {CONFIG_NAME}")

    data_dir = (root / values.get("data_dir", "data")).resolve()
    timezone = values.get("timezone", "UTC")
    try:
        zoneinfo.ZoneInfo(timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"Unknown timezone in {CONFIG_NAME}: {timezone!r}") from None
    return Settings(
        username=normalise_username(name),
        data_dir=data_dir,
        timezone=timezone,
        session_gap_minutes=float(values.get("session_gap_minutes", 30)),
    )
