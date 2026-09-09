"""WP-1.2 — Settings: defaults, TOML merge order, env overrides, secrets repr safety."""

import logging
from pathlib import Path

import pytest

from girder.config import (
    DEFAULT_SECRETS_PATH,
    ModelsConfig,
    Secrets,
    SecretsPermissionError,
    load_secrets,
    load_settings,
)


def test_defaults_when_no_toml_found(tmp_path: Path) -> None:
    settings = load_settings(start_dir=tmp_path)
    assert settings.budget.run_cap_usd == 5.00
    assert settings.limits.attempt_max_turns == 20
    assert settings.limits.attempt_wallclock_s == 600
    assert settings.limits.task_max_attempts == 3
    assert settings.autonomy.tier == 0  # every project starts supervised (§2.3)
    assert settings.sandbox.runtime == "podman"
    assert settings.models.roles == []


def test_toml_overrides_defaults(tmp_path: Path) -> None:
    cfg = tmp_path / "girder.toml"
    cfg.write_text(
        '[budget]\nrun_cap_usd = 9.5\n[project]\nname = "myapp"\nstrict_read_scope = true\n'
    )
    settings = load_settings(config_path=cfg)
    assert settings.budget.run_cap_usd == 9.5
    assert settings.project.name == "myapp"
    assert settings.project.strict_read_scope is True
    # untouched sections keep defaults
    assert settings.limits.attempt_max_turns == 20


def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "girder.toml"
    cfg.write_text("[budget]\nrun_cap_usd = 9.5\n")
    monkeypatch.chdir(tmp_path)  # pick up the toml via directory walk-up
    monkeypatch.setenv("GIRDER_BUDGET__RUN_CAP_USD", "12.0")
    settings = load_settings()
    assert settings.budget.run_cap_usd == 12.0


