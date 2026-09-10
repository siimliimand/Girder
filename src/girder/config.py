"""Settings loading: defaults <- girder.toml <- environment overrides.

Two strictly separated sources:

* :class:`Settings` — project configuration, committed, no secrets.
* :class:`Secrets` — host-only credentials from ``~/.config/girder/secrets.toml``
  (mode 0600). Secrets never enter container environments and never appear in
  any ``repr()`` of a Settings or Secrets object (unit-tested).
"""

from __future__ import annotations

import contextvars
import logging
import os
import tomllib
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import PydanticBaseSettingsSource

from girder.stacks import STACK_REGISTRY

log = logging.getLogger(__name__)


class ProjectConfig(BaseModel):
    name: str = "girder"
    stack: str = "python-3.12"
    test_directories: list[str] = Field(default_factory=lambda: ["tests"])
    test_signal_patterns: list[str] = Field(
        default_factory=lambda: [
            "tests/**",
            "**/test_*.py",
            "**/*_test.py",
            "**/conftest.py",
            "tests/fixtures/**",
            "**/mocks/**",
        ]
    )
    protected_read_paths: list[str] = Field(
        default_factory=lambda: [
            ".github/**",
            "openspec/**",
            ".git/**",
            ".env*",
            "**/*.pem",
            "**/*credentials*",
        ]
    )
    strict_read_scope: bool = False


def project_allows_empty_baseline(repo_path: Path | str) -> bool:
    """Per-project ``allow_empty_baseline`` from the *target repo's* girder.toml.

    The daemon's global :class:`Settings` describe the deployment; whether a
    repo has a test suite at all is a property of that repo, so the flag is
    read from the registered repo root (README: "girder.toml — per project, at
    the repo root"). Strict by §9 rails: a malformed file or a non-boolean
    value is a hard error — config problems escalate, they never silently
    default.
    """
    path = Path(repo_path) / "girder.toml"
    if not path.is_file():
        return False
    data = tomllib.loads(path.read_text())
    value = data.get("project", {}).get("allow_empty_baseline", False)
    if not isinstance(value, bool):
        raise ValueError(
            f"{path}: [project] allow_empty_baseline must be a boolean (got {value!r})"
        )
    return value


class AutonomyConfig(BaseModel):
    """Autonomy tiers per plan.md §2.3 — every project starts at T0."""

    tier: int = 0
    t2_required_streak: int = 10
    t1_review_window: int = 3


class BudgetConfig(BaseModel):
    run_cap_usd: float = 5.00


class LimitsConfig(BaseModel):
    attempt_wallclock_s: int = 600
    attempt_max_turns: int = 20
    task_max_attempts: int = 3
    ci_fix_attempts: int = 3
    conflict_resolution_attempts: int = 2
    tool_output_max_lines: int = 100
    tool_output_max_tokens: int = 4000
    # WP 8.4 codebase index: injected as a [TRUSTED] block at attempt start
    # (rebuilt per attempt, R-SP8-4). inject_index_max_tokens bounds the block
    # by dropping whole directories, lowest-relevance first.
    inject_index: bool = True
    inject_index_max_tokens: int = 2000


class SandboxNetwork(BaseModel):
    network: str = "none"  # none | private
    memory: str = "4g"
    cpus: float = 2.0
    pids_limit: int = 512
    runtime: str = "podman"  # podman | docker (identical flag surface)
    # Interpreter used to run project test suites (baseline + verify loop).
    # "python3" is the portable default; the CI parity image may pin another.
    python_bin: str = "python3"
    # Host package-cache root bound read-only into every attempt container
    # (plan.md Phase 0 task 3, impl-plan §6.4/§8.1): subdirs pip/ npm/ cargo/
    # map onto the image's PIP_CACHE_DIR/npm_config_cache/CARGO_HOME paths
    # (/cache/*). A missing host subdir simply skips that mount.
    cache_dir: str = "/var/cache/orchestrator"


class GithubConfig(BaseModel):
    remote: str = "origin"
    pr_template: str | None = None
    # WP 9.1: inbound webhook receiver. Off by default — flip on once the
    # deployment has a public HTTPS route to /api/webhooks/github and
    # github_webhook_secret is configured.
    webhook_enabled: bool = False
    bot_account: str = ""  # GitHub username watched for issue assignments
    api_url: str = "https://api.github.com"  # overridable for tests / proxies
    poll_interval_s: float = 30.0  # CI checks poll cadence (impl-plan §6.11)
    poll_timeout_s: float = 3600.0  # exceeded ⇒ escalate, never poll forever
    merge_method: str = "squash"  # merge | squash | rebase (§9.2 T1/T2)


class NotifyConfig(BaseModel):
    channels: list[str] = Field(default_factory=list)  # subset of {"telegram","discord","slack"}
    telegram_chat_id: str | None = None
    slack_channel: str | None = None  # Slack channel id for SlackNotifier deliveries


