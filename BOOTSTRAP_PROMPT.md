# Loomstack Bootstrap Prompt

> Paste the **Prompt** section below into a fresh Claude Opus chat (claude.ai or
> Claude Code). Opus will interview you about the project, then emit the four
> files Loomstack needs: `README.md`, `CLAUDE.md`, `PLAN.md`, `loomstack.yaml`.
>
> This file is intentionally self-contained — drop it into any repo and it works
> without other Loomstack files present.

---

## Prompt

You are helping me scaffold a new project that will be built autonomously by
**Loomstack**, a Python tool that reads a `PLAN.md` and dispatches tasks to
tiered AI workers. Each task closes with one mergeable PR ≤ 400 LOC. The human
reviews; Loomstack does not merge to main without approval.

Your job is to produce **four files** that together form a valid Loomstack
project. Before writing anything, interview me with the questions in
**Step 1**. After I answer, generate the files in **Step 2** and validate them
against the rules in **Step 3**.

Do not produce code in any other language, do not invent dependencies I did
not ask for, and do not produce more than ~25 initial tasks. The plan should
get the project to a runnable skeleton + first feature, not the full backlog.

---

### Step 1 — Interview (ask all of these before writing files)

1. **One-line description.** What is this project, in 15 words or fewer?
2. **Primary language and runtime.** (e.g. Rust 1.80, Python 3.12, TypeScript
   + Node 20, Go 1.22.) Pick one primary; secondary languages allowed but
   should be justified.
3. **External dependencies you already know you need.** (e.g. `libp2p`,
   `fastapi`, `tokio`, a specific LLM API.) If unsure, say "minimal" and
   I'll only add what tasks force in.
4. **Quality bar.** One of: `prototype` (move fast, tests where critical),
   `production` (full test coverage, lint, types, CI), `research` (notebooks
   and scripts, throwaway acceptable). This affects acceptance criteria
   defaults.
5. **Architect-gated areas.** Which parts (if any) should always require
   human architect approval before changes? Common picks: `crypto`,
   `auth`, `database schema`, `public API`, `payment`, `none`.
6. **First milestone.** What does "v0.1 works" look like in one sentence?
   This shapes the first ~10 tasks.
7. **Repo prefix for task IDs.** 2–4 uppercase letters. (e.g. `MC` for
   MeshCord, `FW` for Fateweaver.)

If I give vague answers, restate your understanding and ask once for
confirmation. Do not invent answers I did not give.

---

### Step 2 — Produce these four files

Output each file in its own fenced code block, in this order, with a heading
line above each block stating the file path. Do not interleave commentary
inside the code blocks.

#### File 1: `README.md`

A concise human overview. Include:

- One-paragraph description (from Q1).
- "Stack" bullet list (from Q2 + Q3).
- "Status" line stating "v0.1 in progress, managed by Loomstack."
- "Building" section: 3–5 bullets describing what Loomstack does for the
  project (it reads `PLAN.md`, opens PRs, etc.).
- "License" line — default MIT unless I said otherwise.

Keep it under 80 lines. This file is for humans, not agents.

#### File 2: `CLAUDE.md`

Project-specific agent instructions. Loomstack agents read this on every
task. Include these sections:

- **What this project is** — one paragraph from Q1.
- **Tech stack (pinned)** — explicit versions for runtime + key deps.
- **Directory contract** — where source, tests, configs live.
- **Code style** — formatting rules, type/lint commands, what `except`
  patterns are forbidden.
- **Commit + branch conventions** — Conventional Commits (`feat:`, `fix:`,
  etc.), branch naming (`feat/<slug>`, `fix/<slug>`).
- **Testing** — test command, coverage expectations, fixture conventions.
- **Load-bearing subsystems** — list any architect-gated areas from Q5
  with a short reason each.
- **Safe for code worker tier** — list of areas where Loomstack's code
  worker may operate without escalation.
- **Known gotchas** — empty for now; agents will append as they learn.

Match the tone of CLAUDE.md files you have seen: terse, declarative,
"NEVER do X / ALWAYS do Y" style. This is a contract, not a tutorial.

#### File 3: `PLAN.md`

The task list. **This file's format is contractual** — Loomstack's parser
will reject anything that does not match. Use the schema in
**Appendix A** (below) exactly.

Generate 10–25 tasks total, organized as:

1. **Bootstrap tasks (3–5)** — repo skeleton, build/lint/test wiring,
   CI workflow, README finalization. All `role: code_worker` unless they
   touch an architect-gated area.
2. **Foundational tasks (5–10)** — the modules everything else depends on.
   Identify hard dependencies with `depends_on:`.
3. **First-milestone tasks (5–10)** — what gets you to the v0.1 from Q6.

Rules for tasks:

- Each task closes with **one mergeable PR ≤ 400 LOC** (set
  `acceptance.diff_size_max: 400` unless content-heavy, then 600).
- Use shallow dependency chains. Avoid chains > 4 deep.
- Tag any task touching an architect-gated area (Q5) with the appropriate
  tag and `human_review: true`.
- Default `escalate_if: [retries > 2]` on every task.
- Architect tasks are rare. Use `role: architect` only for design / schema
  / breaking decisions.

#### File 4: `loomstack.yaml`

Project config. Use this template, filling in the values from the
interview:

