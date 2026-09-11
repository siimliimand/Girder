"""The ``girder validate`` command (WP 13.3).

Split out of the former single-file ``girder/cli.py`` (see git history for
the original); zero behavior change.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from girder.config import (
    Secrets,
    Settings,
    _find_project_toml,
)
from girder.stacks import STACK_REGISTRY

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
    """Load settings + secrets, capturing failures instead of raising.

    Monkeypatch indirection: tests patch ``girder.cli.load_settings`` and
    ``girder.cli.load_secrets`` — read both through the package namespace
    at call time, never via a from-import frozen at module top.
    """
    from girder import cli

    settings_error: str | None = None
    settings: Settings | None = None
    try:
        settings = cli.load_settings(config_path)
    except Exception as exc:
        settings_error = str(exc)
    secrets_error: str | None = None
    secrets: Secrets | None = None
    try:
        secrets = cli.load_secrets()
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
    # Monkeypatch indirection: tests patch ``girder.cli.DEFAULT_SECRETS_PATH``.
    from girder import cli

    failures: list[str] = []
    path = cli.DEFAULT_SECRETS_PATH
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
    # Monkeypatch indirection: tests patch ``girder.cli.DEFAULT_SECRETS_PATH``.
    from girder import cli

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
                f" — set {field_name} in {cli.DEFAULT_SECRETS_PATH}"
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
        # load_settings() already validates project.stack against STACK_REGISTRY,
        # so this message names an unknown stack when that was the problem.
        # Collect it as ONE failure and keep running the remaining checks.
        failures.append(f"girder.toml: {ctx.config_error}")
    settings = ctx.settings
    if settings is None:
        if ctx.config_error is None:
            failures.append("girder.toml: could not load settings")
    else:
        failures.extend(_check_model_roles(settings))
        # Direct-registry check: catches contexts built without load_settings.
        if ctx.stack not in STACK_REGISTRY:
            failures.append(
                f"project.stack: {ctx.stack!r} is not a known stack"
                f" (known: {', '.join(sorted(STACK_REGISTRY))})"
            )
    # Secrets checks do not depend on settings, so they run even when the
    # config failed to load — collect ALL detectable failures, never stop early.
    if ctx.secrets_error is not None:
        failures.append(f"secrets: {ctx.secrets_error}")
    elif ctx.secrets is not None:
        failures.extend(_check_secrets())
        if settings is not None:
            failures.extend(_check_api_keys(settings, ctx.secrets))
    if check_sandbox and settings is not None:
        failures.extend(_check_runtime_and_image(ctx.stack))
    if settings is not None:
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
