from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


SUPPORTED_MEETING_PLATFORMS = ("tencent", "zoom")
DEFAULT_MEETING_PLATFORM = "tencent"


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
class MeetingClientConfig:
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
    meeting_platform: str = DEFAULT_MEETING_PLATFORM
    meeting_password: str | None = None
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
        return build_join_url(
            self.meeting_platform,
            meeting_code=self.meeting_code,
            meeting_url=self.meeting_url,
            meeting_password=self.meeting_password,
        )


@dataclass(frozen=True)
class AppConfig:
    recorder: RecorderConfig
    tencent_meeting: MeetingClientConfig
    zoom_meeting: MeetingClientConfig
    defaults: DefaultsConfig
    tasks: tuple[MeetingTask, ...]


def normalize_meeting_code(code: str) -> str:
    return re.sub(r"[-\s]", "", code)


def normalize_meeting_platform(
    platform: str | None,
    meeting_url: str | None = None,
) -> str:
    if platform is not None:
        normalized = platform.strip().lower()
        if normalized:
            if normalized not in SUPPORTED_MEETING_PLATFORMS:
                raise ValueError(
                    f"Unsupported meeting_platform {platform!r}; use one of "
                    f"{', '.join(SUPPORTED_MEETING_PLATFORMS)}."
                )
            return normalized

    inferred = meeting_platform_from_url(meeting_url) if meeting_url else None
    return inferred or DEFAULT_MEETING_PLATFORM


def meeting_url_from_code(code: str) -> str:
    return f"wemeet://page/inmeeting?meeting_code={normalize_meeting_code(code)}"


def zoom_join_url_from_code(code: str, meeting_password: str | None = None) -> str:
    base = f"https://app.zoom.us/wc/{normalize_meeting_code(code)}/join"
    query_items: list[tuple[str, str]] = [("wpk", "wcpk")]
    if meeting_password:
        query_items.append(("pwd", meeting_password))
    return f"{base}?{urlencode(query_items)}"


def build_join_url(
    platform: str | None,
    *,
    meeting_code: str | None,
    meeting_url: str | None,
    meeting_password: str | None = None,
) -> str:
    normalized_platform = normalize_meeting_platform(platform, meeting_url)
    if meeting_url:
        return meeting_url_with_details(
            meeting_url,
            normalized_platform,
            code=meeting_code,
            meeting_password=meeting_password,
        )
    if not meeting_code:
        raise ValueError("meeting_code or meeting_url is required.")
    if normalized_platform == "zoom":
        return zoom_join_url_from_code(meeting_code, meeting_password)
    return meeting_url_from_code(meeting_code)


def meeting_platform_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()

    if scheme == "wemeet" or "meeting_code" in parse_qs(parsed.query):
        return "tencent"
    if scheme == "zoommtg":
        return "zoom"
    if host.endswith("zoom.us") and (
        re.search(r"/j/\d+", parsed.path)
        or re.search(r"/wc/join/\d+", parsed.path)
        or re.search(r"/wc/\d+/join", parsed.path)
    ):
        return "zoom"
    return None


def meeting_url_with_details(
    url: str,
    platform: str | None,
    *,
    code: str | None = None,
    meeting_password: str | None = None,
) -> str:
    normalized_platform = normalize_meeting_platform(platform, url)
    if normalized_platform == "zoom":
        return _normalize_zoom_url(url, meeting_code=code, meeting_password=meeting_password)
    return _normalize_tencent_url(url, meeting_code=code)


def _normalize_tencent_url(url: str, meeting_code: str | None = None) -> str:
    normalized = re.sub(
        r"(meeting_code=)([^&]+)",
        lambda match: f"{match.group(1)}{normalize_meeting_code(match.group(2))}",
        url,
    )
    if meeting_code is None or meeting_code_from_url(normalized) is None:
        return normalized
    return re.sub(
        r"(meeting_code=)([^&]+)",
        lambda match: f"{match.group(1)}{normalize_meeting_code(meeting_code)}",
        normalized,
    )


def meeting_url_with_code(
    url: str,
    code: str,
    platform: str | None = None,
    meeting_password: str | None = None,
) -> str:
    if meeting_platform_from_url(url) is None and platform is None:
        return url
    return meeting_url_with_details(
        url,
        platform,
        code=code,
        meeting_password=meeting_password,
    )


def meeting_code_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    values = parse_qs(parsed.query).get("meeting_code")
    if values:
        return normalize_meeting_code(values[0])

    if parsed.scheme.lower() == "zoommtg":
        zoom_values = parse_qs(parsed.query).get("confno")
        if zoom_values:
            return normalize_meeting_code(zoom_values[0])

    match = re.search(r"/j/(\d+)|/wc/join/(\d+)|/wc/(\d+)/join", parsed.path)
    if match:
        return normalize_meeting_code(next(group for group in match.groups() if group))
    return None


def _normalize_zoom_url(
    url: str,
    *,
    meeting_code: str | None = None,
    meeting_password: str | None = None,
) -> str:
    parsed = urlparse(url)
    normalized_code = normalize_meeting_code(meeting_code) if meeting_code else None
    query = parse_qs(parsed.query, keep_blank_values=True)

    if parsed.scheme.lower() == "zoommtg":
        if normalized_code:
            query["confno"] = [normalized_code]
        if meeting_password:
            query["pwd"] = [meeting_password]
        return urlunparse(
            parsed._replace(
                query=urlencode([(key, value) for key, values in query.items() for value in values])
            )
        )

    path = parsed.path
    if normalized_code:
        if re.search(r"/j/\d+", path):
            path = re.sub(r"/j/\d+", f"/wc/{normalized_code}/join", path, count=1)
        elif re.search(r"/wc/join/\d+", path):
            path = re.sub(r"/wc/join/\d+", f"/wc/{normalized_code}/join", path, count=1)
        elif re.search(r"/wc/\d+/join", path):
            path = re.sub(r"/wc/\d+/join", f"/wc/{normalized_code}/join", path, count=1)
        else:
            path = f"/wc/{normalized_code}/join"
    elif re.search(r"/j/(\d+)", path):
        path = re.sub(r"/j/(\d+)", r"/wc/\1/join", path, count=1)
    elif re.search(r"/wc/join/(\d+)", path):
        path = re.sub(r"/wc/join/(\d+)", r"/wc/\1/join", path, count=1)
    if meeting_password and "pwd" not in query and "wpk" not in query:
        query["pwd"] = [meeting_password]

    return urlunparse(
        parsed._replace(
            netloc="app.zoom.us" if parsed.netloc.endswith("zoom.us") else parsed.netloc,
            path=path,
            query=urlencode([(key, value) for key, values in query.items() for value in values]),
        )
    )
