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
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from girder.budget.guard import BudgetExceeded
from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus, WorktreeState
from girder.fsm import transition_run
from girder.github.ci import CiPoller, CiSnapshot, extract_test_ids
from girder.github.client import GitHubClient
from girder.github.conformance import ConformanceError, ConformanceReviewer
from girder.github.diagnostic import DiagnosticEngine
from girder.github.flaky import FlakeClassification, FlakyBreaker
from girder.gitops.branch import BranchOps
from girder.gitops.worktree import WorktreeManager
from girder.guard.redact import Redactor
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.orchestrator.baseline import parse_junit_xml
from girder.sandbox.engine import ContainerSpec, SandboxEngine, SandboxTimeout
from girder.specs.validator import SpecValidationError, parse_spec
from girder.util import run_host_cmd, utcnow_iso

log = logging.getLogger(__name__)

DELIVERY_XML = ".girder-delivery.xml"
SUITE_CMD = [
    "python",
    "-m",
    "pytest",
    "-q",
    f"--junitxml=/workspace/{DELIVERY_XML}",
    "-p",
    "no:cacheprovider",
]
_TAIL_CHARS = 2000
_EXCERPT_CHARS = 4000
_PROPOSAL_PATH = "openspec/proposals/{run_id}.md"


class DeliveryEngine:
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
        # forever). Measured from the run's FIRST entry into ci_running —
        # ci_fixing → ci_running re-entries do not reset it — and derived
        # from the persisted state_transition event, so it survives a
        # restart (fresh boot re-derives the same deadline).
        ts = await repo.get_first_transition_ts_to(
            self.db, run.id, RunStatus.CI_RUNNING.value
        )
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
        """T0: wait for the human merge click; detect out-of-band merges."""
        assert run.pr_number is not None
        pr = await self.github.get_pr(run.pr_number)
        if pr.get("merged"):
            project = await self._project_of(run)
            merge_sha = pr.get("merge_commit_sha")
            await self._post_merge(run, project, str(merge_sha) if merge_sha else None)
            await transition_run(self.db, run.id, RunStatus.MERGED,
                                 payload={"merge_sha": merge_sha})
            return "merged"
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

    async def _final_suite_green(
        self, run: Run, repo_path: Path
    ) -> tuple[bool, str | None, list[str]]:
        """Run the full suite on the run branch in a sandbox (D4 check 1).

        Returns ``(green, redacted tail when red, failing nodeids)`` — the
        nodeids feed baseline/flaky classification when the CI log itself
        yields none (impl-plan §6.11).
        """
        branch_ops = BranchOps(repo_path)
        tip = await branch_ops.run_branch_tip(run.branch)
        if tip is None:
            return False, f"run branch missing: {run.branch}", []
        tmp = Path(tempfile.mkdtemp(prefix="girder-delivery-"))
        worktree = tmp / "wt"
        container: str | None = None
        try:
            await run_host_cmd(
                ["git", "-C", str(repo_path), "worktree", "add", "--detach", str(worktree), tip],
                timeout_s=60,
            )
            container = await self.sandbox.start(
                ContainerSpec(
                    name=f"girder-delivery-{run.id[:8]}",
                    image=f"girder-runner:{self.settings.project.stack}",
                    worktree=worktree,
                    network="none",
                )
            )
            exec_res = await self.sandbox.exec(
                container,
                SUITE_CMD,
                timeout_s=float(self.settings.limits.attempt_wallclock_s),
            )
            xml = worktree / DELIVERY_XML
            if exec_res.timed_out:
                return False, "delivery suite exceeded wall-clock limit", []
            if exec_res.exit_code != 0 and not xml.exists():
                return False, self._tail(exec_res.stderr), []
            if not xml.exists():
                return False, "junit report missing after delivery suite", []
            results = parse_junit_xml(xml.read_text(errors="replace"))
            if not results:
                return False, "delivery junit report contained zero testcases", []
            red = sorted(
                t for t, r in results.items() if r.status not in ("passed", "skipped")
            )
            tail = self._tail(exec_res.stdout + exec_res.stderr) if red else None
            return (not red), tail, red
        except SandboxTimeout:
            return False, "delivery suite sandbox timeout", []
        finally:
            if container is not None:
                await self.sandbox.kill(container)
            await self._cleanup_worktree(repo_path, worktree)
            shutil.rmtree(tmp, ignore_errors=True)

    def _tail(self, text: str | None) -> str | None:
        if not text:
            return None
        clean, _ = self.redactor.redact(text[-_TAIL_CHARS:])
        return clean

    async def _criteria_of(self, run: Run, repo_path: Path) -> list[str]:
        """Success criteria from the frozen proposal (best effort, PR body only)."""
        path = _PROPOSAL_PATH.format(run_id=run.id)
        proc = await run_host_cmd(
            ["git", "-C", str(repo_path), "show", f"{run.branch}:{path}"],
            check=False,
            timeout_s=30,
        )
        if proc.returncode != 0:
            return []
        try:
            spec = parse_spec(proc.stdout)
        except SpecValidationError:
            return []
        return [c for t in spec.tasks for c in t.success_criteria]

    async def _sync_main(self, repo_path: Path) -> None:
        """Fast-forward local ``main`` to the remote after a merge (best effort)."""
        try:
            await run_host_cmd(
                ["git", "-C", str(repo_path), "fetch", self.settings.github.remote, "main"],
                timeout_s=120,
            )
            fetched = (
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "rev-parse", "FETCH_HEAD"], timeout_s=30
                )
            ).stdout.strip()
            old = await BranchOps(repo_path).run_branch_tip("main")
            if old is None:
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "update-ref", "refs/heads/main", fetched],
                    timeout_s=30,
                )
                return
            if old == fetched:
                return
            # ff-only move: old must be an ancestor of fetched
            check = await run_host_cmd(
                ["git", "-C", str(repo_path), "merge-base", "--is-ancestor", old, fetched],
                check=False,
                timeout_s=30,
            )
            if check.returncode == 0:
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "update-ref", "refs/heads/main", fetched, old],
                    timeout_s=30,
                )
        except Exception as exc:  # bookkeeping must never fail the merged run
            log.warning("main sync after merge failed: %s", exc)

    async def _archive_spec(self, run: Run, repo_path: Path) -> None:
        """``openspec/proposals/<run>.md`` → ``openspec/archive/<date>-<run8>.md``
        as a commit on local main (§9.2), via a detached throwaway worktree."""
        proposal = _PROPOSAL_PATH.format(run_id=run.id)
        date = utcnow_iso()[:10].replace("-", "")
        archive = f"openspec/archive/{date}-{run.id[:8]}.md"
        try:
            check = await run_host_cmd(
                ["git", "-C", str(repo_path), "cat-file", "-e", f"main:{proposal}"],
                check=False,
                timeout_s=30,
            )
            if check.returncode != 0:
                return
            main_tip = (
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "rev-parse", "refs/heads/main"], timeout_s=30
                )
            ).stdout.strip()
            tmp = Path(tempfile.mkdtemp(prefix="girder-archive-"))
            worktree = tmp / "wt"
            try:
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "worktree", "add", "--detach",
                     str(worktree), main_tip],
                    timeout_s=60,
                )
                (worktree / "openspec" / "archive").mkdir(parents=True, exist_ok=True)
                await run_host_cmd(
                    ["git", "-C", str(worktree), "mv", proposal, archive], timeout_s=30
                )
                await run_host_cmd(
                    ["git", "-C", str(worktree), "commit", "-m", f"spec: archive {run.id[:8]}"],
                    env={
                        "GIT_AUTHOR_NAME": "girder",
                        "GIT_AUTHOR_EMAIL": "girder@localhost",
                        "GIT_COMMITTER_NAME": "girder",
                        "GIT_COMMITTER_EMAIL": "girder@localhost",
                    },
                    timeout_s=60,
                )
                new_tip = (
                    await run_host_cmd(
                        ["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout_s=30
                    )
                ).stdout.strip()
                await run_host_cmd(
                    ["git", "-C", str(repo_path), "update-ref", "refs/heads/main",
                     new_tip, main_tip],
                    timeout_s=30,
                )
            finally:
                await self._cleanup_worktree(repo_path, worktree)
                shutil.rmtree(tmp, ignore_errors=True)
        except Exception as exc:  # bookkeeping must never fail the merged run
            log.warning("spec archive after merge failed: %s", exc)

    async def _prune_run_worktrees(self, run: Run, repo_path: Path) -> None:
        """Remove any worktree rows belonging to this run's attempts (§9.2)."""
        manager = WorktreeManager(repo_path)
        run_tasks = {t.id for t in await repo.list_tasks_for_run(self.db, run.id)}
        for wt in await repo.list_worktrees(self.db):
            attempt = await repo.get_attempt(self.db, wt.attempt_id)
            if attempt is None or attempt.task_id not in run_tasks:
                continue
            try:
                await manager.remove(wt.path)
                await repo.set_worktree_state(self.db, wt.id, WorktreeState.PRUNED)
            except Exception as exc:
                log.warning("worktree prune failed for %s: %s", wt.path, exc)

    async def _diff_stat(self, run: Run, repo_path: Path) -> str:
        try:
            proc = await run_host_cmd(
                ["git", "-C", str(repo_path), "diff", "--stat", f"main...{run.branch}"],
                check=False,
                timeout_s=30,
            )
            clean, _ = self.redactor.redact(proc.stdout.strip()[-_TAIL_CHARS:])
            return clean
        except Exception:  # pragma: no cover - cosmetic
            return ""

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

    async def _cleanup_worktree(self, repo_path: Path, worktree: Path) -> None:
        try:
            await run_host_cmd(
                ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree)],
                timeout_s=60,
            )
            await run_host_cmd(["git", "-C", str(repo_path), "worktree", "prune"], timeout_s=60)
        except Exception as exc:  # backstop: tempdir rmtree
            log.warning("delivery worktree cleanup failed for %s: %s", worktree, exc)
