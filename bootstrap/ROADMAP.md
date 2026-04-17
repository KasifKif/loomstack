# Loomstack Roadmap

> Feature roadmap and known gaps. Updated 2026-04-16.

---

## Current State (~40% implemented)

**Working:** Weaver dashboard (tasks, budget, health, chat, approvals, providers,
workers, git-project management), plan parser, state derivation, budget system,
claude_code_runner primitive.

**Missing:** Dispatcher, agent implementations, GitHub integration, bootstrap
docs, NemoClaw plugin layer.

---

## Tier 1 — Make agents actually run

1. **Dispatcher** (`core/dispatcher.py`) — The core loop: read PLAN.md, derive
   task states, match ready tasks to agent tiers, execute, handle results.
   Without this, nothing runs autonomously.

2. **Code Worker agent** — Wrap `claude_code_runner` with the `BaseAgent`
   protocol. Accept a task, create a branch, run claude-code, open a PR.
   This handles ~70% of tasks.

3. **Classifier agent** — Triage incoming tasks: read description, decide which
   tier handles it, tag security/breaking_change for architect escalation.

4. **GitHub integration** (`core/github.py`) — Create branches, open PRs, check
   CI status, read review comments. Every agent result ends with a PR.

## Tier 2 — Close the feedback loop

5. **Reviewer agent** — Read a PR diff, run tests, provide structured review
   feedback. Gates the merge.

6. **Escalation engine** — When a code worker fails twice, promote to
   reviewer → architect. Track retry count, expand context on each retry
   (add failing test output, then linked files).

7. **Architect agent** — Design-level reasoning, decomposition of tasks that
   are too large, hard debugging. Always gated by human approval.

8. **Webhook/polling for CI status** — After a PR is opened, poll GitHub Actions
   (or receive webhooks) to know when CI passes/fails and feed that back into
   the task state.

## Tier 3 — Operational robustness

9. **Daemon mode with file watcher** — Watch PLAN.md for changes, re-parse,
   dispatch new tasks. Crash-safe: rebuild state from files on restart.

10. **Cost dashboard live updates** — Real-time cost tracking per task run,
    daily/weekly burn charts, alerts when approaching caps.

11. **Task decomposition** — When a task exceeds `max_diff_loc` (400),
    automatically propose a breakdown into subtasks. The architect agent does
    this, but the UI needs to show/approve the decomposition.

12. **Run log streaming** — Stream claude-code subprocess output to the Weaver
    UI in real-time via SSE or WebSocket, instead of waiting for completion.

## Tier 4 — Multi-project and polish

13. **Project health dashboard** — Per-project view: task burn-down, cost trend,
    agent utilization, blocked tasks, pending approvals.

14. **Provider health monitoring** — Ping configured API endpoints periodically,
    track latency/error rates, auto-failover to backup provider.

15. **Approval workflow** — Approval queues in the UI, Slack/Discord
    notifications for pending approvals, expiration timers.

16. **Bootstrap CLI** — `loomstack init` copies the 4-file scaffold, runs the
    bootstrap prompt, commits. Docs reference this but it doesn't exist yet.

---

## Known Architecture Gaps (documented ≠ implemented)

| Component | Status |
|-----------|--------|
| `core/dispatcher.py` | Not started — load-bearing, needs architect approval |
| `core/github.py` | Not started |
| `agents/classifier.py` | Not started |
| `agents/code_worker.py` | Not started |
| `agents/reviewer.py` | Not started |
| `agents/architect.py` | Not started |
| `bootstrap/INFRA_SPEC.md` | Missing |
| `bootstrap/PROJECT_CONTRACT.md` | Missing |
| `bootstrap/ESCALATION_RULES.md` | Missing |
| `bootstrap/BOOTSTRAP_PROMPT.md` | Missing |
| `templates/project/` | Missing (4-file scaffold) |
| `policies/privacy-router.yaml` | Missing |
| `plugin/` (TypeScript) | Missing entirely |
| `tests/integration/test_end_to_end.py` | Missing |
| `tests/fixtures/llm/` | Missing |
