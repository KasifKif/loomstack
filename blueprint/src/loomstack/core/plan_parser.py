"""
Parse and validate a PLAN.md file into structured Task/Plan models.

PLAN.md format is defined in bootstrap/PLAN_SCHEMA.md.
Every H2 matching ``## Task: <ID> <description>`` is parsed as a task.
The body between consecutive task headings (or EOF) is a YAML block.
Prose outside task headings is ignored.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import aiofiles
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

VALID_ROLES = {
    "classifier",
    "mac_worker",
    "code_worker",
    "content_worker",
    "reviewer",
    "architect",
    "researcher",
    "test_runner",
}

ESCALATE_CONDITION_PATTERNS = [
    re.compile(r"^retries\s*>\s*\d+$"),
    re.compile(r"^retries\s*>=\s*\d+$"),
    re.compile(r"^tag:\s*\S+$"),
    re.compile(r"^diff_size\s*>\s*\d+$"),
    re.compile(r"^ci:\s*failing$"),
    re.compile(r"^reviewer:\s*rejected$"),
    re.compile(r"^cost\s*>\s*[\d.]+$"),
]

TASK_ID_RE = re.compile(r"^[A-Z]{2,4}-\d+$")
TASK_HEADING_RE = re.compile(r"^##\s+Task:\s+([A-Z]{2,4}-\d+)\s+(.+)$")


class Role(StrEnum):
    CLASSIFIER = "classifier"
    MAC_WORKER = "mac_worker"
    CODE_WORKER = "code_worker"
    CONTENT_WORKER = "content_worker"
    REVIEWER = "reviewer"
    ARCHITECT = "architect"
    RESEARCHER = "researcher"
    TEST_RUNNER = "test_runner"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class AcceptanceBlock(BaseModel):
    """Conditions that must pass for a task to be marked DONE."""

    pr_opens_against: str | None = None
    ci: str | None = None  # literal "passes" when present
    diff_size_max: int | None = None
    tests_added: bool | None = None
    tests_pass: str | None = None
    lint_clean: bool | None = None
    spec_compliance: bool | None = None
    docs_updated: bool | None = None
    human_pr_approval: bool | None = None

    @model_validator(mode="after")
    def at_least_one_criterion(self) -> AcceptanceBlock:
        values = self.model_dump(exclude_none=True)
        if not values:
            raise ValueError("acceptance block must contain at least one criterion")
        return self

    @field_validator("ci")
    @classmethod
    def ci_must_be_passes(cls, v: str | None) -> str | None:
        if v is not None and v != "passes":
            raise ValueError(f"acceptance.ci must be 'passes', got {v!r}")
        return v

    @field_validator("diff_size_max")
    @classmethod
    def diff_size_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("acceptance.diff_size_max must be positive")
        return v


class Task(BaseModel):
    """A single task entry parsed from a PLAN.md H2 block."""

    # From heading
    task_id: str
    description: str

    # Required YAML fields
    role: Role
    acceptance: AcceptanceBlock

    # Optional YAML fields
    depends_on: list[str] = Field(default_factory=list)
    context_files: list[str] = Field(default_factory=list)
    escalate_if: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    human_review: bool = False
    notes: str = ""
    max_retries: int = 2
    timeout_s: int = 1800

    @field_validator("task_id")
    @classmethod
    def task_id_format(cls, v: str) -> str:
        if not TASK_ID_RE.match(v):
            raise ValueError(
                f"task_id {v!r} must match <PREFIX>-<NUMBER> (e.g. MC-001)"
            )
        return v

    @field_validator("depends_on", mode="before")
    @classmethod
    def coerce_depends_on(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)

    @field_validator("depends_on")
    @classmethod
    def depends_on_format(cls, v: list[str]) -> list[str]:
        for dep in v:
            if not TASK_ID_RE.match(dep):
                raise ValueError(
                    f"depends_on entry {dep!r} must match <PREFIX>-<NUMBER>"
                )
        return v

    @field_validator("escalate_if", mode="before")
    @classmethod
    def coerce_escalate_if(cls, v: Any) -> list[str]:
        # YAML parses `- tag: security` as {'tag': 'security'}.
        # Coerce single-key dicts back to "key: value" strings so users
        # don't need to quote these entries in their PLAN.md.
        if not isinstance(v, list):
            return list(v) if hasattr(v, "__iter__") else []
        result: list[str] = []
        for item in v:
            if isinstance(item, dict) and len(item) == 1:
                key, val = next(iter(item.items()))
                result.append(f"{key}: {val}")
            else:
                result.append(item)
        return result

    @field_validator("escalate_if")
    @classmethod
    def escalate_if_valid(cls, v: list[str]) -> list[str]:
        for condition in v:
            if not any(p.match(condition) for p in ESCALATE_CONDITION_PATTERNS):
                raise ValueError(
                    f"unknown escalate_if condition: {condition!r}. "
                    "Valid patterns: retries > N, retries >= N, tag: <name>, "
                    "diff_size > N, ci: failing, reviewer: rejected, cost > N"
                )
        return v

    @field_validator("max_retries")
    @classmethod
    def max_retries_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_retries must be >= 0")
        return v

    @field_validator("timeout_s")
    @classmethod
    def timeout_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("timeout_s must be > 0")
        return v


class Plan(BaseModel):
    """A fully parsed and validated PLAN.md."""

    title: str  # First H1 in the file, or empty string
    tasks: list[Task]

    @model_validator(mode="after")
    def no_duplicate_ids(self) -> Plan:
        seen: set[str] = set()
        for task in self.tasks:
            if task.task_id in seen:
                raise ValueError(f"duplicate task ID: {task.task_id}")
            seen.add(task.task_id)
        return self

    @model_validator(mode="after")
    def depends_on_resolvable(self) -> Plan:
        ids = {t.task_id for t in self.tasks}
        for task in self.tasks:
            for dep in task.depends_on:
                if dep not in ids:
                    raise ValueError(
                        f"task {task.task_id} depends_on {dep!r} which does not exist"
                    )
        return self

    @model_validator(mode="after")
    def no_dependency_cycles(self) -> Plan:
        graph: dict[str, list[str]] = {t.task_id: t.depends_on for t in self.tasks}
        visited: set[str] = set()
        in_stack: set[str] = set()

        def dfs(node: str) -> None:
            visited.add(node)
            in_stack.add(node)
            for neighbour in graph.get(node, []):
                if neighbour not in visited:
                    dfs(neighbour)
                elif neighbour in in_stack:
                    raise ValueError(
                        f"dependency cycle detected involving task {neighbour}"
                    )
            in_stack.discard(node)

        for task_id in graph:
            if task_id not in visited:
                dfs(task_id)

        return self

    def get_task(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(task_id)

    def ready_tasks(self, done_ids: set[str]) -> list[Task]:
        """Return tasks whose dependencies are all in done_ids."""
        return [
            t
            for t in self.tasks
            if t.task_id not in done_ids
            and all(dep in done_ids for dep in t.depends_on)
        ]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _extract_title(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return ""


def _split_task_blocks(lines: list[str]) -> list[tuple[str, str, str]]:
    """
    Return list of (task_id, description, yaml_body) tuples,
    one per ``## Task:`` heading found in lines.
    """
    blocks: list[tuple[str, str, str]] = []
    current_id: str | None = None
    current_desc: str | None = None
    body_lines: list[str] = []

    for line in lines:
        m = TASK_HEADING_RE.match(line.rstrip())
        if m:
            if current_id is not None:
                blocks.append((current_id, current_desc or "", "".join(body_lines)))
            current_id = m.group(1)
            current_desc = m.group(2).strip()
            body_lines = []
        elif current_id is not None:
            # Stop accumulating at the next H1 or H2 that isn't a task heading
            if line.startswith("## ") or line.startswith("# "):
                blocks.append((current_id, current_desc or "", "".join(body_lines)))
                current_id = None
                current_desc = None
                body_lines = []
            else:
                body_lines.append(line)

    if current_id is not None:
        blocks.append((current_id, current_desc or "", "".join(body_lines)))

    return blocks


class PlanParseError(Exception):
    """Raised when a PLAN.md cannot be parsed or fails validation."""


def _parse_task_block(task_id: str, description: str, yaml_body: str) -> Task:
    try:
        data: dict[str, Any] = yaml.safe_load(yaml_body) or {}
    except yaml.YAMLError as exc:
        raise PlanParseError(
            f"task {task_id}: invalid YAML block — {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise PlanParseError(
            f"task {task_id}: YAML body must be a mapping, got {type(data).__name__}"
        )

    data["task_id"] = task_id
    data["description"] = description

    # acceptance is required; give a clear error before Pydantic sees it
    if "acceptance" not in data:
        raise PlanParseError(f"task {task_id}: missing required field 'acceptance'")
    if "role" not in data:
        raise PlanParseError(f"task {task_id}: missing required field 'role'")

    from pydantic import ValidationError

    try:
        return Task.model_validate(data)
    except ValidationError as exc:
        # Re-raise with task context attached
        raise PlanParseError(f"task {task_id}: validation error — {exc}") from exc


def _parse_plan(content: str) -> Plan:
    lines = content.splitlines(keepends=True)
    title = _extract_title(lines)
    blocks = _split_task_blocks(lines)

    if not blocks:
        raise PlanParseError("no tasks found — PLAN.md contains no '## Task:' headings")

    tasks = [_parse_task_block(tid, desc, body) for tid, desc, body in blocks]

    from pydantic import ValidationError

    try:
        return Plan(title=title, tasks=tasks)
    except ValidationError as exc:
        raise PlanParseError(f"plan validation error — {exc}") from exc


async def parse_plan_file(path: Path) -> Plan:
    """Read and parse a PLAN.md file. Raises PlanParseError on any failure."""
    try:
        async with aiofiles.open(path, encoding="utf-8") as fh:
            content = await fh.read()
    except OSError as exc:
        raise PlanParseError(f"cannot read {path}: {exc}") from exc

    return _parse_plan(content)


def parse_plan_string(content: str) -> Plan:
    """Parse a PLAN.md from a string (sync, for tests)."""
    return _parse_plan(content)
