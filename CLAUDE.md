# CLAUDE.md — Loomstack Project Instructions

> Every Claude Code session working on Loomstack MUST read this file first.
> This governs work on Loomstack itself — not projects Loomstack manages.
> Per-project `CLAUDE.md` files live in each managed repo and govern work there.

---

## What Loomstack Is

A NemoClaw plugin providing single-repo AI task orchestration. TypeScript CLI layer + Python blueprint (OCI-packaged). Single-repo scope. File-based state. Worker tiers backed by `claude-code` subprocesses pointed at different endpoints.

Loomstack is **project-agnostic**. Zero code may reference MeshCord, Fateweaver, or any specific project. Project behavior lives in each repo's `CLAUDE.md` + `PLAN.md` + `loomstack.yaml`.

If you find yourself writing `if project == "..."`, stop. That logic belongs in the project's own config.

## North Star Principles

1. **Single-repo scope.** No multi-project features. `cd` to switch projects.
2. **File state only.** No database. PLAN.md + git + `.loomstack/` + GitHub = all runtime state.
3. **Daemon is stateless and restartable.** Crash mid-task → next cycle rebuilds state from files.
4. **Agents idempotent.** Re-running a task from the same inputs produces no duplicate side effects.
5. **Small tasks.** Each task closes with a mergeable PR ≤ `max_diff_loc` (default 400). Over limit → reject, decompose.
6. **Cost is first-class.** Every external API call through `budget.py`. No exceptions.
7. **Human reviews, does not operate.** Architect tier gated. Every behavior interruptible.
8. **Local first, cloud on escalation.** Qwen workers handle majority. Cloud tiers for review/architecture/escalation.
9. **NIM is never a default.** Privacy Router must name non-NIM endpoints for every tier.
10. **Failures loud.** No silent retries or swallowed exceptions. Surface within one polling interval.

## Tech Stack (pinned)

**Plugin layer (TypeScript):**
- Node 20+, TypeScript 5.3+
- NemoClaw plugin API via `openclaw.extensions`
- Thin: argument parsing, dispatch to blueprint. <500 LOC total expected.

**Blueprint layer (Python):**
- Python 3.11+
- `uv` for deps (`uv.lock` committed)
- Async by default (`asyncio`). File I/O via `aiofiles` when hot; stdlib otherwise.
- SQLite is **not** a dependency. No database libraries.
- `pydantic` for config/schema validation
- `anthropic`, `google-genai` for API clients
- `openai` package for OpenAI-compatible local endpoints
- `httpx` for HTTP
- `pytest`, `pytest-asyncio` for tests
- `ruff` (format + lint), `mypy --strict` on `blueprint/src/loomstack/core/` and `agents/`
- `portalocker` for ledger file locking

**Packaging:**
- Blueprint built as OCI artifact per NemoClaw spec. `Dockerfile` + `blueprint.yaml` manifest.
- Plugin published as TS package registered with NemoClaw on `install-plugin.sh`.
- Pin NemoClaw version explicitly; alpha software.

Do not add dependencies without updating this list and justifying why stdlib/existing deps were insufficient.

## Directory Contract

- `plugin/` — TS only. Imports nothing from `blueprint/`. Invokes blueprint via NemoClaw's runner API.
- `blueprint/src/loomstack/core/` — pure Python, no I/O side effects in module scope. All async.
- `blueprint/src/loomstack/agents/` — one file per tier. All implement `BaseAgent` (`agents/base.py`). No cross-agent imports.
- `bootstrap/` — documentation only. No executable code. Shipped as data files inside the blueprint OCI.
- `templates/project/` — the 4-file scaffold. Checked at build time for schema validity.
- `policies/` — YAML only. NemoClaw reads these directly; no Python parses them.
- `tests/` — mirrors source. `tests/blueprint/core/test_plan_parser.py` tests `blueprint/src/loomstack/core/plan_parser.py`.

## Code Style

