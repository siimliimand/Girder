# Girder Production Deployment Guide

**Scope:** WP 13.1 (WS-11) — running `girder daemon` and the approval web console (`girder web`) as long-lived services on a single host, with a reverse proxy, secrets management, backups, log rotation, and GitHub App webhook setup.

Every command, flag, path, and default below is verified against the code as of this writing. Where a capability is **planned but not implemented**, it is marked as such — do not assume it works.

---

## 0. What runs where

| Process | Command | Binds | Purpose |
|---|---|---|---|
| Orchestrator daemon | `girder daemon` | nothing (no network listener) | boot recovery, worktree GC, run-engine pump |
| Web console | `girder web` | `127.0.0.1:8787` (default) | approval UI, REST API, `/metrics`, inbound webhooks |

State lives in one SQLite database, default `~/.local/share/girder/girder.db` (the global `--db` flag on every subcommand moves it).

**The CWD-walk-up gotcha.** `girder.toml` is discovered by walking **up from the current working directory** (`config._find_project_toml`). The girder.toml of the directory you *launch* from governs settings for **all projects in the process** — launch the daemon and the web console from the directory that holds the girder.toml you want (typically the repo root of your deployment config), not from `$HOME` or `/`. In systemd terms: set `WorkingDirectory=` correctly. Environment variables (`GIRDER_*`, `__` nesting, e.g. `GIRDER_BUDGET__RUN_CAP_USD=9.5`) override girder.toml values.

**The interpreter knob: `[sandbox] python_bin`** (default `python3`). This is the interpreter *inside the runner image* — it runs the baseline/verify pytest suites, the rerun probes, and the agent tools' in-container snippet execs (`write_file`/`apply_patch`/`edit_file`/`list_directory`, plus the Python stack's symbol outline). The stock runner images ship `python3` on `PATH`; if your custom image keeps the interpreter elsewhere (e.g. `/opt/py/bin/python3`), set the key rather than patching code. When using `girder daemon --sandbox local` on a host without a global `python3` (venv-only installs), point it at a real interpreter or tool execs will fail with `FileNotFoundError`.

---

## 1. Systemd service units

Notes before the units:

- **The daemon auto-migrates.** `Database.open()` applies pending migrations (`db/engine.py`), so no `ExecStartPre=girder migrate` is required. Keep `girder migrate` for explicit pre-flight checks in the deployment checklist instead.
- **Use `--log-format json`** in service mode (the default is `text`); see §5.
- The web console is loopback-only by default (`[web] host = 127.0.0.1`, `port = 8787`) — never change `host` to `0.0.0.0`; put a proxy in front instead (§2). Alternatively bind a Unix domain socket with `[web] unix_socket` / `girder web --unix-socket` (e.g. `/run/girder.sock`).
- API key auth: when `[web] api_key` (or `GIRDER_WEB__API_KEY`, or `girder web --api-key`, or `secrets.toml [web] api_key`) is set, every console route requires `Authorization: Bearer <key>` or `X-API-Key: <key>`. When unset the console is **anonymous** — acceptable only because the default bind is loopback.

`/etc/systemd/system/girder-daemon.service`:

```ini
[Unit]
Description=Girder orchestrator daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# CWD walk-up: this directory's girder.toml governs the whole process.
WorkingDirectory=/opt/girder
ExecStart=/opt/girder/.venv/bin/girder daemon --log-format json
# Secrets come from ~/.config/girder/secrets.toml (mode 0600) — see section 3.
Restart=on-failure
RestartSec=5
# The daemon reconciles unclean exits at boot; SIGTERM triggers the graceful drain.
KillSignal=SIGTERM
TimeoutStopSec=60

# Hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/girder %h/.config/girder
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
ProtectKernelModules=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes

[Install]
WantedBy=multi-user.target
```

Adjust `ReadWritePaths` to cover wherever `--db` points, plus the worktree base if the daemon's GC manages worktrees under `$HOME`. The hardening lines are a starting point — relax selectively if podman interaction (daemon `--sandbox podman`) hits a restriction.

`/etc/systemd/system/girder-web.service`:

```ini
[Unit]
Description=Girder approval web console
After=network-online.target girder-daemon.service

[Service]
Type=simple
WorkingDirectory=/opt/girder
# --api-key wins over config; better: keep the key in secrets.toml [web] api_key
ExecStart=/opt/girder/.venv/bin/girder web
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/girder %h/.config/girder

[Install]
WantedBy=multi-user.target
```

Both processes are also safe to run under a dedicated system user; the secrets path is `$HOME/.config/girder/secrets.toml` of **that** user.

---

## 2. Reverse proxy (nginx / caddy)

The console must stay behind a TLS-terminating proxy for anything beyond localhost. Three rules the proxy must honor:

