# WS-07 — Multi-Stack Support

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 11 (WP 11.1 – WP 11.4) |
| **Status** | Not started |
| **Wave** | **Part A (WP 11.1–11.3): wave 1.** **Part B (WP 11.4): wave 2, after WS-01 merges.** |
| **Effort** | ~3 weeks total · ~25 new tests |
| **Owned files (A)** | `src/girder/stacks/` (new), `container/Dockerfile.node-20` (new), `container/Dockerfile.go-1.23` (new), `tests/fixtures/e2e-target-ts/` (new) |
| **Shared files** | `src/girder/config.py` (stack validation against registry), `src/girder/orchestrator/task_engine.py` (`verify_cmd` resolution — currently resolved there, one touch), `load_settings()` validation |
| **Part B touches** | `src/girder/agent/tools.py` (`view_symbol_outline` internals — WS-01 must merge first) |

---

## Goal

Support JavaScript/TypeScript and Go projects in addition to Python. Expands the addressable use-case ~5× and is required before Girder can run on non-Python codebases.

## Current state (verified 2026-09-10)

- The stack is hardcoded in: `view_symbol_outline` (Python AST), `verify_cmd` (resolved in `task_engine.py`, currently pytest), the Python runner Dockerfile, and the `stack` config key.
- No `src/girder/stacks/` module exists.
- `tests/fixtures/` contains only `e2e-target` (Python) — the TS e2e fixture is new.
- `parse_junit_xml` in `orchestrator/baseline.py` is format-agnostic — Jest with `jest-junit` produces compatible XML. **No changes needed there.**

## Definition of Done

- **SC-20** (new e2e scenario, JS/TS, against `tests/fixtures/e2e-target-ts/`) passes.
- **SC-01** (Python e2e) still passes — the Python stack becomes a plugin with identical behavior.
- `load_settings()` rejects unknown `project.stack` values with a clear error.
- `mypy --strict` clean; new tests green.

---

## Part A — WP 11.1: Stack Plugin Architecture

**New module:**

```
src/girder/stacks/
├── __init__.py        (StackPlugin ABC + registry)
├── python.py          (existing Python-3.12 behavior, extracted)
├── node.py            (NEW — Node.js / TypeScript)
└── golang.py          (NEW — Go)
```

**`StackPlugin` ABC:**

```python
from abc import ABC, abstractmethod

class StackPlugin(ABC):
    name: str          # e.g. "python-3.12", "node-20", "go-1.23"

    @abstractmethod
    def runner_image(self) -> str:
        """Docker/Podman image tag for this stack's runner."""

    @abstractmethod
    def test_command(self, python_bin: str | None = None) -> list[str]:
        """Command to run the full test suite and produce JUnit XML."""

    @abstractmethod
    def symbol_outline_command(self, path: str) -> list[str]:
        """In-container command to list symbols in a source file."""

    @abstractmethod
    def test_signal_patterns(self) -> list[str]:
        """Glob patterns for test files (Layer-2 hash manifest)."""

    @abstractmethod
    def package_cache_mounts(self) -> dict[str, str]:
        """Host path → container path for read-only package cache mounts."""
```

**Registry:** `STACK_REGISTRY: dict[str, StackPlugin]` populated at import time. `load_settings()` validates `project.stack` against the registry keys.

---

## Part A — WP 11.2: Node.js / TypeScript Stack

**New:** `src/girder/stacks/node.py`, `container/Dockerfile.node-20`

```dockerfile
FROM node:20-slim
WORKDIR /workspace
RUN npm install -g typescript jest ts-jest @jest/junit-reporter
# Cache dirs pre-populated by cache-warm.sh
ENV NPM_CONFIG_CACHE=/cache/npm
```

```python
class NodePlugin(StackPlugin):
    name = "node-20"

    def runner_image(self) -> str:
        return "girder-runner:node-20"

    def test_command(self, **kwargs) -> list[str]:
        # Produces junit XML via jest-junit reporter
        return ["npx", "jest", "--ci", "--reporters=jest-junit",
                "--env=JEST_JUNIT_OUTPUT_FILE=/workspace/.girder-verify.xml"]

    def symbol_outline_command(self, path: str) -> list[str]:
        # node -e with acorn or @typescript-eslint/parser
        return ["node", "-e", _TS_OUTLINE_SNIPPET, path]

    def test_signal_patterns(self) -> list[str]:
        return ["**/*.test.ts", "**/*.test.js", "**/*.spec.ts",
                "**/*.spec.js", "jest.config.*", "vitest.config.*"]

    def package_cache_mounts(self) -> dict[str, str]:
        return {"/var/cache/orchestrator/npm": "/cache/npm"}
```

`_TS_OUTLINE_SNIPPET`: a Node.js one-liner using `@typescript-eslint/parser` listing exported functions and classes with line numbers.

---

## Part A — WP 11.3: Go Stack

**New:** `src/girder/stacks/golang.py`, `container/Dockerfile.go-1.23`

```python
class GoPlugin(StackPlugin):
    name = "go-1.23"

    def runner_image(self) -> str:
        return "girder-runner:go-1.23"

    def test_command(self, **kwargs) -> list[str]:
        return ["go", "test", "./...", "-v",
                f"-junit-report=/workspace/.girder-verify.xml"]
        # Uses gotestsum for JUnit output when available

    def symbol_outline_command(self, path: str) -> list[str]:
        return ["go", "doc", "-all", path]

    def test_signal_patterns(self) -> list[str]:
        return ["**/*_test.go"]

    def package_cache_mounts(self) -> dict[str, str]:
        return {"/var/cache/orchestrator/go": "/root/go/pkg/mod"}
```

---

## Part B — WP 11.4: Symbol Outline Phase 2 (wave 2, after WS-01)

Replace the `_AST_OUTLINE_SNIPPET` and `grep -nE` fallback in `tools.py` with per-stack `symbol_outline_command()` from the stack plugin. Each stack uses its native toolchain:

- **Python:** keep `ast.parse` (already in the runner image)
- **JS/TS:** `@typescript-eslint/parser` (in the Node runner image)
- **Go:** `go doc`
- **Other:** fall back to `grep -nE "^(def|class|function|func|pub fn)\b"`

Schema and call signature of `view_symbol_outline` are unchanged (WS-01 was asked to keep them stable).

---

## Tests

- Registry: every built-in stack validates; unknown `project.stack` rejected by `load_settings()`.
- `PythonPlugin` produces byte-identical `test_command` / patterns to today's hardcoded behavior (regression guard for SC-01).
- Node/Go plugins: command construction, pattern globs, cache mounts.
- e2e **SC-20**: `tests/fixtures/e2e-target-ts/` (small TS project with a failing test) — full run reaches merged status.

## Coordination

- **task_engine.py `verify_cmd` resolution** is the one orchestrator touch — a small, contained change; WS-03's index hook is elsewhere in the file (wave-1 coexistence is fine, coordinate the rebase).
- **Part B** must wait for WS-01 (both edit `agent/tools.py`).
- Dockerfiles and cache-warm script changes should be verified against the sandbox runtime (`podman`) actually used in this environment.

## Relevant resolution log decision

- **R-SP11-1:** Stack plugins are Python classes, not TOML/YAML config — arbitrary test commands need code; declarative config can be layered on later.
