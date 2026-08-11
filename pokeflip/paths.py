"""Where configuration and data live.

A command you run inside a project directory and an app you double-click have
different expectations. ``cd myproject && pokeflip scan`` should use that
project's files; an icon on the desktop has no meaningful working directory and
must fall back to the usual per-user location for the platform.

Resolving both through one function is what stops the desktop app and the CLI
quietly ending up on two different databases.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "pokeflip"
CONFIG_FILENAME = "config.json"


def user_config_dir() -> Path:
    """Per-user configuration directory, following each platform's convention."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_NAME


def user_data_dir() -> Path:
    """Per-user data directory - the database, reports, exports."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_NAME


def resolve_config_path(explicit: str | os.PathLike | None = None,
                        cwd: Path | None = None) -> tuple[Path, str]:
    """Work out which config file to use, and say why.

    Order: an explicit path, then ``POKEFLIP_CONFIG``, then a ``config.json``
    in the working directory, then the per-user location. The reason is
    returned so commands can tell you which file they actually wrote.
    """
    if explicit:
        return Path(explicit), "explicit"

    from_env = os.environ.get("POKEFLIP_CONFIG")
    if from_env:
        return Path(from_env), "environment"

    local = (cwd or Path.cwd()) / CONFIG_FILENAME
    if local.is_file():
        return local, "working directory"

    return user_config_dir() / CONFIG_FILENAME, "user directory"


def default_database_for(config_path: Path, source: str) -> str:
    """Where the database belongs, given where the config came from.

    A project-local config keeps its data beside it; a per-user config puts it
    in the per-user data directory, because an app launched from an icon has
    nowhere sensible to write relative to.
    """
    if source == "user directory":
        return str(user_data_dir() / "pokeflip.db")
    return "data/pokeflip.db"


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