1. **Forward the raw request body to `/api/webhooks/*`.** The GitHub HMAC check (`X-Hub-Signature-256`, HMAC-SHA256) is computed over the **raw bytes** (`github/webhook.py: verify_github_signature(secret, body, header)`); any proxy transformation of the body breaks verification. Slack signing-secret verification is likewise body-sensitive. Concretely: no request-body rewriting, no automatic gzip decompress+recompress in the path, buffer off or large enough for big payloads.
2. **Keep `/metrics` internal.** It is a public path by design (Prometheus scrapers cannot send API keys — `auth.PUBLIC_PATH_PREFIXES = ("/metrics", "/api/webhooks/")`), so it has **no auth**. Deny it from the outside or allowlist only your scraper's source address.
3. **Everything else** flows to the console; set the API key in secrets/config so the console itself is not anonymous.

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name girder.example.com;

    ssl_certificate     /etc/letsencrypt/live/girder.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/girder.example.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8787;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        # Webhooks: pass the body through untouched
        proxy_request_buffering on;   # buffer fully, then forward verbatim
        proxy_read_timeout 75s;
    }

    # /metrics has no auth — keep it off the public internet
    location /metrics {
        allow 10.0.0.0/8;    # your scraper / LAN
        deny all;
        proxy_pass http://127.0.0.1:8787;
    }
}
```

### Caddy

```caddy
girder.example.com {
    # /metrics: internal only (no auth on this endpoint)
    @metrics path /metrics
    respond @metrics 403

    reverse_proxy 127.0.0.1:8787
}
```

Caddy's `reverse_proxy` streams the body unchanged — webhook signatures survive automatically. For a scraper allowlist instead of an outright deny, use Caddy's `remote_ip` matcher on `/metrics`.

---

## 3. Secrets management

Girder splits configuration in two:

| File | Content | Committed? |
|---|---|---|
| `girder.toml` (per project, found by CWD walk-up) | settings, model roles, `[web] api_key` *reference*, `[github] webhook_enabled` / `bot_account` | yes |
| `~/.config/girder/secrets.toml` | credentials only | **never** |

**The secrets file must be mode `0600`.** The loader *refuses* a group/world-readable file (`SecretsPermissionError`) — it does not warn and continue. Fix with `chmod 600`.

Full key inventory (flat keys and `[table] key` forms are both accepted; tables flatten with `_`):

| Key | Used for |
|---|---|
| `models_openrouter_api_key` / `models_anthropic_api_key` / `models_openai_api_key` | provider credentials (`[models]` tables) |
| `github_token` | GitHub API client |
| `github_webhook_secret` | HMAC-SHA256 verification of `/api/webhooks/github` deliveries |
| `notify_telegram_bot_token` | Telegram notifier / inbound receiver |
| `notify_discord_webhook_url` | Discord notifications |
| `notify_slack_bot_token` / `notify_slack_signing_secret` | Slack notifier and `/api/webhooks/slack` request signing |
| `[web] api_key` (flattens to `web_api_key`) | console API-key auth fallback |

Example `~/.config/girder/secrets.toml`:

```toml
[models]
openrouter_api_key = "sk-or-..."

[github]
token = "ghp_..."
webhook_secret = "whsec_..."   # the webhook secret you type into the GitHub App

[web]
api_key = "a-long-random-string"   # console auth

[notify]
telegram_bot_token = "12345:..."
slack_bot_token = "xoxb-..."
slack_signing_secret = "..."
```

**Generation and storage with `pass` (or gnome-keyring).** Keep the master copy in your password store; render the file from it:

```bash
#!/usr/bin/env bash
# girder-secrets-render.sh — regenerate secrets.toml from pass entries
set -euo pipefail
OUT="$HOME/.config/girder/secrets.toml"
install -m 600 /dev/null "$OUT"
cat >"$OUT" <<EOF
[models]
openrouter_api_key = "$(pass show girder/openrouter-api-key | head -1)"

[github]
token = "$(pass show girder/github-token | head -1)"
webhook_secret = "$(pass show girder/github-webhook-secret | head -1)"

