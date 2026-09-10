"""Shared FastAPI dependency injection for the console routes (WP 10.1).

The app factory (:func:`girder.api.app.create_app`) populates ``app.state``
during the lifespan startup; these dependencies surface those singletons to
route handlers in a form FastAPI can inject. This is a pure read of the same
state the pre-split handlers accessed as ``request.app.state.*`` — session
lifecycle stays owned by the lifespan, so the refactor is semantically
identical. ``Starlette.datastructures.State`` is untyped, hence the casts.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from girder.budget.guard import BudgetGuard
from girder.config import Secrets, Settings
from girder.db.engine import Database
from girder.guard.redact import Redactor
from girder.notify.notifier import Notifier


def get_db(request: Request) -> Database:
    """The lifespan-owned database handle (opened/migrated/closed by the app)."""
    return cast(Database, request.app.state.db)


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_secrets(request: Request) -> Secrets:
    return cast(Secrets, request.app.state.secrets)


def get_redactor(request: Request) -> Redactor:
    return cast(Redactor, request.app.state.redactor)


def get_notifier(request: Request) -> Notifier:
    return cast(Notifier, request.app.state.notifier)


def get_budget(request: Request) -> BudgetGuard:
    return cast(BudgetGuard, request.app.state.budget)


Db = Annotated[Database, Depends(get_db)]
SettingsDeps = Annotated[Settings, Depends(get_settings)]
SecretsDeps = Annotated[Secrets, Depends(get_secrets)]
RedactorDeps = Annotated[Redactor, Depends(get_redactor)]
NotifierDeps = Annotated[Notifier, Depends(get_notifier)]
BudgetDeps = Annotated[BudgetGuard, Depends(get_budget)]
