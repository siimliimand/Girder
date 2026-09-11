"""Tool schemas: the JSON-schema definitions sent to the model (impl-plan §6.10).

Split out of the former monolithic ``agent/tools.py`` (origin SHA a698868) to
keep modules under the 600-line DoD bound. Text is pinned by unit tests — do
not reword.
"""

from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from the worktree (optionally a line range)."
                " For large files, call view_symbol_outline first, then read"
                " targeted ranges via line_start/line_end."
                " Output is clipped to the configured line budget — you will"
                " see a '[truncated: N more lines]' marker instead of the rest."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line_start": {"type": "integer"},
                    "line_end": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file inside the declared write scope.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a unified diff to the worktree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Primary file the diff touches."},
                    "unified_diff": {"type": "string"},
                },
                "required": ["path", "unified_diff"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files matching a glob pattern across the worktree.",
            "parameters": {
                "type": "object",
                "properties": {"glob": {"type": "string"}},
                "required": ["glob"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ripgrep",
            "description": "Regex search across the worktree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "regex": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                },
                "required": ["regex"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_symbol_outline",
            "description": (
                "List classes/functions with line numbers for a source"
                " file, using the project stack's native toolchain for"
                " its own source files; other file types fall back to a"
                " regex outline."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command inside the sandbox (no network)."
                " Denied commands: curl, wget, nc, ssh, git push, sudo,"
                " podman, docker, mount."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "timeout_s": {"type": "number"},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace a contiguous block of lines in a file."
                " Use view_symbol_outline + read_file first to identify exact"
                " line numbers. start_line and end_line are 1-indexed and"
                " inclusive. The replacement text replaces those lines exactly"
                " — do not include surrounding context lines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {
                        "type": "integer",
                        "description": "First line to replace (1-indexed, inclusive)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to replace (1-indexed, inclusive)",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "New content for lines start_line..end_line",
                    },
                },
                "required": ["path", "start_line", "end_line", "replacement"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List entries in a directory. Returns name, type (file/dir),"
                " and size for each entry. Depth controls recursion"
                " (1 = direct children only). Use this instead of 'ls' or"
                " 'find' when surveying a new area."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "depth": {
                        "type": "integer",
                        "default": 1,
                        "description": "Max recursion depth (1-3)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show the worktree status (git status --short). Read-only.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show unstaged (or staged) changes for the worktree. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Restrict diff to this file (optional)",
                    },
                    "staged": {"type": "boolean", "default": False},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the project test suite (or a subset) and return a"
                " structured summary. Prefer this over 'run_command' with"
                " pytest — it returns structured results and clips verbose"
                " passing output automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Test files or directories to run (empty = full suite)",
                    },
                    "keyword": {"type": "string", "description": "pytest -k filter expression"},
                    "timeout_s": {"type": "number", "default": 120},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_symbols",
            "description": (
                "Find symbol definitions or usages across the codebase."
                " kind='definition' finds where a symbol is defined;"
                " kind='usage' finds all call sites;"
                " kind='export' lists what a module exports."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Symbol name (class, function, variable)",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["definition", "usage", "export"],
                        "default": "definition",
                    },
                    "path": {"type": "string", "description": "Restrict search to this path"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_task_complete",
            "description": (
                "The ONLY way to finish your attempt. The worktree must be"
                " CLEAN first: commit with `git add -A && git commit -m"
                ' "<message>"` BEFORE calling this — a dirty tree bounces'
                " the call and wastes a turn. If the turn budget runs out"
                " before you call this, the attempt is destroyed and all"
                " uncommitted work is lost. Provide a short summary of what"
                " changed; set no_changes=true only when you genuinely made"
                " no changes and none were needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "no_changes": {
                        "type": "boolean",
                        "description": (
                            "Set true only when you genuinely made no changes and none were needed."
                        ),
                        "default": False,
                    },
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_spec_amendment",
            "description": "The frozen spec is impossible/contradictory — request an amendment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "suggested_change": {"type": "string"},
                },
                "required": ["reason", "suggested_change"],
            },
        },
    },
]
