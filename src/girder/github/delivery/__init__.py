"""Vertical delivery pump (Sprint 4, impl-plan §11 WP 4.2-4.6, plan.md Phase 3).

Drives a run from local green to a merge-ready (T0) or merged (T1/T2) PR:

    active ──final gate──> pr_open ──> ci_running ──green──> conformance_review
                                │                │                  │
                                │             red: classify      tier decides
                                │             (baseline/flaky)     │
                                │                │                  ├─ T0 → merge_pending_human
                                │             novel ⇒ ci_fixing    ├─ T1 → merged + notify
                                │                (Diagnostic Fix   └─ T2 → merged iff
                                │               Agent, cap 3)             streak earned
                                └─ integrity violation ⇒ failed (never merges)

Merge gate (WP 4.5): ``runs.integrity_violations > 0`` blocks the merge at
EVERY tier — the D4 quadruple-check (local suite, green CI, conformance
review, orchestrator-side diff audit) is re-verified at the delivery
boundary, independent of anything a task engine believed earlier.

The pump is resumable: every step is idempotent (push is a no-op when
current, PR open returns the existing PR, merge is checked before called),
so a crash between two steps replays cleanly (§5.5 / §8.7).

Split (W6 refactor, no behavior change): the original 778-line module
(origin SHA 5ff969586361fac1ce28ade40cc8d7dabf2d62d9) became a package —
``engine`` (the pump/state machine), ``suite`` (final-suite execution and
PR-body criteria helpers), ``postmerge`` (post-merge bookkeeping). This
``__init__`` re-exports the full original import surface.
"""

from girder.github.delivery.engine import DeliveryEngine

__all__ = ["DeliveryEngine"]
