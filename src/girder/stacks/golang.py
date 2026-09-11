"""Go 1.23 stack plugin (improvements plan WP 11.3).

JUnit output uses ``gotestsum --junitfile`` (the standard way to get JUnit XML
out of ``go test``; plain ``go test`` has no such flag).
"""

from __future__ import annotations

from girder.stacks import StackPlugin

VERIFY_XML = ".girder-verify.xml"

TEST_SIGNAL_PATTERNS: list[str] = ["**/*_test.go"]


class GoPlugin(StackPlugin):
    name = "go-1.23"

    def runner_image(self) -> str:
        return f"girder-runner:{self.name}"

    def test_command(self, python_bin: str | None = None) -> list[str]:
        # gotestsum (baked into container/Dockerfile.go-1.23) runs the suite
        # and writes JUnit XML; everything after `--` is the go test argv.
        return [
            "gotestsum",
            f"--junitfile=/workspace/{VERIFY_XML}",
            "--",
            "go",
            "test",
            "./...",
            "-v",
        ]

    def symbol_outline_command(self, path: str, python_bin: str | None = None) -> list[str]:
        # python_bin is meaningless on this stack — ignored, not errored.
        return ["go", "doc", "-all", path]

    def symbol_outline_extensions(self) -> tuple[str, ...]:
        return (".go",)

    def test_signal_patterns(self) -> list[str]:
        return list(TEST_SIGNAL_PATTERNS)

    def package_cache_mounts(self) -> dict[str, str]:
        # Matches GOPATH/pkg/mod in container/Dockerfile.go-1.23; the module
        # cache is the bulk of Go's dependency footprint.
        return {"/var/cache/orchestrator/go": "/root/go/pkg/mod"}
