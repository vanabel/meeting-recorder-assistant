from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from threading import Event

from . import actions
from .models import AppConfig, MeetingTask

LOGGER = logging.getLogger(__name__)


def enabled_tasks(config: AppConfig) -> list[MeetingTask]:
    return sorted(
        [task for task in config.tasks if task.enabled],
        key=lambda task: task.recorder_start_time(config.defaults),
    )


def next_pending_task(config: AppConfig, now: datetime | None = None) -> MeetingTask | None:
    current = now or datetime.now()
    for task in enabled_tasks(config):
        if task.finish_time(config.defaults) >= current:
            return task
    return None


def run_task(
    config: AppConfig,
    task: MeetingTask,
    dry_run: bool = False,
    stop_event: Event | None = None,
) -> None:
    LOGGER.info("Task %s started: %s", task.id, task.title)
    recording_started = False
    recorder_was_ready = False
    _wait_until(task.recorder_start_time(config.defaults), dry_run, stop_event)
    if _should_stop(stop_event):
        LOGGER.info("Task %s cancelled before joining.", task.id)
        return
    recorder_was_ready = actions.ensure_recorder_running(config, dry_run=dry_run)
    actions.prepare_recorder(config, dry_run=dry_run, recorder_was_ready=recorder_was_ready)
    actions.start_recording(config, dry_run=dry_run)
    recording_started = True
    actions.join_meeting(config, task, dry_run=dry_run)
    _wait_until(task.finish_time(config.defaults), dry_run, stop_event)
    if _should_stop(stop_event):
        LOGGER.info("Task %s stop requested.", task.id)
        if recording_started:
            _run_stop_actions(config, task, dry_run)
        return
    _run_stop_actions(config, task, dry_run)
    LOGGER.info("Task %s finished: %s", task.id, task.title)


def watch(
    config: AppConfig,
    poll_seconds: int = 15,
    dry_run: bool = False,
    stop_event: Event | None = None,
) -> None:
    completed: set[str] = set()
    LOGGER.info("Watcher started with %s enabled task(s).", len(enabled_tasks(config)))
    while True:
        if _should_stop(stop_event):
            LOGGER.info("Watcher stopped by request.")
            return

        now = datetime.now()
        pending = [
            task
            for task in enabled_tasks(config)
            if task.id not in completed and task.finish_time(config.defaults) >= now
        ]
        for task in list(pending):
            if _is_stale_for_watcher(config, task, now):
                LOGGER.warning(
                    "Skipping stale task %s; recorder start time was %s.",
                    task.id,
                    task.recorder_start_time(config.defaults).strftime("%Y-%m-%d %H:%M"),
                )
                completed.add(task.id)
                pending.remove(task)
        if not pending:
            LOGGER.info("No pending tasks remain; watcher exiting.")
            return

        task = pending[0]
        if task.recorder_start_time(config.defaults) <= now:
            try:
                run_task(config, task, dry_run=dry_run, stop_event=stop_event)
            finally:
                completed.add(task.id)
            continue

        wait_seconds = min(
            poll_seconds,
            max(1, int((task.recorder_start_time(config.defaults) - now).total_seconds())),
        )
        LOGGER.debug("Next task %s is not due yet; sleeping %s second(s).", task.id, wait_seconds)
        _sleep(wait_seconds, stop_event)


def _wait_until(target: datetime, dry_run: bool, stop_event: Event | None = None) -> None:
    now = datetime.now()
    if target <= now:
        LOGGER.info("Scheduled time already reached: %s", target.strftime("%Y-%m-%d %H:%M"))
        return

    LOGGER.info("Waiting until %s", target.strftime("%Y-%m-%d %H:%M"))
    if dry_run:
        return

    while datetime.now() < target and not _should_stop(stop_event):
        remaining = (target - datetime.now()).total_seconds()
        _sleep(min(30, max(1, remaining)), stop_event)


def _sleep(seconds: float, stop_event: Event | None) -> None:
    if stop_event is None:
        time.sleep(seconds)
        return
    stop_event.wait(seconds)


def _should_stop(stop_event: Event | None) -> bool:
    return stop_event is not None and stop_event.is_set()


def _run_stop_actions(config: AppConfig, task: MeetingTask, dry_run: bool) -> None:
    actions.stop_recorder(config, dry_run=dry_run)
    actions.leave_meeting_client(config, task, dry_run=dry_run)
    actions.close_meeting_client(config, task, dry_run=dry_run)


def _is_stale_for_watcher(config: AppConfig, task: MeetingTask, now: datetime) -> bool:
    grace = timedelta(minutes=config.defaults.max_late_start_minutes)
    return task.recorder_start_time(config.defaults) + grace < now
