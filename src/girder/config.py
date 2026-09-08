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
import tomllib
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import PydanticBaseSettingsSource

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


class SandboxNetwork(BaseModel):
    network: str = "none"  # none | private
    memory: str = "4g"
    cpus: float = 2.0
    pids_limit: int = 512
    runtime: str = "podman"  # podman | docker (identical flag surface)


class GithubConfig(BaseModel):
    remote: str = "origin"
    pr_template: str | None = None


class NotifyConfig(BaseModel):
    channels: list[str] = Field(default_factory=list)  # subset of {"telegram","discord"}
    telegram_chat_id: str | None = None


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


class WebConfig(BaseModel):
    """Approval web UI bind address (impl-plan §10): loopback-only by default."""

    host: str = "127.0.0.1"
    port: int = 8787


class ModelsConfig(BaseModel):
    roles: list[ModelRole] = Field(default_factory=list)
    request_timeout_s: float = 300.0  # per-call HTTP timeout

    @model_validator(mode="after")
    def _check_tiers(self) -> ModelsConfig:
        # Sprint 1 runs zero model calls, so an empty registry is tolerated;
        # as soon as any role is declared the tier1-tier3 set must be complete.
        if self.roles:
            have = {r.role for r in self.roles}
            missing = {"tier1", "tier2", "tier3"} - have
            if missing:
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
        [redaction]
        secret_env_names = ["GITHUB_TOKEN", …]

    Flat top-level keys using the same field names are also accepted.
    """

    models_openrouter_api_key: str | None = None
    models_anthropic_api_key: str | None = None
    models_openai_api_key: str | None = None
    github_token: str | None = None
    notify_telegram_bot_token: str | None = None
    notify_discord_webhook_url: str | None = None
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


def load_secrets(path: Path | None = None) -> Secrets:
    """Load secrets from TOML; missing file yields an empty (but usable) model."""
    path = (path or DEFAULT_SECRETS_PATH).expanduser()
    if not path.is_file():
        return Secrets()
    # impl-plan §3.2: secrets must be owner-only (0600) — warn, don't refuse.
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        log.warning(
            "%s is mode %o; secrets may be readable by others — run: chmod 600 %s",
            path,
            mode,
            path,
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
        return Settings()
    finally:
        if token is not None:
            _TOML_PATH.reset(token)
