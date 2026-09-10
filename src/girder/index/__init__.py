"""Codebase index (RAG Lite, WP 8.4).

Built fresh at every attempt start (R-SP8-4: never cached across attempts —
worktree state changes between attempts, and a stale index is worse than no
index). ``indexer`` walks a worktree and extracts a structural map;
``inject`` slices it by task scope and formats the trusted prompt block.
"""
