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
from girder.api.app import create_app
from girder.budget.guard import BudgetGuard
from girder.config import Secrets, Settings, load_secrets, load_settings
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.db.models import Run
from girder.github.client import GitHubClient
from girder.gitops.worktree import DEFAULT_BASE
from girder.guard.redact import Redactor
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.orchestrator.gc import WorktreeGC
from girder.orchestrator.recovery import RecoveryService
from girder.orchestrator.run_engine import _PUMPABLE_RUN_STATUSES, RunEngine
from girder.sandbox.engine import SandboxEngine
from girder.sandbox.local import LocalExecSandbox
from girder.sandbox.podman import PodmanEngine

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


async def cmd_web(args: argparse.Namespace) -> int:
    """Serve the approval web console (impl-plan §10) until interrupted."""
    import uvicorn

    settings = load_settings()
    app = create_app(
        db_path=Path(args.db),
        settings=settings,
        migrations_dir=Path(args.migrations_dir) if args.migrations_dir else None,
    )
    host = args.host if args.host is not None else settings.web.host
    port = args.port if args.port is not None else settings.web.port
    log.info("girder web listening on http://%s:%s (db=%s)", host, port, args.db)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()
    return 0


def _build_sandbox(args: argparse.Namespace, settings: Settings) -> SandboxEngine:
    if args.sandbox == "local":
        return LocalExecSandbox()
    return PodmanEngine(settings.sandbox.runtime)


def _build_gateway(
    settings: Settings, secrets: Secrets, db: Database, redactor: Redactor, notifier: Notifier
) -> ModelGateway:
    return ModelGateway(settings, secrets, db, redactor, BudgetGuard(db), notifier)


def _build_github(
    settings: Settings, secrets: Secrets, db: Database, redactor: Redactor
) -> GitHubClient:
    """Delivery client for the invocation directory's project (impl-plan §6.11).

    The token travels only inside this process (and a temp GIT_ASKPASS helper
    at push time) — never into containers, git config, or argv.
    """
    return GitHubClient(settings, secrets, redactor, db, repo_path=Path.cwd())


async def _pumpable_runs(db: Database) -> list[Run]:
    """Runs the pump may act on, across every project (SQL lives in db.repo)."""
    runs: list[Run] = []
    for project in await repo.list_projects(db):
        for run in await repo.list_runs_for_project(db, project.id):
            if run.status in _PUMPABLE_RUN_STATUSES:
                runs.append(run)
    return runs


async def cmd_daemon(args: argparse.Namespace) -> int:
    settings = load_settings()
    secrets = load_secrets()
    redactor = Redactor(secrets.redaction_secret_env_names)
    notifier = _build_notifier(settings, None)
    db = await _open_db(args)
    stop = asyncio.Event()

    def _signal(sig: signal.Signals) -> None:
        log.info("received %s — shutting down", sig.name)
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _signal, s)

    log.info("girder daemon starting (db=%s, sandbox=%s)", args.db, args.sandbox)
    try:
        report = await RecoveryService(db, settings, notifier=notifier).recover()
        log.info("%s", report.summary())
        gc_task = asyncio.create_task(
            WorktreeGC(db, DEFAULT_BASE, notifier=notifier).run_forever()
        )
        engine = RunEngine(
            db=db,
            settings=settings,
            secrets=secrets,
            gateway=_build_gateway(settings, secrets, db, redactor, notifier),
            sandbox=_build_sandbox(args, settings),
            notifier=notifier,
            redactor=redactor,
            github=_build_github(settings, secrets, db, redactor),
        )
        poll_s = 5.0
        while not stop.is_set():
            for run in await _pumpable_runs(db):
                try:
                    descriptor = await engine.pump_once(run.id)
                    log.info("pump run %s -> %s", run.id, descriptor)
                except Exception:  # never let one run kill the loop
                    log.exception("pump failed for run %s", run.id)
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_s)
            except TimeoutError:
                pass
        gc_task.cancel()
        try:
            await gc_task
        except asyncio.CancelledError:
            pass
    finally:
        await db.close()
    log.info("daemon stopped cleanly")
    return 0


async def cmd_pump(args: argparse.Namespace) -> int:
    """One-shot pump: drive a single run one step (or to completion)."""
    settings = load_settings()
    secrets = load_secrets()
    redactor = Redactor(secrets.redaction_secret_env_names)
    notifier = _build_notifier(settings, None)
    db = await _open_db(args)
    try:
        report = await RecoveryService(db, settings, notifier=notifier).recover()
        log.info("%s", report.summary())
        engine = RunEngine(
            db=db,
            settings=settings,
            secrets=secrets,
            gateway=_build_gateway(settings, secrets, db, redactor, notifier),
            sandbox=_build_sandbox(args, settings),
            notifier=notifier,
            redactor=redactor,
            github=_build_github(settings, secrets, db, redactor),
        )
        try:
            if args.wait:
                descriptor = await engine.run_to_completion(args.run_id)
            else:
                descriptor = await engine.pump_once(args.run_id)
        except KeyError:
            log.error("run %s not found", args.run_id)
            return 1
        print(descriptor)
        return 0
    finally:
        await db.close()


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
        help="sandbox engine (local executes on the host — dev/tests only)",
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
        help="sandbox engine (local executes on the host — dev/tests only)",
    )
    web_parser = sub.add_parser("web", help="serve the approval web console (impl-plan §10)")
    web_parser.add_argument(
        "--host", default=None, help="bind address (default: settings.web.host)"
    )
    web_parser.add_argument(
        "--port", type=int, default=None, help="bind port (default: settings.web.port)"
    )

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
        "pump": cmd_pump,
        "web": cmd_web,
    }
    try:
        return asyncio.run(handlers[args.command](args))
    except KeyboardInterrupt:  # pragma: no cover - interactive convenience
        return 130


if __name__ == "__main__":
    sys.exit(main())
