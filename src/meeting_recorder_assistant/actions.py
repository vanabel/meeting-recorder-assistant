from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import webbrowser
import ctypes
from ctypes import byref, wintypes, windll
from pathlib import Path
from typing import Any

from .models import AppConfig, MeetingClientConfig, MeetingTask

LOGGER = logging.getLogger(__name__)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STARTF_USESHOWWINDOW = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
SW_HIDE = 0


class ActionError(RuntimeError):
    """Raised when an automation action fails."""


class ElevationRequiredError(ActionError):
    """Raised when the recorder requires the controller to run as administrator."""


def is_running_as_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _windows_subprocess_kwargs() -> dict[str, Any]:
    if sys.platform != "win32":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = SW_HIDE
    return {
        "creationflags": CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def _powershell_command_args(command: str) -> list[str]:
    return [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-Command",
        command,
    ]


def ensure_recorder_running(config: AppConfig, dry_run: bool) -> bool:
    recorder_path = Path(config.recorder.path)
    if _recorder_is_ready(config):
        LOGGER.info("Recorder is already running with an active window.")
        return True

    if not config.recorder.launch_if_not_running:
        raise ActionError("Recorder is not ready and launch_if_not_running is false.")

    if _recorder_is_running(config):
        LOGGER.info("Recorder process is running without a main window; launching app to show it.")

    LOGGER.info("Starting recorder application: %s", recorder_path)
    if dry_run:
        return False
    if not recorder_path.exists():
        raise ActionError(f"Recorder executable not found: {recorder_path}")
    _launch_executable(recorder_path)
    if _wait_for_recorder_process(config, config.recorder.start_delay_seconds):
        LOGGER.info("Recorder launched successfully.")
    else:
        LOGGER.warning(
            "Recorder launch command finished, but configured process name was not detected: %s",
            ", ".join(_recorder_process_names(config)),
        )
    if not _wait_for_recorder_window(config, config.recorder.start_delay_seconds):
        raise ActionError(
            "Recorder process exists but no recorder window is available for hotkeys. "
            "Open HiRecMaster manually once, or verify recorder.path/process_names."
        )
    if config.recorder.start_delay_seconds:
        LOGGER.info(
            "Waiting %s second(s) after recorder launch.",
            config.recorder.start_delay_seconds,
        )
        time.sleep(config.recorder.start_delay_seconds)
    return False


def start_recording(config: AppConfig, dry_run: bool) -> None:
    if config.recorder.start_command:
        LOGGER.info("Running recorder start command.")
        if _recorder_command_requires_focus(config.recorder.start_command):
            if not _activate_recorder_window(config, dry_run):
                LOGGER.warning("Recorder window could not be activated; sending start command anyway.")
        _run_recorder_command(config, config.recorder.start_command, dry_run)
    else:
        LOGGER.info("No recorder start command configured; assuming recorder starts itself.")


def prepare_recorder(config: AppConfig, dry_run: bool, recorder_was_ready: bool = False) -> None:
    command = config.recorder.prepare_command
    if not command:
        LOGGER.info("No recorder prepare command configured.")
        return
    LOGGER.info("Running recorder prepare command.")
    if not _activate_recorder_window(config, dry_run):
        if _recorder_command_requires_focus(command):
            raise ActionError("Cannot prepare recorder because recorder window could not be activated.")
        LOGGER.warning(
            "Recorder window could not be activated; running prepare command anyway because it does not require foreground focus."
        )
    _run_recorder_command(config, command, dry_run)
    if config.recorder.prepare_delay_seconds:
        LOGGER.info(
            "Waiting %s second(s) after recorder prepare command.",
            config.recorder.prepare_delay_seconds,
        )
        if not dry_run:
            time.sleep(config.recorder.prepare_delay_seconds)


def join_meeting(config: AppConfig, task: MeetingTask, dry_run: bool) -> None:
    url = task.join_url()
    platform_label = _meeting_platform_label(task.meeting_platform)
    meeting_config = _meeting_client_config(config, task)
    LOGGER.info("Opening %s meeting for task %s: %s", platform_label, task.id, url)
    if dry_run:
        return

    if task.meeting_platform == "zoom":
        _open_zoom_meeting_in_browser(url)
        if not _prepare_zoom_browser_join(task.meeting_password, dry_run=dry_run):
            raise ActionError(
                "Zoom browser join page did not complete correctly. The browser page was not ready, "
                "the passcode could not be entered, or the Join button was unavailable."
            )
    elif sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        webbrowser.open(url)

    if meeting_config.open_delay_seconds:
        LOGGER.info(
            "Waiting %s second(s) after opening %s.",
            meeting_config.open_delay_seconds,
            platform_label,
        )
        time.sleep(meeting_config.open_delay_seconds)
    if meeting_config.focus_after_join:
        focus_meeting_client(config, task, dry_run=dry_run)


def focus_meeting_client(config: AppConfig, task: MeetingTask, dry_run: bool) -> None:
    if dry_run or sys.platform != "win32":
        return

    platform_label = _meeting_platform_label(task.meeting_platform)
    hwnd = _meeting_window_handle(config, task)
    if hwnd and _force_foreground_window(hwnd):
        foreground_name = _foreground_process_name()
        if foreground_name:
            LOGGER.info("Foreground window process after meeting focus: %s", foreground_name)
        LOGGER.info("%s window is foreground.", platform_label)
        return
    LOGGER.warning("Could not make %s window foreground.", platform_label)


def stop_recorder(config: AppConfig, dry_run: bool) -> None:
    command = config.recorder.stop_command
    if not command:
        LOGGER.info("No recorder stop command configured; assuming recorder stops itself.")
        return
    LOGGER.info("Running recorder stop command.")
    if _recorder_command_requires_focus(command):
        if not _activate_recorder_window(config, dry_run):
            LOGGER.warning("Recorder window could not be activated; sending stop command anyway.")
    _run_recorder_command(config, command, dry_run)


def close_meeting_client(config: AppConfig, task: MeetingTask, dry_run: bool) -> None:
    command = _meeting_client_config(config, task).close_command
    if not command:
        LOGGER.info("No %s close command configured.", _meeting_platform_label(task.meeting_platform))
        return
    LOGGER.info("Running %s close command.", _meeting_platform_label(task.meeting_platform))
    _run_shell_command(command, dry_run)


def leave_meeting_client(config: AppConfig, task: MeetingTask, dry_run: bool) -> None:
    command = _meeting_client_config(config, task).leave_command
    if not command:
        LOGGER.info("No %s leave command configured.", _meeting_platform_label(task.meeting_platform))
        return
    LOGGER.info("Running %s leave command.", _meeting_platform_label(task.meeting_platform))
    _run_shell_command(command, dry_run)


def _run_shell_command(command: str, dry_run: bool) -> None:
    LOGGER.debug("Command: %s", command)
    if dry_run:
        return

    completed = subprocess.run(
        command,
        check=False,
        shell=True,
        **_windows_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        raise ActionError(f"Command failed with exit code {completed.returncode}: {command}")


def _run_recorder_command(config: AppConfig, command: str, dry_run: bool) -> None:
    if command.lower().startswith("hotkey:"):
        hotkey = command.split(":", 1)[1].strip()
        LOGGER.info("Sending recorder hotkey: %s", hotkey)
        if dry_run:
            return
        _send_hotkey(hotkey)
        return
    if command.lower().startswith("click_text:"):
        text = command.split(":", 1)[1].strip()
        LOGGER.info("Clicking recorder UI text: %s", text)
        if dry_run:
            return
        _click_recorder_text(config, text)
        return
    if command.lower() == "mode:fullscreen":
        LOGGER.info("Selecting recorder full-screen mode.")
        if dry_run:
            return
        _select_fullscreen_mode(config)
        return
    _run_shell_command(command, dry_run)


def _recorder_command_requires_focus(command: str) -> bool:
    _normalized = command.strip().lower()
    return False


def _prepare_zoom_browser_join(meeting_password: str | None, dry_run: bool) -> bool:
    if dry_run or sys.platform != "win32":
        return True

    _dismiss_zoom_open_app_prompt()
    deadline = time.monotonic() + 45
    initial_refresh_done = False
    refresh_attempts = 0
    while time.monotonic() <= deadline:
        browser_page_open = _zoom_browser_page_is_open()
        if browser_page_open and not initial_refresh_done:
            initial_refresh_done = True
            LOGGER.info("Refreshing Zoom browser page after open.")
            _refresh_zoom_browser_page()
            time.sleep(3)
            continue
        if _click_zoom_browser_join_link():
            LOGGER.info("Selected Zoom browser join link.")
            time.sleep(1)
            browser_page_open = True
        if browser_page_open and _click_zoom_browser_continue_without_media():
            LOGGER.info("Selected Zoom browser continue-without-media action.")
            time.sleep(1)
            continue
        if _complete_zoom_native_join(meeting_password):
            LOGGER.info("Completed Zoom native join flow.")
            return True
        if browser_page_open and _complete_zoom_browser_join(meeting_password):
            LOGGER.info("Completed Zoom browser join flow.")
            return True
        if browser_page_open and refresh_attempts < 3:
            refresh_attempts += 1
            LOGGER.info("Refreshing Zoom browser page (attempt %s).", refresh_attempts)
            _refresh_zoom_browser_page()
            time.sleep(2)
            continue
        _dismiss_zoom_open_app_prompt()
        time.sleep(1)
    LOGGER.warning("Could not complete Zoom browser join flow automatically.")
    return False


def _launch_executable(path: Path) -> None:
    try:
        subprocess.Popen([str(path)], **_windows_subprocess_kwargs())
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == 740:
            if is_running_as_admin():
                LOGGER.warning("Recorder still requested elevation while controller is admin.")
                _shell_execute_runas(path)
                return
            raise ElevationRequiredError(
                "Recorder requires administrator privileges. Restart this app as administrator "
                "so it can launch the recorder and send recording hotkeys."
            ) from exc
        raise ActionError(f"Failed to start executable {path}: {exc}") from exc


def _recorder_is_running(config: AppConfig) -> bool:
    names = _recorder_process_names(config)
    if not names:
        return False

    if sys.platform == "win32":
        powershell_names = ", ".join(_powershell_string(_process_lookup_name(name)) for name in names)
        command = (
            f"$names = @({powershell_names}); "
            "foreach ($name in $names) { "
            "if (Get-Process -Name $name -ErrorAction SilentlyContinue) { exit 0 } "
            "}; exit 1"
        )
        completed = subprocess.run(
            _powershell_command_args(command),
            check=False,
            capture_output=True,
            text=True,
            **_windows_subprocess_kwargs(),
        )
        return completed.returncode == 0

    completed = subprocess.run(["pgrep", "-f", "|".join(names)], check=False)
    return completed.returncode == 0


def _recorder_is_ready(config: AppConfig) -> bool:
    if sys.platform != "win32":
        return _recorder_is_running(config)
    return _recorder_has_window(config)


def _recorder_process_names(config: AppConfig) -> tuple[str, ...]:
    if config.recorder.process_names:
        return config.recorder.process_names
    return (Path(config.recorder.path).name,)


def _activate_recorder_window(config: AppConfig, dry_run: bool) -> bool:
    if dry_run or sys.platform != "win32":
        return True

    deadline = time.monotonic() + 5
    while True:
        hwnd = _recorder_window_handle(config)
        if hwnd and _force_foreground_window(hwnd) and _foreground_belongs_to(config):
            LOGGER.info("Recorder window is foreground.")
            return True
        if time.monotonic() >= deadline:
            LOGGER.warning("Could not make recorder window foreground.")
            return False
        time.sleep(0.5)


def _wait_for_recorder_process(config: AppConfig, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        if _recorder_is_running(config):
            return True
        time.sleep(0.5)
    return _recorder_is_running(config)


def _wait_for_recorder_window(config: AppConfig, timeout_seconds: int) -> bool:
    if sys.platform != "win32":
        return True
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        if _recorder_has_window(config):
            return True
        time.sleep(0.5)
    return _recorder_has_window(config)


def _recorder_has_window(config: AppConfig) -> bool:
    return _recorder_window_handle(config) is not None


def _recorder_window_handle(config: AppConfig) -> int | None:
    names = ", ".join(_powershell_string(_process_lookup_name(name)) for name in _recorder_process_names(config))
    command = (
        f"$names = @({names}); "
        "foreach ($name in $names) { "
        "$process = Get-Process -Name $name -ErrorAction SilentlyContinue "
        "| Where-Object { $_.MainWindowHandle -ne 0 } "
        "| Select-Object -First 1; "
        "if ($process) { Write-Output $process.MainWindowHandle; exit 0 } "
        "}; exit 1"
    )
    completed = subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        return None
    try:
        return int(completed.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        return None


def _force_foreground_window(hwnd: int) -> bool:
    user32 = windll.user32
    shell32 = windll.shell32
    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040

    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    result = bool(user32.SetForegroundWindow(hwnd))
    if not result:
        shell32.ShellExecuteW(None, "open", "cmd.exe", "/c exit", None, 0)
        time.sleep(0.1)
        result = bool(user32.SetForegroundWindow(hwnd))
    time.sleep(0.3)
    return result or user32.GetForegroundWindow() == hwnd


def _foreground_belongs_to(config: AppConfig) -> bool:
    hwnd = windll.user32.GetForegroundWindow()
    if not hwnd:
        return False
    process_id = wintypes.DWORD()
    windll.user32.GetWindowThreadProcessId(hwnd, byref(process_id))
    foreground_name = _process_name_from_pid(process_id.value)
    expected = {_process_lookup_name(name).lower() for name in _recorder_process_names(config)}
    if foreground_name:
        LOGGER.info("Foreground window process: %s", foreground_name)
    return foreground_name is not None and foreground_name.lower() in expected


def _process_name_from_pid(pid: int) -> str | None:
    command = f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty ProcessName"
    completed = subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        return None
    name = completed.stdout.strip()
    return name or None


def _foreground_process_name() -> str | None:
    hwnd = windll.user32.GetForegroundWindow()
    if not hwnd:
        return None
    process_id = wintypes.DWORD()
    windll.user32.GetWindowThreadProcessId(hwnd, byref(process_id))
    return _process_name_from_pid(process_id.value)


def _click_recorder_text(config: AppConfig, text: str) -> None:
    if sys.platform != "win32":
        raise ActionError("click_text commands are only supported on Windows.")

    names = ", ".join(_powershell_string(_process_lookup_name(name)) for name in _recorder_process_names(config))
    command = (
        "Add-Type -AssemblyName UIAutomationClient; "
        "Add-Type -AssemblyName UIAutomationTypes; "
        "Add-Type -Namespace Win32 -Name Mouse -MemberDefinition "
        "'[DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int X, int Y); "
        "[DllImport(\"user32.dll\")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);'; "
        f"$text = {_powershell_string(text)}; "
        f"$names = @({names}); "
        "$window = $null; "
        "foreach ($name in $names) { "
        "$process = Get-Process -Name $name -ErrorAction SilentlyContinue "
        "| Where-Object { $_.MainWindowHandle -ne 0 } "
        "| Select-Object -First 1; "
        "if ($process) { $window = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$process.MainWindowHandle); break } "
        "}; "
        "if (-not $window) { Write-Error 'Recorder window not found'; exit 1 }; "
        "$condition = New-Object System.Windows.Automation.PropertyCondition("
        "[System.Windows.Automation.AutomationElement]::NameProperty, $text); "
        "$element = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition); "
        "if (-not $element) { "
        "$all = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, "
        "[System.Windows.Automation.Condition]::TrueCondition); "
        "foreach ($item in $all) { "
        "if ($item.Current.Name -like ('*' + $text + '*')) { $element = $item; break } "
        "} "
        "}; "
        "if (-not $element) { Write-Error ('Recorder UI text not found: ' + $text); exit 2 }; "
        "$pattern = $null; "
        "if ($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { "
        "$pattern.Invoke(); Start-Sleep -Milliseconds 500; exit 0 "
        "}; "
        "$rect = $element.Current.BoundingRectangle; "
        "if ($rect.IsEmpty) { Write-Error ('Recorder UI text has no clickable bounds: ' + $text); exit 3 }; "
        "$x = [int](($rect.Left + $rect.Right) / 2); "
        "$y = [int](($rect.Top + $rect.Bottom) / 2); "
        "[Win32.Mouse]::SetCursorPos($x, $y) | Out-Null; "
        "Start-Sleep -Milliseconds 100; "
        "[Win32.Mouse]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero); "
        "Start-Sleep -Milliseconds 80; "
        "[Win32.Mouse]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero); "
        "Start-Sleep -Milliseconds 500"
    )
    completed = subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ActionError(f"Failed to click recorder UI text {text!r}: {detail}")


def _select_fullscreen_mode(config: AppConfig) -> None:
    try:
        _click_recorder_text(config, "全屏录制")
        LOGGER.info("Selected full-screen mode by UI text.")
        return
    except ActionError as exc:
        LOGGER.warning("Could not select full-screen mode by text: %s", exc)

    LOGGER.info("Selecting full-screen mode by recorder window position fallback.")
    _click_recorder_relative(config, x_ratio=0.14, y_ratio=0.54)


def _click_recorder_relative(config: AppConfig, x_ratio: float, y_ratio: float) -> None:
    hwnd = _recorder_window_handle(config)
    if hwnd is None:
        raise ActionError("Recorder window not found for relative click.")

    rect = wintypes.RECT()
    if not windll.user32.GetWindowRect(hwnd, byref(rect)):
        raise ActionError("Could not read recorder window bounds.")

    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise ActionError("Recorder window bounds are invalid.")

    x = int(rect.left + width * x_ratio)
    y = int(rect.top + height * y_ratio)
    LOGGER.info("Clicking recorder window at relative position %.2f, %.2f.", x_ratio, y_ratio)
    windll.user32.SetCursorPos(x, y)
    time.sleep(0.1)
    windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.5)


def _process_lookup_name(name: str) -> str:
    path = Path(name)
    if path.suffix.lower() == ".exe":
        return path.stem
    return name


def _meeting_client_config(config: AppConfig, task: MeetingTask) -> MeetingClientConfig:
    if task.meeting_platform == "zoom":
        return config.zoom_meeting
    return config.tencent_meeting


def _meeting_platform_label(platform: str) -> str:
    if platform == "zoom":
        return "Zoom"
    return "Tencent Meeting"


def _meeting_window_handle(config: AppConfig, task: MeetingTask) -> int | None:
    meeting_config = _meeting_client_config(config, task)
    process_names = meeting_config.process_names or _default_meeting_process_names(task.meeting_platform)
    hwnd = _window_handle_by_process_names(process_names)
    if hwnd is not None:
        return hwnd

    title_keywords = meeting_config.window_title_keywords or _default_meeting_title_keywords(
        task.meeting_platform
    )
    return _window_handle_by_title_keywords(title_keywords)


def _default_meeting_process_names(platform: str) -> tuple[str, ...]:
    if platform == "zoom":
        return ("Zoom.exe", "Zoom", "Zoom Workplace")
    return ("wemeetapp.exe", "TencentMeeting.exe", "TencentMeeting")


def _default_meeting_title_keywords(platform: str) -> tuple[str, ...]:
    if platform == "zoom":
        return ("Zoom Workplace", "Zoom Meeting", "Zoom")
    return ("\u817e\u8baf\u4f1a\u8bae", "Tencent Meeting", "VooV Meeting")


def _window_handle_by_process_names(process_names: tuple[str, ...]) -> int | None:
    names = ", ".join(_powershell_string(_process_lookup_name(name)) for name in process_names)
    command = (
        f"$names = @({names}); "
        "foreach ($name in $names) { "
        "$process = Get-Process -Name $name -ErrorAction SilentlyContinue "
        "| Where-Object { $_.MainWindowHandle -ne 0 } "
        "| Select-Object -First 1; "
        "if ($process) { Write-Output $process.MainWindowHandle; exit 0 } "
        "}; exit 1"
    )
    return _window_handle_from_powershell(command)


def _window_handle_by_title_keywords(title_keywords: tuple[str, ...]) -> int | None:
    keywords = ", ".join(_powershell_string(keyword) for keyword in title_keywords)
    command = (
        f"$keywords = @({keywords}); "
        "$processes = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 }; "
        "foreach ($keyword in $keywords) { "
        "$process = $processes | Where-Object { $_.MainWindowTitle -like ('*' + $keyword + '*') } "
        "| Select-Object -First 1; "
        "if ($process) { Write-Output $process.MainWindowHandle; exit 0 } "
        "}; exit 1"
    )
    return _window_handle_from_powershell(command)


def _window_handle_from_powershell(command: str) -> int | None:
    completed = subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        return None
    try:
        return int(completed.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        return None


def _powershell_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _open_zoom_meeting_in_browser(url: str) -> None:
    if sys.platform == "win32":
        browser_path = _find_browser_executable()
        if browser_path is not None:
            args = [str(browser_path)]
            if browser_path.stem.lower() == "firefox":
                args.append("-new-window")
            args.append(url)
            subprocess.Popen(args)
            return

    opened = webbrowser.open(url, new=2)
    if opened:
        return

    if sys.platform == "win32":
        completed = subprocess.run(
            _powershell_command_args(f"Start-Process {_powershell_string(url)}"),
            check=False,
            capture_output=True,
            text=True,
            **_windows_subprocess_kwargs(),
        )
        if completed.returncode == 0:
            return
    raise ActionError(f"Failed to open Zoom meeting URL in the browser: {url}")


def _find_browser_executable() -> Path | None:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LocalAppData", "")
    candidates = [
        Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files_x86) / "Mozilla Firefox" / "firefox.exe",
        Path(program_files) / "Mozilla Firefox" / "firefox.exe",
        Path(program_files_x86) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
        Path(program_files) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _dismiss_zoom_open_app_prompt() -> None:
    buttons = ("Cancel", "\u53d6\u6d88", "Don't Open", "\u4e0d\u6253\u5f00")
    command = (
        "Add-Type -AssemblyName UIAutomationClient; "
        "Add-Type -AssemblyName UIAutomationTypes; "
        "$shell = New-Object -ComObject WScript.Shell; "
        "$shell.SendKeys('{ESC}'); "
        "Start-Sleep -Milliseconds 300; "
        "$buttons = @("
        + ", ".join(_powershell_string(text) for text in buttons)
        + "); "
        "$windows = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 }; "
        "foreach ($process in $windows) { "
        "$window = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$process.MainWindowHandle); "
        "if (-not $window) { continue }; "
        "foreach ($text in $buttons) { "
        "$condition = New-Object System.Windows.Automation.PropertyCondition("
        "[System.Windows.Automation.AutomationElement]::NameProperty, $text); "
        "$element = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition); "
        "if (-not $element) { continue }; "
        "$pattern = $null; "
        "if ($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { "
        "$pattern.Invoke(); exit 0 "
        "} "
        "} "
        "}; exit 0"
    )
    subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )


def _zoom_browser_page_is_open() -> bool:
    if sys.platform != "win32":
        return False

    browser_names = ("msedge", "chrome", "firefox", "brave", "opera", "iexplore")
    command = (
        f"$names = @({', '.join(_powershell_string(name) for name in browser_names)}); "
        "$processes = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 }; "
        "foreach ($process in $processes) { "
        "$name = $process.ProcessName.ToLowerInvariant(); "
        "if ($names -contains $name) { exit 0 } "
        "}; exit 1"
    )
    completed = subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )
    return completed.returncode == 0


def _refresh_zoom_browser_page() -> None:
    if sys.platform != "win32":
        return

    browser_names = ("msedge", "chrome", "firefox", "brave", "opera", "iexplore")
    command = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$names = @({', '.join(_powershell_string(name) for name in browser_names)}); "
        "$processes = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | Sort-Object StartTime -Descending; "
        "foreach ($process in $processes) { "
        "$name = $process.ProcessName.ToLowerInvariant(); "
        "if (-not ($names -contains $name)) { continue }; "
        "$shell.AppActivate($process.Id) | Out-Null; "
        "Start-Sleep -Milliseconds 300; "
        "$shell.SendKeys('~'); "
        "Start-Sleep -Milliseconds 400; "
        "$shell.SendKeys('{F5}'); "
        "exit 0 "
        "}; exit 1"
    )
    subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )


def _complete_zoom_browser_join(meeting_password: str | None) -> bool:
    if sys.platform != "win32":
        return False

    browser_names = ("msedge", "chrome", "firefox", "brave", "opera", "iexplore")
    password_labels = (
        "Passcode",
        "Meeting Passcode",
        "Passcode*",
        "\u5bc6\u7801",
        "\u4f1a\u8bae\u5bc6\u7801",
    )
    join_labels = (
        "Join",
        "Join Meeting",
        "\u52a0\u5165",
        "\u52a0\u5165\u4f1a\u8bae",
    )
    password_value = meeting_password or ""
    command = (
        "Add-Type -AssemblyName UIAutomationClient; "
        "Add-Type -AssemblyName UIAutomationTypes; "
        f"$names = @({', '.join(_powershell_string(name) for name in browser_names)}); "
        f"$passwordLabels = @({', '.join(_powershell_string(text) for text in password_labels)}); "
        f"$joinLabels = @({', '.join(_powershell_string(text) for text in join_labels)}); "
        f"$passwordValue = {_powershell_string(password_value)}; "
        "$processes = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | Sort-Object StartTime -Descending; "
        "foreach ($process in $processes) { "
        "$name = $process.ProcessName.ToLowerInvariant(); "
        "if (-not ($names -contains $name)) { continue }; "
        "$window = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$process.MainWindowHandle); "
        "if (-not $window) { continue }; "
        "$all = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, "
        "[System.Windows.Automation.Condition]::TrueCondition); "
        "$passwordFilled = $false; "
        "if ($passwordValue) { "
        "foreach ($item in $all) { "
        "$name = $item.Current.Name; "
        "$controlType = $item.Current.ControlType.ProgrammaticName; "
        "if ($controlType -notlike '*Edit') { continue }; "
        "foreach ($label in $passwordLabels) { "
        "if ($name -eq $label -or $name -like ('*' + $label + '*')) { "
        "$valuePattern = $null; "
        "if ($item.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$valuePattern)) { "
        "$valuePattern.SetValue($passwordValue); $passwordFilled = $true; Start-Sleep -Milliseconds 300; break "
        "} "
        "} "
        "} "
        "if ($passwordFilled) { break } "
        "} "
        "} "
        "foreach ($item in $all) { "
        "$name = $item.Current.Name; "
        "if (-not $name) { continue }; "
        "foreach ($label in $joinLabels) { "
        "if ($name -eq $label -or $name -like ('*' + $label + '*')) { "
        "$pattern = $null; "
        "if ($item.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { "
        "$pattern.Invoke(); exit 0 "
        "} "
        "$rect = $item.Current.BoundingRectangle; "
        "if (-not $rect.IsEmpty) { "
        "$shell = New-Object -ComObject WScript.Shell; "
        "$shell.AppActivate($process.Id) | Out-Null; "
        "Add-Type -Namespace Win32 -Name MouseJoin -MemberDefinition "
        "'[DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int X, int Y); "
        "[DllImport(\"user32.dll\")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);'; "
        "$x = [int](($rect.Left + $rect.Right) / 2); "
        "$y = [int](($rect.Top + $rect.Bottom) / 2); "
        "[Win32.MouseJoin]::SetCursorPos($x, $y) | Out-Null; "
        "Start-Sleep -Milliseconds 100; "
        "[Win32.MouseJoin]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero); "
        "Start-Sleep -Milliseconds 80; "
        "[Win32.MouseJoin]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero); "
        "exit 0 "
        "} "
        "} "
        "} "
        "} "
        "}; exit 1"
    )
    completed = subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )
    return completed.returncode == 0