- Type-hint everything. `mypy --strict` CI-enforced on `blueprint/src/loomstack/core/` and `agents/`.
- No `print()` outside `scripts/`. Use `structlog` with config in `blueprint/src/loomstack/logging.py`.
- Async for all I/O (LLM calls, subprocess, HTTP, file I/O in hot paths).
- `except Exception: pass` is a CI failure.
- No global state except logger + env config object loaded at startup.
- Subprocess calls to `claude-code` use `asyncio.create_subprocess_exec`; never `subprocess.run`.

## Commit + Branch Conventions

- Branch naming: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`, `agent/<role>-<change>`.
- Conventional Commits (`feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`).
- One logical change per PR.
- Every PR: tests updated, `mypy` clean, `ruff check` clean, `ruff format` applied.
- Max diff: 400 LOC by default. Tasks that can't fit must decompose.

## Agent Interface Contract

All agents implement `agents/base.py`:

```python
class BaseAgent(Protocol):
    role: str                             # stable id: "code_worker", "reviewer", ...
    model_id: str                         # human-readable, logged

    async def can_handle(self, task: Task) -> bool: ...
    async def execute(self, task: Task, ctx: TaskContext) -> AgentResult: ...
    def estimate_cost_usd(self, task: Task) -> float: ...
```

Results are one of:
- `Proposed(branch, pr_url)` — PR opened, awaiting review/CI
- `Blocked(reason)` — needs gate, approval, or missing context
- `Failed(error, retry_context)` — retry with expanded context

Rules:
- `execute()` must be idempotent. Re-running from the same inputs creates no duplicate PRs/commits.
- Cost estimation mandatory. Budget system rejects tasks whose estimate exceeds daily cap before `execute()`.
- Agents never commit to default branch directly. Always a feature branch + PR via `core/github.py`.
- Agents never write to `.loomstack/ledger.jsonl` directly. Return `AgentResult`; dispatcher writes ledger.
- Subprocess-based agents (`claude-code` wrappers) stream output to `.loomstack/runs/<task-id>.md` for audit.

## The claude_code_runner Primitive

`agents/claude_code_runner.py` is the core. Every Claude-Code-backed tier (Code/Mac/Content/Architect workers) wraps it.

Signature:
```python
async def run_claude_code(
    endpoint: str,           # e.g., http://gx10.local:8080/v1
    model: str,              # e.g., qwen3-coder-next
    repo_path: Path,
    task: Task,
    claude_md_path: Path,    # repo-local CLAUDE.md
    timeout_s: int = 1800,
) -> ClaudeCodeResult: ...
```

Implementation:
- Spawns `claude-code` as subprocess with `ANTHROPIC_BASE_URL=<endpoint>` and `ANTHROPIC_MODEL=<model>`.
- cwd = `repo_path`.
- Streams stdout/stderr to `.loomstack/runs/<task-id>.md`.
- Parses tail for success/failure signals.
- Returns result with token counts + cost estimate.

## Escalation Ladder

Generic; project-specific rules in `loomstack.yaml::escalation_rules`.

```
classifier ──► code_worker ──2 retries──► reviewer ──fails──► architect ──approval──► runs
                   │
                   └── tag: security|breaking_change ──► architect (always, approval required)
