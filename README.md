# Loomstack

A NemoClaw plugin that turns a single git repo with four standard files into an autonomously-built project. You write the plan; tiered AI agents build it; you review on weekends.

Loomstack is **single-repo**. It operates on whatever repo you run it in, like `git` or `cargo`. If you're working on a different project, `cd` to that repo.

Loomstack runs as a plugin inside NemoClaw's OpenShell sandbox. NemoClaw gives us kernel-level network isolation, audit logging, and approval gates for free; Loomstack provides the plan-to-PR dispatch logic on top.

## What Loomstack Is (and Isn't)

**Is:**
- A NemoClaw plugin (TypeScript CLI surface + versioned Python blueprint)
- A dispatcher that reads `PLAN.md` and routes tasks to tiered AI workers by role
- Agent adapters that wrap `claude-code` pointed at different model endpoints (local Qwen, Opus, Gemini)
- A reusable bootstrap pack you paste into Claude Code to generate new project scaffolding
- File-state based: `PLAN.md` defines work, git branches/PRs track progress, `.loomstack/` holds runs + ledger + approvals. No database.

**Isn't:**
- A multi-project manager. One repo per daemon. `cd` to switch.
- A standalone service. Runs inside NemoClaw; uses NemoClaw's CLI, logs, and approval surface.
- An NVIDIA lock-in. Privacy Router is configured to route to **your** endpoints only; NIM/Nemotron is never a default.
- A Discord bot or chat frontend. NemoClaw's native CLI + GitHub PR notifications are the operator surface.
- A worker framework written from scratch. Workers are `claude-code` subprocesses pointed at different endpoints per tier.

## Worker Tiers

| Role | Runtime | Endpoint | Responsibility |
|------|---------|----------|----------------|
| **Classifier** | `claude-code` → Mac Mini | Qwen3-8B Q4 | First-pass triage; routes each task to the right tier |
| **Mac Worker** | `claude-code` → Mac Mini | Qwen3-8B Q4 | Lightweight reviews, doc edits, trivial refactors |
| **Code Worker** | `claude-code` → GX10 | Qwen3-Coder-Next | Primary implementation; ~70% of tasks |
| **Content Worker** | `claude-code` → GX10 (swap-in) | Qwen3-235B-A22B | Narrative, lore, complex docs |
| **Reviewer** | API client | Gemini 2.5 Flash → Pro | PR review, spec-compliance, test adequacy |
| **Architect** | `claude-code` → Anthropic | Opus 4.6 | Design, escalation, hard debugging. **Gated by NemoClaw approval.** |
| **Researcher** | API client | Gemini + web search | Library vetting, upstream bug search |
| **Test Runner** | Shell | (no LLM) | Runs the project's configured CI command |

Adding a tier = one file in `blueprint/src/loomstack/agents/` + one entry in `policies/privacy-router.yaml`.

## Architecture

```
Ubuntu workstation (always-on)
└── NemoClaw sandbox (OpenShell: Landlock + seccomp + namespaces)
    ├── Privacy Router (routes by tier, enforces allowlist)
    └── Loomstack blueprint (Python, async)
        ├── plan_parser   → PLAN.md → task records
        ├── dispatcher    → routes tasks by role
        ├── agents/       → spawn claude-code subprocesses per tier
        ├── ledger        → .loomstack/ledger.jsonl
        ├── state         → derives status from git + files
        └── github        → gh CLI wrapper

                    │
    ┌───────────────┼──────────────────┐
    ▼               ▼                  ▼
GX10 llama-server   Mac Mini llama-server   Cloud APIs
  Qwen Coder-Next     Qwen3-8B Q4             Opus (architect)
  Qwen 235B (swap)    embeddings              Gemini (reviewer/researcher)
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
├── README.md  CLAUDE.md
├── plugin/                      ← TypeScript NemoClaw plugin
│   ├── package.json  tsconfig.json
│   └── src/index.ts + commands/
├── blueprint/                   ← Python blueprint, OCI-packaged
│   ├── Dockerfile  blueprint.yaml  pyproject.toml
│   └── src/loomstack/
│       ├── core/                ← plan_parser, dispatcher, state, ledger, budget, github, gx10
│       ├── agents/              ← claude_code_runner + per-tier wrappers + reviewer + researcher + test_runner
│       └── runner.py            ← blueprint entry point
├── bootstrap/                   ← authoring pack (ships inside blueprint)
│   ├── INFRA_SPEC.md  PROJECT_CONTRACT.md  PLAN_SCHEMA.md
│   ├── ESCALATION_RULES.md  BOOTSTRAP_PROMPT.md
│   └── EXAMPLES/
├── templates/project/           ← 4-file scaffold copied by `loomstack init`
├── policies/                    ← NemoClaw config
│   ├── privacy-router.yaml  network-allowlist.yaml  approval-gates.yaml
└── tests/
```

