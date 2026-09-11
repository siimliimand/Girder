"""The delivery pump / state machine (split from the original
``girder.github.delivery`` module, SHA 5ff969586361fac1ce28ade40cc8d7dabf2d62d9).

Suite execution and PR-body criteria helpers live in ``suite``;
post-merge bookkeeping lives in ``postmerge``; both are mixed into
:class:`DeliveryEngine` below, so the instance surface is unchanged.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from girder.budget.guard import BudgetExceeded
from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus
from girder.fsm import transition_run
from girder.github.ci import CiPoller, CiSnapshot, extract_test_ids
from girder.github.client import GitHubClient
from girder.github.conformance import ConformanceError, ConformanceReviewer
from girder.github.diagnostic import DiagnosticEngine
from girder.github.flaky import FlakeClassification, FlakyBreaker
from girder.gitops.branch import BranchOps
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.guard.redact import Redactor
from girder.sandbox.engine import SandboxEngine

from girder.github.delivery.postmerge import PostMergeMixin
from girder.github.delivery.suite import _EXCERPT_CHARS, SuiteMixin

log = logging.getLogger("girder.github.delivery")


class DeliveryEngine(PostMergeMixin):  # PostMergeMixin already mixes in SuiteMixin
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        secrets: Secrets,
        gateway: ModelGateway,
        sandbox: SandboxEngine,
        notifier: Notifier | None,
        redactor: Redactor,
        github: GitHubClient,
        repo_path: Path | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.secrets = secrets
        self.gateway = gateway
        self.sandbox = sandbox
        self.notifier = notifier
        self.redactor = redactor
        self.github = github
        self._repo_path = Path(repo_path) if repo_path else None
        self._poller = CiPoller(db=db, client=github, settings=settings, redactor=redactor)
        self._last_poll: dict[str, float] = {}
        # Separate from _last_poll: a parked T0 run must not inherit its CI
        # polling timestamp, or its first merge-pending poll would be skipped.
        self._last_merge_poll: dict[str, float] = {}

    # ------------------------------------------------------------------- pump

    async def pump(self, run: Run) -> str:
        try:
            if run.status is RunStatus.PR_OPEN:
                return await self._pump_pr_open(run)
            if run.status is RunStatus.CI_RUNNING:
                return await self._pump_ci_running(run)
            if run.status is RunStatus.CI_FIXING:
                return await self._pump_ci_fixing(run)
            if run.status is RunStatus.CONFORMANCE_REVIEW:
                return await self._pump_conformance(run)
            if run.status is RunStatus.MERGE_PENDING_HUMAN:
                return await self._pump_merge_pending(run)
            return str(run.status)
        except BudgetExceeded:
            # The gateway has already frozen the attempt and tripped the run
            # into budget_exhausted (§8.3) — report the descriptor only.
            return "budget_exhausted"

    # -------------------------------------------------- entry (active → PR)

    async def enter_delivery(self, run: Run) -> str:
        """All tasks completed: final gate → push → PR → ``pr_open``.

        D4 quadruple-check at run level: integrity ledger clean (check 4),
        full suite green on the run branch (check 1). Checks 2 (CI) and 3
        (conformance) happen downstream in the pump.
        """
        repo_path = await self._repo_path_of(run)
        fresh = await repo.get_run(self.db, run.id)
        assert fresh is not None
        if fresh.integrity_violations > 0:
            reason = (
                f"merge gate: {fresh.integrity_violations} integrity violation(s) — "
                "merge blocked at every tier"
            )
            await self._notify(
                "error", "Girder: merge gate blocked run", f"run {run.id}: {reason}", run
            )
            return await transition_run(self.db, run.id, RunStatus.FAILED,
                                        payload={"reason": reason})
        green, tail, _red_ids = await self._final_suite_green(run, repo_path)
        await repo.insert_event(
            self.db, "delivery_suite", {"green": green, "tail_redacted": tail}, run_id=run.id
        )
        if not green:
            reason = "final run-branch suite is red (D4 check 1)"
            await self._notify(
                "error", "Girder: delivery suite red", f"run {run.id}: {reason}", run
            )
            return await transition_run(self.db, run.id, RunStatus.FAILED,
                                        payload={"reason": reason})

        sha = await self.github.push_branch(run.branch)
        tasks = await repo.list_tasks_for_run(self.db, run.id)
        body = self.github.pr_body(
            intent=run.intent,
            task_lines=[t.title for t in tasks],
            criteria=await self._criteria_of(run, repo_path),
            spend_usd=fresh.spend_usd,
            run_id=run.id,
            tasks=[
                {
                    "title": t.title,
                    "task_type": t.task_type.value,
                    "status": t.status.value,
                }
                for t in tasks
            ],
            total_attempts=sum(t.attempts_used for t in tasks),
        )
        pr_number = await self.github.open_pr(
            head=run.branch,
            base="main",
            title=f"girder: {run.intent.strip()[:80]}",
            body=body,
        )
        await repo.update_run_fields(self.db, run.id, pr_number=pr_number)
        await repo.insert_event(
            self.db, "pr_opened", {"pr_number": pr_number, "head_sha": sha}, run_id=run.id
        )
        if self.notifier is not None:
            await self.notifier.notify(
                "info",
                "Girder: PR opened",
                f"run {run.id}: PR #{pr_number} pushed ({sha[:8]})",
                run_id=run.id,
            )
        await transition_run(self.db, run.id, RunStatus.PR_OPEN, payload={"pr_number": pr_number})
        return "pr_opened"

    # ------------------------------------------------------------- PR / CI

    async def _pump_pr_open(self, run: Run) -> str:
        sha = await self._head_of(run)
        snapshot = await self._observe(run, sha)
        if snapshot is None:
            return "ci_pending"
        await transition_run(self.db, run.id, RunStatus.CI_RUNNING, payload={"head_sha": sha})
        return await self._classify_and_route(run, sha, snapshot)

    async def _pump_ci_running(self, run: Run) -> str:
        # Poll deadline (impl-plan §6.11: timeout ⇒ escalated, never poll
        # forever). The baseline is the run's most recent entry into
        # ci_running — a diagnostic fix push (ci_fixing → ci_running) resets
        # the clock, so fix-episode time never eats the post-fix CI budget —
        # and is derived from the persisted state_transition events, so it
        # survives a restart (fresh boot re-derives the same deadline).
        ts = await self._ci_running_baseline(run)
        if ts is not None:
            entered = datetime.fromisoformat(ts)
            waited_s = (datetime.now(UTC) - entered).total_seconds()
            timeout_s = self.settings.github.poll_timeout_s
            if waited_s > timeout_s:
                return await self._escalate(
                    run,
                    f"CI checks still pending after {waited_s:.0f}s in ci_running "
                    f"(budget {timeout_s:.0f}s) — poll timeout ⇒ escalated",
                )
        sha = await self._head_of(run)
        snapshot = await self._observe(run, sha)
        if snapshot is None:
            return "ci_pending"
        return await self._classify_and_route(run, sha, snapshot)

    async def _ci_running_baseline(self, run: Run) -> str | None:
        """Timestamp of the ci_running entry the poll deadline is measured from.

        The most recent ``ci_fixing → ci_running`` transition (the fix push)
        when one exists — so a fix episode buys a fresh CI budget — falling
        back to the first ci_running entry when no fix has occurred. Both come
        from persisted events, so a restart re-derives the same deadline.
        """
        latest = await repo.get_latest_event(self.db, run.id, "state_transition")
        if latest is not None and latest["payload"].get("to") == RunStatus.CI_RUNNING.value:
            return str(latest["ts"])
        return await repo.get_first_transition_ts_to(
            self.db, run.id, RunStatus.CI_RUNNING.value
        )

    async def _classify_and_route(self, run: Run, sha: str, snapshot: CiSnapshot) -> str:
        """Green ⇒ conformance; red ⇒ classify (baseline/flaky) then fix or escalate."""
        if snapshot.settled and snapshot.green:
            await transition_run(self.db, run.id, RunStatus.CONFORMANCE_REVIEW,
                                 payload={"head_sha": sha})
            return "ci_green"

        if snapshot.pending:
            return "ci_pending"

        # Red. Compare against baseline + flake registry before treating any
        # failure as novel (plan.md Phase 3 task 3 bullet 2 + task 4).
        test_ids = extract_test_ids(
            *[excerpt for check in snapshot.failed
              for excerpt in (check.output_summary, check.output_text)]
        )
        repo_path = await self._repo_path_of(run)
        project = await self._project_of(run)
        breaker = FlakyBreaker(
            db=self.db, sandbox=self.sandbox, settings=self.settings,
            redactor=self.redactor,
        )
        classifications: list[FlakeClassification]
        if test_ids:
            classifications = await breaker.classify(
                project=project, run=run, test_ids=test_ids,
                commit_sha=sha, repo_path=repo_path,
            )
        else:
            # No nodeids extractable from the CI log — classification must not
            # be skipped (impl-plan §6.11 guarantee: baseline-broken is never
            # auto-fixed). Classify from a local suite run on the run head.
            from_local = await self._classify_from_local_suite(
                run, sha, breaker, project, repo_path
            )
            if from_local is None:
                return "escalated"  # unattributable divergence — already escalated
            classifications = from_local

        verdicts = {c.verdict for c in classifications}
        if "regression" in verdicts:
            return await self._escalate(
                run,
                "new flake regression: nondeterminism appeared with the agent's diff "
                f"(SC-10): {[c.test_id for c in classifications if c.verdict == 'regression']}",
            )
        if "baseline_broken" in verdicts:
            return await self._escalate(
                run,
                "CI failures pre-exist on the baseline — never auto-fixed: "
                f"{[c.test_id for c in classifications if c.verdict == 'baseline_broken']}",
            )
        if verdicts and verdicts <= {"known_flaky"}:
            await repo.insert_event(
                self.db,
                "ci_green_known_flaky",
                {"known_flaky": sorted(test_ids)},
                run_id=run.id,
            )
            await transition_run(self.db, run.id, RunStatus.CONFORMANCE_REVIEW,
                                 payload={"head_sha": sha, "flaky": sorted(test_ids)})
            return "ci_green_flaky"
        # real_failure ⇒ fall through to the diagnostic fix agent.

        await transition_run(self.db, run.id, RunStatus.CI_FIXING,
                             payload={"failed": [c.name for c in snapshot.failed]})
        return "ci_red_fixing"

    async def _classify_from_local_suite(
        self,
        run: Run,
        sha: str,
        breaker: FlakyBreaker,
        project: Project,
        repo_path: Path,
    ) -> list[FlakeClassification] | None:
        """Classification source when the CI log yields no pytest nodeids.

        Runs the full suite locally on the run head. Locally RED ⇒ classify
        the failing set against the baseline/flake registry exactly as the
        extracted-nodeid path would — this preserves the impl-plan §6.11
        guarantee that a baseline-broken test failure is never auto-fixed,
        even when its CI log formatting defeats nodeid extraction. Locally
        GREEN ⇒ the CI failure is not a test failure at all (lint/env/
        infra-shaped) ⇒ no test could be masked, so the failure is novel and
        goes to the diagnostic fix agent (SC-09's linter-mismatch case). An
        unparseable local report is unattributable: escalate, never auto-fix.
        ``None`` ⇒ the run was escalated here.
        """
        green, _tail, red_ids = await self._final_suite_green(run, repo_path)
        if green:
            return []
        if not red_ids:
            await self._escalate(
                run,
                "CI is red and the local junit report yielded no test results — "
                "cannot classify the failure; human review required",
            )
            return None
        return await breaker.classify(
            project=project, run=run, test_ids=red_ids,
            commit_sha=sha, repo_path=repo_path,
        )

    async def _pump_ci_fixing(self, run: Run) -> str:
        """One diagnostic fix episode: build failure report, run the fix agent,
        push, and re-poll (§ Phase 3 task 3; cap enforced inside DiagnosticEngine)."""
        sha = await self._head_of(run)
        failures = await self.github.fetch_failure_logs(sha)
        parts: list[str] = []
        for check in failures:
            parts.append(f"## check: {check.name} (conclusion={check.conclusion})")
            if check.output_summary:
                parts.append(check.output_summary[:_EXCERPT_CHARS])
            if check.output_text:
                parts.append(check.output_text[:_EXCERPT_CHARS])
        report = "\n\n".join(parts) or "CI failed; no failure excerpts available."

        project = await self._project_of(run)
        repo_path = await self._repo_path_of(run)
        engine = DiagnosticEngine(
            db=self.db, gateway=self.gateway, sandbox=self.sandbox,
            settings=self.settings, redactor=self.redactor, notifier=self.notifier,
            project=project, repo_path=repo_path,
        )
        outcome = await engine.run(run=run, failure_report=report)
        if outcome.kind == "fixed":
            await self.github.push_branch(run.branch)
            await transition_run(self.db, run.id, RunStatus.CI_RUNNING,
                                 payload={"fix": outcome.detail})
            return "ci_fix_pushed"
        if outcome.kind == "budget_exhausted":
            return "budget_exhausted"
        if outcome.kind == "amendment_pending":
            # A CI fix requesting a spec amendment is an anomaly at this stage
            # (CI_FIXING has no awaiting_amendment edge): human review required.
            return await self._escalate(
                run, f"diagnostic fix agent requested a spec amendment: {outcome.detail}"
            )
        reason = f"diagnostic fix agent failed: {outcome.detail or outcome.kind}"
        await self._notify("error", "Girder: CI fix failed", f"run {run.id}: {reason}", run)
        return await transition_run(self.db, run.id, RunStatus.FAILED,
                                    payload={"reason": reason})

    # --------------------------------------------------------- conformance

    async def _pump_conformance(self, run: Run) -> str:
        repo_path = await self._repo_path_of(run)
        reviewer = ConformanceReviewer(
            db=self.db, gateway=self.gateway, redactor=self.redactor,
            settings=self.settings, client=self.github,
        )
        try:
            verdict = await reviewer.review(run=run, repo_path=repo_path)
        except ConformanceError as exc:
            return await self._escalate(run, f"conformance review failed: {exc}")

        if verdict.severity == "catastrophic":
            return await self._escalate(
                run, f"conformance: catastrophic deviation (D4, every tier): {verdict.summary}"
            )
        if verdict.severity in ("minor", "major") or verdict.undeclared_changes:
            await reviewer.post_warnings(run=run, verdict=verdict, pr_number=run.pr_number)

        project = await self._project_of(run)
        tier = project.autonomy_tier
        if tier == 0:
            return await self._park_for_human(run, project, "T0 supervised: human merges")
        unreviewed = await repo.count_unreviewed_merges(self.db, project.id)
        if tier == 1 and unreviewed >= self.settings.autonomy.t1_review_window:
            return await self._park_for_human(
                run,
                project,
                f"T1 review window full: {unreviewed} unreviewed merges "
                f"(>= {self.settings.autonomy.t1_review_window})",
            )
        if tier == 2 and project.clean_merge_streak < self.settings.autonomy.t2_required_streak:
            return await self._park_for_human(
                run,
                project,
                f"T2 not earned: clean streak {project.clean_merge_streak} < "
                f"{self.settings.autonomy.t2_required_streak} (SC-13)",
            )
        return await self._merge(run, project)

    async def _park_for_human(self, run: Run, project: Project, reason: str) -> str:
        body = (
            f"run {run.id} (project {project.name}, tier {project.autonomy_tier}): "
            f"PR #{run.pr_number} — {reason}"
        )
        # §9.2: the parked-for-human notification carries the diff summary,
        # same as the T1 merged notification.
        stat = await self._diff_stat(run, await self._repo_path_of(run))
        if stat:
            body += f"\n{stat}"
        await self._notify("info", "Girder: PR ready to merge", body, run)
        await transition_run(self.db, run.id, RunStatus.MERGE_PENDING_HUMAN,
                             payload={"reason": reason})
        return "merge_pending_human"

    # --------------------------------------------------------------- merge

    async def _merge(self, run: Run, project: Project) -> str:
        """Auto-merge (T1, or T2 with an earned streak) — WP 4.6 / §9.2."""
        fresh = await repo.get_run(self.db, run.id)
        assert fresh is not None
        if fresh.integrity_violations > 0:
            return await self._escalate(
                run, "merge gate: integrity violations appeared — merge blocked at every tier"
            )
        assert fresh.pr_number is not None
        outcome = await self.github.merge_pr(
            fresh.pr_number, commit_title=f"{fresh.branch}: {fresh.intent.strip()[:60]}"
        )
        if not outcome.merged:
            # §9.2: a refused/escalated merge is not a clean merge — reset the
            # streak (regression/baseline escalations are not merges and keep
            # their current behavior).
            await repo.reset_clean_merge_streak(self.db, project.id)
            return await self._escalate(
                run, f"GitHub merge refused for PR #{fresh.pr_number}: {outcome.reason}"
            )
        await self._post_merge(fresh, project, outcome.sha)
        await transition_run(self.db, run.id, RunStatus.MERGED, payload={"merge_sha": outcome.sha})
        return "merged"

    async def _pump_merge_pending(self, run: Run) -> str:
        """Wait for the merge: T0 for the human click, T1/T2 for window capacity.

        get_pr is throttled to ``settings.github.poll_interval_s`` per run
        (same cadence as the CI poll) — parked runs get pumped on every cycle,
        and an unthrottled poll would hammer the GitHub API. The last-poll
        map is in-memory and resets on daemon restart: worst case that costs
        one extra poll.

        §2.3: the T1 review window only pauses NEW runs — a T1 run parked by a
        full window re-evaluates the (freshly derived) unreviewed count on
        every poll and auto-merges once the user marks prior merges reviewed.
        A T2 run parked without an earned streak re-checks the streak the same
        way. T0 keeps waiting for the human merge click unconditionally."""
        assert run.pr_number is not None
        interval = self.settings.github.poll_interval_s
        last = self._last_merge_poll.get(run.id)
        if last is not None and interval > 0 and (time.monotonic() - last) < interval:
            return "merge_pending_human"
        self._last_merge_poll[run.id] = time.monotonic()
        pr = await self.github.get_pr(run.pr_number)
        project = await self._project_of(run)
        if pr.get("merged"):
            merge_sha = pr.get("merge_commit_sha")
            await self._post_merge(run, project, str(merge_sha) if merge_sha else None)
            await transition_run(self.db, run.id, RunStatus.MERGED,
                                 payload={"merge_sha": merge_sha})
            return "merged"
        tier = project.autonomy_tier
        if tier == 1:
            unreviewed = await repo.count_unreviewed_merges(self.db, project.id)
            if unreviewed < self.settings.autonomy.t1_review_window:
                return await self._merge(run, project)
        elif tier == 2 and project.clean_merge_streak >= self.settings.autonomy.t2_required_streak:
            return await self._merge(run, project)
        return "merge_pending_human"

    async def _post_merge(self, run: Run, project: Project, merge_sha: str | None) -> None:
        """§9.2 post-merge (any tier): sync main, archive spec, prune worktrees,
        streak bump, notify (rich for T0/T1; silent-but-logged for T2)."""
        repo_path = await self._repo_path_of(run)
        await self._sync_main(repo_path)
        await self._archive_spec(run, repo_path)
        await self._prune_run_worktrees(run, repo_path)
        streak = await repo.bump_clean_merge_streak(self.db, project.id)
        await repo.insert_event(
            self.db,
            "run_merged",
            {"merge_sha": merge_sha, "pr_number": run.pr_number, "clean_merge_streak": streak},
            run_id=run.id,
        )
        if self.notifier is not None and project.autonomy_tier <= 1:
            stat = await self._diff_stat(run, repo_path)
            await self.notifier.notify(
                "info",
                "Girder: merged",
                f"run {run.id} merged (PR #{run.pr_number}, streak {streak})\n{stat}",
                run_id=run.id,
            )

    # -------------------------------------------------------------- helpers

    async def _observe(self, run: Run, sha: str) -> CiSnapshot | None:
        """One CI poll, throttled to settings.github.poll_interval_s per run.
        ``None`` ⇒ throttled (treat as pending: no API call this pump)."""
        interval = self.settings.github.poll_interval_s
        last = self._last_poll.get(run.id)
        if last is not None and interval > 0 and (time.monotonic() - last) < interval:
            return None
        self._last_poll[run.id] = time.monotonic()
        return await self._poller.observe(run=run, head_sha=sha)

    async def _escalate(self, run: Run, reason: str) -> str:
        await self._notify("error", "Girder: run escalated", f"run {run.id}: {reason}", run)
        return await transition_run(self.db, run.id, RunStatus.ESCALATED,
                                    payload={"reason": reason})

    async def _notify(self, level: str, title: str, body: str, run: Run) -> None:
        if self.notifier is not None:
            await self.notifier.notify(level, title, body, run_id=run.id)

    async def _head_of(self, run: Run) -> str:
        repo_path = await self._repo_path_of(run)
        tip = await BranchOps(repo_path).run_branch_tip(run.branch)
        assert tip is not None, f"run branch {run.branch} vanished"
        return tip

    async def _project_of(self, run: Run) -> Project:
        project = await repo.get_project(self.db, run.project_id)
        if project is None:
            raise KeyError(f"project {run.project_id} not found")
        return project

    async def _repo_path_of(self, run: Run) -> Path:
        if self._repo_path is not None:
            return self._repo_path
        project = await self._project_of(run)
        return Path(project.repo_path)
