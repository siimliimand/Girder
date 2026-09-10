"""Typed payloads for audit-event and steering rows (WP 10.5).

The event tables store free-form JSON, so these TypedDicts document the
shapes the code actually writes rather than enforcing a schema at the DB
boundary. Repo functions accept them (via ``Mapping[str, object]``
parameters) but also accept plain dicts — legacy call sites are unchanged.

Deviations from the original spec sketch (audited against real call
sites):

- ``TransitionPayload``: real ``state_transition`` events carry ``entity``,
  ``id``, ``from`` and ``to`` (written by :func:`girder.fsm.transition`),
  not ``from_`` — and callers merge arbitrary extra keys into the same
  JSON (``reason``, ``pr_number``, ``head_sha``, ``merge_sha``, ``fix``,
  ``flaky``, ``failed``). All are optional (total=False).
- ``SteeringPayload``: matches reality — inject events carry ``directive``,
  skip/force-pass events carry ``task_id``; never both.
- ``BudgetEventPayload``: the spec's four fields are required nowhere.
  ``budget_preflight_denied`` carries all of them plus token estimates,
  ``prompt_chars`` and ``reason``; ``model_call_dispatch`` omits
  ``remaining_usd``. Hence total=False with the full observed key set.
"""

from __future__ import annotations

from typing import TypedDict

# Functional syntax: the stored JSON key is the reserved word "from".
TransitionPayload = TypedDict(
    "TransitionPayload",
    {
        "entity": str,
        "id": str,
        "from": str,
        "to": str,
        "reason": str,
        "pr_number": int,
        "head_sha": str,
        "merge_sha": str,
        "fix": str,
        "flaky": list[str],
        "failed": list[str],
    },
    total=False,
)


class SteeringPayload(TypedDict, total=False):
    """Steering-event payload (api/routes/steering.py)."""

    directive: str
    task_id: str


class BudgetEventPayload(TypedDict, total=False):
    """Budget/model-call audit payloads (models/gateway.py)."""

    role: str
    model: str
    est_in_tokens: int
    est_out_tokens: int
    est_cost_usd: float
    remaining_usd: float
    prompt_chars: int
    reason: str
