"""Sandbox argv snippets: base64-argv python one-liners (impl-plan §6.10).

Split out of the former monolithic ``agent/tools.py`` (origin SHA a698868).
No stdin: every in-container write goes through base64-encoded argv decoded by
a ``-c`` one-liner run under the configured interpreter
(``sandbox.python_bin``) — no shell-quoting hazards. These strings are
pinned by unit tests; do not reword.
"""

from __future__ import annotations

_B64_DECODE_SNIPPET = (
    "import base64,sys,pathlib; pathlib.Path(sys.argv[1]).write_bytes("
    "base64.b64decode(sys.argv[2]))"
)
_PATCH_PREFIX = "/tmp/.girder-patch"
_MATCH_CAP = 200  # rg / grep match cap before truncation

# edit_file (WP 7.1): replace lines start..end (1-indexed, inclusive) with the
# base64-argv replacement. Out-of-range ranges fail via assert — a silent
# clamp would corrupt the wrong lines.
_EDIT_SNIPPET = (
    "import sys, base64; "
    "p = sys.argv[1]; "
    "lines = open(p, newline='').read().splitlines(keepends=True); "
    "s, e = int(sys.argv[2]) - 1, int(sys.argv[3]); "
    "repl = base64.b64decode(sys.argv[4]).decode(); "
    "assert 1 <= s + 1 <= e <= len(lines), ("
    "f'line range {s + 1}..{e} out of range: file has {len(lines)} lines'); "
    "lines[s:e] = [repl] if repl.endswith('\\n') else [repl + '\\n']; "
    "open(p, 'w', newline='').write(''.join(lines))"
)

# list_directory (WP 7.2): os.walk with a depth limit; depth counts tree
# levels (1 = direct children of `root`). Output lines are
# "<path> [dir]" / "<path> [file N bytes]", .git pruned, entries sorted.
# (Multi-line: compound statements cannot be chained with semicolons —
# still argv-only, no stdin.)
_DIR_LIST_SNIPPET = """\
import os, sys
root, maxd = sys.argv[1], max(1, min(3, int(sys.argv[2])))
if not os.path.isdir(root):
    raise SystemExit("error: not a directory: " + root)
base = os.path.normpath(root)
out = []
for d, dirs, files in os.walk(root):
    rel = os.path.relpath(d, base)
    depth = 0 if rel == "." else rel.count(os.sep) + 1
    dirs[:] = sorted(x for x in dirs if x != ".git")
    files = sorted(files)
    out.extend(os.path.join(d, n) + " [dir]" for n in dirs)
    out.extend(
        os.path.join(d, n) + " [file " + str(
            os.path.getsize(os.path.join(d, n))
            if os.path.exists(os.path.join(d, n)) else 0
        ) + " bytes]" for n in files
    )
    if depth + 1 >= maxd:
        dirs[:] = []
print("\\n".join(out))
"""

__all__ = [
    "_B64_DECODE_SNIPPET",
    "_DIR_LIST_SNIPPET",
    "_EDIT_SNIPPET",
    "_MATCH_CAP",
    "_PATCH_PREFIX",
]