def _click_zoom_browser_continue_without_media() -> bool:
    if sys.platform != "win32":
        return False

    browser_names = ("msedge", "chrome", "firefox", "brave", "opera", "iexplore")
    title_keywords = ("Zoom", "Join Meeting", "\u52a0\u5165\u4f1a\u8bae", "app.zoom.us", "zoom.us")
    button_texts = (
        "Continue without microphone and camera",
        "\u5728\u6ca1\u6709\u9ea6\u514b\u98ce\u548c\u6444\u50cf\u5934\u7684\u60c5\u51b5\u4e0b\u7ee7\u7eed",
    )
    command = (
        "Add-Type -AssemblyName UIAutomationClient; "
        "Add-Type -AssemblyName UIAutomationTypes; "
        f"$names = @({', '.join(_powershell_string(name) for name in browser_names)}); "
        f"$keywords = @({', '.join(_powershell_string(text) for text in title_keywords)}); "
        f"$targets = @({', '.join(_powershell_string(text) for text in button_texts)}); "
        "$processes = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 }; "
        "$windows = @(); "
        "foreach ($process in $processes) { "
        "$name = $process.ProcessName.ToLowerInvariant(); "
        "$title = $process.MainWindowTitle; "
        "$matchesName = $names -contains $name; "
        "$matchesTitle = $false; "
        "foreach ($keyword in $keywords) { "
        "if ($title -like ('*' + $keyword + '*')) { $matchesTitle = $true; break } "
        "} "
        "if ($matchesName -or $matchesTitle) { $windows += $process } "
        "}; "
        "foreach ($process in $windows) { "
        "$window = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$process.MainWindowHandle); "
        "if (-not $window) { continue }; "
        "$all = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, "
        "[System.Windows.Automation.Condition]::TrueCondition); "
        "foreach ($target in $targets) { "
        "foreach ($item in $all) { "
        "$name = $item.Current.Name; "
        "if (-not $name) { continue }; "
        "if ($name -eq $target -or $name -like ('*' + $target + '*')) { "
        "$pattern = $null; "
        "if ($item.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { "
        "$pattern.Invoke(); exit 0 "
        "} "
        "$rect = $item.Current.BoundingRectangle; "
        "if (-not $rect.IsEmpty) { "
        "$x = [int](($rect.Left + $rect.Right) / 2); "
        "$y = [int](($rect.Top + $rect.Bottom) / 2); "
        "$shell = New-Object -ComObject WScript.Shell; "
        "$shell.AppActivate($process.Id) | Out-Null; "
        "Add-Type -Namespace Win32 -Name MouseContinueWithoutMedia -MemberDefinition "
        "'[DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int X, int Y); "
        "[DllImport(\"user32.dll\")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);'; "
        "[Win32.MouseContinueWithoutMedia]::SetCursorPos($x, $y) | Out-Null; "
        "Start-Sleep -Milliseconds 100; "
        "[Win32.MouseContinueWithoutMedia]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero); "
        "Start-Sleep -Milliseconds 80; "
        "[Win32.MouseContinueWithoutMedia]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero); "
        "exit 0 "
        "} "
        "} "
        "} "
        "} "
        "}; exit 1"
    )
    completed = subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )
    return completed.returncode == 0


