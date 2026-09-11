# WS-11 — Self-Hosting & Productionisation

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 13 (WP 13.1, WP 13.4) |
| **Status** | In progress — WP 13.1 done (docs/deployment.md, restored after being lost in a merge); WP 13.4 still blocked — webhooks + auth/prune are merged, remaining: `girder-ready` label gating, GitHub App creation, the girder-on-girder run itself |
| **Wave** | **4** — requires **WS-05** (webhooks) deployed and **WS-10** (auth, prune) merged. WP 13.1 (deployment docs) is pure documentation and may be pulled forward to wave 1. |
| **Effort** | ~1 week · ~5 new tests (mostly ops/config) |
| **Owned files** | `docs/deployment.md` (new), repo-root `girder.toml` |
| **Shared files** | none in code — this is the operational capstone |

---

## Goal

Girder processes its own GitHub issues. This is the north-star validation that all previous workstreams work end-to-end.

## Definition of Done

- **At least one non-trivial Girder improvement (≥ 3 files changed) was implemented by Girder itself**; the resulting PR was opened, CI passed, and it was merged.
- `self_hosting_success_rate` is tracked: ratio of self-processed Girder issues that produced a merged PR without human amendment.

---

## WP 13.1 — Production Deployment Docs

**New file:** `docs/deployment.md`

Covers:

- Systemd service unit for `girder daemon`
- Reverse proxy (nginx/caddy) in front of the web console for external access
- Secrets management with `gnome-keyring` or `pass`
- Backup strategy for `~/.local/share/girder/girder.db`
- Log rotation for structured JSON logs (WS-09's `--log-format json`)
- GitHub App setup: webhook URL, permissions, installation

*(Can be written any time after WS-05/WS-09/WS-10 specs are stable — no code dependency.)*

---

## WP 13.4 — Girder-on-Girder Self-Hosting

**Changes to `girder.toml`** (repo root):

1. Enable webhook processing: `[github] webhook_enabled = true`, `bot_account = "girder-bot"`.
2. Create the GitHub App and configure the webhook URL (per `docs/deployment.md`).
3. Add the Girder repo as a project via the web console.
4. Label issues with **`girder-ready`** to gate which issues Girder picks up automatically.

**North-star metric:** track `self_hosting_success_rate` — merged-PR-without-human-amendment / total self-processed issues. Compute it from run history (WS-09's metrics endpoint is a natural home for a gauge; the postmortem/history views already hold the raw data).

**Operational loop:**

1. An issue in this repo gets labeled `girder-ready` (by a human).
2. The webhook fires → run created → spec → tasks → PR (WS-05 pipeline).
3. A human reviews the PR; merge or amend.
4. Record the outcome; feed failure patterns back into WS-02 prompt work.

---

## Tests / Validation

- End-to-end is the test: pick a real `girder-ready` issue, run the full pipeline, and verify the PR.
- Unit-level: `webhook_enabled` config plumbing; `girder-ready` label gating (issues without the label never create runs) — add this assertion to WS-05's webhook handler tests if not already covered there.

## Coordination

- **Hard dependency: WS-05 deployed** with a reachable webhook URL (public HTTPS or tunnel).
- **WS-10** should be live first: the console is then exposed behind a proxy with API-key auth, and the DB has bounded growth (`prune`).
- Failure modes discovered here (agent exploration spirals, bad tool outputs) feed follow-up iterations of **WS-01/WS-02** — log them in the resolution log of this doc.

## Relevant resolution log decision

- **R-SP13-4:** Self-hosting gated by the `girder-ready` label — prevents Girder from processing every typo/question issue in its own repo.
