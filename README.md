# Loomstack

A standalone Python tool that turns a single git repo with four standard files into an autonomously-built project. You write the plan; tiered AI agents build it; you review on weekends.

Loomstack is **single-repo scoped**. It operates on whatever repo you point it at, like `git` or `cargo`. Working on a different project? `cd` to that repo, or use Weaver's multi-project mode.

## What Loomstack Is (and Isn't)

**Is:**
- A Python CLI + web dashboard for AI task orchestration
- A dispatcher that reads `PLAN.md` and routes tasks to tiered AI workers by role
- Agent adapters that wrap `claude-code` pointed at different model endpoints (local Qwen, Opus, Gemini)
- A reusable bootstrap pack you paste into Claude Code to generate new project scaffolding
- File-state based: `PLAN.md` defines work, git branches/PRs track progress, `.loomstack/` holds runs + ledger + approvals. No database.

**Isn't:**
- A multi-project manager. One repo per run. `cd` to switch (or use Weaver's sidebar selector).
- A worker framework written from scratch. Workers are `claude-code` subprocesses pointed at different endpoints per tier.

## Worker Tiers

| Role | Runtime | Endpoint | Responsibility |
|------|---------|----------|----------------|
| **Classifier** | `claude-code` → Mac Mini | Qwen3-8B Q4 | First-pass triage; routes each task to the right tier |
| **Mac Worker** | `claude-code` → Mac Mini | Qwen3-8B Q4 | Lightweight reviews, doc edits, trivial refactors |
| **Code Worker** | `claude-code` → GX10 | Qwen3-Coder-Next | Primary implementation; ~70% of tasks |
| **Content Worker** | `claude-code` → GX10 (swap-in) | Qwen3-235B-A22B | Narrative, lore, complex docs |
| **Reviewer** | API client | Gemini 2.5 Flash → Pro | PR review, spec-compliance, test adequacy |
| **Architect** | `claude-code` → Anthropic | Opus 4.6 | Design, escalation, hard debugging. **Always gated by approval.** |
| **Researcher** | API client | Gemini + web search | Library vetting, upstream bug search |
| **Test Runner** | Shell | (no LLM) | Runs the project's configured CI command |

Adding a tier = one file in `blueprint/src/loomstack/agents/` + one entry in endpoint config.

## Architecture

```
Ubuntu workstation (always-on)
└── Loomstack (Python, async)
    ├── plan_parser   → PLAN.md → task records
    ├── dispatcher    → routes tasks by role
    ├── agents/       → spawn claude-code subprocesses per tier
    ├── ledger        → .loomstack/ledger.jsonl
    ├── state         → derives status from git + files
    ├── github        → gh CLI wrapper
    └── weaver        → FastAPI web dashboard

                │
    ┌───────────┼──────────────────┐
    ▼           ▼                  ▼
GX10 llama     Mac Mini llama     Cloud APIs
  Qwen Coder     Qwen3-8B Q4       Opus (architect)
  Qwen 235B      embeddings        Gemini (reviewer/researcher)
```

Hardware provisioning lives in [`ailab-infra`](https://github.com/ronkam/ailab-infra).

## State Model (No Database)

Every piece of runtime state is a file re-read each cycle.

| State | Source |
|-------|--------|
| Task definitions | `PLAN.md` (re-parsed every cycle) |
| Task progress | Git branches + open PRs (via `gh`) |
| Per-task history + retry state | `.loomstack/runs/<task-id>.md` (multi-block frontmatter → `RunMeta`) |
| Cost audit trail | `.loomstack/ledger.jsonl` (append-only, file-locked) |
| Approval gates | `.loomstack/approvals/<task-id>` marker files |
| Agent output | Branch commits |

Daemon is stateless. Restart any time with zero data loss. `git log` is your audit trail.

## Project Repo Layout (any repo Loomstack manages)

```
your-project/
├── README.md                    ← project overview (you write)
├── CLAUDE.md                    ← project-specific agent instructions
├── PLAN.md                      ← tasks
├── loomstack.yaml               ← project config
├── .loomstack/                  ← runtime state
│   ├── runs/*.md
│   ├── ledger.jsonl
│   └── approvals/
├── .github/workflows/           ← CI that test_runner invokes
└── src/ or crates/ or lib/      ← the actual code
```

Loomstack refuses to run if any of the four top-level files are missing or invalid.

## Loomstack's Own Repo Layout

```
loomstack/
├── README.md  CLAUDE.md  BOOTSTRAP_PROMPT.md
├── blueprint/                   ← Python package
│   ├── pyproject.toml
│   └── src/loomstack/
│       ├── core/                ← plan_parser, dispatcher, state, ledger, budget, github, gx10
│       ├── agents/              ← claude_code_runner + per-tier wrappers + reviewer + researcher + test_runner
│       ├── weaver/              ← FastAPI dashboard (routes, templates, static)
│       └── runner.py            ← entry point
├── bootstrap/                   ← authoring pack (docs, shipped as data)
│   ├── PLAN_SCHEMA.md  ROADMAP.md
│   └── (INFRA_SPEC, PROJECT_CONTRACT, ESCALATION_RULES — planned)
├── templates/project/           ← 4-file scaffold copied by `loomstack init`
└── tests/
```

## CLI

```
loomstack init                   # scaffold the 4 files in cwd
loomstack parse                  # validate PLAN.md + show task graph
loomstack run [--once]           # dispatch ready tasks
loomstack status                 # task graph + current state
loomstack approve <task>         # unblock approval gate
loomstack escalate <task>        # force-promote to architect
loomstack cost [--since 7d]      # spend breakdown
loomstack doctor                 # health-check endpoints
loomstack weaver                 # start the Weaver web dashboard
```

## Weaver Dashboard

Weaver is a local web dashboard for monitoring and interacting with Loomstack. Built with FastAPI, Jinja2, and HTMX — no JavaScript framework, no build step.

```bash
loomstack weaver                  # start on default port (8400)
```

**Pages:**

| Page | Path | What it shows |
|------|------|---------------|
| Dashboard | `/` | Overview with health status and budget snapshot |
| Tasks | `/tasks` | Task table with status badges, dependency graph (dagre-d3), and detail side panel |
| Budget | `/budget` | Daily spend, history chart, recent ledger entries |
| Health | `/health` | Endpoint health checks |
| Chat | `/chat` | LLM chat interface for ad-hoc queries |
| Providers | `/providers` | CRUD for API provider configs (endpoint, key, cost, rate limits) |
| Workers | `/workers` | CRUD for worker tier configs (tier, provider, model, timeout) |
| Dispatcher | `/dispatcher` | Start/stop the dispatch loop; live status, cycle times, dispatch counts |

**Key features:**
- **Live task table** — auto-refreshes every 10s via HTMX; click any row for full detail panel
- **Dependency graph** — interactive DAG visualization with zoom/pan (d3 + dagre-d3)
- **Inline approvals** — approve gated tasks directly from the table; button swaps to badge without page reload
- **Run logs** — rendered markdown view of `.loomstack/runs/<task-id>.md` per task
- **Multi-project mode** — configure multiple project dirs; sidebar selector switches between them
- **Provider & worker management** — configure API endpoints and per-tier model assignments from the UI; persisted to `~/.loomstack/weaver/`
- **JSON API** — every page has a corresponding `/api/` endpoint returning JSON for scripting

Weaver is read-only for project state (it never modifies `PLAN.md` or pushes code). The dispatcher page can start/stop the dispatch loop, and the approval/provider/worker endpoints modify their respective stores.

## Bootstrap Flow

The bootstrap prompt lives at [`BOOTSTRAP_PROMPT.md`](./BOOTSTRAP_PROMPT.md) at
the repo root. It is self-contained so you can copy it into any new repo and
use it without pulling in the rest of Loomstack.

1. Create the new project repo (`gh repo create my-thing --private`).
2. Copy `BOOTSTRAP_PROMPT.md` into it (or just open the file from this repo).
3. Open a fresh **Claude Opus** chat — claude.ai or `claude` CLI.
4. Paste the **Prompt** section (everything between the two `---` rules).
5. Opus interviews you (~7 questions: language, deps, quality bar, etc.).
6. Opus emits four files: `README.md`, `CLAUDE.md`, `PLAN.md`, `loomstack.yaml`.
7. Save them in the new repo, commit, push.
8. On the workstation: `cd` into the repo and `loomstack run`.
9. Review PRs. Approve architect-tier tasks via `loomstack approve <task>`.

~20 minutes from idea to agents running.

**Why Opus and not Code Worker?** Bootstrap planning needs strong reasoning over
the whole project shape. Code Worker (Qwen) handles the tasks Opus generates,
but is too small-context for the planning step itself.

**When to re-run the prompt.** Only at project birth. Once `PLAN.md` exists,
edit it directly — re-running the prompt would clobber task IDs and history.
For mid-project replanning, ask Opus to "extend the existing PLAN.md with
tasks for X, preserving all existing IDs."

## Quick Start

### 1. Install Loomstack

```bash
git clone https://github.com/ronkam/loomstack.git ~/loomstack
cd ~/loomstack/blueprint
uv sync
```

### 2. Configure endpoints

Edit `loomstack.yaml` in your project to point each tier at your endpoints (local llama-server, Anthropic API, Google AI).

```bash
loomstack doctor                 # verify all endpoints are reachable
```

### 3. Onboard first project

```bash
cd ~/src/meshcord
loomstack init
# Edit the four files (or use bootstrap flow)
loomstack parse
loomstack run
```

## Cost Controls

- **Daily cap** in `loomstack.yaml::budget_daily_usd` (hard-stop, resets midnight UTC)
- **Architect approval gate** — Opus never runs without `.loomstack/approvals/<task>` marker
- **Reviewer defaults cheap** — Gemini Flash first; Pro only on self-flagged low confidence
- **Worker retries bounded** — 2 retries with expanded context before escalating
- **Diff size cap** — PRs over `loomstack.yaml::max_diff_loc` rejected, forcing plan decomposition

Typical monthly burn, 30-min/day cadence, one active project: **$25–$60**. Local workers free.

## License

MIT
