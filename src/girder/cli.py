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
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from girder import __version__, fsm
from girder.api.app import create_app
from girder.budget.guard import BudgetGuard
from girder.config import (
    Secrets,
    SecretsPermissionError,
    Settings,
    load_secrets,
    load_settings,
)
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.db.models import Run, RunStatus
from girder.github.client import GitHubClient
from girder.gitops.worktree import DEFAULT_BASE
from girder.guard.redact import Redactor
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.notify.telegram_inbound import TelegramReceiver
from girder.orchestrator.gc import WorktreeGC
from girder.orchestrator.recovery import RecoveryService
from girder.orchestrator.run_engine import _PUMPABLE_RUN_STATUSES, RunEngine
from girder.sandbox.engine import SandboxEngine
from girder.sandbox.local import LocalExecSandbox
from girder.sandbox.podman import PodmanEngine
from girder.specs.amendment import AmendmentError, resolve_amendment

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
    uds = getattr(args, "unix_socket", None) or settings.web.unix_socket
    if uds is not None:
        # UDS bind (§10: "or UDS /run/girder.sock"): clear a stale socket from
        # a previous crash and make sure the parent directory exists.
        socket_path = Path(uds)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Startup-only path: unlinking a stale socket here is fine.
        if os.path.lexists(uds):  # noqa: ASYNC240
            os.unlink(uds)
        log.info("girder web listening on uds://%s (db=%s)", uds, args.db)
        config = uvicorn.Config(app, uds=uds, log_level="info")
    else:
        log.info("girder web listening on http://%s:%s (db=%s)", host, port, args.db)
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()
    return 0


class LocalSandboxRefused(RuntimeError):
    """``--sandbox local`` was requested without an explicit dev opt-in."""


def _local_sandbox_allowed(args: argparse.Namespace) -> bool:
    """Local sandbox runs attempts directly on the host with zero isolation
    ("NOT A SECURITY BOUNDARY") — allowed only with an explicit ``--dev`` flag
    or ``GIRDER_DEV=1`` in the environment."""
    return bool(getattr(args, "dev", False)) or os.environ.get("GIRDER_DEV") == "1"


def _build_sandbox(args: argparse.Namespace, settings: Settings) -> SandboxEngine:
    if args.sandbox == "local":
        if not _local_sandbox_allowed(args):
            raise LocalSandboxRefused(
                "--sandbox local executes attempts directly on the host with no"
                " isolation (documented NOT A SECURITY BOUNDARY). Refusing to run"
                " it in a daemon/production context: pass --dev (or set"
                " GIRDER_DEV=1) to confirm you are in a development environment."
            )
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


def _due_run_ids(run_ids: list[str], active: dict[str, asyncio.Task[None]]) -> list[str]:
    """Pure scheduling step (issue 23): runs to spawn this cycle — every
    pumpable run that does not already have a pump task in flight. A run whose
    pump is still running is skipped, which preserves the per-run ordering
    guarantee the sequential loop had."""
    return [rid for rid in run_ids if rid not in active]


async def pump_runs_concurrently(
    run_ids: list[str],
    pump: Callable[[str], Awaitable[object]],
    active: dict[str, asyncio.Task[None]],
    *,
    logger: logging.Logger = log,
) -> None:
    """Spawn one task per due run, gather with exception isolation.

    One crashing (or long-running) pump never blocks or kills the others; the
    *active* registry is the caller-owned per-run task table, trimmed of
    finished tasks each cycle.
    """
    for run_id in _due_run_ids(run_ids, active):
        active[run_id] = asyncio.create_task(_pump_one(run_id, pump, logger=logger))
    finished = [rid for rid, task in active.items() if task.done()]
    for rid in finished:
        active.pop(rid)


async def _pump_one(
    run_id: str,
    pump: Callable[[str], Awaitable[object]],
    *,
    logger: logging.Logger,
) -> None:
    try:
        descriptor = await pump(run_id)
        logger.info("pump run %s -> %s", run_id, descriptor)
    except asyncio.CancelledError:
        raise
    except Exception:  # never let one run kill the loop
        logger.exception("pump failed for run %s", run_id)


_DRAIN_TIMEOUT_S = 10.0


async def _drain(
    tasks: list[asyncio.Task[None]],
    *,
    # ASYNC109: asyncio.timeout cannot bound this wait — asyncio.wait must own the deadline.
    timeout: float = _DRAIN_TIMEOUT_S,  # noqa: ASYNC109
) -> None:
    """Cancel background tasks and wait for them, bounded.

    Shutdown must never hang: a task with a slow or wedged cleanup would
    otherwise hold Ctrl+C forever. Two incident classes are covered:
    the GC task was awaited in a gather without ever being cancelled
    (``run_forever()`` is an infinite loop → SIGKILL was the only exit),
    and ``wait_for(gather(...))`` cannot bound that wait at all — a gather
    whose children ignore cancellation never completes, so the timeout
    never fires. ``asyncio.wait`` returns (done, pending) at the deadline
    unconditionally.
    """
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        log.error(
            "shutdown drain exceeded %ss; abandoning %s — press Ctrl+C again"
            " to force immediate exit",
            timeout,
            sorted(task.get_name() for task in pending),
        )
    for task in done:
        if not task.cancelled() and task.exception() is not None:
            log.error("task %s failed during shutdown: %r", task.get_name(), task.exception())


