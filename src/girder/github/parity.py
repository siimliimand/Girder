"""Local/remote CI parity audit (impl-plan §9.1, Sprint 4 WP 4.1).

The sandbox runner image must mirror the GitHub Actions runner for the
project's stack: identical toolchain versions so ``--network=none`` local
verification never diverges from remote CI. This module

1. collects ``tool -> version`` dumps from the runner image (one probe
   command per tool, executed in a scratch sandbox container), and
2. compares them against a CI-side dump produced by
   ``container/ci-env-dump.sh`` inside the workflow's ``env-dump`` step.

Divergence is a hard failure (exit 1 from the CLI, empty diff = parity).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from girder.sandbox.engine import ContainerSpec, SandboxEngine
from girder.sandbox.local import LocalExecSandbox
from girder.sandbox.podman import PodmanEngine
from girder.util import CommandError

log = logging.getLogger(__name__)

# tool name -> probe argv executed inside the image. The first stdout line
# is parsed (e.g. "Python 3.12.3" -> "3.12.3").
DEFAULT_PROBES: dict[str, list[str]] = {
    "python": ["python", "--version"],
    "pip": ["python", "-m", "pip", "--version"],
    "pytest": ["python", "-m", "pytest", "--version"],
}


@dataclass(frozen=True)
class ParityDelta:
    """One tool where image and CI disagree (either side may be missing)."""

    tool: str
    image_version: str | None
    ci_version: str | None

    @property
    def diverged(self) -> bool:
        return self.image_version != self.ci_version

    def reason(self) -> str:
        if self.ci_version is None:
            return f"{self.tool}: {self.image_version} in image, absent from CI dump"
        if self.image_version is None:
            return f"{self.tool}: {self.ci_version} in CI, absent from image"
        return f"{self.tool}: image {self.image_version} != CI {self.ci_version}"


def parse_version_line(tool: str, output: str) -> str | None:
    """First non-empty line of a ``<tool> --version`` output, tool name stripped."""
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        stripped = re.sub(rf"^{re.escape(tool)}\s*", "", line, count=1, flags=re.IGNORECASE)
        return stripped or line
    return None


def parse_dump(text: str) -> dict[str, str]:
    """Parse a CI env dump: ``tool<whitespace>version`` per line, ``#`` comments."""
    dump: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tool, _, version = line.partition("=")
        tool = tool.strip()
        if tool:
            dump[tool] = version.strip()
    return dump


def compare(image: dict[str, str], ci: dict[str, str]) -> list[ParityDelta]:
    """Every mismatch or one-sided entry, in deterministic (sorted) order."""
    deltas: list[ParityDelta] = []
    for tool in sorted(set(image) | set(ci)):
        if image.get(tool) == ci.get(tool):
            continue
        deltas.append(ParityDelta(tool, image.get(tool), ci.get(tool)))
    return deltas


async def collect(
    sandbox: SandboxEngine,
    image: str,
    *,
    probes: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """Probe tool versions inside a scratch container of *image*."""
    resolved = probes if probes is not None else DEFAULT_PROBES
    versions: dict[str, str] = {}
    container = await sandbox.start(
        ContainerSpec(name="girder-parity-probe", image=image, network="none")
    )
    try:
        for tool, argv in sorted(resolved.items()):
            result = await sandbox.exec(container, argv, timeout_s=120.0)
            if result.exit_code != 0:
                raise CommandError(argv, result.exit_code, result.stderr)
            version = parse_version_line(tool, result.stdout)
            if version is not None:
                versions[tool] = version
    finally:
        await sandbox.kill(container)
    return versions


def render_dump(versions: dict[str, str]) -> str:
    """Canonical dump format (``tool=version`` lines, sorted)."""
    return "\n".join(f"{tool}={version}" for tool, version in sorted(versions.items())) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="girder-parity", description="Runner image vs GitHub Actions CI parity check"
    )
    parser.add_argument("ci_dump", help="CI env dump file (container/ci-env-dump.sh output)")
    parser.add_argument(
        "--image", required=True, help="runner image tag, e.g. girder-runner:python-3.12"
    )
    parser.add_argument(
        "--sandbox", default="podman", choices=["podman", "local"], help="probe executor"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sandbox: SandboxEngine = LocalExecSandbox() if args.sandbox == "local" else PodmanEngine()
    image_versions = asyncio.run(collect(sandbox, args.image))
    ci_versions = parse_dump(Path(args.ci_dump).read_text(errors="replace"))
    deltas = compare(image_versions, ci_versions)
    if not deltas:
        print("parity: OK — image matches CI")
        return 0
    for delta in deltas:
        print(delta.reason())
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
