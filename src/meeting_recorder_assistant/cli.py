from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .actions import ActionError
from .config import ConfigError, load_config
from .runtime import default_config_path, ensure_log_dir
from .scheduler import enabled_tasks, next_pending_task, run_task, watch


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows meeting recorder automation MVP.")
    parser.add_argument("--config", default=None, help="Path to config JSON.")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without launching apps.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate the config file.")
    subparsers.add_parser("list", help="List enabled tasks.")
    subparsers.add_parser("run-next", help="Run the next pending task.")

    run_parser = subparsers.add_parser("run", help="Run one task by id.")
    run_parser.add_argument("task_id")

    watch_parser = subparsers.add_parser("watch", help="Watch and run all pending tasks.")
    watch_parser.add_argument("--poll-seconds", type=int, default=15)

    args = parser.parse_args()
    _setup_logging(args.verbose)
    config_path = Path(args.config) if args.config else default_config_path()

    try:
        config = load_config(config_path)
        return _dispatch(args, config)
    except (ConfigError, ActionError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 2


def _dispatch(args: argparse.Namespace, config) -> int:
    if args.command == "validate":
        print("Config OK")
        return 0

    if args.command == "list":
        for task in enabled_tasks(config):
            start = task.recorder_start_time(config.defaults).strftime("%Y-%m-%d %H:%M")
            finish = task.finish_time(config.defaults).strftime("%Y-%m-%d %H:%M")
            print(f"{task.id}: {task.title} [{start} -> {finish}]")
        return 0

    if args.command == "run-next":
        task = next_pending_task(config)
        if task is None:
            print("No pending task.")
            return 0
        run_task(config, task, dry_run=args.dry_run)
        return 0

    if args.command == "run":
        task = next((item for item in config.tasks if item.id == args.task_id), None)
        if task is None:
            print(f"Task not found: {args.task_id}")
            return 1
        run_task(config, task, dry_run=args.dry_run)
        return 0

    if args.command == "watch":
        watch(config, poll_seconds=args.poll_seconds, dry_run=args.dry_run)
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")


def _setup_logging(verbose: bool) -> None:
    log_dir = ensure_log_dir()
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "meeting-recorder.log", encoding="utf-8"),
        ],
    )


if __name__ == "__main__":
    raise SystemExit(main())
