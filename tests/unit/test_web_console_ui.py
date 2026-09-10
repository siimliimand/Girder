"""WS-08 web console UI tests (WP 12.1 dark mode, WP 12.2 live DAG, WP 12.6 diff viewer).

Template-level assertions against the rendered pages (existing console-suite
style), plus a node-based unit test of ``static/diff.js`` (the spec allows a
lightweight JS harness; node is the runtime already on dev machines, and the
test skips cleanly when node is absent).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import TaskType
from tests.unit.test_console_api import make_app, make_client, make_project, make_run

STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "girder" / "api" / "static"
)


# --------------------------------------------------- WP 12.1 dark mode toggle


async def test_base_layout_has_theme_toggle_and_pref_hook(tmp_path: Path) -> None:
    """base.html ships the manual toggle button, reads the persisted
    localStorage choice before first paint, and supports data-theme on <html>."""
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        resp = await client.get(f"/runs/{r.id}")
        assert resp.status_code == 200
        assert 'id="theme-toggle"' in resp.text
        assert 'localStorage.getItem("girder-theme")' in resp.text
        assert 'localStorage.setItem("girder-theme"' in resp.text
        assert 'data-theme' in resp.text
        assert "/static/style.css" in resp.text


async def test_stylesheet_defines_theme_variables(tmp_path: Path) -> None:
    """style.css exposes the spec palette as custom properties in light and
    dark scopes, and the manual [data-theme] override exists."""
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    for var in ("--bg", "--bg-surface", "--border", "--text", "--text-muted",
                "--accent", "--accent-dark", "--red", "--amber", "--green"):
        assert css.count(var) >= 3, var  # :root, dark media query, data-theme
    assert "#4da3ff" in css  # dark accent from the spec palette
    assert "@media (prefers-color-scheme: dark)" in css
    assert '[data-theme="dark"]' in css
    assert '[data-theme="light"]' in css


# ------------------------------------------------------ WP 12.2 live DAG hooks


async def test_run_page_tasks_carry_task_id_and_status_badge(tmp_path: Path) -> None:
    """Every task div has data-task-id, the status sits in a
    .task-status-badge span, and the page subscribes to SSE state events."""
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        wave = await repo.create_wave(db, r.id, 0)
        task = await repo.create_task(db, wave.id, 1, "T1", TaskType.CODE_CHANGE)

        resp = await client.get(f"/runs/{r.id}")
        assert resp.status_code == 200
        assert f'data-task-id="{task.id}"' in resp.text
        assert 'class="badge task-status-badge"' in resp.text
        assert 'es.addEventListener("state"' in resp.text
        assert 'p.entity !== "task"' in resp.text
        # badge-flip wiring targets the real fsm payload fields
        assert 'p.id' in resp.text and 'p.to' in resp.text


async def test_run_page_eventsource_carries_api_key(tmp_path: Path) -> None:
    """EventSource cannot send headers (WP 12.5): the run page must append the
    localStorage key as ?api_key= to the SSE URL, skipping it when unset."""
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)

        resp = await client.get(f"/runs/{r.id}")
        assert resp.status_code == 200
        assert 'localStorage.getItem("girder_api_key")' in resp.text
        assert '"&api_key=" + encodeURIComponent(key)' in resp.text
        assert 'if (key)' in resp.text
        assert 'new EventSource(esUrl)' in resp.text
        # base.html's GET-form guard: never leak the key into a GET URL
        assert 'toLowerCase() === "get"' in resp.text


# ---------------------------------------------------------- WP 12.6 diff viewer


async def test_diff_blocks_render_through_diff_js(tmp_path: Path) -> None:
    """Run page and post-mortem emit <pre class="diff-src"> plus the diff.js
    include instead of bare <pre> diffs; diff.js is served as a static asset."""
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        wave = await repo.create_wave(db, r.id, 0)
        task = await repo.create_task(db, wave.id, 1, "T1", TaskType.CODE_CHANGE)
        attempt = await repo.create_attempt(db, task.id, base_commit="deadbeef")
        await repo.insert_attempt_diff(
            db,
            attempt_id=attempt.id,
            task_id=task.id,
            run_id=r.id,
            base_commit="deadbeef",
            head_commit="c0ffee0",
            diff_redacted="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-old\n+new\n",
        )

        run_page = await client.get(f"/runs/{r.id}")
        assert run_page.status_code == 200
        assert '<pre class="diff-src">' in run_page.text
        assert "/static/diff.js" in run_page.text

        pm = await client.get(f"/runs/{r.id}/postmortem")
        assert pm.status_code == 200
        assert '<pre class="diff-src">' in pm.text
        assert "/static/diff.js" in pm.text

        asset = await client.get("/static/diff.js")
        assert asset.status_code == 200
        assert "GirderDiff" in asset.text


_DIFF_HARNESS = r"""
const assert = require("assert");
const GirderDiff = require(process.argv[2]);

