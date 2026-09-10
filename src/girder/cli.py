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
import shutil
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from girder import __version__, fsm
from girder.api.app import create_app
from girder.budget.guard import BudgetGuard
from girder.config import (
    DEFAULT_SECRETS_PATH,
    Secrets,
    SecretsPermissionError,
    Settings,
    _find_project_toml,
    load_secrets,
    load_settings,
)
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.db.models import TERMINAL_RUN_STATUSES, Run, RunStatus
from girder.github.client import GitHubClient
from girder.gitops.worktree import DEFAULT_BASE
from girder.guard.redact import Redactor
from girder.logging_config import configure_logging
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


# ------------------------------------------------------------------- prune (WP 13.2)
#
# No FK in migrations 001-013 declares ON DELETE CASCADE, and the engine runs
# with ``PRAGMA foreign_keys=ON`` — deleting a run with children would fail.
# Prune therefore deletes child rows explicitly, deepest dependents first,
# inside one transaction (``db.tx()``). Rows linked only by bare TEXT columns
# (no FK) — steering_events, integrity_violations, notifications_log — are
# included so nothing referencing a pruned run survives.


@dataclass
class PruneReport:
    """What one prune pass deleted (or would delete, in dry-run mode)."""

    run_ids: list[str]
    child_rows: dict[str, int] = field(default_factory=dict)

    @property
    def run_count(self) -> int:
        return len(self.run_ids)

    def summary(self, *, dry_run: bool) -> str:
        verb = "would delete" if dry_run else "deleted"
        lines = [f"{verb} {self.run_count} run(s) past the prune horizon:"]
        lines.extend(f"  {run_id}" for run_id in self.run_ids)
        lines.append(f"{verb} child rows:")
        for table, count in self.child_rows.items():
            lines.append(f"  {table}: {count}")
        return "\n".join(lines)


# Run-id scoping subqueries. _RUN_SCOPE selects the ids to prune; _ATTEMPT_SCOPE
# selects the attempts belonging to those runs via waves -> tasks -> attempts.
_RUN_SCOPE = "SELECT id FROM prune_run_ids"
_ATTEMPT_SCOPE = (
    "SELECT a.id FROM attempts a"
    " JOIN tasks t ON a.task_id = t.id"
    " JOIN waves w ON t.wave_id = w.id"
    f" WHERE w.run_id IN ({_RUN_SCOPE})"
)
_TASK_SCOPE = (
    "SELECT t.id FROM tasks t"
    " JOIN waves w ON t.wave_id = w.id"
    f" WHERE w.run_id IN ({_RUN_SCOPE})"
)

# (table, where-clause) pairs in dependency-safe delete order. FK targets are
# always emptied before their parents: attempt-level rows, then attempts, then
# tasks, then waves, then run-level rows, then runs themselves.
_PRUNE_TABLES: list[tuple[str, str]] = [
    ("worktrees", f"attempt_id IN ({_ATTEMPT_SCOPE})"),
    ("redaction_log", f"attempt_id IN ({_ATTEMPT_SCOPE})"),
    ("tool_calls", f"attempt_id IN ({_ATTEMPT_SCOPE})"),
    ("token_usage", f"attempt_id IN ({_ATTEMPT_SCOPE}) OR run_id IN ({_RUN_SCOPE})"),
    ("attempt_prompts", f"attempt_id IN ({_ATTEMPT_SCOPE}) OR run_id IN ({_RUN_SCOPE})"),
    ("attempt_diffs", f"attempt_id IN ({_ATTEMPT_SCOPE}) OR run_id IN ({_RUN_SCOPE})"),
    ("attempts", f"task_id IN ({_TASK_SCOPE})"),
    ("spec_amendments", f"run_id IN ({_RUN_SCOPE}) OR task_id IN ({_TASK_SCOPE})"),
    ("tasks", f"wave_id IN (SELECT id FROM waves WHERE run_id IN ({_RUN_SCOPE}))"),
    ("waves", f"run_id IN ({_RUN_SCOPE})"),
    ("ci_check_results", f"run_id IN ({_RUN_SCOPE})"),
    ("agent_events", f"run_id IN ({_RUN_SCOPE}) OR attempt_id IN ({_ATTEMPT_SCOPE})"),
    ("integrity_violations", f"run_id IN ({_RUN_SCOPE})"),
    ("steering_events", f"run_id IN ({_RUN_SCOPE})"),
    ("notifications_log", f"run_id IN ({_RUN_SCOPE})"),
]


