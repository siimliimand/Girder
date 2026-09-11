"""``girder`` console entry point.

Sprint 1 surface: migrate / recover / gc / daemon. The daemon opens the state
store, runs migrations, reconciles after any crash, and keeps the GC loop
alive; the run engine pump (Sprint 3) and web console (Sprint 2+) attach to
the same loop.
"""

# This was a single 946-line module until the CLI split; the command groups now
# live in girder.cli.ops / engine / prune / validate with shared helpers in
# girder.cli._common (see git history for the original). The docstring above is
# byte-identical to the original module docstring: argparse embeds it in the
# --help output.

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from girder import __version__
from girder.api.app import create_app
from girder.cli._common import _build_notifier, _open_db, log
from girder.cli.engine import (
    LocalSandboxRefused,
    _build_gateway,
    _build_github,
    _build_sandbox,
    _drain,
    _due_run_ids,
    _local_sandbox_allowed,
    _pump_one,
    _pumpable_runs,
    _start_telegram_receiver,
    cmd_daemon,
    cmd_pump,
    cmd_review,
    pump_runs_concurrently,
    review_run,
)
from girder.cli.ops import cmd_gc, cmd_migrate, cmd_recover, cmd_web
from girder.cli.prune import PruneReport, _count_prune_rows, _prune_run_ids, cmd_prune, prune_runs
from girder.cli.validate import (
    ValidationContext,
    _check_api_keys,
    _check_model_roles,
    _check_runtime_and_image,
    _check_secrets,
    _check_test_directories,
    _detect_runtime,
    _gather_validation_context,
    cmd_validate,
    collect_validation_failures,
)
from girder.config import (
    DEFAULT_SECRETS_PATH,
    Secrets,
    SecretsPermissionError,
    Settings,
    load_secrets,
    load_settings,
)
from girder.db.engine import default_migrations_dir
from girder.logging_config import configure_logging