```yaml
# Loomstack project config. Schema: bootstrap/CONFIG_SCHEMA.md
# (only budget_daily_usd is enforced today; other fields are forward-looking)

budget_daily_usd:
  global: 10.00            # hard daily cap across all tiers
  code_worker: 2.00        # local Qwen — should be ~free in practice
  reviewer: 3.00           # Gemini Flash
  architect: 5.00          # Opus, gated by approval

max_diff_loc: 400          # PR size cap; tasks over this get rejected

tiers_enabled:
  - classifier
  - code_worker
  - reviewer
  - architect
  # add mac_worker / content_worker / researcher / test_runner as needed

ci:
  command: <test command from CLAUDE.md>   # e.g. "cargo test", "pytest"

escalation_rules:
  # Tag-driven escalation. Default ladder is code_worker → reviewer → architect.
  # Add per-tag overrides here only when needed.
  - tag: security
    target: architect
    require_approval: true
```

If the user said "minimal" for dependencies (Q3), default the budget to
`global: 5.00` instead of 10.

---

### Step 3 — Validate before returning

After writing the four files, do a self-check and tell me which (if any) of
these you violated. Do not proceed if you can't pass all of them.

1. PLAN.md has between 10 and 25 tasks.
2. Every task has a unique ID using the prefix from Q7.
3. Every task has `role:` and `acceptance:`.
4. Every `depends_on:` reference resolves to another task in the file.
5. No dependency cycles.
6. Every task touching an architect-gated area (Q5) has `human_review: true`.
7. CLAUDE.md names the test command, the lint command, and the formatter.
8. loomstack.yaml `ci.command` matches the test command in CLAUDE.md.
9. README.md is under 80 lines.

If any check fails, fix it before returning the files.

---

## Appendix A — PLAN.md Schema (authoritative)

A `PLAN.md` is a markdown document. The first H1 (`#`) is the project title.
Prose is allowed and ignored by the parser. Each task is an H2 heading
followed by a YAML key/value block.

```markdown
# Project Title

Optional prose. Ignored by parser. Use it for human context.

## Task: ABC-001 Short description in title case
role: code_worker
depends_on: []
context_files:
  - path/to/relevant/file.ext
  - docs/SCHEMA.md
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 400
  tests_added: true
escalate_if:
  - retries > 2
tags: []
human_review: false
notes: >
  Free-text context for the agent. Anything it needs to know that isn't
  in CLAUDE.md or context_files.
```

### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `role` | string | One of: `classifier`, `mac_worker`, `code_worker`, `content_worker`, `reviewer`, `architect`, `researcher`, `test_runner`. Must be in `tiers_enabled` in loomstack.yaml. |
| `acceptance` | map | At least one criterion required. See below. |

### Optional fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `depends_on` | list of task IDs | `[]` | Task waits until listed tasks are DONE. |
| `context_files` | list of repo-relative paths | `[]` | Files the agent must read. |
| `escalate_if` | list of conditions | `[]` | When to promote to next tier. |
| `tags` | list of strings | `[]` | Labels. Canonical: `security`, `breaking_change`, `cross_crate`, `narrative`, `perf`, `infra`. |
| `human_review` | bool | `false` | Pause for `.loomstack/approvals/<task-id>` after role completes. |
| `notes` | string | `""` | Free-text context, prepended to agent prompts. |
| `max_retries` | int | 2 | Retries before escalation. |
| `timeout_s` | int | 1800 | Subprocess timeout. |

### Acceptance criteria (at least one required)

| Key | Type | Meaning |
|-----|------|---------|
| `pr_opens_against` | string | Target branch (default repo default). |
| `ci` | `passes` | `test_runner` exits 0 on the branch. |
| `diff_size_max` | int | Max LOC in diff (added+removed). Default 400. |
| `tests_added` | bool | Worker must add at least one new test. |
| `tests_pass` | string | Explicit test command for this task. |
| `lint_clean` | bool | Repo lint command exits 0. |
| `spec_compliance` | bool | Reviewer confirms diff matches description. |
| `docs_updated` | bool | At least one doc file modified if public API changes. |
| `human_pr_approval` | bool | PR needs human approval before merge. |

### `escalate_if` conditions

| Pattern | Meaning |
|---------|---------|
| `retries > N` | More than N retries at current tier. |
| `tag: <name>` | Task has this tag. Tag escalations always go to architect. |
| `diff_size > N` | Last attempt exceeded N LOC. |
| `ci: failing` | Last test_runner exit was non-zero. |
| `reviewer: rejected` | Reviewer tier rejected the PR. |
| `cost > N` | Cumulative cost exceeds N USD. |

### Task IDs

Format `<PREFIX>-<NUMBER>`. Prefix is project-chosen, 2–4 uppercase letters.
IDs are unique within a `PLAN.md`, stable, never renumbered. Deleted IDs
stay retired.

### Examples

```markdown
## Task: MC-012 Add config loader for peer discovery
role: code_worker
depends_on: [MC-008]
context_files:
  - crates/config/src/lib.rs
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 250
  tests_added: true
escalate_if:
  - retries > 2
tags: [config]
```

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
  Propose Automerge document schema for channels, messages, presence.
  Address: concurrent edits, ordering, offline reconciliation, GC.
```
