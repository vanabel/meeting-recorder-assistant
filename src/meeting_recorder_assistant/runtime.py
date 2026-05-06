from __future__ import annotations

import sys
from pathlib import Path


def is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    if is_frozen_app():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return app_root() / "config.json"


def ensure_log_dir() -> Path:
    path = app_root() / "logs"
    path.mkdir(exist_ok=True)
    return path


def gui_restart_command() -> list[str]:
    if is_frozen_app():
        return [sys.executable]
    return [sys.executable, str(app_root() / "meeting_recorder_gui.py")]
