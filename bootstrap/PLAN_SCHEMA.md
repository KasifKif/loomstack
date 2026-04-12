# PLAN_SCHEMA.md — Loomstack Task Format

> This is the schema for `PLAN.md` in any Loomstack-managed repo.
> Claude Code targets this schema when generating new projects via `BOOTSTRAP_PROMPT.md`.
> The `plan_parser` in the Loomstack blueprint validates against this spec on every cycle.

---

## Top-Level Structure

A `PLAN.md` is a markdown document containing any number of H2 (`##`) sections, each defining one task. Prose between tasks is allowed and ignored by the parser. The first H1 (`#`) is the project title; subsequent H1s are treated as section dividers (also ignored by the parser).

```markdown
# MeshCord

Prose here is ignored. Use it for human context — roadmap summaries, phase notes,
anything that helps humans (and Claude Code during bootstrap) understand the shape
of the work.

## Task: MC-001 Bootstrap Cargo workspace
...

## Task: MC-002 Implement libp2p transport layer
...
```

## Task Format

Every task begins with `## Task: <ID> <Short description>` and is followed by a YAML-style key/value block (parsed as YAML between the heading and either the next `##`/`#` or end-of-file).

```markdown
## Task: MC-042 Implement OpenMLS group creation
role: code_worker
depends_on: [MC-041]
context_files:
  - CONTEXT/CRYPTO_SCHEMA.md
  - crates/crypto/src/lib.rs
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 400
  tests_added: true
  spec_compliance: true
escalate_if:
  - retries > 2
  - tag: security
  - tag: breaking_change
tags: [crypto, security]
human_review: true
notes: >
  First task in the security-sensitive crypto crate. Worker must add property
  tests against the openmls reference vectors.
```

## Field Reference

### Required

| Field | Type | Description |
|-------|------|-------------|
| `role` | string | One of: `classifier`, `mac_worker`, `code_worker`, `content_worker`, `reviewer`, `architect`, `researcher`, `test_runner`. Tiers not enabled in `loomstack.yaml::tiers_enabled` fail parse. |
| `acceptance` | map | Conditions that must pass for task → DONE. See below. |

### Optional

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `depends_on` | list of task IDs | `[]` | Task cannot dispatch until listed tasks are DONE. |
| `context_files` | list of repo-relative paths | `[]` | Files the agent MUST read before starting work. Passed to `claude-code` as pre-load context. |
| `escalate_if` | list of conditions | `[]` | Promote to next tier (or architect on `tag:` matches) when any condition is true. See below. |
| `tags` | list of strings | `[]` | Arbitrary labels. Used by `escalate_if: tag:` and filtering. Canonical tags: `security`, `breaking_change`, `cross_crate`, `narrative`, `perf`, `infra`. |
| `human_review` | bool | `false` | If true: after the role completes, pause and wait for `.loomstack/approvals/<task-id>` marker before proceeding. |
| `notes` | string | `""` | Free-text context for agents. Prepended to agent prompts. |
| `max_retries` | int | 2 | Retries before escalating. |
| `timeout_s` | int | 1800 | Subprocess timeout for agent work. |

## `acceptance` Block

At least one acceptance criterion is required. All listed criteria must pass.

| Key | Type | Meaning |
|-----|------|---------|
| `pr_opens_against` | string | Target branch for the PR (default: repo default branch). |
| `ci` | `passes` | `test_runner` must exit 0 on the branch. |
| `diff_size_max` | int | Max LOC in the diff (added+removed). Enforces task granularity. Default 400. |
| `tests_added` | bool | Worker must add at least one new test. |
| `tests_pass` | string (command) | Explicit test command overriding `loomstack.yaml::ci.command` for this task. |
| `lint_clean` | bool | Repo's lint command must exit 0. |
| `spec_compliance` | bool | Reviewer tier confirms the diff matches the task description + referenced context files. |
| `docs_updated` | bool | At least one file under `docs/` or a `README.md` must be modified if public API changes. |
| `human_pr_approval` | bool | PR must have human approval reaction/review before merge (separate from `human_review`, which gates dispatch). |

