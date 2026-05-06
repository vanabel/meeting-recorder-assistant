from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class RecorderConfig:
    path: str
    start_delay_seconds: int = 8
    process_names: tuple[str, ...] = ()
    launch_if_not_running: bool = True
    prepare_command: str | None = None
    prepare_delay_seconds: int = 10
    start_command: str | None = None
    stop_command: str | None = None


@dataclass(frozen=True)
class TencentMeetingConfig:
    close_command: str | None = None
    leave_command: str | None = None
    process_names: tuple[str, ...] = ()
    window_title_keywords: tuple[str, ...] = ()
    open_delay_seconds: int = 8
    focus_after_join: bool = True


@dataclass(frozen=True)
class DefaultsConfig:
    join_early_minutes: int = 2
    recording_tail_minutes: int = 1
    max_late_start_minutes: int = 10


@dataclass(frozen=True)
class MeetingTask:
    id: str
    title: str
    meeting_code: str | None
    meeting_url: str | None
    start_time: datetime
    end_time: datetime
    enabled: bool = True
    join_early_minutes: int | None = None
    recording_tail_minutes: int | None = None

    def recorder_start_time(self, defaults: DefaultsConfig) -> datetime:
        minutes = self.join_early_minutes
        if minutes is None:
            minutes = defaults.join_early_minutes
        return self.start_time - timedelta(minutes=minutes)

    def finish_time(self, defaults: DefaultsConfig) -> datetime:
        minutes = self.recording_tail_minutes
        if minutes is None:
            minutes = defaults.recording_tail_minutes
        return self.end_time + timedelta(minutes=minutes)

    def join_url(self) -> str:
        if self.meeting_url:
            return _normalize_meeting_url(self.meeting_url)
        if self.meeting_code:
            return meeting_url_from_code(self.meeting_code)
        raise ValueError(f"Task {self.id!r} has neither meeting_url nor meeting_code.")


@dataclass(frozen=True)
class AppConfig:
    recorder: RecorderConfig
    tencent_meeting: TencentMeetingConfig
    defaults: DefaultsConfig
    tasks: tuple[MeetingTask, ...]


def normalize_meeting_code(code: str) -> str:
    return re.sub(r"[-\s]", "", code)


def meeting_url_from_code(code: str) -> str:
    return f"wemeet://page/inmeeting?meeting_code={normalize_meeting_code(code)}"


def _normalize_meeting_url(url: str) -> str:
    return re.sub(
        r"(meeting_code=)([^&]+)",
        lambda match: f"{match.group(1)}{normalize_meeting_code(match.group(2))}",
        url,
    )


def meeting_url_with_code(url: str, code: str) -> str:
    if meeting_code_from_url(url) is None:
        return url
    return re.sub(
        r"(meeting_code=)([^&]+)",
        lambda match: f"{match.group(1)}{normalize_meeting_code(code)}",
        url,
    )


def meeting_code_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    values = parse_qs(parsed.query).get("meeting_code")
    if not values:
        return None
    return normalize_meeting_code(values[0])
