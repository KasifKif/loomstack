"""Tests for blueprint/src/loomstack/core/ci_status.py."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, patch

import pytest

from loomstack.core.ci_status import (
    CheckResult,
    CIResult,
    CIState,
    _aggregate_state,
    _build_summary,
    _map_check_state,
    poll_pr_checks,
)
from loomstack.core.github import GitError

# ---------------------------------------------------------------------------
# _map_check_state
# ---------------------------------------------------------------------------


class TestMapCheckState:
    def test_maps_basic_check(self) -> None:
        raw = {"name": "lint", "status": "COMPLETED", "conclusion": "success"}
        result = _map_check_state(raw)
        assert result.name == "lint"
        assert result.state == "COMPLETED"
        assert result.conclusion == "success"

    def test_uses_context_as_fallback_name(self) -> None:
        raw = {"context": "ci/build", "status": "COMPLETED", "conclusion": "failure"}
        result = _map_check_state(raw)
        assert result.name == "ci/build"

    def test_unknown_name_on_empty(self) -> None:
        raw = {"status": "QUEUED"}
        result = _map_check_state(raw)
        assert result.name == "unknown"

    def test_missing_conclusion(self) -> None:
        raw = {"name": "test", "status": "IN_PROGRESS"}
        result = _map_check_state(raw)
        assert result.conclusion == ""


# ---------------------------------------------------------------------------
# _aggregate_state
# ---------------------------------------------------------------------------


class TestAggregateState:
    def test_empty_checks_is_pending(self) -> None:
        assert _aggregate_state([]) == CIState.PENDING

    def test_all_success(self) -> None:
        checks = [
            CheckResult(name="lint", state="COMPLETED", conclusion="success"),
            CheckResult(name="test", state="COMPLETED", conclusion="success"),
        ]
        assert _aggregate_state(checks) == CIState.SUCCESS

    def test_any_failure(self) -> None:
        checks = [
            CheckResult(name="lint", state="COMPLETED", conclusion="success"),
            CheckResult(name="test", state="COMPLETED", conclusion="failure"),
        ]
        assert _aggregate_state(checks) == CIState.FAILURE

    def test_any_error(self) -> None:
        checks = [
            CheckResult(name="lint", state="COMPLETED", conclusion="success"),
            CheckResult(name="deploy", state="COMPLETED", conclusion="error"),
        ]
        assert _aggregate_state(checks) == CIState.ERROR

    def test_pending_when_in_progress(self) -> None:
        checks = [
            CheckResult(name="lint", state="COMPLETED", conclusion="success"),
            CheckResult(name="test", state="IN_PROGRESS", conclusion=""),
        ]
        assert _aggregate_state(checks) == CIState.PENDING

    def test_pending_when_queued(self) -> None:
        checks = [
            CheckResult(name="test", state="QUEUED", conclusion=""),
        ]
        assert _aggregate_state(checks) == CIState.PENDING

    def test_failure_beats_pending(self) -> None:
        checks = [
            CheckResult(name="lint", state="COMPLETED", conclusion="failure"),
            CheckResult(name="test", state="IN_PROGRESS", conclusion=""),
        ]
        assert _aggregate_state(checks) == CIState.FAILURE

    def test_timed_out_is_failure(self) -> None:
        checks = [
            CheckResult(name="test", state="COMPLETED", conclusion="timed_out"),
        ]
        assert _aggregate_state(checks) == CIState.FAILURE


# ---------------------------------------------------------------------------
# _build_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    def test_no_checks(self) -> None:
        assert "No CI checks" in _build_summary(CIState.PENDING, [])

    def test_all_passed(self) -> None:
        checks = [
            CheckResult(name="lint", state="COMPLETED", conclusion="success"),
            CheckResult(name="test", state="COMPLETED", conclusion="success"),
        ]
        summary = _build_summary(CIState.SUCCESS, checks)
        assert "2 passed" in summary
        assert "2 checks" in summary

    def test_failure_lists_names(self) -> None:
        checks = [
            CheckResult(name="lint", state="COMPLETED", conclusion="success"),
            CheckResult(name="test", state="COMPLETED", conclusion="failure"),
        ]
        summary = _build_summary(CIState.FAILURE, checks)
        assert "test" in summary
        assert "Failed:" in summary

    def test_mixed_counts(self) -> None:
        checks = [
            CheckResult(name="lint", state="COMPLETED", conclusion="success"),
            CheckResult(name="test", state="IN_PROGRESS", conclusion=""),
            CheckResult(name="deploy", state="COMPLETED", conclusion="failure"),
        ]
        summary = _build_summary(CIState.FAILURE, checks)
        assert "1 passed" in summary
        assert "1 failed" in summary
        assert "1 pending" in summary


# ---------------------------------------------------------------------------
# poll_pr_checks
# ---------------------------------------------------------------------------


class TestPollPrChecks:
    @pytest.mark.asyncio
    async def test_success_result(self) -> None:
        data = {
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [
                {"name": "lint", "status": "COMPLETED", "conclusion": "success"},
                {"name": "test", "status": "COMPLETED", "conclusion": "success"},
            ],
        }
        with patch("loomstack.core.ci_status.get_pr_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = data
            result = await poll_pr_checks(Path("/repo"), "https://github.com/org/repo/pull/1")

        assert result.state == CIState.SUCCESS
        assert len(result.checks) == 2
        assert result.pr_url == "https://github.com/org/repo/pull/1"

    @pytest.mark.asyncio
    async def test_failure_result(self) -> None:
        data = {
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [
                {"name": "test", "status": "COMPLETED", "conclusion": "failure"},
            ],
        }
        with patch("loomstack.core.ci_status.get_pr_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = data
            result = await poll_pr_checks(Path("/repo"), "url")

        assert result.state == CIState.FAILURE
        assert "test" in result.summary

    @pytest.mark.asyncio
    async def test_pending_result(self) -> None:
        data = {
            "state": "OPEN",
            "mergeable": "UNKNOWN",
            "statusCheckRollup": [
                {"name": "test", "status": "IN_PROGRESS"},
            ],
        }
        with patch("loomstack.core.ci_status.get_pr_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = data
            result = await poll_pr_checks(Path("/repo"), "url")

        assert result.state == CIState.PENDING

    @pytest.mark.asyncio
    async def test_empty_checks(self) -> None:
        data = {"state": "OPEN", "mergeable": "MERGEABLE", "statusCheckRollup": []}
        with patch("loomstack.core.ci_status.get_pr_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = data
            result = await poll_pr_checks(Path("/repo"), "url")

        assert result.state == CIState.PENDING
        assert len(result.checks) == 0

    @pytest.mark.asyncio
    async def test_null_checks(self) -> None:
        data = {"state": "OPEN", "mergeable": "MERGEABLE", "statusCheckRollup": None}
        with patch("loomstack.core.ci_status.get_pr_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = data
            result = await poll_pr_checks(Path("/repo"), "url")

        assert result.state == CIState.PENDING

    @pytest.mark.asyncio
    async def test_error_on_fetch_failure(self) -> None:
        with patch("loomstack.core.ci_status.get_pr_status", new_callable=AsyncMock) as mock_status:
            mock_status.side_effect = GitError("gh pr view", 1, "not found")
            result = await poll_pr_checks(Path("/repo"), "bad-url")

        assert result.state == CIState.ERROR
        assert "Failed to fetch" in result.summary


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class TestCIResult:
    def test_frozen(self) -> None:
        r = CIResult(state=CIState.SUCCESS, pr_url="url")
        with pytest.raises(AttributeError):
            r.state = CIState.FAILURE  # type: ignore[misc]


class TestCheckResult:
    def test_frozen(self) -> None:
        c = CheckResult(name="test", state="COMPLETED", conclusion="success")
        with pytest.raises(AttributeError):
            c.name = "other"  # type: ignore[misc]