Acceptance evaluated by:
1. `test_runner` for `ci`, `tests_pass`, `lint_clean`.
2. Worker self-report for `diff_size_max`, `tests_added`, `docs_updated`.
3. `reviewer_gemini` for `spec_compliance`.
4. GitHub API for `pr_opens_against`, `human_pr_approval`.

## `escalate_if` Conditions

Each entry is a single string. Parser accepts:

| Pattern | Meaning |
|---------|---------|
| `retries > N` | Task has been retried more than N times at current tier. |
| `retries >= N` | Same, inclusive. |
| `tag: <name>` | Task has this tag. Usually escalates directly to `architect` rather than next tier. |
| `diff_size > N` | Most recent attempt produced a diff larger than N LOC. Forces decomposition. |
| `ci: failing` | `test_runner` returned non-zero on the last attempt. |
| `reviewer: rejected` | Reviewer tier rejected the PR. |
| `cost > N` | Cumulative cost for this task exceeds N USD. |

Escalation target:
- `tag:` conditions always escalate to `architect`.
- All other conditions escalate to the next tier in the ladder (see `ESCALATION_RULES.md`).

## Task IDs

- Format: `<PREFIX>-<NUMBER>`. Prefix is project-chosen (MC, FW, etc.), typically 2–4 uppercase letters.
- IDs are unique within a `PLAN.md`.
- IDs are stable — never renumbered. Deleted tasks leave their IDs permanently retired.
- `depends_on` references must resolve to existing task IDs in the same file.

## Parse Errors

`nemoclaw loomstack parse` rejects a plan with clear error messages for:
- Duplicate task IDs
- Unresolvable `depends_on` references
- Dependency cycles
- Unknown `role` values
- Roles not enabled in `loomstack.yaml::tiers_enabled`
- Invalid YAML block
- Missing required fields
- `context_files` paths that don't exist in the repo
- `diff_size_max` unset and no `loomstack.yaml::max_diff_loc` default

Parse failure prevents dispatch. Fix the file; re-run.

## Example: Small Code Task

```markdown
## Task: MC-012 Add config loader for peer discovery
role: code_worker
depends_on: [MC-008]
context_files:
  - crates/config/src/lib.rs
  - CONTEXT/CONFIG_SCHEMA.md
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 250
  tests_added: true
escalate_if:
  - retries > 2
tags: [config]
```

## Example: Content Task

```markdown
## Task: FW-087 Draft Oathbreaker faction lore (3 NPCs)
role: content_worker
depends_on: [FW-080]
context_files:
  - lore/world-overview.md
  - lore/factions/README.md
acceptance:
  pr_opens_against: main
  diff_size_max: 600
  spec_compliance: true
  docs_updated: true
escalate_if:
  - retries > 2
  - tag: lore_inconsistency
tags: [narrative, lore]
human_review: true
notes: >
  Three NPCs with unique voice. Must not contradict established events in
  lore/events/*.md. Check against the global event log before asserting history.
```

## Example: Architect Task (always gated)

```markdown
## Task: MC-001 Design CRDT schema for channel state
role: architect
acceptance:
  pr_opens_against: main
  diff_size_max: 800
  docs_updated: true
  human_pr_approval: true
tags: [design, breaking_change]
human_review: true
notes: >
  Propose Automerge document schema for channels, messages, presence. Must
  address: concurrent edits, message ordering, offline reconciliation, GC of
  deleted channels. Produce CONTEXT/CRDT_SCHEMA.md + ADR in docs/adr/.
```

## Rules of Thumb for Writing Tasks

1. **One mergeable PR per task.** If it can't ship in a PR, split it.
2. **≤ 400 LOC by default.** Harder cap for `code_worker`; content tasks can stretch to 600.
3. **Name the context files.** Agents are faster and more accurate when `context_files` is explicit.
4. **Prefer shallow dependency chains.** Deep chains (>5 deep) serialize work and stall throughput.
5. **Tag security-sensitive work.** `tag: security` always routes to architect with approval gate.
6. **Use `notes` liberally.** Anything an agent would need to know that isn't in `CLAUDE.md` or `context_files`.
7. **Don't over-specify implementation.** Describe the outcome; trust the worker to get there. Over-specification fights the model.
8. **Architect tasks are rare.** Most work should not be `role: architect`. Reserve for design and unblocking.
