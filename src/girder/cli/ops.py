"""One-shot CLI operations: migrate / recover / gc / web.

Split out of the former single-file ``girder/cli.py`` (see git history for
the original); zero behavior change.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from girder.cli._common import _build_notifier, log
from girder.gitops.worktree import DEFAULT_BASE
from girder.orchestrator.gc import WorktreeGC
from girder.orchestrator.recovery import RecoveryService


async def cmd_migrate(args: argparse.Namespace) -> int:
    # Monkeypatch indirection: read through the package namespace.
    from girder import cli

    db = await cli._open_db(args)
    await db.close()
    log.info("database %s is up to date", args.db)
    return 0


async def cmd_recover(args: argparse.Namespace) -> int:
    # Monkeypatch indirection: tests patch ``girder.cli.load_settings``.
    from girder import cli

    settings = cli.load_settings()
    db = await cli._open_db(args)
    try:
        service = RecoveryService(db, settings, notifier=_build_notifier(settings, db))
        report = await service.recover()
        print(report.summary())
        return 0
    finally:
        await db.close()


async def cmd_gc(args: argparse.Namespace) -> int:
    # Monkeypatch indirection: tests patch ``girder.cli.load_settings``.
    from girder import cli

    settings = cli.load_settings()
    db = await cli._open_db(args)
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

    # Monkeypatch indirection: tests patch ``girder.cli.load_settings`` and
    # ``girder.cli.create_app`` — read both through the package namespace.
    from girder import cli

    settings = cli.load_settings()
    app = cli.create_app(
        db_path=Path(args.db),
        settings=settings,
        migrations_dir=Path(args.migrations_dir) if args.migrations_dir else None,
    )
    host = args.host if args.host is not None else settings.web.host
    port = args.port if args.port is not None else settings.web.port
    api_key = getattr(args, "api_key", None)
    if api_key is not None:
        # The auth dependency reads the key from app.state.settings at request
        # time, so mutating the Settings object before create_app is enough.
        settings.web.api_key = api_key
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