def _complete_zoom_native_join(meeting_password: str | None) -> bool:
    if sys.platform != "win32" or not meeting_password:
        return False

    title_keywords = (
        "Zoom",
        "Zoom Workplace",
        "\u8f93\u5165\u4f1a\u8bae\u5bc6\u7801",
        "Enter Meeting Passcode",
        "Passcode",
    )
    input_labels = (
        "Passcode",
        "Meeting Passcode",
        "\u4f1a\u8bae\u5bc6\u7801",
        "\u5bc6\u7801",
    )
    join_labels = (
        "Join Meeting",
        "Join",
        "\u52a0\u5165\u4f1a\u8bae",
        "\u52a0\u5165",
    )
    command = (
        "Add-Type -AssemblyName UIAutomationClient; "
        "Add-Type -AssemblyName UIAutomationTypes; "
        f"$keywords = @({', '.join(_powershell_string(text) for text in title_keywords)}); "
        f"$inputLabels = @({', '.join(_powershell_string(text) for text in input_labels)}); "
        f"$joinLabels = @({', '.join(_powershell_string(text) for text in join_labels)}); "
        f"$passwordValue = {_powershell_string(meeting_password)}; "
        "$processes = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 }; "
        "foreach ($process in $processes) { "
        "$title = $process.MainWindowTitle; "
        "$matchesTitle = $false; "
        "foreach ($keyword in $keywords) { "
        "if ($title -like ('*' + $keyword + '*')) { $matchesTitle = $true; break } "
        "} "
        "if (-not $matchesTitle) { continue }; "
        "$window = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$process.MainWindowHandle); "
        "if (-not $window) { continue }; "
        "$all = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, "
        "[System.Windows.Automation.Condition]::TrueCondition); "
        "$filled = $false; "
        "foreach ($item in $all) { "
        "$controlType = $item.Current.ControlType.ProgrammaticName; "
        "$name = $item.Current.Name; "
        "if ($controlType -notlike '*Edit') { continue }; "
        "$valuePattern = $null; "
        "if ($item.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$valuePattern)) { "
        "if (-not $name) { $valuePattern.SetValue($passwordValue); $filled = $true; break } "
        "foreach ($label in $inputLabels) { "
        "if ($name -eq $label -or $name -like ('*' + $label + '*')) { "
        "$valuePattern.SetValue($passwordValue); $filled = $true; break "
        "} "
        "} "
        "if ($filled) { break } "
        "} "
        "} "
        "if (-not $filled) { continue }; "
        "Start-Sleep -Milliseconds 300; "
        "foreach ($item in $all) { "
        "$name = $item.Current.Name; "
        "if (-not $name) { continue }; "
        "foreach ($label in $joinLabels) { "
        "if ($name -eq $label -or $name -like ('*' + $label + '*')) { "
        "$pattern = $null; "
        "if ($item.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { "
        "$pattern.Invoke(); exit 0 "
        "} "
        "$rect = $item.Current.BoundingRectangle; "
        "if (-not $rect.IsEmpty) { "
        "Add-Type -Namespace Win32 -Name MouseNativeZoom -MemberDefinition "
        "'[DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int X, int Y); "
        "[DllImport(\"user32.dll\")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);'; "
        "$x = [int](($rect.Left + $rect.Right) / 2); "
        "$y = [int](($rect.Top + $rect.Bottom) / 2); "
        "[Win32.MouseNativeZoom]::SetCursorPos($x, $y) | Out-Null; "
        "Start-Sleep -Milliseconds 100; "
        "[Win32.MouseNativeZoom]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero); "
        "Start-Sleep -Milliseconds 80; "
        "[Win32.MouseNativeZoom]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero); "
        "exit 0 "
        "} "
        "} "
        "} "
        "} "
        "}; exit 1"
    )
    completed = subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )
    return completed.returncode == 0


