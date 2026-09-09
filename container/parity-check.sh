#!/usr/bin/env bash
# Local/remote CI parity check (impl-plan §9.1, Sprint 4 WP 4.1).
#
#   container/parity-check.sh <image> <ci-env-dump.txt>
#
# Probes the runner image's toolchain inside a scratch container and diffs it
# against the CI env dump produced by container/ci-env-dump.sh in the
# workflow. Exit 0 = parity, exit 1 = divergence (hard failure).
set -euo pipefail

image="${1:?usage: parity-check.sh <image> <ci-env-dump.txt>}"
dump="${2:?usage: parity-check.sh <image> <ci-env-dump.txt>}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

exec uv run python -m girder.github.parity "$dump" --image "$image" --sandbox podman