```

- Retries expand context: first adds failing test output (`TaskContext.prior_error`); second adds linked files from failed diff (`TaskContext.extra_context_files`).
- `human_review: true` pauses after assigned role completes, waits for approval marker.
- Architect tasks ALWAYS pause. Write `.loomstack/approvals/<task-id>` to unblock. No exceptions.
- Retry state (count, tier, last error, last diff) is stored in run-file frontmatter and read back via `RunMeta`.

## Budget System

- Every API call: `await budget.check(tier, estimated_usd)` BEFORE call; `await budget.charge(tier, actual_usd)` AFTER.
- Caps in `loomstack.yaml::budget_daily_usd` and `.env::GLOBAL_DAILY_CAP_USD`.
- Exceeding cap does not crash; requeues task for next day, writes notice to `.loomstack/runs/<task-id>.md`.
- Ledger is append-only JSONL with `portalocker` file lock on write.

## State Derivation

`core/state.py` exposes two levels of state query:

- `derive_status(task_id) -> TaskStatus` — the status-only fast path (PENDING / IN_PROGRESS / PROPOSED / BLOCKED / DONE / FAILED).
- `derive_run_meta(task_id) -> RunMeta` — full run-file metadata including `status`, `tier`, `retry_count`, `last_error`, `last_diff`, cost/token fields. Used by the dispatcher for escalation decisions.

Status derivation combines:
1. `.loomstack/runs/<task-id>.md` frontmatter `status:` field (authoritative if present)
2. GitHub: open PR with branch matching pattern → PROPOSED
3. Local git: feature branch exists → IN_PROGRESS
4. Else: PENDING

**Run-file multi-block frontmatter:** Run files may contain multiple `---` frontmatter blocks (initial header + completion footer). The parser merges all blocks, with later values overriding earlier ones. This lets `claude_code_runner` append a footer (status: done/failed) without rewriting the initial header.

## Testing

- Unit tests for every module in `core/` and `agents/`.
- Agents tested against recorded LLM responses (fixtures in `tests/fixtures/llm/`). No real API calls in CI.
- Integration test `tests/integration/test_end_to_end.py`: synthetic task through dispatch → mock agent → fake PR → ledger entry. Must pass before every merge.
- Coverage: 80% on `core/`, 70% on `agents/`.
- Never make real external API calls in tests. Everything mocked or recorded.

## Load-Bearing Subsystems (require approval to change)

These need `human_review: true` + architect review for any change:

- `core/budget.py` — cost guardrails
- `core/state.py` — status derivation logic
- `core/dispatcher.py` escalation logic
- `agents/base.py` — interface changes ripple to every agent
- `agents/claude_code_runner.py` — every worker tier depends on it
- `bootstrap/` — reusable artifact; changes ripple to every future project
- `policies/privacy-router.yaml` — NIM-free routing invariant
- `blueprint/blueprint.yaml` — OCI packaging manifest

## Safe for Code Worker Tier

- New agent implementations in `agents/`
- New NemoClaw CLI commands in `plugin/src/commands/`
- Scripts, test additions, documentation outside `bootstrap/`
- Logging/observability improvements

## Known Gotchas

| Issue | Mitigation |
|-------|------------|
| NemoClaw alpha; breaking changes possible | Pin blueprint + plugin versions in `~/.nemoclaw/config.json` |
| NemoClaw defaults routes to NIM | `privacy-router.yaml` names explicit non-NIM endpoints; `doctor` verifies |
| `claude-code` subprocess can hang on prompt | 30-min timeout; kill + mark FAILED with retry context |
| `watchdog` fires multiple events per save on macOS | Debounce PLAN.md changes 500ms in watcher |
| GitHub rate limits | Serialize `gh` calls; respect `X-RateLimit-Remaining` |
| Opus rate limits on burst | `max_concurrent: 1` for role `architect` (enforced in dispatcher) |
| Qwen occasionally returns malformed JSON | Wrap parses in `agents/_json_utils.py` retry-with-stricter-prompt helper |
| `claude_code_runner` parses stdout via regex | Fragile if output format changes. Migrate to `--output-format json` when schema is verified. |

## Reference Docs

- `bootstrap/INFRA_SPEC.md` — canonical description of Loomstack for external LLMs
- `bootstrap/PROJECT_CONTRACT.md` — the four-file schema
- `bootstrap/PLAN_SCHEMA.md` — task format spec
- `bootstrap/ESCALATION_RULES.md` — tier promotion details
- `docs/NEMOCLAW_FALLBACK.md` — how to lift Loomstack out of NemoClaw if needed
- [`ailab-infra`](https://github.com/ronkam/ailab-infra) — hardware layer
