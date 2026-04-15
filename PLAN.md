# Loomstack — Weaver Dashboard

Weaver is Loomstack's built-in operator dashboard. A lightweight web UI + Discord bot
that replaces OpenClaw, running on the Mac Mini and backed by the GX10 Qwen3-Coder-Next
model. HTMX + FastAPI, no build step, direct imports from loomstack.core.

Sequencing: scaffold → GX10 chat (validates core value) → Loomstack read views →
approval gates → budget dashboard → Discord bot → health monitoring → polish.

## Task: LS-001 Weaver package scaffold and dependencies
role: code_worker
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 300
  lint_clean: true
notes: >
  Create src/loomstack/weaver/ package with __init__.py, app.py (FastAPI app factory),
  config.py (pydantic-settings: GX10_BASE_URL, LOOMSTACK_PROJECT_DIR, HOST, PORT),
  empty routes/ package, templates/ dir with base.html skeleton, static/style.css stub.
  Add deps to pyproject.toml: fastapi, uvicorn[standard], jinja2, python-multipart,
  websockets. Wire runner.py to accept "weaver" subcommand that starts uvicorn.
  Verify: uv run python -m loomstack.runner weaver starts and serves a blank page.

## Task: LS-002 GX10 client module with streaming
role: code_worker
depends_on: [LS-001]
context_files:
  - blueprint/src/loomstack/weaver/config.py
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 300
  tests_added: true
notes: >
  weaver/gx10_client.py — async httpx client wrapping the OpenAI-compatible
  /v1/chat/completions endpoint on GX10. Functions: complete(messages, stream=False)
  -> str, stream_complete(messages) -> AsyncIterator[str]. Must parse SSE chunks
  from llama-server. Handle connection refused, timeout, 5xx gracefully. Tests
  mock httpx and verify SSE chunk parsing and error paths.

## Task: LS-003 WebSocket chat endpoint
role: code_worker
depends_on: [LS-002]
context_files:
  - blueprint/src/loomstack/weaver/gx10_client.py
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 350
  tests_added: true
notes: >
  weaver/routes/chat.py — FastAPI WebSocket endpoint at /ws/chat. Client sends
  JSON {message: str, conversation_id: str}. Server maintains in-memory conversation
  history keyed by conversation_id (dict of lists, no persistence). Streams tokens
  back as JSON {type: "token", content: str} and {type: "done"} on completion.
  Conversation history capped at 50 messages (sliding window). Also add REST endpoint
  POST /api/chat for OpenClaw-compatible non-streaming requests. Tests use FastAPI
  TestClient WebSocket support.

## Task: LS-004 Chat UI page
role: code_worker
depends_on: [LS-003]
context_files:
  - blueprint/src/loomstack/weaver/routes/chat.py
  - blueprint/src/loomstack/weaver/templates/base.html
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 400
  tests_added: true
human_review: true
notes: >
  templates/chat.html with message list, input box, send button. Vanilla JS
  WebSocket client (~80 LOC): connects to /ws/chat, sends messages, appends
  streamed tokens to a response bubble, auto-scrolls. Nav sidebar in base.html:
  Chat, Tasks, Budget, Health. Dark theme CSS, monospace code blocks. New
  conversation / clear buttons. Human reviewer must chat with GX10 through the
  browser and confirm streaming works before approval.

## Task: LS-005 Task list API routes
role: code_worker
depends_on: [LS-001]
context_files:
  - blueprint/src/loomstack/core/plan_parser.py
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 250
  tests_added: true
notes: >
  weaver/routes/tasks.py — GET /api/tasks returns JSON list of tasks parsed from
  PLAN.md via loomstack.core.plan_parser. Import directly, no reimplementation.
  Reads from LOOMSTACK_PROJECT_DIR config. Returns task id, description, role,
  depends_on, tags, human_review. Tests use a fixture PLAN.md in tests/fixtures/.

## Task: LS-006 Run-file status and merged task view
role: code_worker
depends_on: [LS-005]
context_files:
  - blueprint/src/loomstack/core/state.py
  - blueprint/src/loomstack/weaver/routes/tasks.py
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 300
  tests_added: true
notes: >
  Extend /api/tasks to merge plan data with run-file status from
  loomstack.core.state. Read .loomstack/runs/<task-id>.md frontmatter for status,
  tier, retry_count, cost_usd, pr_url, branch. Check .loomstack/approvals/<task-id>
  for approval status. GET /api/tasks/<task-id> returns full detail for one task.
  Tests use fixture .loomstack/ directory with sample run files.

## Task: LS-007 Task list and dependency graph page
role: code_worker
depends_on: [LS-006, LS-004]
context_files:
  - blueprint/src/loomstack/weaver/routes/tasks.py
  - blueprint/src/loomstack/weaver/templates/base.html
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 400
  tests_added: true
notes: >
  templates/tasks.html — table with task ID, description, role, status
  (color-coded badge), depends_on, human_review flag. HTMX auto-refresh every
  10s via hx-trigger="every 10s". Dependency graph using dagre-d3 or elkjs
  (single CDN include). Nodes colored by status. Click a node to show task detail
  in side panel (hx-get /api/tasks/<id>). Tests verify route returns correct HTML.

## Task: LS-008 Approval gate endpoint and UI
role: code_worker
depends_on: [LS-006]
context_files:
  - blueprint/src/loomstack/core/state.py
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 250
  tests_added: true
