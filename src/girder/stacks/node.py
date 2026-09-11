"""Node.js 20 / TypeScript stack plugin (improvements plan WP 11.2).

JUnit output comes from the real npm package ``jest-junit`` (a jest reporter),
driven by the ``JEST_JUNIT_OUTPUT_FILE`` environment variable baked into
``container/Dockerfile.node-20`` — argv-only reporters cannot set the output
path.
"""

from __future__ import annotations

from girder.stacks import StackPlugin

VERIFY_XML = ".girder-verify.xml"

TEST_SIGNAL_PATTERNS: list[str] = [
    "**/*.test.ts",
    "**/*.test.js",
    "**/*.spec.ts",
    "**/*.spec.js",
    "jest.config.*",
    "vitest.config.*",
]

# Symbol outline: prefer @typescript-eslint/parser (baked into the runner
# image) for exported functions/classes with line numbers; fall back to a
# regex pass when the parser is not resolvable (e.g. plain-JS repos).
_TS_OUTLINE_SNIPPET = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
let ast = null;
try {
  const parser = require("@typescript-eslint/parser");
  ast = parser.parse(src, { ecmaVersion: "latest", sourceType: "module" }).ast;
} catch (e) {
  if (e.code !== "MODULE_NOT_FOUND") throw e;
}
if (ast) {
  for (const n of ast.body) {
    const kind =
      n.type === "ExportDefaultDeclaration" || n.type === "ExportNamedDeclaration"
        ? n.declaration?.type
        : n.type;
    if (["FunctionDeclaration", "ClassDeclaration"].includes(kind)) {
      console.log(`${n.loc.start.line}: ${kind} ${n.declaration?.name ?? n.name ?? ""}`);
    }
  }
} else {
  const re = /^export\s+(?:default\s+)?(?:async\s+)?(function|class)\s+([A-Za-z_$][\w$]*)/gm;
  let m;
  while ((m = re.exec(src)))
    console.log(`${src.slice(0, m.index).split("\n").length}: ${m[1]} ${m[2]}`);
}
""".strip()


class NodePlugin(StackPlugin):
    name = "node-20"

    def runner_image(self) -> str:
        return f"girder-runner:{self.name}"

    def test_command(self, python_bin: str | None = None) -> list[str]:
        # jest-junit writes to JEST_JUNIT_OUTPUT_FILE (ENV in the runner image)
        # = /workspace/.girder-verify.xml.
        return ["npx", "jest", "--ci", "--reporters=default", "--reporters=jest-junit"]

    def symbol_outline_command(self, path: str) -> list[str]:
        return ["node", "-e", _TS_OUTLINE_SNIPPET, path]

    def symbol_outline_extensions(self) -> tuple[str, ...]:
        return (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

    def test_signal_patterns(self) -> list[str]:
        return list(TEST_SIGNAL_PATTERNS)

    def package_cache_mounts(self) -> dict[str, str]:
        # Matches npm_config_cache in container/Dockerfile.node-20.
        return {"/var/cache/orchestrator/npm": "/cache/npm"}