class ModelRole(BaseModel):
    """Capability role (D7): the role is the commitment; the model id is config."""

    role: str  # tier1 | tier2 | tier3
    provider: str  # openrouter | anthropic | openai
    model: str
    context_window: int = 200_000
    max_output_tokens: int = 4096
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    base_url: str | None = None  # override the provider default endpoint; None = provider default

    @property
    def price_in_per_tok(self) -> float:
        return self.price_in_per_mtok / 1_000_000

    @property
    def price_out_per_tok(self) -> float:
        return self.price_out_per_mtok / 1_000_000


class SpecsConfig(BaseModel):
    """Spec engine source (impl-plan §6.9/R7): shell out to the openspec CLI
    instead of the native generator when cli is true."""

    cli: bool = False
    cli_bin: str = "openspec"
    generation_attempts: int = 3  # bounded validation-feedback retries for the native generator
    # §8.5: force the pre-spec clarification loop on every new run. Default
    # off — the zero-friction flow is untouched unless explicitly enabled.
    require_clarification: bool = False


class WebConfig(BaseModel):
    """Approval web UI bind address (impl-plan §10): loopback-only by default,
    or a Unix domain socket (``unix_socket``) instead of host/port."""

    host: str = "127.0.0.1"
    port: int = 8787
    unix_socket: str | None = None
    # Console API key (WP 12.5). Empty/unset ⇒ no auth (loopback default).
    # When set, all routes need Authorization: Bearer / X-API-Key. Override via
    # GIRDER_WEB__API_KEY or `girder web --api-key`; secrets.toml [web]
    # api_key works too (Secrets.web_api_key).
    api_key: str = ""


