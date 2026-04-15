# Notes for Gemini — code review feedback + guidance for upcoming tasks

## LS-005 review feedback

Your implementation was solid — correct structure, clean imports, proper DI,
structlog. A few things to improve going forward:

**1. Broaden test fixtures.**
The LS-005 fixture has one minimal task. Tests should cover realistic data:
multiple tasks, `depends_on`, `human_review: true`, `tags`. If `plan_parser`
silently drops a field you'd never catch it with a single-task fixture.

**2. Catch specific exceptions, not bare `Exception`.**
```python
# avoid
except Exception as exc:
    raise HTTPException(status_code=500, ...) from exc

# prefer — catch what you know can fail
except PlanParseError as exc:
    raise HTTPException(status_code=422, ...) from exc
except OSError as exc:
    raise HTTPException(status_code=500, ...) from exc
```
The CLAUDE.md calls `except Exception: pass` a CI failure. Raising is fine,
but broad catches that swallow the type make debugging harder. Look at what
`plan_parser.py` actually raises and catch those specifically.

**3. Response schemas.**
Returning the raw `Plan` pydantic model directly means internal parser changes
become immediate API breakage. Starting with LS-006, define explicit response
models (e.g. `TaskSummary`, `TaskDetail`) in the route file and map from the
core types. Keeps the API surface stable.

---

## LS-006 review feedback

Good improvement over LS-005: response models defined, `asyncio.gather` for
concurrent status fetches, `KeyError` caught specifically for missing task IDs,
`strict=True` on zip. Tests now cover status merging and 404. Clean.

Issues to fix going forward:

**1. `except Exception` still present** — same note as LS-005. `plan_parser`
raises `PlanParseError` (defined in `core/plan_parser.py`). Catch that
specifically at 422/500, and `OSError` separately for file I/O failures.

**2. `TaskSummary(Task)` inherits from `Task`** — inheriting a core model into
an API response model couples them tightly. If `Task` grows private/internal
fields, they leak into the API response. Prefer composition: define
`TaskSummary` with explicit fields and populate from `task.model_dump()`.
It's fine for now but watch this as `Task` evolves.

**3. Spurious `@pytest.mark.asyncio` on sync tests** — `test_list_tasks_includes_status`
is decorated `@pytest.mark.asyncio` but uses `TestClient` (synchronous). The
decorator is harmless but misleading — remove it from any test that doesn't
`await` anything directly.

**4. `derive_status` mock in `test_list_tasks_includes_status` uses a plain
list as `side_effect` instead of `AsyncMock`** — works by accident because
`asyncio.gather` is lenient, but not correct. Mock async functions with
`AsyncMock`:
```python
with patch("...derive_status", new_callable=AsyncMock) as mock:
    mock.side_effect = [TaskStatus.DONE, TaskStatus.IN_PROGRESS]
```

**5. No test for missing `.loomstack/` dir** — `derive_status` should return
PENDING gracefully when no run files exist. This is tested in `core/test_state.py`
but not exercised through the route. Add one test that calls `/api/tasks`
without mocking `derive_status` and with a real (empty) tmp dir — verifies
the happy path end-to-end.

---

## Guidance for LS-007, LS-008, LS-009

**LS-006 — run-file status merge**
- `core/state.py` has `derive_status(task_id)` and `derive_run_meta(task_id)`.
  Use both — `derive_status` for the list view, `derive_run_meta` for the detail view.
- Define `TaskSummaryResponse` and `TaskDetailResponse` pydantic models in
  `routes/tasks.py`. Don't return raw core types.
- The `.loomstack/` dir may not exist — handle `FileNotFoundError` gracefully
  (treat as PENDING, don't 500).
- Test fixtures: add a `tests/fixtures/loomstack/` dir with sample run files
  and an approvals file. Mirror the structure `state.py` expects.
- GET /api/tasks/<task-id> should 404 cleanly if the task_id isn't in PLAN.md.

**LS-007 — task list HTML page**
- Register the HTML route in `routes/tasks.py` (not a new file).
- Use `app.state.templates` (set in `app.py`) — access via `request.app.state.templates`.
- HTMX `hx-trigger="every 10s"` should only refresh the table body, not the
  whole page — use `hx-target` + `hx-swap` on a `<tbody>` element.
- dagre-d3 via CDN is fine. Nodes should be colored by status:
  PENDING=gray, IN_PROGRESS=blue, PROPOSED=yellow, DONE=green, FAILED=red, BLOCKED=orange.
- Test: use `TestClient` and assert the response contains expected HTML
  fragments (`task_id`, status badge text). Don't test exact CSS.

**LS-008 — approval gate**
- Approval marker path: `<LOOMSTACK_PROJECT_DIR>/.loomstack/approvals/<task-id>`.
  Create parent dirs with `mkdir(parents=True, exist_ok=True)`.
- POST must be idempotent — if the file exists, return 200 not 201, or just
  always return 200. Don't error on re-approve.
- GET /api/pending-approvals: cross-reference tasks where `human_review: true`
  in PLAN.md against the approvals dir. No approval file = pending.
- Test idempotency explicitly: call POST twice, assert no error and file exists once.

**LS-009 — run log viewer**
- Add `markdown2` to `pyproject.toml` dependencies and justify it in the PR
  description (stdlib has no markdown renderer).
- Route: `GET /tasks/{task_id}/log` — HTML page, not JSON.
- Split the run file into frontmatter metadata card + markdown body. The
  `RunMeta` dataclass from `core/state.py` already parses frontmatter — use it.
- Auto-refresh: `hx-trigger="every 5s"` only when `status` is IN_PROGRESS.
  Use a conditional in the template: `{% if run_meta.status == 'in_progress' %}`.
- 404 if no run file exists. Don't 500.
- Test: fixture run file with both frontmatter blocks (initial + completion
  footer) to exercise the multi-block merge logic in the state parser.

---

## General reminders

- Every new router must be registered in `app.py` with `app.include_router(...)`.
- `uv sync --all-extras` before starting each task (deps may have changed).
- After merging each PR: `git checkout feat/gemini-work && git merge origin/develop`
  before branching for the next task.
- Max 400 LOC per PR including tests. If a task is running long, stop and ask
  before adding more — don't compress tests to fit.
