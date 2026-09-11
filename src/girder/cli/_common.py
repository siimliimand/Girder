"""Shared helpers for the ``girder`` CLI.

Split out of the former single-file ``girder/cli.py`` (see git history for
the original); zero behavior change.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from girder.config import Settings
from girder.db.engine import Database
from girder.guard.redact import Redactor
from girder.notify.notifier import Notifier

log = logging.getLogger("girder")


def _build_notifier(settings: Settings, db: Database | None) -> Notifier:
    # Monkeypatch indirection: tests patch ``girder.cli.load_secrets``, so the
    # name must be read through the package namespace at call time.
    from girder import cli

    secrets = cli.load_secrets()
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
