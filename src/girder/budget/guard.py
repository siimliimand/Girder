"""Pre-flight cost estimation and reconciliation — plan.md §8.3, impl-plan §6.7.

The estimate is checked BEFORE a model call is dispatched (§8.3.1); the call
is refused rather than reconciled after the fact. Estimated cost stays in
token_usage.estimated_before_call for calibration.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from girder.config import ModelRole
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Run


@dataclass(slots=True)
class PreflightDecision:
    allowed: bool
    est_in_tokens: int
    est_out_tokens: int
    est_cost_usd: float
    remaining_usd: float
    reason: str | None = None


class BudgetExceeded(RuntimeError):
    """Raised instead of dispatching a call whose pre-flight estimate would
    breach the run cap (impl-plan §6.8 step 1: deny aborts the dispatch)."""

    def __init__(self, decision: PreflightDecision) -> None:
        super().__init__(decision.reason or "pre-flight estimate exceeds remaining budget")
        self.decision = decision


@dataclass(slots=True)
class ReconcileOutcome:
    usage_row_id: int
    actual_cost_usd: float
    run_spend_usd: float
    over_budget: bool


class BudgetGuard:
    """Pure estimator + spend reconciler against a run's budget cap."""

    def __init__(self, db: Database, *, chars_per_token: float = 4.0) -> None:
        # chars_per_token: heuristic tokenizer (R8) — pluggable conservatism.
        self.db = db
        self.chars_per_token = chars_per_token

    def preflight(self, role: ModelRole, prompt_chars: int, run: Run) -> PreflightDecision:
        """Pure estimate — no I/O (impl-plan §6.7).

        est_in  = ceil(prompt_chars / chars_per_token)
        remaining = max(0.0, budget_cap_usd - spend_usd)
        est_out = min(role.max_output_tokens, int(remaining / role.price_out_per_tok))
                  when price_out_per_tok > 0, else role.max_output_tokens
        est_cost = est_in*price_in_per_tok + est_out*price_out_per_tok
        allowed  = run.spend_usd + est_cost <= run.budget_cap_usd
        """
        est_in = math.ceil(prompt_chars / self.chars_per_token)
        remaining = max(0.0, run.budget_cap_usd - run.spend_usd)
        if role.price_out_per_tok > 0:
            est_out = min(role.max_output_tokens, int(remaining / role.price_out_per_tok))
        else:
            est_out = role.max_output_tokens
        est_cost = est_in * role.price_in_per_tok + est_out * role.price_out_per_tok
        allowed = run.spend_usd + est_cost <= run.budget_cap_usd
        reason = None
        if not allowed:
            reason = (
                f"estimated call cost ${est_cost:.6f} + spent ${run.spend_usd:.6f} "
                f"exceeds run cap ${run.budget_cap_usd:.6f}"
            )
        return PreflightDecision(
            allowed=allowed,
            est_in_tokens=est_in,
            est_out_tokens=est_out,
            est_cost_usd=est_cost,
            remaining_usd=remaining,
            reason=reason,
        )

    async def reconcile(
        self,
        usage_row_id: int,
        run_id: str,
        role: ModelRole,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        projected_total: float,
    ) -> ReconcileOutcome:
        """Post-call reconciliation (§8.3.1 step 3): overwrite the estimate row
        with actuals, add actual cost to run.spend_usd, and re-check the tripwire.

        Returns ``over_budget=True`` when reconciled spend now exceeds the cap —
        the caller owns the FSM transition + notification.
        """
        cost = round(
            prompt_tokens * role.price_in_per_tok + completion_tokens * role.price_out_per_tok, 6
        )
        await repo.update_token_usage_actual(
            self.db,
            usage_row_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )
        await repo.add_spend(self.db, run_id, cost, projected_total)
        run = await repo.get_run(self.db, run_id)
        spend = run.spend_usd if run is not None else 0.0
        return ReconcileOutcome(
            usage_row_id=usage_row_id,
            actual_cost_usd=cost,
            run_spend_usd=spend,
            over_budget=spend > run.budget_cap_usd if run is not None else False,
        )