[web]
api_key = "$(pass show girder/web-api-key | head -1)"
EOF
chmod 600 "$OUT"   # loader hard-fails on anything looser
```

Generate keys with `openssl rand -hex 32`; store the new value in `pass` first, then render. After editing the file re-check the mode — the daemon and web processes load secrets at startup, so restart the units after rotating. (With gnome-keyring, `secret-tool store`/`lookup` against attribute `service=girder` substitutes for `pass show` in the same script.)

---

## 4. Backup strategy

All durable state is one SQLite file: `~/.local/share/girder/girder.db`. Plain `cp` on a live database can capture a torn page; use the SQLite online backup API instead.

`/usr/local/bin/girder-backup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
DB="$HOME/.local/share/girder/girder.db"
DEST="$HOME/.local/share/girder/backups"
mkdir -p "$DEST"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
sqlite3 "$DB" ".backup '$DEST/girder-$STAMP.db'"
# keep 14 days of daily snapshots
find "$DEST" -name 'girder-*.db' -mtime +14 -delete
```

Run it from a user timer (systemd `OnCalendar=daily`) or cron as the service user. The `.backup` command is safe while the daemon is writing.

**Retention of run history** is a separate concern from file backups — use `girder prune`:

```bash
girder prune --older-than-days 90 --dry-run   # always look first
girder prune --older-than-days 90             # then delete
```

Prune semantics: it deletes **terminal-status runs only** (runs whose status is in `TERMINAL_RUN_STATUSES`) older than the horizon, plus their child rows. In-flight or stuck runs are never touched — recover them (`girder recover`) first if you actually want them gone. Default horizon is 90 days. Add `--dry-run`-first pruning to the same timer as the backup.

---

## 5. Log rotation

**systemd path (recommended):** with `--log-format json` under a service unit, everything goes to journald, which rotates and rate-limits by default. Just set a sensible retention:

```bash
# /etc/systemd/journald.conf: SystemMaxUse=2G
journalctl -u girder-daemon --since today
```

**logrotate path** (if you redirect stdout to a file, e.g. via `StandardOutput=append:/var/log/girder/daemon.log`):

```
/var/log/girder/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

The JSON line shape (from `JsonFormatter`) is one record per line with keys `ts` (ISO-8601 UTC), `level`, `logger`, `msg` (plus `module` and extras) — so `jq` filters work directly on the file: `jq 'select(.level=="ERROR")' daemon.log`. Note the `ts` values are UTC; logrotate's `dateext` uses local time.

---

## 6. GitHub App setup (webhook → run pipeline)

Today's webhook surface (all implemented — WS-05 / WP 9.1):

- Endpoint: `POST /api/webhooks/github`. It returns **404** unless `[github] webhook_enabled = true` in girder.toml, and **403** if the `X-Hub-Signature-256` HMAC (with `github_webhook_secret` from secrets.toml) does not match. Unknown events are answered 200 and ignored (GitHub disables webhooks that fail repeatedly).
- Handled events: `issues`, `issue_comment`, `pull_request_review_comment`.
- **Trigger today:** the webhook creates a run when the issue is **assigned to `bot_account`** (`[github] bot_account = "girder-bot"`), or when a comment starts with **`/girder <intent>`** (`/girder fix this` also works on PR review comments). The bot's own comments are ignored.
- Slack counterpart: `POST /api/webhooks/slack`, gated the same way and verified with `notify_slack_signing_secret`.

**GitHub App configuration:**

1. Create the App (GitHub → Settings → Developer settings → GitHub Apps). Set the webhook URL to `https://<your-host>/api/webhooks/github`.
2. Set the webhook secret to the same value as `github_webhook_secret` in `secrets.toml`; set `[github] webhook_enabled = true` and `bot_account = "<the app's bot login, e.g. girder-bot[bot] or girder-bot>"` in girder.toml.
3. App permissions the webhook + PR flow need: **Issues — read & write**, **Contents — read & write**, **Pull requests — read & write**. Subscribe to webhook events **Issues**, **Issue comment**, and **Pull request review comment**.
4. Install the App on the target repositories; put the resulting installation token / app credentials behind `github_token` (or the GitHub client's auth config) in secrets.toml.

**PLANNED — not implemented (WP 13.4):** the **`girder-ready` label gate**. The spec (R-SP13-4) wants automatic pickup restricted to issues labeled `girder-ready`, so Girder does not process every typo/question issue in its own repo. No such gating exists in the code today — assignment of the bot account is the only issue-level trigger. Until label gating lands, **do not auto-assign the bot**; drive runs explicitly with `/girder <intent>` comments, or assign carefully.

---

## 7. First deployment checklist

```bash
girder validate                 # girder.toml + secrets + sandbox sanity (run from the dir with girder.toml)
girder migrate                  # optional: pre-flight migration check (the daemon auto-migrates anyway)
systemctl enable --now girder-daemon
systemctl enable --now girder-web
curl -s http://127.0.0.1:8787/metrics | head   # console reachable on loopback
# proxy + TLS in front (section 2); confirm:
curl -I https://girder.example.com/api/webhooks/github   # expect 404 (disabled) or 403 (no signature) — NOT 200 HTML
# GitHub App per section 6, then a test issue: comment "/girder <intent>" and watch:
journalctl -u girder-daemon -f
```
