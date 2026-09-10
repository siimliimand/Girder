# WS-08 — Web Console UI

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 12 (WP 12.1, WP 12.2, WP 12.6) |
| **Status** | Merged (wave 1, 2026-09-10) |
| **Wave** | **1** — can run in parallel with WS-01, WS-03, WS-04, WS-05, WS-07A, WS-09 |
| **Effort** | ~4–5 days · ~5 new tests |
| **Owned files** | `src/girder/api/static/style.css`, `src/girder/api/static/diff.js` (new), `src/girder/api/templates/` (`base.html`, `run.html`, `postmortem.html`) |
| **Shared files** | none — pure static/template work, no Python route changes |

---

## Goal

Transform the minimal functional console into a developer-grade UI: dark mode with a design system, live DAG updates over SSE, and a readable diff viewer.

## Current state (verified 2026-09-10)

- `src/girder/api/static/` and `templates/` exist; the CSS is ~185 lines of light-mode functional styling.
- The SSE stream already emits `state_transition` events with `entity=task` (emitted from the existing routes module) — WP 12.2 is frontend-only.
- The diff display is a `<pre>{{ diff }}</pre>` block with raw unified diff text in `run.html` and `postmortem.html`.

## Definition of Done

- All existing HTML/SSE behavior preserved (no route or payload changes).
- Dark mode via `prefers-color-scheme` **and** a manual toggle persisted to `localStorage`.
- Task DAG updates live via SSE without a full-panel re-fetch.
- Diffs render as a two-column table with line numbers, `+`/`-` coloring.
- `mypy --strict` clean (trivial — no Python changes expected).

**Pre-flight:** screenshot/record current panel rendering as a visual regression baseline.

---

## WP 12.1 — Dark Mode & Design System

**Implementation in `src/girder/api/static/style.css`:**

1. Add CSS custom properties for all colors:

```css
:root {
  --bg:          #fafaf8;
  --bg-surface:  #f2f2ef;
  --border:      #e0e0dc;
  --text:        #1c1c1c;
  --text-muted:  #6b6b6b;
  --accent:      #0b5fa5;
  --accent-dark: #094c84;
  --red:         #c02020;
  --amber:       #d99a1a;
  --green:       #2c8a3c;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg:          #111;
    --bg-surface:  #1e1e1e;
    --border:      #333;
    --text:        #e8e8e8;
    --text-muted:  #888;
    --accent:      #4da3ff;
    --accent-dark: #6bb8ff;
    --red:         #ff6b6b;
    --amber:       #ffc845;
    --green:       #5cbf70;
  }
}
```

2. Replace all hardcoded hex colors in existing rules with `var(--...)` references.
3. Manual toggle button in `base.html`: sets `data-theme="dark"` on `<html>`, persists to `localStorage`, wins over `prefers-color-scheme` when set.

---

## WP 12.2 — Live DAG with Status Updates

**Why:** The task DAG is rendered server-side as static HTML and only refreshes on the panel poll (every 2s). It should update live via SSE without a full-panel re-fetch.

**Implementation (frontend-only):**

Add a handler in `run.html`:

```javascript
es.addEventListener("state", function(e) {
    var d = JSON.parse(e.data);
    if (d.payload && d.payload.entity === "task") {
        var taskId = d.payload.id;
        var newStatus = d.payload.to;
        var el = document.querySelector('[data-task-id="' + taskId + '"]');
        if (el) {
            // Update CSS class and badge text
            el.className = el.className.replace(/task-\w+/, "task-" + _statusClass(newStatus));
            var badge = el.querySelector('.task-status-badge');
            if (badge) badge.textContent = newStatus;
        }
    }
});
```

Markup changes in `run.html`:

- Add `data-task-id="{{ t.id }}"` to each task `<div>`.
- Wrap status text in `<span class="task-status-badge">`.

---

## WP 12.6 — Diff Viewer Improvements

**Why:** Raw `<pre>` unified diffs are unreadable for review.

**Implementation:**

1. Add `src/girder/api/static/diff.js` — a ~100-line dependency-free script that takes a unified diff string and renders it as a two-column table:
   - `+` lines green, `-` lines red
   - line numbers on the left
   - `<table>` + CSS classes only, no external dependencies
2. Replace the `<pre>{{ diff }}</pre>` blocks in `run.html` and `postmortem.html` with the renderer.

---

## Tests

- Template tests (existing suite style): task divs carry `data-task-id`; badge span present; toggle button present in `base.html`.
- `diff.js`: render a fixture unified diff → assert table structure, class assignment for +/-/context lines, correct line numbers (unit-test via a lightweight DOM harness or node, matching however existing static assets are tested).
- Manual/SSE verification: a state transition event flips the badge without a panel re-fetch (verify against a running daemon).

## Coordination

- None in wave 1 — this workstream touches no Python.
- **WS-06** later moves route handlers; the SSE event payload consumed here must not change (it won't — refactor is behavior-preserving).
- **WS-10** adds the API-key header to fetch/form requests — a small follow-up edit to the JS added here; noted in WS-10.
