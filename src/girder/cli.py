"""``girder`` console entry point.

Sprint 1 surface: migrate / recover / gc / daemon. The daemon opens the state
store, runs migrations, reconciles after any crash, and keeps the GC loop
alive; the run engine pump (Sprint 3) and web console (Sprint 2+) attach to
the same loop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from girder import __version__
from girder.config import Settings, load_secrets, load_settings
from girder.db.engine import Database, default_migrations_dir
from girder.gitops.worktree import DEFAULT_BASE
from girder.guard.redact import Redactor
from girder.notify.notifier import Notifier
from girder.orchestrator.gc import WorktreeGC
from girder.orchestrator.recovery import RecoveryService

log = logging.getLogger("girder")


def _build_notifier(settings: Settings, db: Database | None) -> Notifier:
    secrets = load_secrets()
    return Notifier(
        settings.notify,
        secrets,
        Redactor(secrets.redaction_secret_env_names),
        db=db,
    )


async def _open_db(args: argparse.Namespace) -> Database:
    return await Database.open(
        args.db, migrations_dir=Path(args.migrations_dir) if args.migrations_dir else None
    )


async def cmd_migrate(args: argparse.Namespace) -> int:
    db = await _open_db(args)
    await db.close()
    log.info("database %s is up to date", args.db)
    return 0


async def cmd_recover(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = await _open_db(args)
    try:
        service = RecoveryService(db, settings, notifier=_build_notifier(settings, db))
        report = await service.recover()
        print(report.summary())
        return 0
    finally:
        await db.close()


async def cmd_gc(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = await _open_db(args)
    try:
        gc = WorktreeGC(db, DEFAULT_BASE, notifier=_build_notifier(settings, db))
        if args.once:
            result = await gc.run_once()
            print(
                f"pruned={len(result.pruned)} quarantined={len(result.quarantined)} "
                f"disk_alert={'yes' if result.disk_alert else 'no'}"
            )
            return 0
        await gc.run_forever()
        return 0
    finally:
        await db.close()


async def cmd_daemon(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = await _open_db(args)
    stop = asyncio.Event()

    def _signal(sig: signal.Signals) -> None:
        log.info("received %s — shutting down", sig.name)
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _signal, s)

    log.info("girder daemon starting (db=%s)", args.db)
    try:
        await RecoveryService(db, settings, notifier=_build_notifier(settings, db)).recover()
        gc_task = asyncio.create_task(
            WorktreeGC(db, DEFAULT_BASE, notifier=_build_notifier(settings, db)).run_forever()
        )
        # Sprint 3 mounts the run-engine pump here; for now: idle until signal.
        await stop.wait()
        gc_task.cancel()
        try:
            await gc_task
        except asyncio.CancelledError:
            pass
    finally:
        await db.close()
    log.info("daemon stopped cleanly")
    return 0


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
    sub.add_parser("daemon", help="run the orchestrator daemon (recovery + GC loop)")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
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
    }
    try:
        return asyncio.run(handlers[args.command](args))
    except KeyboardInterrupt:  # pragma: no cover - interactive convenience
        return 130


if __name__ == "__main__":
    sys.exit(main())
