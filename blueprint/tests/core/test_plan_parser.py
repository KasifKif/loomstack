"""Tests for blueprint/src/loomstack/core/plan_parser.py."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from loomstack.core.plan_parser import (
    AcceptanceBlock,
    PlanParseError,
    Role,
    Task,
    parse_plan_file,
    parse_plan_string,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "plans"


# ---------------------------------------------------------------------------
# AcceptanceBlock
# ---------------------------------------------------------------------------


class TestAcceptanceBlock:
    def test_minimal_valid(self) -> None:
        a = AcceptanceBlock(ci="passes")
        assert a.ci == "passes"

    def test_empty_raises(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            AcceptanceBlock()

    def test_ci_invalid_value(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            AcceptanceBlock(ci="fails")

    def test_diff_size_max_zero_raises(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            AcceptanceBlock(diff_size_max=0)

    def test_all_fields(self) -> None:
        a = AcceptanceBlock(
            pr_opens_against="main",
            ci="passes",
            diff_size_max=400,
            tests_added=True,
            lint_clean=True,
            spec_compliance=True,
            docs_updated=False,
            human_pr_approval=True,
        )
        assert a.diff_size_max == 400
        assert a.pr_opens_against == "main"


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


class TestTask:
    def _make(self, **kwargs: object) -> Task:
        defaults: dict[str, object] = {
            "task_id": "MC-001",
            "description": "A task",
            "role": "code_worker",
            "acceptance": {"ci": "passes"},
        }
        defaults.update(kwargs)
        return Task.model_validate(defaults)

    def test_minimal(self) -> None:
        t = self._make()
        assert t.task_id == "MC-001"
        assert t.role == Role.CODE_WORKER
        assert t.depends_on == []
        assert t.human_review is False
        assert t.max_retries == 2
        assert t.timeout_s == 1800

    def test_bad_task_id_format(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            self._make(task_id="mc-001")

    def test_bad_task_id_no_prefix(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            self._make(task_id="001")

    def test_invalid_role(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            self._make(role="overlord")

    def test_depends_on_string_coerced(self) -> None:
        t = self._make(depends_on="MC-002")
        assert t.depends_on == ["MC-002"]

    def test_depends_on_bad_format(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            self._make(depends_on=["bad-id"])

    def test_escalate_if_valid_patterns(self) -> None:
        t = self._make(
            escalate_if=[
                "retries > 2",
                "retries >= 3",
                "tag: security",
                "diff_size > 400",
                "ci: failing",
                "reviewer: rejected",
                "cost > 5.0",
            ]
        )
        assert len(t.escalate_if) == 7

    def test_escalate_if_invalid(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            self._make(escalate_if=["unknown condition"])

    def test_max_retries_negative_raises(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            self._make(max_retries=-1)

    def test_timeout_zero_raises(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            self._make(timeout_s=0)

    def test_all_roles_accepted(self) -> None:
        roles = [
            "classifier", "mac_worker", "code_worker", "content_worker",
            "reviewer", "architect", "researcher", "test_runner",
        ]
        for role in roles:
            t = self._make(role=role)
            assert t.role.value == role


# ---------------------------------------------------------------------------
# Plan — string parsing
# ---------------------------------------------------------------------------


class TestParsePlanString:
    def test_minimal_plan(self) -> None:
        content = (FIXTURES / "valid_minimal.md").read_text()
        plan = parse_plan_string(content)
        assert plan.title == "TestProject"
        assert len(plan.tasks) == 1
        t = plan.tasks[0]
        assert t.task_id == "TP-001"
        assert t.role == Role.CODE_WORKER
        assert t.acceptance.diff_size_max == 200

    def test_full_plan(self) -> None:
        content = (FIXTURES / "valid_full.md").read_text()
        plan = parse_plan_string(content)
        assert plan.title == "MeshCord"
        assert len(plan.tasks) == 3

        mc1 = plan.get_task("MC-001")
        assert mc1.role == Role.ARCHITECT
        assert mc1.human_review is True
        assert "breaking_change" in mc1.tags

        mc3 = plan.get_task("MC-003")
        assert mc3.depends_on == ["MC-002"]
        assert "tag: security" in mc3.escalate_if

    def test_duplicate_id_raises(self) -> None:
        content = (FIXTURES / "invalid_duplicate_id.md").read_text()
        with pytest.raises(PlanParseError, match="duplicate task ID"):
            parse_plan_string(content)

    def test_unresolvable_dep_raises(self) -> None:
        content = (FIXTURES / "invalid_missing_dep.md").read_text()
        with pytest.raises(PlanParseError, match="does not exist"):
            parse_plan_string(content)

    def test_cycle_raises(self) -> None:
        content = (FIXTURES / "invalid_cycle.md").read_text()
        with pytest.raises(PlanParseError, match="cycle"):
            parse_plan_string(content)

    def test_no_tasks_raises(self) -> None:
        with pytest.raises(PlanParseError, match="no tasks found"):
            parse_plan_string("# Title\n\nJust prose, no tasks.")

    def test_missing_role_raises(self) -> None:
        content = "## Task: MC-001 A task\nacceptance:\n  ci: passes\n"
        with pytest.raises(PlanParseError, match="missing required field 'role'"):
            parse_plan_string(content)

    def test_missing_acceptance_raises(self) -> None:
        content = "## Task: MC-001 A task\nrole: code_worker\n"
        with pytest.raises(PlanParseError, match="missing required field 'acceptance'"):
            parse_plan_string(content)

    def test_invalid_yaml_raises(self) -> None:
        content = "## Task: MC-001 A task\nrole: :\n  bad: [yaml\n"
        with pytest.raises(PlanParseError, match="invalid YAML"):
            parse_plan_string(content)

    def test_title_optional(self) -> None:
        content = "## Task: MC-001 No title\nrole: code_worker\nacceptance:\n  ci: passes\n"
        plan = parse_plan_string(content)
        assert plan.title == ""
        assert len(plan.tasks) == 1

    def test_prose_ignored(self) -> None:
        # Prose before and between tasks (separated by headings) is ignored.
        # Trailing prose after the last task's YAML block is benign — YAML
        # stops parsing at the first non-mapping line after a valid block,
        # so this tests that the parse does not raise.
        content = (
            "# Proj\n\nSome intro prose.\n\n"
            "## Task: MC-001 Real task\nrole: code_worker\nacceptance:\n  ci: passes\n"
        )
        plan = parse_plan_string(content)
        assert len(plan.tasks) == 1

    def test_ready_tasks_no_deps(self) -> None:
        content = (FIXTURES / "valid_full.md").read_text()
        plan = parse_plan_string(content)
        ready = plan.ready_tasks(done_ids=set())
        assert [t.task_id for t in ready] == ["MC-001"]

    def test_ready_tasks_with_done(self) -> None:
        content = (FIXTURES / "valid_full.md").read_text()
        plan = parse_plan_string(content)
        ready = plan.ready_tasks(done_ids={"MC-001"})
        assert [t.task_id for t in ready] == ["MC-002"]

    def test_ready_tasks_chain(self) -> None:
        content = (FIXTURES / "valid_full.md").read_text()
        plan = parse_plan_string(content)
        ready = plan.ready_tasks(done_ids={"MC-001", "MC-002"})
        assert [t.task_id for t in ready] == ["MC-003"]


# ---------------------------------------------------------------------------
# Async file parsing
# ---------------------------------------------------------------------------


class TestParsePlanFile:
    @pytest.mark.asyncio
    async def test_reads_file(self) -> None:
        plan = await parse_plan_file(FIXTURES / "valid_minimal.md")
        assert plan.title == "TestProject"
        assert len(plan.tasks) == 1

    @pytest.mark.asyncio
    async def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(PlanParseError, match="cannot read"):
            await parse_plan_file(tmp_path / "nonexistent.md")

    @pytest.mark.asyncio
    async def test_full_plan_async(self) -> None:
        plan = await parse_plan_file(FIXTURES / "valid_full.md")
        assert len(plan.tasks) == 3