def test_toml_found_by_directory_walkup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "girder.toml"
    cfg.write_text('[project]\nname = "walkup"\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    settings = load_settings()
    assert settings.project.name == "walkup"


def test_model_roles_must_be_complete_when_declared() -> None:
    with pytest.raises(ValueError, match="tier1"):
        ModelsConfig(roles=[{"role": "tier2", "provider": "x", "model": "y"}])
    ok = ModelsConfig(
        roles=[
            {"role": "tier1", "provider": "x", "model": "a"},
            {"role": "tier2", "provider": "x", "model": "b"},
            {"role": "tier3", "provider": "x", "model": "c"},
        ]
    )
    assert len(ok.roles) == 3


def test_model_role_token_prices() -> None:
    role = dict(
        role="tier1",
        provider="openrouter",
        model="m",
        price_in_per_mtok=3.0,
        price_out_per_mtok=15.0,
    )
    from girder.config import ModelRole

    r = ModelRole(**role)
    assert r.price_in_per_tok == pytest.approx(3e-6)
    assert r.price_out_per_tok == pytest.approx(15e-6)


def test_settings_repr_contains_no_secret_values(tmp_path: Path) -> None:
    settings = load_settings(start_dir=tmp_path)
    r = repr(settings)
    assert "Secrets" not in r


def test_secrets_repr_masks_values() -> None:
    secrets = Secrets(
        github_token="ghp_supersecretvalue123456",
        notify_telegram_bot_token="1234:ABCDtelegramtoken",
        redaction_secret_env_names=["GITHUB_TOKEN"],
    )
    r = repr(secrets)
    assert "ghp_supersecret" not in r
    assert "ABCDtelegramtoken" not in r
    assert r.count("***") == 2
    assert "redaction_secret_env_names=1 entries" in r
    assert str(secrets) == r


def test_load_secrets_missing_file(tmp_path: Path) -> None:
    secrets = load_secrets(tmp_path / "nope.toml")
    assert secrets.github_token is None
    assert "GITHUB_TOKEN" in secrets.redaction_secret_env_names


def test_load_secrets_flat_and_table_forms(tmp_path: Path) -> None:
    f = tmp_path / "secrets.toml"
    f.write_text(
        'github_token = "ghp_flat"\n'
        "[github]\ntoken = 'ghp_table'\n"
        '[notify]\ntelegram_bot_token = "tg-xyz"\n'
    )
    f.chmod(0o600)  # impl-plan §3.2: permissive files are refused
    secrets = load_secrets(f)
    assert secrets.github_token == "ghp_flat"  # flat wins over table form
    assert secrets.notify_telegram_bot_token == "tg-xyz"
    assert DEFAULT_SECRETS_PATH.name == "secrets.toml"


def test_load_secrets_0600_loads_cleanly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    f = tmp_path / "secrets.toml"
    f.write_text('github_token = "ghp_x"\n')
    f.chmod(0o600)
    with caplog.at_level(logging.WARNING, logger="girder.config"):
        secrets = load_secrets(f)
    assert secrets.github_token == "ghp_x"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_empty_model_registry_warns_at_load(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # issue 16: an empty registry must not silently pass — warn that model
    # calls will fail.
    with caplog.at_level(logging.WARNING, logger="girder.config"):
        settings = load_settings(start_dir=tmp_path)
    assert settings.models.roles == []
    messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no model roles configured" in m for m in messages)


def test_partial_registry_still_refused() -> None:
    with pytest.raises(ValueError, match="tier3"):
        ModelsConfig(
            roles=[
                {"role": "tier1", "provider": "x", "model": "a"},
                {"role": "tier2", "provider": "x", "model": "b"},
            ]
        )


def test_load_secrets_permissive_refused(tmp_path: Path) -> None:
    # impl-plan §3.2 (issue 15): a group/world-accessible secrets file must be
    # refused with a fix-it message, not loaded with a warning.
    f = tmp_path / "secrets.toml"
    f.write_text('github_token = "ghp_x"\n')
    f.chmod(0o644)
    with pytest.raises(SecretsPermissionError, match=r"chmod 600"):
        load_secrets(f)


def test_load_secrets_group_writable_refused(tmp_path: Path) -> None:
    f = tmp_path / "secrets.toml"
    f.write_text('github_token = "ghp_x"\n')
    f.chmod(0o660)
    with pytest.raises(SecretsPermissionError, match="660"):
        load_secrets(f)


def test_specs_and_web_defaults(tmp_path: Path) -> None:
    settings = load_settings(start_dir=tmp_path)
    assert settings.specs.cli is False
    assert settings.specs.cli_bin == "openspec"
    assert settings.web.host == "127.0.0.1"  # impl-plan §10: loopback bind
    assert settings.web.port == 8787


def test_model_role_base_url_defaults_to_none() -> None:
    from girder.config import ModelRole

    r = ModelRole(role="tier1", provider="openrouter", model="m")
    assert r.base_url is None


def test_models_config_accepts_request_timeout_s() -> None:
    from girder.config import ModelsConfig

    cfg = ModelsConfig(request_timeout_s=12.5)
    assert cfg.request_timeout_s == 12.5


def test_budget_cap_must_be_positive(tmp_path: Path) -> None:
    cfg = tmp_path / "girder.toml"
    cfg.write_text("[budget]\nrun_cap_usd = 0\n")
    with pytest.raises(ValueError, match=r"budget\.run_cap_usd"):
        load_settings(config_path=cfg)
    cfg.write_text("[budget]\nrun_cap_usd = -1.0\n")
    with pytest.raises(ValueError, match=r"budget\.run_cap_usd"):
        load_settings(config_path=cfg)


@pytest.mark.parametrize(
    "key",
    [
        "attempt_wallclock_s",
        "attempt_max_turns",
        "task_max_attempts",
        "ci_fix_attempts",
        "conflict_resolution_attempts",
        "tool_output_max_lines",
        "tool_output_max_tokens",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_limits_must_be_positive(tmp_path: Path, key: str, value: int) -> None:
    cfg = tmp_path / "girder.toml"
    cfg.write_text(f"[limits]\n{key} = {value}\n")
    with pytest.raises(ValueError, match=f"limits\\.{key}"):
        load_settings(config_path=cfg)


def test_test_directories_must_be_non_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "girder.toml"
    cfg.write_text('[project]\ntest_directories = []\n')
    with pytest.raises(ValueError, match=r"project\.test_directories"):
        load_settings(config_path=cfg)


def test_good_fixture_config_still_loads(tmp_path: Path) -> None:
    cfg = tmp_path / "girder.toml"
    cfg.write_text(
        "[budget]\nrun_cap_usd = 9.5\n"
        "[limits]\nattempt_max_turns = 5\n"
        '[project]\ntest_directories = ["tests", "spec"]\n'
    )
    settings = load_settings(config_path=cfg)
    assert settings.limits.attempt_max_turns == 5
    assert settings.project.test_directories == ["tests", "spec"]


def test_toml_roundtrips_specs_web_and_base_url(tmp_path: Path) -> None:
    cfg = tmp_path / "girder.toml"
    cfg.write_text(
        "[web]\nport = 9000\n"
        "[[models.roles]]\n"
        'role = "tier1"\nprovider = "openrouter"\nmodel = "a"\n'
        'base_url = "http://localhost:9999/v1"\n'
        "[[models.roles]]\n"
        'role = "tier2"\nprovider = "openrouter"\nmodel = "b"\n'
        "[[models.roles]]\n"
        'role = "tier3"\nprovider = "openrouter"\nmodel = "c"\n'
    )
    settings = load_settings(config_path=cfg)
    assert settings.web.port == 9000
    assert settings.web.host == "127.0.0.1"  # untouched key keeps default
    assert len(settings.models.roles) == 3
    assert settings.models.roles[0].base_url == "http://localhost:9999/v1"


def test_web_unix_socket_default_and_toml(tmp_path: Path) -> None:
    """impl-plan §10: the console may bind a UDS instead of host/port."""
    settings = load_settings(start_dir=tmp_path)
    assert settings.web.unix_socket is None  # default: TCP loopback bind

    cfg = tmp_path / "girder.toml"
    cfg.write_text('[web]\nunix_socket = "/run/girder/girder.sock"\n')
    settings = load_settings(config_path=cfg)
    assert settings.web.unix_socket == "/run/girder/girder.sock"
    # host/port keep their defaults; they are simply unused in UDS mode.
    assert settings.web.host == "127.0.0.1"
    assert settings.web.port == 8787
