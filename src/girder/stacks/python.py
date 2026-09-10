"""Python 3.12 stack plugin — extraction of the previously hardcoded behavior.

Regression guard (ws07 Tests, SC-01 sentinel): the argv and pattern lists
here must stay byte-identical to the pre-plugin hardcoded values:

* ``task_engine.verify_cmd(python_bin)`` (now kept as a thin alias),
* ``config.ProjectConfig.test_signal_patterns`` defaults.
"""

from __future__ import annotations

from girder.stacks import StackPlugin

# Interpreter-independent: matches ``task_engine.VERIFY_XML`` exactly.
VERIFY_XML = ".girder-verify.xml"

TEST_SIGNAL_PATTERNS: list[str] = [
    "tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/conftest.py",
    "tests/fixtures/**",
    "**/mocks/**",
]

# In-container `ast`-based symbol listing (part B wires this into
# view_symbol_outline; defined here so the command is real, not a stub).
_AST_OUTLINE_SNIPPET = (
    "import ast,sys\n"
    "src=open(sys.argv[1]).read()\n"
    "for n in ast.parse(src).body:\n"
    "    if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):\n"
    "        print(f'{n.lineno}: {type(n).__name__} {n.name}')\n"
    "        if isinstance(n, ast.ClassDef):\n"
    "            for m in n.body:\n"
    "                if isinstance(m,(ast.FunctionDef,ast.AsyncFunctionDef)):\n"
    "                    print(f'{m.lineno}:   method {m.name}')\n"
)


class PythonPlugin(StackPlugin):
    name = "python-3.12"

    def runner_image(self) -> str:
        return f"girder-runner:{self.name}"

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

    def test_signal_patterns(self) -> list[str]:
        return list(TEST_SIGNAL_PATTERNS)

    def package_cache_mounts(self) -> dict[str, str]:
        # Matches the pip cache dir baked into container/Dockerfile.python-3.12
        # and the host cache root default of [sandbox] cache_dir.
        return {"/var/cache/orchestrator/pip": "/cache/pip"}
