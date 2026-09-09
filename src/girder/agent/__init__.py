"""Custom agent runtime (Sprint 3 WP 3.1/3.2)."""

from girder.agent.context import (
    CompactionStats,
    compact,
    estimate_tokens,
    should_compact,
)
from girder.agent.prompts import build_system_prompt, build_task_message, wrap_tool_result
from girder.agent.runtime import AgentRuntime, AttemptOutcome
from girder.agent.tools import (
    TOOL_SCHEMAS,
    CommandDenied,
    ToolExecResult,
    ToolRegistry,
    screen_command,
)

__all__ = [
    "TOOL_SCHEMAS",
    "AgentRuntime",
    "AttemptOutcome",
    "CommandDenied",
    "CompactionStats",
    "ToolExecResult",
    "ToolRegistry",
    "build_system_prompt",
    "build_task_message",
    "compact",
    "estimate_tokens",
    "screen_command",
    "should_compact",
    "wrap_tool_result",
]