function El(tag) {
  this.tagName = tag;
  this.children = [];
  this.className = "";
  this.attrs = {};
  this._text = "";
}
Object.defineProperty(El.prototype, "textContent", {
  get() { return this._text; },
  set(v) { this._text = String(v); },
});
El.prototype.appendChild = function (c) { this.children.push(c); return c; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
const doc = { createElement: (t) => new El(t) };

const diff = [
  "diff --git a/x.py b/x.py",
  "--- a/x.py",
  "+++ b/x.py",
  "@@ -1,3 +1,3 @@",
  " keep",
  "-gone",
  "+here",
].join("\n");

const rows = GirderDiff.parseUnified(diff);
assert.deepStrictEqual(rows.map((r) => r.type),
                       ["meta", "meta", "meta", "hunk", "ctx", "del", "add"]);

const table = GirderDiff.buildTable(rows, doc);
assert.strictEqual(table.className, "diff-table");
assert.strictEqual(table.children.length, 1);           // one tbody
const trs = table.children[0].children;
assert.strictEqual(trs.length, 7);
assert.strictEqual(trs[0].className, "diff-meta");
assert.strictEqual(trs[3].className, "diff-hunk");
assert.strictEqual(trs[4].className, "diff-ctx");
assert.strictEqual(trs[5].className, "diff-del");
assert.strictEqual(trs[6].className, "diff-add");
// line numbers: old col (cells[0]), new col (cells[1]); meta/hunk have none
assert.strictEqual(trs[4].children[0].textContent, "1");
assert.strictEqual(trs[4].children[1].textContent, "1");
assert.strictEqual(trs[5].children[0].textContent, "2");
assert.strictEqual(trs[5].children[1].textContent, "");
assert.strictEqual(trs[6].children[0].textContent, "");
assert.strictEqual(trs[6].children[1].textContent, "2");
// deleted/added text is the payload without the +/- prefix
assert.strictEqual(trs[5].children[2].textContent, "gone");
assert.strictEqual(trs[6].children[2].textContent, "here");

// hunk headers reset numbering: second hunk starts at its declared line
const diff2 = "@@ -10,1 +20,1 @@\n a\n b\n";
const rows2 = GirderDiff.parseUnified(diff2);
assert.strictEqual(rows2[1].oldNo, 10);
assert.strictEqual(rows2[1].newNo, 20);
assert.strictEqual(rows2[2].oldNo, 11);
assert.strictEqual(rows2[2].newNo, 21);

// empty input renders an (empty) table, not a crash
assert.strictEqual(GirderDiff.parseUnified("").length, 0);
assert.strictEqual(GirderDiff.buildTable([], doc).children[0].children.length, 0);

console.log("diff.js harness OK");
"""


def test_diff_js_unified_diff_to_table() -> None:
    """diff.js parses a unified diff into a two-column line-numbered table
    with diff-add / diff-del / diff-ctx / diff-hunk / diff-meta classes."""
    if shutil.which("node") is None:  # pragma: no cover - CI without node
        pytest.skip("node not available for the diff.js harness")
    script = Path("/tmp") / f"ws08-diff-harness-{id(_DIFF_HARNESS)}.js"
    script.write_text(_DIFF_HARNESS, encoding="utf-8")
    try:
        proc = subprocess.run(
            ["node", str(script), str(STATIC_DIR / "diff.js")],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script.unlink(missing_ok=True)
    assert proc.returncode == 0, f"diff.js harness failed:\n{proc.stdout}\n{proc.stderr}"
    assert "diff.js harness OK" in proc.stdout