# Explicit re-export surface: everything the former single-file module had at
# top level, so ``girder.cli.<name>`` keeps resolving (including the
# monkeypatch targets load_settings / load_secrets / create_app /
# DEFAULT_SECRETS_PATH, and the shutil / subprocess module attributes).
__all__ = [
    "DEFAULT_SECRETS_PATH",
    "LocalSandboxRefused",
    "PruneReport",
    "Secrets",
    "SecretsPermissionError",
    "Settings",
    "ValidationContext",
    "__version__",
    "_build_gateway",
    "_build_github",
    "_build_notifier",
    "_build_sandbox",
    "_check_api_keys",
    "_check_model_roles",
    "_check_runtime_and_image",
    "_check_secrets",
    "_check_test_directories",
    "_count_prune_rows",
    "_detect_runtime",
    "_drain",
    "_due_run_ids",
    "_gather_validation_context",
    "_local_sandbox_allowed",
    "_open_db",
    "_prune_run_ids",
    "_pump_one",
    "_pumpable_runs",
    "_start_telegram_receiver",
    "argparse",
    "asyncio",
    "cmd_daemon",
    "cmd_gc",
    "cmd_migrate",
    "cmd_prune",
    "cmd_pump",
    "cmd_recover",
    "cmd_review",
    "cmd_validate",
    "cmd_web",
    "collect_validation_failures",
    "create_app",
    "load_secrets",
    "load_settings",
    "log",
    "main",
    "prune_runs",
    "pump_runs_concurrently",
    "review_run",
    "shutil",
    "subprocess",
    "sys",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="girder", description=__doc__)
    parser.add_argument("--version", action="version", version=f"girder {__version__}")
    parser.add_argument("--db", default=str(Path.home() / ".local/share/girder/girder.db"))
    parser.add_argument(
        "--migrations-dir",
        default=None,
        help="override migrations directory (default: repo migrations/)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending migrations and exit")
    sub.add_parser("recover", help="run boot reconciliation and print the report")
    gc_parser = sub.add_parser("gc", help="run the worktree garbage collector")
    gc_parser.add_argument("--once", action="store_true", help="single pass instead of a loop")
    daemon_parser = sub.add_parser(
        "daemon", help="run the orchestrator daemon (recovery + GC + run-engine pump)"
    )
    daemon_parser.add_argument(
        "--sandbox",
        default="podman",
        choices=["podman", "local"],
        help="sandbox engine (local executes on the host with no isolation;"
        " requires --dev and is NOT a security boundary)",
    )
    daemon_parser.add_argument(
        "--dev",
        action="store_true",
        help="confirm a development environment (required for --sandbox local,"
        " alternatively set GIRDER_DEV=1)",
    )
    daemon_parser.add_argument(
        "--log-format",
        dest="log_format",
        default="text",
        choices=["text", "json"],
        help="log output format; json for daemon/service mode (default: text)",
    )
    pump_parser = sub.add_parser(
        "pump", help="drive one run a single pump step (or to completion with --wait)"
    )
    pump_parser.add_argument("run_id", help="run id to pump")
    pump_parser.add_argument(
        "--wait", action="store_true", help="pump to completion instead of one step"
    )
    pump_parser.add_argument(
        "--sandbox",
        default="podman",
        choices=["podman", "local"],
        help="sandbox engine (local executes on the host with no isolation;"
        " requires --dev and is NOT a security boundary)",
    )
    pump_parser.add_argument(
        "--dev",
        action="store_true",
        help="confirm a development environment (required for --sandbox local,"
        " alternatively set GIRDER_DEV=1)",
    )
    web_parser = sub.add_parser("web", help="serve the approval web console (impl-plan §10)")
    web_parser.add_argument(
        "--host", default=None, help="bind address (default: settings.web.host)"
    )
    web_parser.add_argument(
        "--port", type=int, default=None, help="bind port (default: settings.web.port)"
    )
    web_parser.add_argument(
        "--unix-socket",
        dest="unix_socket",
        default=None,
        help="bind a Unix domain socket instead of host/port (default: settings.web.unix_socket)",
    )
    web_parser.add_argument(
        "--api-key",
        default=None,
        help="require this API key on every console request (default:"
        " settings.web.api_key / GIRDER_WEB__API_KEY; empty = no auth)",
    )
    review_parser = sub.add_parser(
        "review", help="mark a merged run as human-reviewed (T1 review window, §2.3)"
    )
    review_parser.add_argument("run_id", help="merged run id to mark reviewed")
    prune_parser = sub.add_parser(
        "prune", help="delete terminal-status runs older than the horizon (WP 13.2)"
    )
    prune_parser.add_argument(
        "--older-than-days",
        dest="older_than_days",
        type=int,
        default=90,
        help="delete runs created more than this many days ago (default: 90)",
    )
    prune_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="report what would be deleted without touching the database",
    )
    validate_parser = sub.add_parser(
        "validate", help="check girder.toml, secrets and sandbox setup (WP 13.3)"
    )
    validate_parser.add_argument(
        "--config-path",
        dest="config_path",
        default=None,
        help="explicit girder.toml path (default: walk up from CWD)",
    )

    args = parser.parse_args(argv)
    configure_logging(
        level=logging.DEBUG if args.verbose else logging.INFO,
        fmt=getattr(args, "log_format", "text"),
    )
    # fail fast with a readable message when migrations cannot be located
    try:
        default_migrations_dir()
    except Exception as exc:
        log.error("%s", exc)
        return 2

    handlers = {
        "migrate": cmd_migrate,
        "recover": cmd_recover,
        "gc": cmd_gc,
        "daemon": cmd_daemon,
        "pump": cmd_pump,
        "web": cmd_web,
        "review": cmd_review,
        "prune": cmd_prune,
        "validate": cmd_validate,
    }
    try:
        return asyncio.run(handlers[args.command](args))
    except LocalSandboxRefused as exc:
        log.error("%s", exc)
        return 2
    except SecretsPermissionError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive convenience
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
