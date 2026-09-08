#!/usr/bin/env bash
# Populate the shared host-side package caches (plan.md §8.1) so containers
# never fetch dependencies over the network. Run on the host, not in guests.
#
#   ./container/cache-warm.sh /var/cache/orchestrator [requirements.txt]
#
# Caches are mounted read-only into sandboxes at /cache/*; with the agent
# running as the worktree owner and CAP_DAC_OVERRIDE dropped, guests can read
# but never poison them.

set -euo pipefail

BASE="${1:-/var/cache/orchestrator}"
REQUIREMENTS="${2:-requirements.txt}"

mkdir -p "$BASE/pip" "$BASE/npm" "$BASE/cargo"

if command -v pip >/dev/null 2>&1; then
  if [[ -f "$REQUIREMENTS" ]]; then
    pip download -r "$REQUIREMENTS" -d "$BASE/pip/wheels" --quiet
  fi
  # Warm the HTTP response cache for the configured index.
  pip install --dry-run --quiet --no-deps pip >/dev/null 2>&1 || true
fi

if command -v npm >/dev/null 2>&1; then
  # npm makes its own cache layout; a primed registry cache needs one install.
  npm install --cache "$BASE/npm" --dry-run --silent --no-audit --no-fund || true
fi

if command -v cargo >/dev/null 2>&1; then
  export CARGO_HOME="$BASE/cargo"
  cargo search serde >/dev/null 2>&1 || true   # primes the sparse index
fi

echo "cache warmed under $BASE"