class ModelsConfig(BaseModel):
    roles: list[ModelRole] = Field(default_factory=list)
    request_timeout_s: float = 300.0  # per-call HTTP timeout

    @model_validator(mode="after")
    def _check_tiers(self) -> ModelsConfig:
        # A partial registry is always an error: every sprint's pipeline calls
        # all three roles, so a missing tier would fail mid-run (issue 16).
        # An *empty* registry still passes construction — load_settings raises
        # (impl-plan §6.1 "at least one model role per tier"), unless the
        # documented GIRDER_ALLOW_NO_ROLES=1 test/dev opt-out is set.
        have = {r.role for r in self.roles}
        missing = {"tier1", "tier2", "tier3"} - have
        if self.roles and missing:
            raise ValueError(f"model roles incomplete, missing: {sorted(missing)}")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GIRDER_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence (first wins): init kwargs > environment > girder.toml.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            _TomlSettingsSource(settings_cls),
        )

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    sandbox: SandboxNetwork = Field(default_factory=SandboxNetwork)
    github: GithubConfig = Field(default_factory=GithubConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    specs: SpecsConfig = Field(default_factory=SpecsConfig)
    web: WebConfig = Field(default_factory=WebConfig)

    @model_validator(mode="after")
    def _check_ranges(self) -> Settings:
        # impl-plan §6.1: budget > 0, limits positive, test_directories non-empty.
        errors: list[str] = []
        if self.budget.run_cap_usd <= 0:
            errors.append(f"budget.run_cap_usd must be > 0 (got {self.budget.run_cap_usd})")
        for name, value in self.limits.__dict__.items():
            if isinstance(value, bool):
                continue  # inject_index is a flag, not a positive quantity
            if value <= 0:
                errors.append(f"limits.{name} must be > 0 (got {value})")
        if not self.project.test_directories:
            errors.append("project.test_directories must be a non-empty list")
        if self.project.stack not in STACK_REGISTRY:
            errors.append(
                f"project.stack {self.project.stack!r} is not a known stack"
                f" (valid: {', '.join(sorted(STACK_REGISTRY))})"
            )
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def __repr__(self) -> str:  # pragma: no cover - defensive, exercised in tests
        # Settings holds no secrets, but keep the habit: repr never grows a
        # secrets field silently because Secrets is a separate type.
        return f"Settings(project={self.project.name!r}, …)"


class Secrets(BaseModel):
    """Host-only credentials. Values must never be logged, persisted, or mounted.

    Canonical file form is tables matching the impl-plan §3.2 example::

        [models]
        openrouter_api_key = "…"
        [github]
        token = "…"
        [notify]
        telegram_bot_token = "…"
        discord_webhook_url = "…"
        slack_bot_token = "…"
        slack_signing_secret = "…"
        [github]
        webhook_secret = "…"
        [redaction]
        secret_env_names = ["GITHUB_TOKEN", …]

    Flat top-level keys using the same field names are also accepted.
    """

    models_openrouter_api_key: str | None = None
    models_anthropic_api_key: str | None = None
    models_openai_api_key: str | None = None
    github_token: str | None = None
    github_webhook_secret: str | None = None  # HMAC-SHA256 verify of inbound webhooks (WP 9.1)
    notify_telegram_bot_token: str | None = None
    notify_discord_webhook_url: str | None = None
    notify_slack_bot_token: str | None = None
    notify_slack_signing_secret: str | None = None  # request signing for /api/webhooks/slack
    # WP 12.5: secrets.toml "[web] api_key = …" flattens to web_api_key
    # (load_secrets). Fallback when settings.web.api_key is empty.
    web_api_key: str | None = None
    redaction_secret_env_names: list[str] = Field(
        default_factory=lambda: [
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "TELEGRAM_BOT_TOKEN",
        ]
    )

    def __repr__(self) -> str:
        # Mask every credential field; the non-secret denylist stays readable.
        parts = [f"{k}=***" for k, v in self.__dict__.items() if isinstance(v, str)]
        parts.append(f"redaction_secret_env_names={len(self.redaction_secret_env_names)} entries")
        return f"Secrets({', '.join(parts)})"

    __str__ = __repr__

    def get(self, name: str) -> str | None:
        return getattr(self, name, None)


DEFAULT_SECRETS_PATH = Path("~/.config/girder/secrets.toml").expanduser()

# Explicit config path for the TOML settings source (see load_settings).
# Values: Path (use this file) | "none" (resolved: no file) | None (walk up from CWD).
_TOML_PATH: ContextVar[Path | str | None] = ContextVar("girder_toml_path", default=None)


class _TomlSettingsSource(PydanticBaseSettingsSource):
    """Lowest-priority settings source reading girder.toml.

    Path resolution: explicit ContextVar (set by load_settings) > walk-up
    from CWD. Placed *after* the env source so environment always wins.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        explicit = _TOML_PATH.get()
        if isinstance(explicit, str):  # sentinel: caller resolved "no toml file"
            return {}
        path = explicit or _find_project_toml()
        if path is None:
            return {}
        data = tomllib.loads(path.read_text())
        return {k: v for k, v in data.items() if k in self.settings_cls.model_fields}


class SecretsPermissionError(RuntimeError):
    """The secrets file is readable/writable by group or others (impl-plan §3.2)."""


def load_secrets(path: Path | None = None) -> Secrets:
    """Load secrets from TOML; missing file yields an empty (but usable) model.

    impl-plan §3.2: the secrets file must be owner-only (mode 0600, host-only).
    A permissive file is *refused*, not merely warned about — credentials must
    never be group/world readable.
    """
    path = (path or DEFAULT_SECRETS_PATH).expanduser()
    if not path.is_file():
        return Secrets()
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise SecretsPermissionError(
            f"{path} is mode {mode:o}; the secrets file must be owner-only (0600)"
            f" — run: chmod 600 {path}"
        )
    data = tomllib.loads(path.read_text())
    # Accept both table form ([github] token = "…") and flat keys (github_token = "…").
    flat: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                flat.setdefault(f"{k}_{kk}", vv)
        else:
            flat[k] = v
    return Secrets(**{k: v for k, v in flat.items() if k in Secrets.model_fields})


def _find_project_toml(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default CWD) looking for girder.toml."""
    p = (start or Path.cwd()).resolve()
    for candidate in (p, *p.parents):
        f = candidate / "girder.toml"
        if f.is_file():
            return f
    return None


def load_settings(
    config_path: Path | None = None,
    *,
    start_dir: Path | None = None,
) -> Settings:
    """Load settings with precedence: environment > girder.toml > defaults.

    Environment overrides use the ``GIRDER_`` prefix and ``__`` nesting, e.g.
    ``GIRDER_BUDGET__RUN_CAP_USD=9.5``.
    """
    if config_path is not None:
        token: contextvars.Token[Path | str | None] | None = _TOML_PATH.set(config_path)
    elif start_dir is not None:
        found = _find_project_toml(start_dir)
        token = _TOML_PATH.set(found if found is not None else "none")
    else:
        token = None
    try:
        settings = Settings()
        if not settings.models.roles:
            # impl-plan §6.1 ("Validates: … at least one model role per tier",
            # issue 16): an empty registry must be a hard configuration error,
            # not a warning — every sprint's pipeline calls all three roles.
            # The CLI's documented test/dev opt-out keeps working role-less.
            if os.environ.get("GIRDER_ALLOW_NO_ROLES") != "1":
                raise ValueError(
                    "no model roles configured: declare [[models.roles]]"
                    " (tier1..tier3) in girder.toml"
                    " (set GIRDER_ALLOW_NO_ROLES=1 to run role-less)"
                )
            log.warning(
                "no model roles configured; model calls will fail —"
                " declare [[models.roles]] (tier1..tier3) in girder.toml"
            )
        return settings
    finally:
        if token is not None:
            _TOML_PATH.reset(token)
