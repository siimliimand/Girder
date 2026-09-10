"""WP 13.2 ``girder prune`` and WP 13.3 ``girder validate`` CLI behaviours.

Prune tests use the shared migrated ``db`` fixture and seed child rows with raw
SQL (bypassing the FSM/repo layer on purpose, mirroring conftest's
``seed_run_status``). Validate tests build :class:`Settings` objects directly
and monkeypatch the filesystem/process probes.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from girder.cli import (
    PruneReport,
    _check_api_keys,
    _check_runtime_and_image,
    _check_secrets,
    _check_test_directories,
    cmd_validate,
    collect_validation_failures,
    prune_runs,
)
from girder.config import Secrets, Settings
from girder.db import repo
from tests.conftest import seed_run_status

# ------------------------------------------------------------------ prune helpers


async def _seed_old_run(db, run_id: str, status: str, days_old: int) -> None:  # type: ignore[no-untyped-def]
    """Insert a run with a full child-row chain (wave/task/attempt/tool/event)."""
    project = await repo.create_project(db, f"p-{run_id}", "/tmp/somewhere")
    run = await repo.create_run(db, project.id, "intent", f"run/{run_id}", 5.0)
    await seed_run_status(db, run.id, status)
    await db.execute(
        "UPDATE runs SET created_at = datetime('now', ?) WHERE id = ?",
        (f"-{days_old} days", run.id),
    )
    await db.execute(
        "INSERT INTO waves (id, run_id, sequence_order, status) VALUES (?, ?, 0, 'done')",
        (f"wave-{run_id}", run.id),
    )
    await db.execute(
        "INSERT INTO tasks (id, wave_id, seq, title, task_type, spec_slice_md, status)"
        " VALUES (?, ?, 0, 't', 'code_change', 'spec', 'completed')",
        (f"task-{run_id}", f"wave-{run_id}"),
    )
    await db.execute(
        "INSERT INTO attempts (id, task_id, attempt_num, base_commit, status)"
        " VALUES (?, ?, 1, 'c0ffee', 'succeeded')",
        (f"att-{run_id}", f"task-{run_id}"),
    )
    await db.execute(
        "INSERT INTO tool_calls (attempt_id, ts, tool_name, input_json)"
        " VALUES (?, datetime('now'), 'edit_file', '{}')",
        (f"att-{run_id}",),
    )
    await db.execute(
        "INSERT INTO agent_events (ts, event_type, run_id, payload_json)"
        " VALUES (datetime('now'), 'note', ?, '{}')",
        (run.id,),
    )
    await db.execute(
        "INSERT INTO steering_events (run_id, kind, payload_json, created_at)"
        " VALUES (?, 'pause', '{}', datetime('now'))",
        (run.id,),
    )
    await db.execute(
        "INSERT INTO integrity_violations (run_id, kind, detail_json, ts)"
        " VALUES (?, 'scope_violation', '{}', datetime('now'))",
        (run.id,),
    )
    await db.execute(
        "INSERT INTO notifications_log (channel, payload_redacted, status, ts, run_id)"
        " VALUES ('telegram', 'payload', 'sent', datetime('now'), ?)",
        (run.id,),
    )
    return run.id


async def _child_row_counts(db, table: str) -> int:  # type: ignore[no-untyped-def]
    row = await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")
    return int(row["n"]) if row is not None else 0


# ------------------------------------------------------------------------- prune


async def test_prune_dry_run_deletes_nothing_and_reports_accurately(db) -> None:  # type: ignore[no-untyped-def]
    rid = await _seed_old_run(db, "old-merged", "merged", days_old=200)
    report = await prune_runs(db, older_than_days=90, dry_run=True)
    assert report.run_ids == [rid]
    assert report.child_rows == {
        "worktrees": 0,
        "redaction_log": 0,
        "tool_calls": 1,
        "token_usage": 0,
        "attempt_prompts": 0,
        "attempt_diffs": 0,
        "attempts": 1,
        "spec_amendments": 0,
        "tasks": 1,
        "waves": 1,
        "ci_check_results": 0,
        "agent_events": 1,
        "integrity_violations": 1,
        "steering_events": 1,
        "notifications_log": 1,
    }
    # nothing actually removed
    assert await _child_row_counts(db, "runs") == 1
    assert await _child_row_counts(db, "tool_calls") == 1
    assert await _child_row_counts(db, "waves") == 1


async def test_prune_deletes_only_terminal_runs_past_horizon(db) -> None:  # type: ignore[no-untyped-def]
    old_merged = await _seed_old_run(db, "old-merged", "merged", days_old=200)
    old_failed = await _seed_old_run(db, "old-failed", "failed", days_old=200)
    old_aborted = await _seed_old_run(db, "old-aborted", "aborted", days_old=200)
    old_active = await _seed_old_run(db, "old-active", "active", days_old=200)
    old_budget = await _seed_old_run(db, "old-budget", "budget_exhausted", days_old=200)
    new_merged = await _seed_old_run(db, "new-merged", "merged", days_old=1)
    report = await prune_runs(db, older_than_days=90, dry_run=False)
    assert sorted(report.run_ids) == sorted([old_aborted, old_failed, old_merged])
    remaining = sorted(r["id"] for r in await db.fetchall("SELECT id FROM runs"))
    assert remaining == sorted([new_merged, old_active, old_budget])


async def test_prune_cascade_removes_all_child_rows(db) -> None:  # type: ignore[no-untyped-def]
    gone = await _seed_old_run(db, "gone", "merged", days_old=200)
    kept = await _seed_old_run(db, "kept", "failed", days_old=1)
    report = await prune_runs(db, older_than_days=90, dry_run=False)
    assert report.run_ids == [gone]
    assert kept not in report.run_ids
    # the pruned run's rows are gone everywhere; each table keeps exactly the
    # retained run's single row (both runs were seeded identically)
    for table in (
        "runs",
        "waves",
        "tasks",
        "attempts",
        "tool_calls",
        "agent_events",
        "steering_events",
        "integrity_violations",
        "notifications_log",
    ):
        assert await _child_row_counts(db, table) == 1, table


def test_prune_report_summary_verbs() -> None:
    report = PruneReport(run_ids=["r1"], child_rows={"waves": 2})
    assert "would delete 1 run(s)" in report.summary(dry_run=True)
    assert "deleted 1 run(s)" in report.summary(dry_run=False)
    assert "waves: 2" in report.summary(dry_run=False)


# ---------------------------------------------------------------------- validate


def _settings(**overrides: object) -> Settings:
    roles = [
        {"role": "tier1", "provider": "anthropic", "model": "m1", "price_in_per_mtok": 1.0,
         "price_out_per_mtok": 2.0},
        {"role": "tier2", "provider": "openrouter", "model": "m2", "price_in_per_mtok": 1.0,
         "price_out_per_mtok": 2.0},
        {"role": "tier3", "provider": "openai", "model": "m3", "price_in_per_mtok": 1.0,
         "price_out_per_mtok": 2.0},
    ]
    payload: dict[str, object] = {
        "project": {"stack": "python-3.12", "test_directories": ["tests"]},
        "models": {"roles": roles},
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


def _ctx(tmp_path: Path, settings: Settings | None, **kw: object) -> SimpleNamespace:
    (tmp_path / "tests").mkdir(exist_ok=True)
    base: dict[str, object] = {
        "settings": settings,
        "config_error": None,
        "secrets": Secrets(),
        "secrets_error": None,
        "config_root": tmp_path,
        "stack": settings.project.stack if settings is not None else "",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_validate_bad_stack(tmp_path: Path) -> None:
    # Settings must stay constructible: strict registry validation in config.py
    # (stacks commit) rejects unknown stacks at the model layer; the CLI-level
    # check under test is driven via the `stack` ctx override instead.
    failures = collect_validation_failures(
        _ctx(tmp_path, _settings(project={"stack": "python-3.12", "test_directories": ["tests"]}),
             stack="ruby-3.4"),
        check_sandbox=False,
    )
    assert any("project.stack" in f and "ruby-3.4" in f for f in failures)


def test_validate_missing_tier_and_zero_price(tmp_path: Path) -> None:
    # Settings' own validator refuses a partial role registry (issue 16), so
    # build the shape validate must diagnose directly.
    settings = SimpleNamespace(
        models=SimpleNamespace(
            roles=[
                SimpleNamespace(role="tier1", model="m1", provider="anthropic",
                                price_in_per_mtok=0.0, price_out_per_mtok=2.0),
                SimpleNamespace(role="tier3", model="m3", provider="openai",
                                price_in_per_mtok=1.0, price_out_per_mtok=2.0),
            ]
        ),
        project=SimpleNamespace(stack="python-3.12", test_directories=["tests"]),
    )
    failures = collect_validation_failures(_ctx(tmp_path, settings, stack="python-3.12"),
                                           check_sandbox=False)
    assert any("missing tier(s) tier2" in f for f in failures)
    assert any("tier1" in f and "non-zero prices" in f for f in failures)


def test_validate_secrets_file_mode_0644(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text("[models]\nanthropic_api_key = 'k'\n")
    os.chmod(secrets_file, 0o644)
    monkeypatch.setattr("girder.cli.DEFAULT_SECRETS_PATH", secrets_file)
    monkeypatch.setattr("girder.config.DEFAULT_SECRETS_PATH", secrets_file)
    failures = _check_secrets()
    assert any("644" in f and "chmod 600" in f for f in failures)


def test_validate_missing_api_key_for_provider(tmp_path: Path) -> None:
    failures = _check_api_keys(_settings(), Secrets())
    assert any("anthropic" in f for f in failures)
    assert any("openrouter" in f for f in failures)
    assert any("openai" in f for f in failures)


def test_validate_missing_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("girder.cli.shutil.which", lambda name: None)
    failures = _check_runtime_and_image("python-3.12")
    assert any("neither podman nor docker" in f for f in failures)


def test_validate_missing_runner_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("girder.cli.shutil.which", lambda name: "podman")

    def fake_run(argv: list[str], **kw: object) -> SimpleNamespace:
        assert argv[:2] == ["podman", "images"]
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr("girder.cli.subprocess.run", fake_run)
    failures = _check_runtime_and_image("python-3.12")
    assert any("girder-runner:python-3.12 not found" in f for f in failures)


def test_validate_runner_image_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # _detect_runtime prefers podman; hide it so the docker fallback is picked.
    monkeypatch.setattr(
        "girder.cli.shutil.which", lambda name: "docker" if name == "docker" else None
    )

    def fake_run(argv: list[str], **kw: object) -> SimpleNamespace:
        assert argv[0] == "docker"
        return SimpleNamespace(stdout="abc123\n", returncode=0)

    monkeypatch.setattr("girder.cli.subprocess.run", fake_run)
    assert _check_runtime_and_image("python-3.12") == []


def test_validate_missing_test_directory(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    failures = _check_test_directories(_settings(), tmp_path)
    assert failures == []  # conftest _ctx created tmp_path/tests
    failures = _check_test_directories(
        _settings(project={"stack": "python-3.12", "test_directories": ["nope"]}), tmp_path
    )
    assert any("'nope'" in f for f in failures)


def test_validate_collects_all_failures_not_just_first(tmp_path: Path) -> None:
    # Same as test_validate_bad_stack: unknown stack is enforced by Settings
    # itself now, so keep the model valid and test the CLI check via ctx.
    settings = Settings.model_validate(
        {
            "project": {"stack": "python-3.12", "test_directories": ["tests"]},
            "models": {"roles": []},
        }
    )
    failures = collect_validation_failures(
        _ctx(tmp_path, settings, stack="ruby-3.4", secrets_error="bad toml"),
        check_sandbox=False,
    )
    assert len(failures) >= 3  # empty roles + bad stack + secrets error + more
    assert any("tier" in f for f in failures)
    assert any("project.stack" in f for f in failures)
    assert any("bad toml" in f for f in failures)


async def test_cmd_validate_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "girder.toml"
    config.write_text(
        'project.stack = "python-3.12"\n'
        "[[models.roles]]\n"
        'role = "tier1"\nprovider = "anthropic"\nmodel = "m"\n'
        "price_in_per_mtok = 1.0\nprice_out_per_mtok = 1.0\n"
        "[[models.roles]]\n"
        'role = "tier2"\nprovider = "anthropic"\nmodel = "m"\n'
        "price_in_per_mtok = 1.0\nprice_out_per_mtok = 1.0\n"
        "[[models.roles]]\n"
        'role = "tier3"\nprovider = "anthropic"\nmodel = "m"\n'
        "price_in_per_mtok = 1.0\nprice_out_per_mtok = 1.0\n"
    )
    (tmp_path / "tests").mkdir(exist_ok=True)
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text('[models]\nanthropic_api_key = "sk"\n')
    os.chmod(secrets_file, 0o600)
    monkeypatch.setattr("girder.cli.DEFAULT_SECRETS_PATH", secrets_file)
    monkeypatch.setattr("girder.config.DEFAULT_SECRETS_PATH", secrets_file)
    monkeypatch.setattr(
        "girder.cli._check_runtime_and_image", lambda stack: []
    )  # no container runtime in CI
    monkeypatch.setenv("GIRDER_ALLOW_NO_ROLES", "1")  # keep unrelated loaders quiet
    args = SimpleNamespace(config_path=str(config))

    # Invalid on purpose: openrouter key missing is fine (no openrouter role),
    # so this config plus stubs should pass.
    assert await cmd_validate(args) == 0
    assert "OK" in capsys.readouterr().out

    # Now break the stack: exit 1 with a human-friendly message.
    config.write_text(config.read_text().replace("python-3.12", "ruby-3.4"))
    assert await cmd_validate(args) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "ruby-3.4" in out
