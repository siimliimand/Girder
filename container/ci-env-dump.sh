#!/usr/bin/env bash
# CI-side toolchain dump (impl-plan §9.1, Sprint 4 WP 4.1).
#
# Run this inside the GitHub Actions workflow's "env-dump" step on the SAME
# runner image the project's CI uses, and commit/archive its output:
#
#   .github/workflows/ci.yml:
#     - name: env-dump
#       run: |
#         path/to/girder/container/ci-env-dump.sh > ci-env-dump.txt
#
# Then run `container/parity-check.sh <image> ci-env-dump.txt` — any
# divergence between the local sandbox image and CI is a hard failure.
set -euo pipefail

echo "python=$(python --version 2>&1 | cut -d' ' -f2)"
echo "pip=$(python -m pip --version | cut -d' ' -f2)"
echo "pytest=$(python -m pytest --version 2>&1 | head -1 | cut -d' ' -f2)"