## CLI

All commands via NemoClaw:

```
nemoclaw loomstack init              # scaffold the 4 files in cwd
nemoclaw loomstack parse             # validate PLAN.md + show task graph
nemoclaw loomstack run [--once]      # dispatch ready tasks
nemoclaw loomstack status            # task graph + current state
nemoclaw loomstack approve <task>    # unblock architect gate
nemoclaw loomstack escalate <task>   # force-promote to architect
nemoclaw loomstack cost [--since 7d] # spend breakdown
nemoclaw loomstack doctor            # health-check endpoints, verify NIM-free
nemoclaw loomstack bootstrap         # print BOOTSTRAP_PROMPT.md

# NemoClaw native, use these too:
nemoclaw status                      # sandbox + gateway health
nemoclaw logs                        # audit log: every tool call, every egress
```

## Bootstrap Flow

1. Have an idea.
2. `nemoclaw loomstack bootstrap | pbcopy`
3. Paste into Claude Code with your idea.
4. Claude Code reads the bootstrap pack, asks ~5 scoping questions, generates the four files.
5. Commit, push to GitHub.
6. On the Ubuntu workstation: `git clone`, `cd`, `nemoclaw loomstack run`.
7. Review PRs. Approve architect tasks. Repeat.

~20 minutes from idea to agents running.

## Quick Start

### 1. Install NemoClaw on Ubuntu

```bash
curl -fsSL https://raw.githubusercontent.com/NVIDIA/nemoclaw/main/install.sh | sh
nemoclaw onboard
```

### 2. Install Loomstack plugin

```bash
git clone https://github.com/ronkam/loomstack.git ~/loomstack
cd ~/loomstack
./scripts/install-plugin.sh     # builds TS plugin + blueprint OCI, registers with NemoClaw
```

### 3. Configure endpoints (critical: NIM-free routing)

```bash
cp policies/privacy-router.yaml.example ~/.nemoclaw/privacy-router.yaml
# Edit to point each tier at YOUR endpoints.
# NEVER add NIM URLs. Doctor will fail if NIM endpoints are reachable.
nemoclaw loomstack doctor
```

### 4. Onboard first project

```bash
cd ~/src/meshcord
nemoclaw loomstack init
# Edit the four files (or use bootstrap flow)
nemoclaw loomstack parse
nemoclaw loomstack run
```

## Cost Controls

- **Daily cap** in `loomstack.yaml::budget_daily_usd` (hard-stop, resets midnight UTC)
- **Architect approval gate** — Opus never runs without `.loomstack/approvals/<task>` marker
- **Reviewer defaults cheap** — Gemini Flash first; Pro only on self-flagged low confidence
- **Worker retries bounded** — 2 retries with expanded context before escalating
- **Diff size cap** — PRs over `loomstack.yaml::max_diff_loc` rejected, forcing plan decomposition

Typical monthly burn, 30-min/day cadence, one active project: **$25–$60**. Local workers free.

## Lock-In Avoidance

NemoClaw defaults to NIM/Nemotron. Loomstack explicitly overrides every tier:

1. **NIM is never a default.** Every tier in `privacy-router.yaml` names a non-NIM endpoint.
2. **`loomstack doctor` fails loud** if any tier resolves to a NemoClaw-default NIM URL.
3. **Blueprint version pinned** in `~/.nemoclaw/config.json`; alpha software.
4. **Fallback path documented** in `docs/NEMOCLAW_FALLBACK.md` — Loomstack can lift out and run as bare Python against the same repos.

## License

MIT
