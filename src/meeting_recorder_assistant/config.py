from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    AppConfig,
    DefaultsConfig,
    MeetingClientConfig,
    MeetingTask,
    RecorderConfig,
    meeting_code_from_url,
    meeting_platform_from_url,
    normalize_meeting_code,
    normalize_meeting_platform,
)


class ConfigError(ValueError):
    """Raised when the JSON configuration is invalid."""


def load_config(path: Path) -> AppConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a JSON object.")

    recorder = _load_recorder(raw.get("recorder"))
    tencent_meeting = _load_meeting_client(raw.get("tencent_meeting", {}), "tencent_meeting")
    zoom_meeting = _load_meeting_client(raw.get("zoom_meeting", {}), "zoom_meeting")
    defaults = _load_defaults(raw.get("defaults", {}))
    tasks = tuple(_load_task(item, defaults) for item in _require_list(raw, "tasks"))
    _validate_unique_task_ids(tasks)

    return AppConfig(
        recorder=recorder,
        tencent_meeting=tencent_meeting,
        zoom_meeting=zoom_meeting,
        defaults=defaults,
        tasks=tasks,
    )


def _load_recorder(raw: Any) -> RecorderConfig:
    if not isinstance(raw, dict):
        raise ConfigError("recorder must be an object.")
    return RecorderConfig(
        path=_require_str(raw, "path", "recorder"),
        start_delay_seconds=_optional_int(raw, "start_delay_seconds", 8),
        process_names=tuple(_optional_str_list(raw, "process_names")),
        launch_if_not_running=_optional_bool(raw, "launch_if_not_running", True),
        prepare_command=_optional_str(raw, "prepare_command"),
        prepare_delay_seconds=_optional_int(raw, "prepare_delay_seconds", 10),
        start_command=_optional_str(raw, "start_command"),
        stop_command=_optional_str(raw, "stop_command"),
    )


def _load_meeting_client(raw: Any, field_name: str) -> MeetingClientConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{field_name} must be an object.")
    return MeetingClientConfig(
        close_command=_optional_str(raw, "close_command"),
        leave_command=_optional_str(raw, "leave_command"),
        process_names=tuple(_optional_str_list(raw, "process_names")),
        window_title_keywords=tuple(_optional_str_list(raw, "window_title_keywords")),
        open_delay_seconds=_optional_int(raw, "open_delay_seconds", 8),
        focus_after_join=_optional_bool(raw, "focus_after_join", True),
    )


def _load_defaults(raw: Any) -> DefaultsConfig:
    if not isinstance(raw, dict):
        raise ConfigError("defaults must be an object.")
    return DefaultsConfig(
        join_early_minutes=_optional_int(raw, "join_early_minutes", 2),
        recording_tail_minutes=_optional_int(raw, "recording_tail_minutes", 1),
        max_late_start_minutes=_optional_int(raw, "max_late_start_minutes", 10),
    )


def _load_task(raw: Any, defaults: DefaultsConfig) -> MeetingTask:
    if not isinstance(raw, dict):
        raise ConfigError("Each task must be an object.")

    task = MeetingTask(
        id=_require_str(raw, "id", "task"),
        title=_require_str(raw, "title", "task"),
        meeting_code=_optional_str(raw, "meeting_code"),
        meeting_url=_optional_str(raw, "meeting_url"),
        start_time=_parse_datetime(_require_str(raw, "start_time", "task")),
        end_time=_parse_datetime(_require_str(raw, "end_time", "task")),
        meeting_platform=_optional_meeting_platform(raw),
        meeting_password=_optional_meeting_password(raw),
        enabled=bool(raw.get("enabled", True)),
        join_early_minutes=_optional_int_or_none(raw, "join_early_minutes"),
        recording_tail_minutes=_optional_int_or_none(raw, "recording_tail_minutes"),
    )

    if not task.enabled:
        return task

    if not task.meeting_code and not task.meeting_url:
        raise ConfigError(f"Task {task.id!r} must include meeting_code or meeting_url.")
    if task.meeting_password and task.meeting_platform != "zoom":
        raise ConfigError(
            f"Task {task.id!r} uses meeting_password, but meeting_platform is not 'zoom'."
        )
    if task.meeting_url:
        url_platform = meeting_platform_from_url(task.meeting_url)
        if url_platform and url_platform != task.meeting_platform:
            raise ConfigError(
                f"Task {task.id!r} meeting_platform {task.meeting_platform!r} does not match "
                f"meeting_url platform {url_platform!r}."
            )
    if task.meeting_code and task.meeting_url:
        url_code = meeting_code_from_url(task.meeting_url)
        if url_code and normalize_meeting_code(task.meeting_code) != url_code:
            raise ConfigError(
                f"Task {task.id!r} has conflicting meeting_code and meeting_url meeting_code."
            )
    if task.end_time <= task.start_time:
        raise ConfigError(f"Task {task.id!r} end_time must be after start_time.")
    if task.recorder_start_time(defaults) >= task.finish_time(defaults):
        raise ConfigError(f"Task {task.id!r} has an invalid execution window.")
    return task


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed.replace(tzinfo=None)

    formats = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ConfigError(f"Invalid datetime {value!r}; use YYYY-MM-DD HH:MM.")


def _require_list(raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list.")
    return value


def _require_str(raw: dict[str, Any], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string.")
    return value.strip()


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string or null.")
    value = value.strip()
    return value or None


def _optional_meeting_platform(raw: dict[str, Any]) -> str:
    value = raw.get("meeting_platform")
    if value is None:
        value = meeting_platform_from_url(_optional_str(raw, "meeting_url"))
    if value is not None and not isinstance(value, str):
        raise ConfigError("meeting_platform must be a string or null.")
    try:
        return normalize_meeting_platform(value, _optional_str(raw, "meeting_url"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _optional_meeting_password(raw: dict[str, Any]) -> str | None:
    if "meeting_password" in raw and "meeting_passcode" in raw:
        password = _optional_str(raw, "meeting_password")
        passcode = _optional_str(raw, "meeting_passcode")
        if password and passcode and password != passcode:
            raise ConfigError("meeting_password and meeting_passcode must match when both are set.")
        return password or passcode
    if "meeting_password" in raw:
        return _optional_str(raw, "meeting_password")
    if "meeting_passcode" in raw:
        return _optional_str(raw, "meeting_passcode")
    return None


def _optional_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or value < 0:
        raise ConfigError(f"{key} must be a non-negative integer.")
    return value


def _optional_int_or_none(raw: dict[str, Any], key: str) -> int | None:
    if key not in raw or raw[key] is None:
        return None
    return _optional_int(raw, key, 0)


def _optional_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean.")
    return value


def _optional_str_list(raw: dict[str, Any], key: str) -> list[str]:
    value = raw.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list of strings.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{key} must be a list of non-empty strings.")
        result.append(item.strip())
    return result


def _validate_unique_task_ids(tasks: tuple[MeetingTask, ...]) -> None:
    seen: set[str] = set()
    for task in tasks:
        if task.id in seen:
            raise ConfigError(f"Duplicate task id: {task.id}")
        seen.add(task.id)