notes: >
  weaver/routes/approvals.py — POST /api/approve/<task-id> creates marker file at
  <LOOMSTACK_PROJECT_DIR>/.loomstack/approvals/<task-id>. Returns 201. Idempotent.
  GET /api/pending-approvals returns tasks with human_review=true lacking a marker.
  Task list page (LS-007) shows "Approve" button on pending tasks via hx-post.
  Button disappears after approval (hx-swap). Tests verify file creation and
  idempotency.

## Task: LS-009 Run log viewer
role: code_worker
depends_on: [LS-007]
context_files:
  - blueprint/src/loomstack/core/state.py
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 300
  tests_added: true
notes: >
  GET /tasks/<task-id>/log renders .loomstack/runs/<task-id>.md as HTML. Parse
  frontmatter into metadata card (status, tier, cost, PR link). Render markdown
  body as HTML using markdown2 (add dep). Auto-refresh via HTMX for in-progress
  tasks. Linked from task list. 404 if no run file. Tests verify rendering with
  fixture run file.

## Task: LS-010 Budget API routes
role: code_worker
depends_on: [LS-001]
context_files:
  - blueprint/src/loomstack/core/budget.py
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 300
  tests_added: true
notes: >
  weaver/routes/budget.py — read-only routes over loomstack.core.budget and
  ledger.jsonl. GET /api/budget/today returns daily spend total and per-tier
  breakdown. GET /api/budget/history?days=N returns daily totals. GET
  /api/budget/recent?n=50 returns recent charge entries. Weaver never writes
  to the ledger. Tests use fixture ledger.jsonl.

## Task: LS-011 Budget dashboard page
role: code_worker
depends_on: [LS-010, LS-004]
context_files:
  - blueprint/src/loomstack/weaver/routes/budget.py
  - blueprint/src/loomstack/weaver/templates/base.html
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 350
  tests_added: true
notes: >
  templates/budget.html — today's spend total and per-tier breakdown (table +
  bar chart), daily cap vs spent (progress bar), 30-day history (Chart.js via
  CDN), recent charges table (last 50 entries). HTMX auto-refresh on today panel.
  Tests verify route returns expected HTML elements.

## Task: LS-012 GX10 health monitoring
role: code_worker
depends_on: [LS-002]
context_files:
  - blueprint/src/loomstack/weaver/gx10_client.py
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 300
  tests_added: true
notes: >
  weaver/routes/health.py — polls GX10: GET /health, GET /v1/models, GET /slots.
  Returns GX10Status dataclass with is_healthy, model_id, slots_active,
  slots_total, context_used. /health page shows green/red status, model name,
  slot utilization. HTMX polls every 15s. Tests mock GX10 endpoints.

## Task: LS-013 Discord bot relay
role: code_worker
depends_on: [LS-002]
context_files:
  - blueprint/src/loomstack/weaver/gx10_client.py
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 250
  tests_added: true
notes: >
  weaver/discord_bot.py — standalone entry point (python -m loomstack.weaver.discord_bot).
  Uses discord.py. On message in configured channels or DM from allowlisted user:
  send to GX10 via gx10_client.complete(), reply with response. No streaming needed
  for Discord. Per-user conversation history (in-memory dict, capped 20 messages).
  Config from .env: DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_USER_ID. Add
  discord.py to pyproject.toml. Target ~100 LOC. Tests mock discord.py and gx10_client.

## Task: LS-014 Landing page and nav polish
role: code_worker
depends_on: [LS-007, LS-011, LS-012]
context_files:
  - blueprint/src/loomstack/weaver/templates/base.html
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 300
  tests_added: true
notes: >
  Landing page at / — dashboard overview: GX10 status badge (hx-get), today's
  spend summary, pending/in-progress/done task counts, pending approval count.
  Each card links to detail page. Nav sidebar active-state highlighting. Favicon
  (simple SVG). Mobile-responsive CSS. Tests verify landing route assembles data
  from all sub-readers.

## Task: LS-015 Multi-project support
role: code_worker
depends_on: [LS-006, LS-010]
context_files:
  - blueprint/src/loomstack/weaver/config.py
  - blueprint/src/loomstack/weaver/routes/tasks.py
  - blueprint/src/loomstack/weaver/routes/budget.py
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 350
  tests_added: true
notes: >
  Config change: LOOMSTACK_PROJECT_DIRS (comma-separated paths). Each reader
  gains project_dir parameter. API routes: /api/<project>/tasks, /api/<project>/budget.
  Nav sidebar shows project selector. Task list and budget scope to selected project.
  Enables monitoring meshcord, fateweaver, and loomstack itself from one dashboard.
  Tests verify multi-project routing.

## Task: LS-016 End-to-end review and hardening
role: reviewer
depends_on: [LS-014, LS-013, LS-015]
human_review: true
acceptance:
  pr_opens_against: main
  diff_size_max: 300
  docs_updated: true
  human_pr_approval: true
notes: >
  Full audit: error handling on GX10 calls (connection refused, timeout), input
  validation on WebSocket messages, path traversal protection on project_dir params,
  rate limiting on approval endpoint, graceful degradation when .loomstack/ doesn't
  exist. Update README with Weaver setup instructions and verification commands.