async def cmd_daemon(args: argparse.Namespace) -> int:
    settings = load_settings()
    # §6.1: at least one model role per tier — config validation only warns,
    # but a daemon that cannot call a model must not start (issue 16).
    if not settings.models.roles and os.environ.get("GIRDER_ALLOW_NO_ROLES") != "1":
        log.error(
            "no model roles configured — the daemon cannot drive a run."
            " Declare [[models.roles]] (tier1..tier3) in girder.toml"
            " (set GIRDER_ALLOW_NO_ROLES=1 to override)"
        )
        return 2
    secrets = load_secrets()
    redactor = Redactor(secrets.redaction_secret_env_names)
    notifier = _build_notifier(settings, None)
    db = await _open_db(args)
    stop = asyncio.Event()

    def _signal(sig: signal.Signals) -> None:
        if stop.is_set():
            # Second signal: the graceful drain is wedged (or the operator
            # won't wait). Die immediately — os._exit skips db.close() by
            # design; boot recovery reconciles any unclean exit.
            log.warning("received %s again — forcing exit", sig.name)
            os._exit(128 + int(sig))
        log.info("received %s — shutting down", sig.name)
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _signal, s)

    log.info("girder daemon starting (db=%s, sandbox=%s)", args.db, args.sandbox)
    try:
        # §6.3 boot-time self-test: fail fast if the edge tables and the
        # guarded transition engine ever disagree.
        await fsm.self_test()
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
        telegram_task: asyncio.Task[None] | None = _start_telegram_receiver(
            args, settings, secrets, db
        )
        poll_s = 5.0
        active_pumps: dict[str, asyncio.Task[None]] = {}
        while not stop.is_set():
            run_ids = [run.id for run in await _pumpable_runs(db)]
            # issue 23: pump runs concurrently — one run's long attempt no
            # longer blocks steering/pump processing of every other run.
            await pump_runs_concurrently(
                run_ids, engine.pump_once, active_pumps, logger=log
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_s)
            except TimeoutError:
                pass
        background: list[asyncio.Task[None]] = [gc_task, *active_pumps.values()]
        if telegram_task is not None:
            background.append(telegram_task)
        await _drain(background)
    finally:
        await db.close()
    log.info("daemon stopped cleanly")
    return 0


def _start_telegram_receiver(
    args: argparse.Namespace, settings: Settings, secrets: Secrets, db: Database
) -> asyncio.Task[None] | None:
    """Start the §8.4 Telegram inbound poller when configured; never fatal."""
    if "telegram" not in settings.notify.channels:
        return None
    if not secrets.notify_telegram_bot_token:
        log.warning("telegram channel configured but no bot token — inbound disabled")
        return None
    notifier = _build_notifier(settings, db)

    async def resolver(amendment_id: str, decision: str, guidance: str | None) -> str:
        amendment = await repo.get_spec_amendment(db, amendment_id)
        if amendment is None:
            raise AmendmentError(f"amendment {amendment_id} not found")
        run = await repo.get_run(db, amendment.run_id)
        if run is None:
            raise AmendmentError(f"run {amendment.run_id} not found")
        project = await repo.get_project(db, run.project_id)
        if project is None:
            raise AmendmentError(f"project {run.project_id} not found")
        outcome = await resolve_amendment(
            db,
            project=project,
            run=run,
            amendment=amendment,
            decision=decision,
            guidance=guidance,
            notifier=notifier,
        )
        return (
            f"amendment resolved: {outcome.decision}"
            f" (run {run.id} -> {outcome.run_status}, task -> {outcome.task_status})"
        )

    receiver = TelegramReceiver(db, settings.notify, secrets, notifier, resolver)
    log.info("telegram amendment inbound enabled (chat %s)", settings.notify.telegram_chat_id)
    return asyncio.create_task(receiver.run())


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


async def review_run(db: Database, run_id: str) -> int:
    """Record ``merge_reviewed`` for a merged run; 0 ok, 1 refused (plan.md §2.3
    T1 rolling review window: unreviewed merges pause new merges)."""
    run = await repo.get_run(db, run_id)
    if run is None:
        log.error("run %s not found", run_id)
        return 1
    if run.status is not RunStatus.MERGED:
        log.error(
            "run %s is %s, not merged — only merged runs can be marked reviewed",
            run_id,
            run.status,
        )
        return 1
    await repo.insert_event(db, "merge_reviewed", {"reviewed_by": "cli"}, run_id=run.id)
    print(f"run {run.id} marked merge_reviewed")
    return 0


async def cmd_review(args: argparse.Namespace) -> int:
    db = await _open_db(args)
    try:
        return await review_run(db, args.run_id)
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
        help="sandbox engine (local executes on the host with no isolation;"
        " requires --dev and is NOT a security boundary)",
    )
    daemon_parser.add_argument(
        "--dev",
        action="store_true",
        help="confirm a development environment (required for --sandbox local,"
        " alternatively set GIRDER_DEV=1)",
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
    review_parser = sub.add_parser(
        "review", help="mark a merged run as human-reviewed (T1 review window, §2.3)"
    )
    review_parser.add_argument("run_id", help="merged run id to mark reviewed")

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
        "review": cmd_review,
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


if __name__ == "__main__":
    sys.exit(main())