async def _prune_run_ids(db: Database, older_than_days: int) -> list[str]:
    """Terminal-status runs whose created_at is older than the horizon."""
    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).replace(
        microsecond=0
    ).isoformat()
    statuses = sorted(s.value for s in TERMINAL_RUN_STATUSES)
    placeholders = ",".join("?" * len(statuses))
    rows = await db.fetchall(
        f"SELECT id FROM runs WHERE status IN ({placeholders})"
        " AND created_at < ? ORDER BY created_at",
        (*statuses, cutoff),
    )
    return [str(r["id"]) for r in rows]


async def _count_prune_rows(conn: aiosqlite.Connection, run_ids: list[str]) -> dict[str, int]:
    """Per-child-table row counts that a prune of *run_ids* would remove."""
    counts: dict[str, int] = {}
    for table, where in _PRUNE_TABLES:
        cur = await conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}")
        row = await cur.fetchone()
        counts[table] = int(row[0]) if row is not None else 0
    return counts


async def prune_runs(db: Database, *, older_than_days: int, dry_run: bool) -> PruneReport:
    """Delete terminal runs older than the horizon (or just report them).

    Both modes scope the victim set into a TEMP table first so every count and
    delete below shares one exact definition of "the runs being pruned".
    Dry-run never issues a DELETE against the main database.
    """
    run_ids = await _prune_run_ids(db, older_than_days)
    conn = db.conn
    await conn.execute("CREATE TEMP TABLE IF NOT EXISTS prune_run_ids (id TEXT PRIMARY KEY)")
    await conn.execute("DELETE FROM prune_run_ids")
    if run_ids:
        await conn.executemany(
            "INSERT INTO prune_run_ids (id) VALUES (?)", [(rid,) for rid in run_ids]
        )
    if dry_run:
        counts = await _count_prune_rows(conn, run_ids)
        await conn.execute("DROP TABLE IF EXISTS temp.prune_run_ids")
        return PruneReport(run_ids=run_ids, child_rows=counts)
    async with db.tx() as tx:
        counts = await _count_prune_rows(tx, run_ids)
        for table, where in _PRUNE_TABLES:
            await tx.execute(f"DELETE FROM {table} WHERE {where}")
        await tx.execute(f"DELETE FROM runs WHERE id IN ({_RUN_SCOPE})")
        await tx.execute("DELETE FROM prune_run_ids")
        await tx.execute("DROP TABLE IF EXISTS temp.prune_run_ids")
    return PruneReport(run_ids=run_ids, child_rows=counts)


async def cmd_prune(args: argparse.Namespace) -> int:
    db = await _open_db(args)
    try:
        report = await prune_runs(
            db, older_than_days=args.older_than_days, dry_run=args.dry_run
        )
    finally:
        await db.close()
    print(report.summary(dry_run=args.dry_run))
    return 0


# ----------------------------------------------------------------- validate (WP 13.3)

# Sprint-11 multi-stack has not landed (no STACK_REGISTRY yet); the known set is
# the single stack the codebase currently ships (config.ProjectConfig default).
KNOWN_STACKS = frozenset({"python-3.12"})

_MODEL_TIERS = ("tier1", "tier2", "tier3")

# provider -> the Secrets field that must hold a non-empty key for it.
_PROVIDER_SECRET_FIELDS = {
    "openrouter": "models_openrouter_api_key",
    "anthropic": "models_anthropic_api_key",
    "openai": "models_openai_api_key",
}


@dataclass
class ValidationContext:
    """Everything the checks read, gathered before any check runs."""

    settings: Settings | None
    config_error: str | None
    secrets: Secrets | None
    secrets_error: str | None
    config_root: Path
    stack: str


def _gather_validation_context(config_path: Path | None) -> ValidationContext:
    """Load settings + secrets, capturing failures instead of raising."""
    settings_error: str | None = None
    settings: Settings | None = None
    try:
        settings = load_settings(config_path)
    except Exception as exc:
        settings_error = str(exc)
    secrets_error: str | None = None
    secrets: Secrets | None = None
    try:
        secrets = load_secrets()
    except Exception as exc:  # SecretsPermissionError and malformed TOML
        secrets_error = str(exc)
    if config_path is not None:
        config_root = config_path.resolve().parent
    else:
        found = _find_project_toml()
        config_root = found.parent if found is not None else Path.cwd()
    stack = settings.project.stack if settings is not None else ""
    return ValidationContext(
        settings=settings,
        config_error=settings_error,
        secrets=secrets,
        secrets_error=secrets_error,
        config_root=config_root,
        stack=stack,
    )


