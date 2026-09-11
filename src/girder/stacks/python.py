"""Python 3.12 stack — byte-identical extraction of the former hardcoded behavior."""

from __future__ import annotations

from girder.stacks import VERIFY_XML, StackPlugin

_AST_OUTLINE_SNIPPET = (
    "import ast,sys;"
    "t=ast.parse(open(sys.argv[1]).read());"
    "[print(f\"{n.lineno}: {'class' if isinstance(n,ast.ClassDef) else 'function'}:"
    " {n.name}\") for n in ast.walk(t)"
    " if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef))]"
)


class PythonPlugin(StackPlugin):
    name = "python-3.12"

    def runner_image(self) -> str:
        return "girder-runner:python-3.12"

    def test_command(self, python_bin: str | None = None) -> list[str]:
        return [
            python_bin or "python3",
            "-m",
            "pytest",
            "-q",
            f"--junitxml=/workspace/{VERIFY_XML}",
            "-p",
            "no:cacheprovider",
        ]

    def symbol_outline_command(self, path: str) -> list[str]:
        return ["python3", "-c", _AST_OUTLINE_SNIPPET, path]

    def symbol_outline_extensions(self) -> tuple[str, ...]:
        return (".py",)

    def test_signal_patterns(self) -> list[str]:
        return [
            "tests/**",
            "**/test_*.py",
            "**/*_test.py",
            "**/conftest.py",
            "tests/fixtures/**",
            "**/mocks/**",
        ]

    def package_cache_mounts(self) -> dict[str, str]:
        return {"/var/cache/orchestrator/pip": "/cache/pip"}
