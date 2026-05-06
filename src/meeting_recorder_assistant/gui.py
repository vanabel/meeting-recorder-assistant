from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from .actions import ElevationRequiredError, is_running_as_admin
from .config import ConfigError, load_config
from .models import (
    meeting_code_from_url,
    meeting_url_from_code,
    meeting_url_with_code,
    normalize_meeting_code,
)
from .runtime import app_root, default_config_path, ensure_log_dir, gui_restart_command, is_frozen_app
from .scheduler import enabled_tasks, next_pending_task, run_task, watch

LOGGER = logging.getLogger(__name__)
APP_ROOT = app_root()
QUICK_START_TIMES = (
    ("09:00", 9, 0),
    ("09:30", 9, 30),
    ("10:00", 10, 0),
    ("14:00", 14, 0),
    ("14:30", 14, 30),
    ("15:00", 15, 0),
    ("15:30", 15, 30),
    ("16:00", 16, 0),
)
QUICK_DURATIONS = (
    ("1h", 60),
    ("1.5h", 90),
)


class QueueLogHandler(logging.Handler):
    def __init__(self, target: queue.Queue[str]) -> None:
        super().__init__()
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        self.target.put(self.format(record))


class MeetingRecorderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Meeting Recorder Assistant")
        self.root.geometry("1180x760")

        self.config_path = tk.StringVar(value=str(default_config_path()))
        self.dry_run = tk.BooleanVar(value=False)
        self.launch_if_not_running = tk.BooleanVar(value=True)
        self.focus_after_join = tk.BooleanVar(value=True)
        self.enabled = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Ready")
        self.admin_status = tk.StringVar(value="")

        self.raw_config: dict[str, Any] = {}
        self.tasks: list[dict[str, Any]] = []
        self.worker: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self._meeting_code_syncing = False
        self.datetime_entries: dict[str, ttk.Entry] = {}
        self.datetime_picker: tk.Toplevel | None = None
        self.datetime_picker_target: tk.StringVar | None = None
        self.quick_slot_date = tk.StringVar(value=datetime.now().date().isoformat())
        self.quick_slot_time = tk.StringVar(value=QUICK_START_TIMES[0][0])
        self.quick_slot_duration = tk.StringVar(value=QUICK_DURATIONS[0][0])

        self._setup_logging()
        self._build_ui()
        self._bind_field_events()
        self.load_config_file()
        self.root.after(100, self._drain_logs)

    def _setup_logging(self) -> None:
        log_dir = ensure_log_dir()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                logging.FileHandler(log_dir / "meeting-recorder.log", encoding="utf-8"),
            ],
        )
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        logging.getLogger().addHandler(handler)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        path_row = ttk.Frame(main)
        path_row.pack(fill=tk.X)
        ttk.Label(path_row, text="Config").pack(side=tk.LEFT)
        ttk.Entry(path_row, textvariable=self.config_path).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(path_row, text="Load", command=self.load_config_file).pack(side=tk.LEFT)
        ttk.Button(path_row, text="Save", command=self.save_config_file).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(path_row, text="Validate", command=self.validate_config).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(path_row, text="Restart App", command=self.restart_app).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(path_row, text="Restart as Admin", command=self.restart_as_admin).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        paned = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, pady=10)

        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=3)
        paned.add(right, weight=2)

        self._build_task_list(left)
        self._build_editor(right)
        self._build_controls(main)
        self._build_log(main)

    def _build_task_list(self, parent: ttk.Frame) -> None:
        columns = ("id", "title", "code", "start", "end", "enabled")
        self.task_tree = ttk.Treeview(parent, columns=columns, show="headings", height=15)
        for key, title, width in (
            ("id", "ID", 190),
            ("title", "Title", 220),
            ("code", "Meeting Code", 120),
            ("start", "Start", 130),
            ("end", "End", 130),
            ("enabled", "Enabled", 70),
        ):
            self.task_tree.heading(key, text=title)
            self.task_tree.column(key, width=width, anchor=tk.W)
        self.task_tree.pack(fill=tk.BOTH, expand=True)
        self.task_tree.bind("<<TreeviewSelect>>", self._on_task_selected)

        buttons = ttk.Frame(parent)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text="New Task", command=self.new_task).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Apply Task", command=self.apply_task).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(buttons, text="Delete Task", command=self.delete_task).pack(side=tk.LEFT, padx=(6, 0))

    def _build_editor(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.pack(fill=tk.BOTH, expand=True)

        task_page = ttk.Frame(notebook, padding=8)
        recorder_page = ttk.Frame(notebook, padding=8)
        meeting_page = ttk.Frame(notebook, padding=8)
        notebook.add(task_page, text="Task")
        notebook.add(recorder_page, text="Recorder")
        notebook.add(meeting_page, text="Meeting")

        self.task_fields = {
            "id": tk.StringVar(),
            "title": tk.StringVar(),
            "meeting_code": tk.StringVar(),
            "meeting_url": tk.StringVar(),
            "start_time": tk.StringVar(),
            "end_time": tk.StringVar(),
        }
        self._entry(task_page, "ID", self.task_fields["id"], 0)
        self._entry(task_page, "Title", self.task_fields["title"], 1)
        self._entry(task_page, "Meeting Code", self.task_fields["meeting_code"], 2)
        self._entry(task_page, "Meeting URL", self.task_fields["meeting_url"], 3)
        ttk.Button(task_page, text="Sync URL From Code", command=self.sync_url_from_code).grid(
            row=4, column=1, sticky=tk.W, pady=4
        )
        self._datetime_entry(task_page, "Start Time", self.task_fields["start_time"], "start_time", 5)
        self._datetime_entry(task_page, "End Time", self.task_fields["end_time"], "end_time", 6)
        self._build_common_slots(task_page, 7)
        ttk.Checkbutton(task_page, text="Enabled", variable=self.enabled).grid(
            row=8, column=1, sticky=tk.W, pady=4
        )
        task_page.columnconfigure(1, weight=1)

        self.recorder_fields = {
            "path": tk.StringVar(),
            "start_delay_seconds": tk.StringVar(),
            "process_names": tk.StringVar(),
            "prepare_command": tk.StringVar(),
            "prepare_delay_seconds": tk.StringVar(),
            "start_command": tk.StringVar(),
            "stop_command": tk.StringVar(),
        }
        self._entry(recorder_page, "Path", self.recorder_fields["path"], 0)
        self._entry(recorder_page, "Start Delay Sec", self.recorder_fields["start_delay_seconds"], 1)
        self._entry(recorder_page, "Process Names", self.recorder_fields["process_names"], 2)
        ttk.Checkbutton(
            recorder_page,
            text="Launch if not running",
            variable=self.launch_if_not_running,
        ).grid(row=3, column=1, sticky=tk.W, pady=4)
        self._entry(recorder_page, "Prepare Command", self.recorder_fields["prepare_command"], 4)
        self._entry(recorder_page, "Prepare Delay Sec", self.recorder_fields["prepare_delay_seconds"], 5)
        self._entry(recorder_page, "Start Command", self.recorder_fields["start_command"], 6)
        self._entry(recorder_page, "Stop Command", self.recorder_fields["stop_command"], 7)
        recorder_page.columnconfigure(1, weight=1)

        self.meeting_fields = {
            "leave_command": tk.StringVar(),
            "close_command": tk.StringVar(),
            "process_names": tk.StringVar(),
            "window_title_keywords": tk.StringVar(),
            "open_delay_seconds": tk.StringVar(),
            "join_early_minutes": tk.StringVar(),
            "recording_tail_minutes": tk.StringVar(),
            "max_late_start_minutes": tk.StringVar(),
        }
        self._entry(meeting_page, "Leave Command", self.meeting_fields["leave_command"], 0)
        self._entry(meeting_page, "Close Command", self.meeting_fields["close_command"], 1)
        self._entry(meeting_page, "Process Names", self.meeting_fields["process_names"], 2)
        self._entry(meeting_page, "Title Keywords", self.meeting_fields["window_title_keywords"], 3)
        self._entry(meeting_page, "Open Delay Sec", self.meeting_fields["open_delay_seconds"], 4)
        ttk.Checkbutton(
            meeting_page,
            text="Focus after join",
            variable=self.focus_after_join,
        ).grid(row=5, column=1, sticky=tk.W, pady=4)
        self._entry(meeting_page, "Join Early Min", self.meeting_fields["join_early_minutes"], 6)
        self._entry(meeting_page, "Tail Min", self.meeting_fields["recording_tail_minutes"], 7)
        self._entry(meeting_page, "Max Late Start Min", self.meeting_fields["max_late_start_minutes"], 8)
        meeting_page.columnconfigure(1, weight=1)

    def _entry(self, parent: ttk.Frame, label: str, var: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky=tk.EW, padx=(8, 0), pady=4)

    def _datetime_entry(
        self,
        parent: ttk.Frame,
        label: str,
        var: tk.StringVar,
        field_name: str,
        row: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=4)
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky=tk.EW, padx=(8, 0), pady=4)
        entry.bind(
            "<Button-1>",
            lambda event, target=var, widget=entry: self._on_datetime_entry_click(event, target, widget),
        )
        self.datetime_entries[field_name] = entry

    def _build_common_slots(self, parent: ttk.Frame, row: int) -> None:
        ttk.Label(parent, text="Quick Slots").grid(row=row, column=0, sticky=tk.NW, pady=4)
        slots = ttk.Frame(parent)
        slots.grid(row=row, column=1, sticky=tk.EW, padx=(8, 0), pady=4)
        slots.columnconfigure(0, weight=1)

        ttk.Label(slots, text="Date").grid(row=0, column=0, sticky=tk.W)
        date_row = ttk.Frame(slots)
        date_row.grid(row=1, column=0, sticky=tk.EW, pady=(2, 6))
        for index, (label, offset_days) in enumerate((("Today", 0), ("Tomorrow", 1), ("+2d", 2), ("+3d", 3))):
            date_value = (datetime.now() + timedelta(days=offset_days)).date().isoformat()
            ttk.Radiobutton(
                date_row,
                text=label,
                value=date_value,
                variable=self.quick_slot_date,
            ).grid(row=0, column=index, sticky=tk.W, padx=(0, 6))

        ttk.Label(slots, text="Start").grid(row=2, column=0, sticky=tk.W)
        time_row = ttk.Frame(slots)
        time_row.grid(row=3, column=0, sticky=tk.EW, pady=(2, 6))
        for index, (label, _hour, _minute) in enumerate(QUICK_START_TIMES):
            ttk.Radiobutton(
                time_row,
                text=label,
                value=label,
                variable=self.quick_slot_time,
            ).grid(row=index // 4, column=index % 4, sticky=tk.W, padx=(0, 6), pady=2)

        ttk.Label(slots, text="Duration").grid(row=4, column=0, sticky=tk.W)
        duration_row = ttk.Frame(slots)
        duration_row.grid(row=5, column=0, sticky=tk.W, pady=(2, 0))
        for index, (label, _minutes) in enumerate(QUICK_DURATIONS):
            ttk.Radiobutton(
                duration_row,
                text=label,
                value=label,
                variable=self.quick_slot_duration,
            ).grid(row=0, column=index, sticky=tk.W, padx=(0, 8))

        ttk.Button(slots, text="Apply Quick Slot", command=self._apply_quick_slot).grid(
            row=6, column=0, sticky=tk.W, pady=(8, 0)
        )

    def _bind_field_events(self) -> None:
        self.task_fields["meeting_code"].trace_add("write", self._on_meeting_code_changed)

    def _build_controls(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent)
        controls.pack(fill=tk.X)
        ttk.Checkbutton(controls, text="Dry run", variable=self.dry_run).pack(side=tk.LEFT)
        ttk.Button(controls, text="Run Selected", command=self.run_selected).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(controls, text="Test 1 Min", command=self.test_selected_one_minute).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(controls, text="Run Next", command=self.run_next).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(controls, text="Start Watcher", command=self.start_watcher).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(controls, text="Stop Current", command=self.stop_current).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(controls, textvariable=self.admin_status).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(controls, textvariable=self.status).pack(side=tk.RIGHT)
        self._refresh_admin_status()

    def _build_log(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Log")
        frame.pack(fill=tk.BOTH, expand=False, pady=(10, 0))
        self.log_text = tk.Text(frame, height=9, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def load_config_file(self) -> None:
        path = Path(self.config_path.get())
        try:
            self.raw_config = json.loads(path.read_text(encoding="utf-8-sig"))
            load_config(path)
        except (OSError, json.JSONDecodeError, ConfigError) as exc:
            self._error("Load failed", exc)
            return

        self.tasks = [dict(item) for item in self.raw_config.get("tasks", [])]
        self._load_settings_into_fields()
        self._refresh_task_tree()
        self.status.set(f"Loaded {path.name}")

    def save_config_file(self) -> bool:
        try:
            self.apply_task(silent=True)
            data = self._collect_config()
            path = Path(self.config_path.get())
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            load_config(path)
        except (OSError, ValueError, ConfigError) as exc:
            self._error("Save failed", exc)
            return False

        self.raw_config = data
        self.status.set("Saved")
        LOGGER.info("Configuration saved: %s", self.config_path.get())
        return True

    def validate_config(self) -> None:
        if not self.save_config_file():
            return
        try:
            config = load_config(Path(self.config_path.get()))
        except ConfigError as exc:
            self._error("Validation failed", exc)
            return
        self.status.set(f"Config OK: {len(config.tasks)} task(s)")
        LOGGER.info("Config OK")

    def restart_app(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Stop the current task or watcher before restarting.")
            return
        if not self.save_config_file():
            return

        LOGGER.info("Restarting GUI app.")
        subprocess.Popen(gui_restart_command(), cwd=str(APP_ROOT))
        self.root.destroy()

    def restart_as_admin(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Stop the current task or watcher before restarting.")
            return
        if not self.save_config_file():
            return
        self._restart_as_admin()

    def _load_settings_into_fields(self) -> None:
        recorder = self.raw_config.get("recorder", {})
        defaults = self.raw_config.get("defaults", {})
        tencent = self.raw_config.get("tencent_meeting", {})

        self.recorder_fields["path"].set(str(recorder.get("path", "")))
        self.recorder_fields["start_delay_seconds"].set(str(recorder.get("start_delay_seconds", 8)))
        self.recorder_fields["process_names"].set(", ".join(recorder.get("process_names", [])))
        self.launch_if_not_running.set(bool(recorder.get("launch_if_not_running", True)))
        self.recorder_fields["prepare_command"].set(str(recorder.get("prepare_command") or ""))
        self.recorder_fields["prepare_delay_seconds"].set(str(recorder.get("prepare_delay_seconds", 10)))
        self.recorder_fields["start_command"].set(str(recorder.get("start_command") or ""))
        self.recorder_fields["stop_command"].set(str(recorder.get("stop_command") or ""))

        self.meeting_fields["leave_command"].set(str(tencent.get("leave_command") or ""))
        self.meeting_fields["close_command"].set(str(tencent.get("close_command") or ""))
        self.meeting_fields["process_names"].set(", ".join(tencent.get("process_names", [])))
        self.meeting_fields["window_title_keywords"].set(
            ", ".join(tencent.get("window_title_keywords", []))
        )
        self.meeting_fields["open_delay_seconds"].set(str(tencent.get("open_delay_seconds", 8)))
        self.focus_after_join.set(bool(tencent.get("focus_after_join", True)))
        self.meeting_fields["join_early_minutes"].set(str(defaults.get("join_early_minutes", 2)))
        self.meeting_fields["recording_tail_minutes"].set(str(defaults.get("recording_tail_minutes", 1)))
        self.meeting_fields["max_late_start_minutes"].set(str(defaults.get("max_late_start_minutes", 10)))

    def _collect_config(self) -> dict[str, Any]:
        process_names = [
            item.strip()
            for item in self.recorder_fields["process_names"].get().split(",")
            if item.strip()
        ]
        return {
            "recorder": {
                "path": self.recorder_fields["path"].get().strip(),
                "start_delay_seconds": self._int_field(
                    self.recorder_fields["start_delay_seconds"], "start_delay_seconds"
                ),
                "process_names": process_names,
                "launch_if_not_running": self.launch_if_not_running.get(),
                "prepare_command": self._none_if_blank(self.recorder_fields["prepare_command"].get()),
                "prepare_delay_seconds": self._int_field(
                    self.recorder_fields["prepare_delay_seconds"], "prepare_delay_seconds"
                ),
                "start_command": self._none_if_blank(self.recorder_fields["start_command"].get()),
                "stop_command": self._none_if_blank(self.recorder_fields["stop_command"].get()),
            },
            "tencent_meeting": {
                "leave_command": self._none_if_blank(self.meeting_fields["leave_command"].get()),
                "close_command": self._none_if_blank(self.meeting_fields["close_command"].get()),
                "process_names": self._comma_list(self.meeting_fields["process_names"].get()),
                "window_title_keywords": self._comma_list(
                    self.meeting_fields["window_title_keywords"].get()
                ),
                "open_delay_seconds": self._int_field(
                    self.meeting_fields["open_delay_seconds"], "open_delay_seconds"
                ),
                "focus_after_join": self.focus_after_join.get(),
            },
            "defaults": {
                "join_early_minutes": self._int_field(
                    self.meeting_fields["join_early_minutes"], "join_early_minutes"
                ),
                "recording_tail_minutes": self._int_field(
                    self.meeting_fields["recording_tail_minutes"], "recording_tail_minutes"
                ),
                "max_late_start_minutes": self._int_field(
                    self.meeting_fields["max_late_start_minutes"], "max_late_start_minutes"
                ),
            },
            "tasks": self.tasks,
        }

    def _refresh_task_tree(self, selected_key: tuple[str, str, str, str] | None = None) -> None:
        self._sort_tasks()
        self.task_tree.delete(*self.task_tree.get_children())
        for index, task in enumerate(self.tasks):
            self.task_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    task.get("id", ""),
                    task.get("title", ""),
                    task.get("meeting_code", ""),
                    task.get("start_time", ""),
                    task.get("end_time", ""),
                    str(bool(task.get("enabled", True))),
                ),
            )
        if self.tasks:
            selected_iid = "0"
            if selected_key is not None:
                for index, task in enumerate(self.tasks):
                    if self._task_key(task) == selected_key:
                        selected_iid = str(index)
                        break
            self.task_tree.selection_set(selected_iid)
            self._show_task(int(selected_iid))
        else:
            for var in self.task_fields.values():
                var.set("")
            self.enabled.set(False)

    def _on_task_selected(self, _event: tk.Event) -> None:
        index = self._selected_index()
        if index is not None:
            self._show_task(index)

    def _show_task(self, index: int) -> None:
        task = self.tasks[index]
        for key, var in self.task_fields.items():
            var.set(str(task.get(key) or ""))
        self.enabled.set(bool(task.get("enabled", True)))

    def new_task(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        task = {
            "id": f"meeting-{datetime.now().strftime('%Y%m%d-%H%M')}",
            "title": "New Meeting",
            "meeting_code": "",
            "meeting_url": "",
            "start_time": now,
            "end_time": now,
            "enabled": False,
        }
        self.tasks.append(task)
        self._refresh_task_tree(selected_key=self._task_key(task))

    def apply_task(self, silent: bool = False) -> None:
        index = self._selected_index()
        if index is None:
            if silent:
                return
            self.new_task()
            index = self._selected_index()
            if index is None:
                return

        meeting_code = self._none_if_blank(self.task_fields["meeting_code"].get())
        meeting_url = self._synced_meeting_url(meeting_code, self.task_fields["meeting_url"].get())
        self.task_fields["meeting_url"].set(meeting_url or "")

        task = {
            "id": self.task_fields["id"].get().strip(),
            "title": self.task_fields["title"].get().strip(),
            "meeting_code": meeting_code,
            "meeting_url": meeting_url,
            "start_time": self.task_fields["start_time"].get().strip(),
            "end_time": self.task_fields["end_time"].get().strip(),
            "enabled": self.enabled.get(),
        }
        self.tasks[index] = task
        self._refresh_task_tree(selected_key=self._task_key(task))
        if not silent:
            self.status.set("Task updated")

    def delete_task(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        del self.tasks[index]
        self._refresh_task_tree()
        self.status.set("Task deleted")

    def run_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("No task", "Select a task first.")
            return
        self._start_worker(
            "Run selected",
            lambda config, stop_event: run_task(
                config,
                config.tasks[index],
                self.dry_run.get(),
                stop_event=stop_event,
            ),
        )

    def test_selected_one_minute(self) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("No task", "Select a task first.")
            return

        def target(config: Any, stop_event: threading.Event) -> None:
            now = datetime.now()
            task = replace(
                config.tasks[index],
                start_time=now,
                end_time=now + timedelta(minutes=1),
                enabled=True,
                join_early_minutes=0,
                recording_tail_minutes=0,
            )
            LOGGER.info("Running one-minute test task for %s.", task.id)
            run_task(config, task, self.dry_run.get(), stop_event=stop_event)

        self._start_worker("Test 1 min", target)

    def run_next(self) -> None:
        def target(config: Any, stop_event: threading.Event) -> None:
            task = next_pending_task(config)
            if task is None:
                LOGGER.info("No pending task.")
                return
            run_task(config, task, self.dry_run.get(), stop_event=stop_event)

        self._start_worker("Run next", target)

    def start_watcher(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A task or watcher is already running.")
            return
        self._start_worker(
            "Watcher",
            lambda config, stop_event: watch(config, dry_run=self.dry_run.get(), stop_event=stop_event),
        )

    def stop_current(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
            self.status.set("Stop requested")
            LOGGER.info("Stop requested from GUI.")
        else:
            self.status.set("No active task")

    def _start_worker(
        self,
        label: str,
        target: Callable[[Any, threading.Event], None],
    ) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A task or watcher is already running.")
            return
        if not self.save_config_file():
            return
        self.stop_event = threading.Event()

        def run() -> None:
            try:
                config = load_config(Path(self.config_path.get()))
                self._set_status(f"{label} running")
                if self.stop_event is None:
                    return
                target(config, self.stop_event)
                self._set_status(f"{label} finished")
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, ElevationRequiredError):
                    LOGGER.warning("%s", exc)
                    self.log_queue.put("Restarting GUI as administrator. Approve the Windows UAC prompt.")
                    self._set_status("Restarting as admin")
                    self.root.after(0, self._restart_as_admin)
                    return
                LOGGER.exception("%s failed", label)
                self.log_queue.put(f"ERROR: {exc}")
                self._set_status(f"{label} failed")
            finally:
                self.root.after(0, self._clear_worker_state)

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _clear_worker_state(self) -> None:
        if self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self.stop_event = None
            self._refresh_admin_status()

    def _selected_index(self) -> int | None:
        selection = self.task_tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def _drain_logs(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.insert(tk.END, line + "\n")
            self.log_text.see(tk.END)
        self.root.after(100, self._drain_logs)

    def _error(self, title: str, exc: Exception) -> None:
        messagebox.showerror(title, str(exc))
        self.status.set(title)
        LOGGER.error("%s: %s", title, exc)

    def sync_url_from_code(self) -> None:
        meeting_code = self._none_if_blank(self.task_fields["meeting_code"].get())
        if not meeting_code:
            messagebox.showinfo("No meeting code", "Enter a meeting code first.")
            return
        self.task_fields["meeting_code"].set(normalize_meeting_code(meeting_code))
        self.task_fields["meeting_url"].set(
            self._synced_meeting_url(meeting_code, self.task_fields["meeting_url"].get()) or ""
        )
        self.status.set("Meeting URL synced")

    def _on_meeting_code_changed(self, *_args: str) -> None:
        if self._meeting_code_syncing:
            return

        raw_value = self.task_fields["meeting_code"].get()
        normalized_code = normalize_meeting_code(raw_value)
        updated_url = None
        if normalized_code.isdigit() and len(normalized_code) == 9:
            updated_url = self._synced_meeting_url(
                normalized_code,
                self.task_fields["meeting_url"].get(),
            )

        self._meeting_code_syncing = True
        try:
            if raw_value != normalized_code:
                self.task_fields["meeting_code"].set(normalized_code)
            if updated_url is not None:
                self.task_fields["meeting_url"].set(updated_url or "")
        finally:
            self._meeting_code_syncing = False

    def _on_datetime_entry_click(
        self,
        _event: tk.Event,
        target: tk.StringVar,
        widget: ttk.Entry,
    ) -> str:
        self.open_datetime_picker(target, widget)
        return "break"

    def open_datetime_picker(self, target: tk.StringVar, widget: ttk.Entry) -> None:
        current = self._parse_datetime_value(target.get()) or datetime.now().astimezone().replace(
            second=0,
            microsecond=0,
            tzinfo=None,
        )
        if self.datetime_picker is not None:
            self.datetime_picker.destroy()

        picker = tk.Toplevel(self.root)
        picker.title("Pick Date/Time")
        picker.transient(self.root)
        picker.grab_set()
        picker.resizable(False, False)
        self.datetime_picker = picker
        self.datetime_picker_target = target

        selected_date = tk.StringVar(value=current.strftime("%Y-%m-%d"))
        hour_var = tk.StringVar(value=current.strftime("%H"))
        minute_var = tk.StringVar(value=current.strftime("%M"))

        main = ttk.Frame(picker, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="Date").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(main, textvariable=selected_date).grid(row=0, column=1, sticky=tk.W, padx=(8, 0))

        shortcuts = ttk.Frame(main)
        shortcuts.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        for index in range(14):
            day = (datetime.now() + timedelta(days=index)).date()
            label = day.strftime("%m-%d")
            if index == 0:
                label = f"Today\n{label}"
            elif index == 1:
                label = f"Tomorrow\n{label}"
            ttk.Button(
                shortcuts,
                text=label,
                command=lambda value=day.isoformat(): selected_date.set(value),
                width=10,
            ).grid(row=index // 2, column=index % 2, sticky=tk.EW, padx=2, pady=2)

        ttk.Label(main, text="Hour").grid(row=2, column=0, sticky=tk.W, pady=(10, 0))
        ttk.Combobox(
            main,
            textvariable=hour_var,
            values=[f"{value:02d}" for value in range(24)],
            state="readonly",
            width=6,
        ).grid(row=2, column=1, sticky=tk.W, padx=(8, 0), pady=(10, 0))

        ttk.Label(main, text="Minute").grid(row=3, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Combobox(
            main,
            textvariable=minute_var,
            values=[f"{value:02d}" for value in range(60)],
            state="readonly",
            width=6,
        ).grid(row=3, column=1, sticky=tk.W, padx=(8, 0), pady=(8, 0))

        buttons = ttk.Frame(main)
        buttons.grid(row=4, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))

        def apply_value() -> None:
            try:
                chosen = datetime.strptime(
                    f"{selected_date.get()} {hour_var.get()}:{minute_var.get()}",
                    "%Y-%m-%d %H:%M",
                )
            except ValueError as exc:
                messagebox.showerror("Invalid date/time", str(exc), parent=picker)
                return
            target.set(chosen.strftime("%Y-%m-%d %H:%M"))
            self._close_datetime_picker()

        ttk.Button(buttons, text="Cancel", command=self._close_datetime_picker).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="OK", command=apply_value).pack(side=tk.RIGHT, padx=(0, 6))
        picker.bind("<Escape>", lambda _event: self._close_datetime_picker())
        picker.bind("<Destroy>", lambda _event: self._clear_datetime_picker_reference(picker))

        picker.update_idletasks()
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height() + 2
        picker.geometry(f"+{x}+{y}")

    def _close_datetime_picker(self) -> None:
        if self.datetime_picker is not None:
            self.datetime_picker.destroy()

    def _clear_datetime_picker_reference(self, picker: tk.Toplevel) -> None:
        if self.datetime_picker is picker:
            self.datetime_picker = None
            self.datetime_picker_target = None

    def _apply_quick_slot(self) -> None:
        selected_date = datetime.fromisoformat(self.quick_slot_date.get())
        time_label = self.quick_slot_time.get()
        duration_label = self.quick_slot_duration.get()
        start_hour, start_minute = next(
            (hour, minute) for label, hour, minute in QUICK_START_TIMES if label == time_label
        )
        duration_minutes = next(
            minutes for label, minutes in QUICK_DURATIONS if label == duration_label
        )
        start_time = selected_date.replace(
            hour=start_hour,
            minute=start_minute,
            second=0,
            microsecond=0,
        )
        end_time = start_time + timedelta(minutes=duration_minutes)
        self.task_fields["start_time"].set(start_time.strftime("%Y-%m-%d %H:%M"))
        self.task_fields["end_time"].set(end_time.strftime("%Y-%m-%d %H:%M"))
        self.status.set("Quick slot applied")

    def _set_status(self, value: str) -> None:
        self.root.after(0, self.status.set, value)

    def _refresh_admin_status(self) -> None:
        self.admin_status.set("Admin" if is_running_as_admin() else "Not admin")

    def _restart_as_admin(self) -> None:
        if sys.platform != "win32":
            messagebox.showinfo("Unsupported", "Administrator restart is only supported on Windows.")
            return
        if is_frozen_app():
            file_path = sys.executable
            params = None
        else:
            file_path = sys.executable
            params = f'"{APP_ROOT / "meeting_recorder_gui.py"}"'
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                (
                    "Start-Process "
                    f"-FilePath {self._powershell_string(file_path)} "
                    + (
                        f"-ArgumentList {self._powershell_string(params)} "
                        if params
                        else ""
                    )
                    + f"-WorkingDirectory {self._powershell_string(str(APP_ROOT))} "
                    "-Verb RunAs"
                ),
            ],
            check=False,
        )
        if result.returncode != 0:
            self.status.set("Admin restart failed")
            return
        self.root.destroy()

    def _int_field(self, var: tk.StringVar, name: str) -> int:
        try:
            value = int(var.get().strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer.") from exc
        if value < 0:
            raise ValueError(f"{name} must be non-negative.")
        return value

    def _none_if_blank(self, value: str) -> str | None:
        value = value.strip()
        return value or None

    def _parse_datetime_value(self, value: str) -> datetime | None:
        value = value.strip()
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is not None:
                return parsed.astimezone().replace(tzinfo=None)
            return parsed.replace(tzinfo=None)
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    def _comma_list(self, value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    def _powershell_string(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _task_key(self, task: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(task.get("id", "")),
            str(task.get("start_time", "")),
            str(task.get("end_time", "")),
            str(task.get("title", "")),
        )

    def _sort_tasks(self) -> None:
        self.tasks.sort(
            key=lambda task: (
                self._parse_datetime_value(str(task.get("start_time", ""))) or datetime.max,
                self._parse_datetime_value(str(task.get("end_time", ""))) or datetime.max,
                str(task.get("id", "")),
            )
        )

    def _synced_meeting_url(self, meeting_code: str | None, meeting_url: str) -> str | None:
        meeting_url = meeting_url.strip()
        if not meeting_code:
            return meeting_url or None

        normalized_code = normalize_meeting_code(meeting_code)
        self.task_fields["meeting_code"].set(normalized_code)
        if not meeting_url:
            return meeting_url_from_code(normalized_code)

        url_code = meeting_code_from_url(meeting_url)
        if url_code is not None:
            return meeting_url_with_code(meeting_url, normalized_code)
        return meeting_url


def main() -> int:
    root = tk.Tk()
    MeetingRecorderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
