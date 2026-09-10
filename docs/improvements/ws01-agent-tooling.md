# WS-01 — Agent Tooling Expansion

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 7 (WP 7.1 – WP 7.6) |
| **Status** | Implemented on `wave1/ws01-agent-tooling` — merge deferred (owns `tools.py`, which in-flight parallel work also edits; rebase onto the stacks-aware `tools.py` before merging) |
| **Wave** | **1** — can run in parallel with WS-03, WS-04, WS-05, WS-07A, WS-08, WS-09 |
| **Effort** | ~1 week · ~20 new unit tests |
| **Owned files** | `src/girder/agent/tools.py`, `tests/unit/test_agent_tools.py` |
| **Shared files** | none (see [Coordination](#coordination)) |

---

## Goal

Give agents a richer, more precise tool surface. This is the highest single-ROI change in the whole improvements plan: better tools eliminate the exploration spirals documented in the dogfood runs without requiring any model improvement.

## Current state (verified 2026-09-10)

`TOOL_SCHEMAS` in `src/girder/agent/tools.py` currently exposes **nine** tools:

`read_file`, `write_file`, `apply_patch`, `find_files`, `ripgrep`, `view_symbol_outline`, `run_command`, `mark_task_complete`, `request_spec_amendment`

This workstream adds **six** new tools (`edit_file`, `list_directory`, `git_status`, `git_diff`, `run_tests`, `search_symbols`) and audits the descriptions of the existing ones.

## Definition of Done

- All new tools pass **scope gating, redaction, and tool-call logging** through the existing pipeline — no tool bypasses the standard dispatch path.
- `mypy --strict` clean; full unit suite green; e2e **SC-01** still passes with the expanded tool surface.
- New unit tests in `tests/unit/test_agent_tools.py` cover each tool (see per-WP test notes).

**Pre-flight:** run the full test suite once and record the baseline count before starting.

---

## WP 7.1 — `edit_file` Tool (line-range replacement)

**Why:** `write_file` replaces the entire file. On a 600-line file an agent must re-transmit all 600 lines to change 5 of them, wasting tokens and introducing transcription errors. A line-targeted edit tool is the single biggest agent effectiveness gain possible with no model changes.

**Schema:**

```python
{
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Replace a contiguous block of lines in a file. "
            "Use view_symbol_outline + read_file first to identify exact line numbers. "
            "start_line and end_line are 1-indexed and inclusive. "
            "The replacement text replaces those lines exactly — "
            "do not include surrounding context lines."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path":         {"type": "string"},
                "start_line":   {"type": "integer", "description": "First line to replace (1-indexed, inclusive)"},
                "end_line":     {"type": "integer", "description": "Last line to replace (1-indexed, inclusive)"},
                "replacement":  {"type": "string",  "description": "New content for lines start_line..end_line"},
            },
            "required": ["path", "start_line", "end_line", "replacement"],
        },
    },
}
```

**Sandbox execution** via a Python one-liner (same base64-argv pattern as the existing `write_file` snippet — no stdin):

```python
_EDIT_SNIPPET = (
    "import sys, pathlib; "
    "p = pathlib.Path(sys.argv[1]); "
    "lines = p.read_text().splitlines(keepends=True); "
    "s, e = int(sys.argv[2]) - 1, int(sys.argv[3]); "
    "import base64; repl = base64.b64decode(sys.argv[4]).decode(); "
    "lines[s:e] = [repl] if repl.endswith('\\n') else [repl + '\\n']; "
    "p.write_text(''.join(lines))"
)
```

**Scope gate:** applies the same write-policy as `write_file` — the `path` argument determines the verdict.

**Tests:** edit middle, start, and end of a file; verify character-level accuracy (line endings preserved); verify out-of-range line numbers fail cleanly; verify scope violations are held.

---

## WP 7.2 — `list_directory` Tool

**Why:** Agents currently enumerate files by shelling out to `ls` or using `find_files` with a wildcard glob. A dedicated structured tool saves turns and produces token-efficient output.

**Schema:**

```python
{
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": (
            "List entries in a directory. Returns name, type (file/dir), "
            "and size for each entry. Depth controls recursion (1 = direct children only). "
            "Use this instead of 'ls' or 'find' when surveying a new area."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path":  {"type": "string", "default": "."},
                "depth": {"type": "integer", "default": 1, "description": "Max recursion depth (1–3)"},
            },
            "required": [],
        },
    },
}
```

**Execution:** compact Python one-liner using `os.walk` with a depth limit.
**Output format:** one line per entry, `[dir]` or `[file N bytes]` prefix. Truncated to `_MATCH_CAP` lines.
**Scope gate:** read-only — applies the `ALLOW_LOGGED` verdict for in-scope reads.

---

## WP 7.3 — `git_status` and `git_diff` Tools

**Why:** Agents call `run_command` with `git status` / `git diff` multiple times per attempt. Dedicated tools produce consistently structured output and bypass the `run_command` denylist screening overhead.

**`git_status`:** no parameters. Returns `git status --short` output.

**`git_diff` schema:**

```python
{
    "parameters": {
        "type": "object",
        "properties": {
            "path":  {"type": "string", "description": "Restrict diff to this file (optional)"},
            "staged":{"type": "boolean", "default": False},
        },
        "required": [],
    },
}
```

**Scope gate:** both read-only — apply `ALLOW_LOGGED` (they read the git object store, which is always in-scope).

---

## WP 7.4 — `run_tests` Tool

**Why:** Agents run pytest via `run_command`. Raw output is multi-hundred lines. A dedicated tool can: (a) run only relevant test files, (b) return structured pass/fail/error counts and per-test summaries, (c) clip output intelligently to the highest-signal parts (failure tracebacks, not passing dots).

**Schema:**

```python
{
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": (
            "Run the project test suite (or a subset) and return a structured summary. "
            "Prefer this over 'run_command' with pytest — it returns structured results "
            "and clips verbose passing output automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Test files or directories to run (empty = full suite)",
                },
                "keyword": {"type": "string", "description": "pytest -k filter expression"},
                "timeout_s": {"type": "number", "default": 120},
            },
            "required": [],
        },
    },
}
```

**Execution:** runs `pytest --tb=short --no-header -q [paths] [-k keyword] --junitxml=/tmp/.girder-run-tests.xml` inside the sandbox container, then parses the JUnit XML with the existing `parse_junit_xml` in `orchestrator/baseline.py` (verified present; returns `dict[str, PerTestResult]`) to produce:

```
PASSED: 42  FAILED: 2  ERROR: 0

FAILURES:
  test_api.py::test_rate_limit — AssertionError: expected 429, got 200
    File "src/api.py", line 47, in handle_request
      if self.limiter.check(key):
  test_api.py::test_headers — KeyError: 'X-Rate-Limit-Remaining'
```

The failure section is always shown in full; the passing summary is one line. This is ~10× more token-efficient than raw pytest output.

**Scope gate:** treated the same as `run_command` (write-policy on path args).

**Note for WS-02:** WS-02 (agent intelligence) will consume this tool's structured result for the scratchpad `test_results` field — return the summary in a parseable, stable format.

---

## WP 7.5 — `search_symbols` Tool (semantic code search, Phase 1)

**Why:** `ripgrep` is text search. Agents need to find *definitions* ("where is class RateLimiter defined?"), *usages* ("what calls handle_request?"), and *imports* ("what does module X export?"). Currently this takes 3–5 ripgrep calls.

**Schema:**

```python
{
    "type": "function",
    "function": {
        "name": "search_symbols",
        "description": (
            "Find symbol definitions or usages across the codebase. "
            "kind='definition' finds where a symbol is defined; "
            "kind='usage' finds all call sites; "
            "kind='export' lists what a module exports."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name (class, function, variable)"},
                "kind": {
                    "type": "string",
                    "enum": ["definition", "usage", "export"],
                    "default": "definition",
                },
                "path": {"type": "string", "description": "Restrict search to this path"},
            },
            "required": ["name"],
        },
    },
}
```

**Implementation strategy (two phases):**
1. **Phase 1 (this workstream):** intelligent ripgrep composition. `definition` → `rg "^(def|class|async def)\s+{name}\b"`. `usage` → `rg "\b{name}\s*\("`. `export` → `rg "__all__"` + parse.
2. **Phase 2:** replaced by WS-07B (WP 11.4) with `ast.parse` / `tree-sitter` when stack plugins land.

---

## WP 7.6 — Tool Description Audit

Review all existing tool descriptions in `TOOL_SCHEMAS` against actual dogfood behavior:

| Tool | Current issue | Fix |
|---|---|---|
| `read_file` | Already improved in Group C of the effectiveness plan | Verify shipped; close if done |
| `view_symbol_outline` | Says "Python" only but uses grep fallback for others — misleading | State language support clearly |
| `find_files` | Says "fd if available" but agent doesn't know if fd is available | Remove the implementation detail; say "find files matching glob" |
| `ripgrep` | Description says "rg if available" — same issue | Say "regex search across the worktree" |
| `run_command` | Denylist is not surfaced in the description | Add: "Denied commands: curl, wget, nc, ssh, git push, sudo, podman, docker, mount" |

---

## Coordination

- **WS-02** (agent intelligence, wave 2) will hook the tool-execution layer to record `files_read` / `files_written` / `test_results` for the structured scratchpad. Keep the tool dispatch a clean registry (name → handler) and keep `run_tests` output parseable.
- **WS-07B** (WP 11.4, wave 2) will replace `view_symbol_outline` internals with stack-plugin commands — do **not** change its schema or call signature in this workstream.

## Relevant resolution log decisions

- **R-SP7-1:** `edit_file` uses line numbers, not context matching — line numbers are unambiguous; context matching fails on duplicate code.
- **R-SP7-2:** `search_symbols` Phase 1 uses ripgrep composition, not tree-sitter — ships faster; tree-sitter arrives with stack plugins.