def _click_zoom_browser_join_link() -> bool:
    if sys.platform != "win32":
        return False

    browser_names = ("msedge", "chrome", "firefox", "brave", "opera", "iexplore")
    title_keywords = ("Zoom", "Join Meeting", "\u52a0\u5165\u4f1a\u8bae")
    link_texts = (
        "Join from Your Browser",
        "Join from your browser",
        "\u901a\u8fc7\u6d4f\u89c8\u5668\u52a0\u5165",
    )
    command = (
        "Add-Type -AssemblyName UIAutomationClient; "
        "Add-Type -AssemblyName UIAutomationTypes; "
        f"$names = @({', '.join(_powershell_string(name) for name in browser_names)}); "
        f"$keywords = @({', '.join(_powershell_string(text) for text in title_keywords)}); "
        f"$targets = @({', '.join(_powershell_string(text) for text in link_texts)}); "
        "$processes = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 }; "
        "$windows = @(); "
        "foreach ($process in $processes) { "
        "$name = $process.ProcessName.ToLowerInvariant(); "
        "$title = $process.MainWindowTitle; "
        "$matchesName = $names -contains $name; "
        "$matchesTitle = $false; "
        "foreach ($keyword in $keywords) { "
        "if ($title -like ('*' + $keyword + '*')) { $matchesTitle = $true; break } "
        "} "
        "if ($matchesName -or $matchesTitle) { $windows += $process } "
        "}; "
        "foreach ($process in $windows) { "
        "$window = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$process.MainWindowHandle); "
        "if (-not $window) { continue }; "
        "$all = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, "
        "[System.Windows.Automation.Condition]::TrueCondition); "
        "foreach ($target in $targets) { "
        "foreach ($item in $all) { "
        "$name = $item.Current.Name; "
        "if (-not $name) { continue }; "
        "if ($name -eq $target -or $name -like ('*' + $target + '*')) { "
        "$pattern = $null; "
        "if ($item.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { "
        "$pattern.Invoke(); exit 0 "
        "} "
        "$rect = $item.Current.BoundingRectangle; "
        "if (-not $rect.IsEmpty) { "
        "$x = [int](($rect.Left + $rect.Right) / 2); "
        "$y = [int](($rect.Top + $rect.Bottom) / 2); "
        "$shell = New-Object -ComObject WScript.Shell; "
        "$shell.AppActivate($process.Id) | Out-Null; "
        "Add-Type -Namespace Win32 -Name Mouse -MemberDefinition "
        "'[DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int X, int Y); "
        "[DllImport(\"user32.dll\")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);'; "
        "[Win32.Mouse]::SetCursorPos($x, $y) | Out-Null; "
        "Start-Sleep -Milliseconds 100; "
        "[Win32.Mouse]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero); "
        "Start-Sleep -Milliseconds 80; "
        "[Win32.Mouse]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero); "
        "exit 0 "
        "} "
        "} "
        "} "
        "} "
        "}; exit 1"
    )
    completed = subprocess.run(
        _powershell_command_args(command),
        check=False,
        capture_output=True,
        text=True,
        **_windows_subprocess_kwargs(),
    )
    return completed.returncode == 0


