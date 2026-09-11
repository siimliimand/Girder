"""Agent tool registry package: schemas, scope gating, sandbox execution.

Split from the former monolithic ``agent/tools.py`` (origin SHA a698868) to
keep every module under the 600-line DoD bound, with zero behavior change:

* :mod:`schemas` — the JSON-schema ``TOOL_SCHEMAS`` sent to the model.
* :mod:`snippets` — the base64-argv sandbox python one-liners.
* :mod:`gating` — denylist screening and the derived scope verdicts.
* :mod:`registry` — ``ToolRegistry``, dispatch, truncate/redact/persist.

The import surface of the old module is preserved exactly: every name below
is importable from ``girder.agent.tools`` as before.
"""

from girder.agent.tools.gating import (
    CommandDenied,
    _NEW_TOOL_ALIASES,
    _WRITE_TOOLS,
    _gate_new_tool,
    screen_command,
)
from girder.agent.tools.registry import ToolExecResult, ToolRegistry, _summarize_junit
from girder.agent.tools.snippets import _MATCH_CAP
from girder.agent.tools.schemas import TOOL_SCHEMAS

__all__ = [
    "CommandDenied",
    "TOOL_SCHEMAS",
    "ToolExecResult",
    "ToolRegistry",
    "_MATCH_CAP",
    "_NEW_TOOL_ALIASES",
    "_WRITE_TOOLS",
    "_gate_new_tool",
    "_summarize_junit",
    "screen_command",
]