def _check_model_roles(settings: Settings) -> list[str]:
    failures: list[str] = []
    roles = {role.role: role for role in settings.models.roles}
    missing = [tier for tier in _MODEL_TIERS if tier not in roles]
    if missing:
        failures.append(
            "model roles: missing tier(s) " + ", ".join(missing)
            + " — declare [[models.roles]] entries for tier1, tier2 and tier3 in girder.toml"
        )
    for name in sorted(set(roles) - set(_MODEL_TIERS)):
        failures.append(f"model roles: unknown tier {name!r} (expected tier1..tier3)")
    for tier in _MODEL_TIERS:
        role = roles.get(tier)
        if role is None:
            continue
        if role.price_in_per_mtok <= 0 or role.price_out_per_mtok <= 0:
            failures.append(
                f"model roles: {tier} ({role.model}) must declare non-zero prices"
                " (price_in_per_mtok and price_out_per_mtok)"
            )
    return failures


def _check_secrets() -> list[str]:
    failures: list[str] = []
    path = DEFAULT_SECRETS_PATH
    if not path.is_file():
        failures.append(f"secrets: file {path} does not exist — create it (mode 0600)")
        return failures
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        failures.append(
            f"secrets: {path} is mode {mode:o}, not owner-only 0600 — run: chmod 600 {path}"
        )
    return failures


def _check_api_keys(settings: Settings, secrets: Secrets) -> list[str]:
    failures: list[str] = []
    providers = sorted({role.provider for role in settings.models.roles})
    for provider in providers:
        field_name = _PROVIDER_SECRET_FIELDS.get(provider)
        if field_name is None:
            failures.append(
                f"api keys: unknown provider {provider!r}"
                f" (known: {', '.join(sorted(_PROVIDER_SECRET_FIELDS))})"
            )
            continue
        value: str | None = getattr(secrets, field_name)
        if not value:
            failures.append(
                f"api keys: no key configured for provider {provider!r}"
                f" — set {field_name} in {DEFAULT_SECRETS_PATH}"
            )
    return failures


def _detect_runtime(preferred: str) -> str | None:
    """First container runtime in PATH, preferring the configured one."""
    for runtime in (preferred, "podman", "docker"):
        if runtime in ("podman", "docker") and shutil.which(runtime):
            return runtime
    return None


def _check_runtime_and_image(stack: str) -> list[str]:
    failures: list[str] = []
    runtime = _detect_runtime("podman")
    if runtime is None:
        failures.append(
            "sandbox runtime: neither podman nor docker found in PATH"
            " — install rootless podman (see docs/deployment.md)"
        )
        return failures
    try:
        result = subprocess.run(
            [runtime, "images", "-q", f"girder-runner:{stack}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        failures.append(f"runner image: could not query {runtime}: {exc}")
        return failures
    if not result.stdout.strip():
        failures.append(
            f"runner image: girder-runner:{stack} not found in {runtime}"
            f" — build it before running (podman build -t girder-runner:{stack})"
        )
    return failures


def _check_test_directories(settings: Settings, config_root: Path) -> list[str]:
    failures: list[str] = []
    for directory in settings.project.test_directories:
        if not (config_root / directory).is_dir():
            failures.append(
                f"test directories: {directory!r} does not exist under {config_root}"
            )
    return failures


def collect_validation_failures(
    ctx: ValidationContext,
    *,
    check_sandbox: bool = True,
) -> list[str]:
    """Run every WP 13.3 check, collecting ALL failures (never stop at the first)."""
    failures: list[str] = []
    if ctx.config_error is not None:
        failures.append(f"girder.toml: {ctx.config_error}")
    settings = ctx.settings
    if settings is None:
        if ctx.config_error is None:
            failures.append("girder.toml: could not load settings")
        return failures
    failures.extend(_check_model_roles(settings))
    if ctx.stack not in KNOWN_STACKS:
        failures.append(
            f"project.stack: {ctx.stack!r} is not a known stack"
            f" (known: {', '.join(sorted(KNOWN_STACKS))})"
        )
    if ctx.secrets_error is not None:
        failures.append(f"secrets: {ctx.secrets_error}")
    elif ctx.secrets is not None:
        failures.extend(_check_secrets())
        failures.extend(_check_api_keys(settings, ctx.secrets))
    if check_sandbox:
        failures.extend(_check_runtime_and_image(ctx.stack))
    failures.extend(_check_test_directories(settings, ctx.config_root))
    return failures


async def cmd_validate(args: argparse.Namespace) -> int:
    config_path = Path(args.config_path) if args.config_path else None
    ctx = _gather_validation_context(config_path)
    failures = collect_validation_failures(ctx)
    if failures:
        print("girder validate: FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("girder validate: OK — configuration looks valid")
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


if __name__ == "__main__":
    sys.exit(main())
