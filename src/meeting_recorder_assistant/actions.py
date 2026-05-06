from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import webbrowser
from ctypes import byref, wintypes, windll
from pathlib import Path

from .models import AppConfig, MeetingTask

LOGGER = logging.getLogger(__name__)


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


def ensure_recorder_running(config: AppConfig, dry_run: bool) -> None:
    recorder_path = Path(config.recorder.path)
    if _recorder_is_ready(config):
        LOGGER.info("Recorder is already running with an active window.")
        return

    if not config.recorder.launch_if_not_running:
        raise ActionError("Recorder is not ready and launch_if_not_running is false.")

    if _recorder_is_running(config):
        LOGGER.info("Recorder process is running without a main window; launching app to show it.")

    LOGGER.info("Starting recorder application: %s", recorder_path)
    if dry_run:
        return
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


def start_recording(config: AppConfig, dry_run: bool) -> None:
    if config.recorder.start_command:
        LOGGER.info("Running recorder start command.")
        if not _activate_recorder_window(config, dry_run):
            LOGGER.warning("Recorder window could not be activated; sending start command anyway.")
        _run_recorder_command(config, config.recorder.start_command, dry_run)
    else:
        LOGGER.info("No recorder start command configured; assuming recorder starts itself.")


def prepare_recorder(config: AppConfig, dry_run: bool) -> None:
    command = config.recorder.prepare_command
    if not command:
        LOGGER.info("No recorder prepare command configured.")
        return
    LOGGER.info("Running recorder prepare command.")
    if not _activate_recorder_window(config, dry_run):
        raise ActionError("Cannot prepare recorder because recorder window could not be activated.")
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
    LOGGER.info("Opening meeting for task %s: %s", task.id, url)
    if dry_run:
        return

    if sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        webbrowser.open(url)

    if config.tencent_meeting.open_delay_seconds:
        LOGGER.info(
            "Waiting %s second(s) after opening Tencent Meeting.",
            config.tencent_meeting.open_delay_seconds,
        )
        time.sleep(config.tencent_meeting.open_delay_seconds)
    if config.tencent_meeting.focus_after_join:
        focus_tencent_meeting(config, dry_run=dry_run)


def focus_tencent_meeting(config: AppConfig, dry_run: bool) -> None:
    if dry_run or sys.platform != "win32":
        return

    hwnd = _tencent_meeting_window_handle(config)
    if hwnd and _force_foreground_window(hwnd):
        foreground_name = _foreground_process_name()
        if foreground_name:
            LOGGER.info("Foreground window process after meeting focus: %s", foreground_name)
        LOGGER.info("Tencent Meeting window is foreground.")
        return
    LOGGER.warning("Could not make Tencent Meeting window foreground.")


def stop_recorder(config: AppConfig, dry_run: bool) -> None:
    command = config.recorder.stop_command
    if not command:
        LOGGER.info("No recorder stop command configured; assuming recorder stops itself.")
        return
    LOGGER.info("Running recorder stop command.")
    if not _activate_recorder_window(config, dry_run):
        LOGGER.warning("Recorder window could not be activated; sending stop command anyway.")
    _run_recorder_command(config, command, dry_run)


def close_tencent_meeting(config: AppConfig, dry_run: bool) -> None:
    command = config.tencent_meeting.close_command
    if not command:
        LOGGER.info("No Tencent Meeting close command configured.")
        return
    LOGGER.info("Running Tencent Meeting close command.")
    _run_shell_command(command, dry_run)


def leave_tencent_meeting(config: AppConfig, dry_run: bool) -> None:
    command = config.tencent_meeting.leave_command
    if not command:
        LOGGER.info("No Tencent Meeting leave command configured.")
        return
    LOGGER.info("Running Tencent Meeting leave command.")
    _run_shell_command(command, dry_run)


def _run_shell_command(command: str, dry_run: bool) -> None:
    LOGGER.debug("Command: %s", command)
    if dry_run:
        return

    completed = subprocess.run(command, check=False, shell=True)
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


def _launch_executable(path: Path) -> None:
    try:
        subprocess.Popen([str(path)])
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
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
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
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
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
        shell32.ShellExecuteW(None, None, "cmd.exe", "/c exit", None, 0)
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
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
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
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
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


def _tencent_meeting_window_handle(config: AppConfig) -> int | None:
    process_names = config.tencent_meeting.process_names or (
        "wemeetapp.exe",
        "TencentMeeting.exe",
        "TencentMeeting",
    )
    hwnd = _window_handle_by_process_names(process_names)
    if hwnd is not None:
        return hwnd

    title_keywords = config.tencent_meeting.window_title_keywords or (
        "\u817e\u8baf\u4f1a\u8bae",
        "Tencent Meeting",
        "VooV Meeting",
    )
    return _window_handle_by_title_keywords(title_keywords)


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
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    try:
        return int(completed.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        return None


def _powershell_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _send_hotkey(hotkey: str) -> None:
    if sys.platform != "win32":
        raise ActionError("hotkey commands are only supported on Windows.")

    parts = [part.strip().lower() for part in hotkey.split("+") if part.strip()]
    if not parts:
        raise ActionError("Hotkey command is empty.")

    vk_codes = [_hotkey_part_to_vk(part) for part in parts]
    for vk in vk_codes:
        windll.user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.08)
    for vk in reversed(vk_codes):
        windll.user32.keybd_event(vk, 0, 2, 0)


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