def _send_hotkey(hotkey: str) -> None:
    if sys.platform != "win32":
        raise ActionError("hotkey commands are only supported on Windows.")

    parts = [part.strip().lower() for part in hotkey.split("+") if part.strip()]
    if not parts:
        raise ActionError("Hotkey command is empty.")

    vk_codes = [_hotkey_part_to_vk(part) for part in parts]
    _send_virtual_keys(vk_codes)


def _send_virtual_keys(vk_codes: list[int]) -> None:
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("ki_union", INPUT_UNION),
        ]

    def build_input(vk: int, key_up: bool) -> INPUT:
        flags = KEYEVENTF_KEYUP if key_up else 0
        return INPUT(
            type=INPUT_KEYBOARD,
            ki_union=INPUT_UNION(
                ki=KEYBDINPUT(
                    wVk=vk,
                    wScan=0,
                    dwFlags=flags,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )

    inputs = [build_input(vk, key_up=False) for vk in vk_codes]
    inputs.extend(build_input(vk, key_up=True) for vk in reversed(vk_codes))
    input_array = (INPUT * len(inputs))(*inputs)
    result = windll.user32.SendInput(len(inputs), input_array, ctypes.sizeof(INPUT))
    if result != len(inputs):
        LOGGER.warning("SendInput failed; falling back to keybd_event.")
        _send_virtual_keys_legacy(vk_codes)


def _send_virtual_keys_legacy(vk_codes: list[int]) -> None:
    for vk in vk_codes:
        windll.user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
    time.sleep(0.12)
    for vk in reversed(vk_codes):
        windll.user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.03)


def _hotkey_part_to_vk(part: str) -> int:
    aliases = {
        "alt": 0x12,
        "ctrl": 0x11,
        "control": 0x11,
        "shift": 0x10,
    }
    if part in aliases:
        return aliases[part]
    if len(part) == 1 and part.isdigit():
        return ord(part)
    if len(part) == 1 and "a" <= part <= "z":
        return ord(part.upper())
    raise ActionError(f"Unsupported hotkey part: {part}")


def _shell_execute_runas(path: Path) -> None:
    result = windll.shell32.ShellExecuteW(None, "runas", str(path), None, str(path.parent), 1)
    if result <= 32:
        raise ActionError(
            f"Failed to request administrator approval for {path}; ShellExecuteW={result}"
        )
