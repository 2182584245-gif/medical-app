from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import APP_ID


def is_frozen_app() -> bool:
    """Return whether the code is running from a PyInstaller executable."""

    return bool(getattr(sys, "frozen", False))


def application_dir() -> Path:
    """Return the folder that contains the portable executable or source project."""

    if is_frozen_app():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def legacy_user_data_dir() -> Path:
    """Return the per-user data folder used by version 0.1.x."""

    if local_app_data := os.getenv("LOCALAPPDATA"):
        return Path(local_app_data) / APP_ID
    return Path.home() / f".{APP_ID}"


def legacy_database_path() -> Path:
    return legacy_user_data_dir() / "app.db"


def user_data_dir() -> Path:
    """Return the data directory used by this run.

    Packaged builds are deliberately portable: their database lives beside the
    executable in ``data`` so copying the complete application folder preserves
    accounts and chats. Source/PyCharm runs continue to use the normal per-user
    data directory unless the test/development override is set.
    """

    override = os.getenv("OLLAMA_DUAL_CHAT_DATA_DIR")
    if override:
        root = Path(override).expanduser().resolve()
    elif is_frozen_app():
        root = application_dir() / "data"
    else:
        root = legacy_user_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def database_path() -> Path:
    return user_data_dir() / "app.db"


def log_path() -> Path:
    log_dir = user_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "app.log"


__all__ = [
    "application_dir",
    "database_path",
    "is_frozen_app",
    "legacy_database_path",
    "legacy_user_data_dir",
    "log_path",
    "user_data_dir",
]
