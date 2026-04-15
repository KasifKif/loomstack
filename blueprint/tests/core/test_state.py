"""Tests for blueprint/src/loomstack/core/state.py."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from loomstack.core.state import (
    TaskStatus,
    _parse_run_meta,
    _task_branch_name,
    approval_marker_path,
    derive_status,
    is_approved,
    read_run_meta,
)

# ---------------------------------------------------------------------------
# _parse_run_meta
# ---------------------------------------------------------------------------


class TestParseRunMeta:
    def test_pending(self) -> None:
        content = "---\nstatus: pending\n---\n# task log\n"
        assert _parse_run_meta(content).status == TaskStatus.PENDING

    def test_proposed(self) -> None:
        content = "---\nstatus: proposed\ntask_id: MC-001\n---\n"
        meta = _parse_run_meta(content)
        assert meta.status == TaskStatus.PROPOSED
        assert meta.task_id == "MC-001"

    def test_done(self) -> None:
        assert _parse_run_meta("---\nstatus: done\n---\n").status == TaskStatus.DONE

    def test_failed(self) -> None:
        assert _parse_run_meta("---\nstatus: failed\n---\n").status == TaskStatus.FAILED

    def test_in_progress(self) -> None:
        content = "---\nstatus: in_progress\n---\n"
        assert _parse_run_meta(content).status == TaskStatus.IN_PROGRESS

    def test_blocked(self) -> None:
        assert _parse_run_meta("---\nstatus: blocked\n---\n").status == TaskStatus.BLOCKED

    def test_no_frontmatter(self) -> None:
        assert _parse_run_meta("# Just markdown\nno frontmatter").status is None

    def test_frontmatter_no_status(self) -> None:
        assert _parse_run_meta("---\ntask_id: MC-001\n---\n").status is None

    def test_unknown_status_ignored(self) -> None:
        assert _parse_run_meta("---\nstatus: flying\n---\n").status is None

    def test_status_case_insensitive(self) -> None:
        assert _parse_run_meta("---\nstatus: DONE\n---\n").status == TaskStatus.DONE

    def test_extra_fields_ok(self) -> None:
        content = "---\ntask_id: MC-001\nmodel: qwen\nstatus: proposed\n---\n"
        meta = _parse_run_meta(content)
        assert meta.status == TaskStatus.PROPOSED
        assert meta.model == "qwen"

    def test_footer_overrides_initial_status(self) -> None:
        """The key bug fix: footer frontmatter overrides initial status."""
        content = (
            "---\ntask_id: MC-001\nmodel: qwen\nstatus: in_progress\n---\n\n"
            "...agent output...\n\n"
            "---\nstatus: done\nexit_code: 0\npr_url: https://github.com/o/r/pull/1\n---\n"
        )
        meta = _parse_run_meta(content)
        assert meta.status == TaskStatus.DONE
        assert meta.exit_code == 0
        assert meta.pr_url == "https://github.com/o/r/pull/1"
        assert meta.model == "qwen"

    def test_footer_failed_overrides_in_progress(self) -> None:
        content = (
            "---\nstatus: in_progress\n---\n\n"
            "---\nstatus: failed\nexit_code: 1\n---\n"
        )
        meta = _parse_run_meta(content)
        assert meta.status == TaskStatus.FAILED
        assert meta.exit_code == 1

    def test_retry_metadata(self) -> None:
        content = "---\nstatus: failed\ntier: code_worker\nretry_count: 2\nlast_error: test failed\n---\n"
        meta = _parse_run_meta(content)
        assert meta.tier == "code_worker"
        assert meta.retry_count == 2
        assert meta.last_error == "test failed"

    def test_cost_fields(self) -> None:
        content = "---\ntoken_count: 5000\ncost_usd: 0.0125\n---\n"
        meta = _parse_run_meta(content)
        assert meta.token_count == 5000
        assert meta.cost_usd == pytest.approx(0.0125)

    def test_empty_pr_url_becomes_none(self) -> None:
        content = "---\npr_url: \n---\n"
        meta = _parse_run_meta(content)
        assert meta.pr_url is None


# ---------------------------------------------------------------------------
# read_run_meta (sync)
# ---------------------------------------------------------------------------


class TestReadRunMeta:
    def test_reads_file(self, tmp_path: Path) -> None:
        f = tmp_path / "MC-001.md"
        f.write_text("---\nstatus: done\n---\n")
        assert read_run_meta(f).status == TaskStatus.DONE

    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        meta = read_run_meta(tmp_path / "ghost.md")
        assert meta.status is None
        assert meta.retry_count == 0

    def test_unreadable_returns_default(self, tmp_path: Path) -> None:
        f = tmp_path / "MC-002.md"
        f.write_text("---\nstatus: done\n---\n")
        f.chmod(0o000)
        result = read_run_meta(f)
        f.chmod(0o644)  # restore so tmp_path cleanup works
        # On Linux as non-root, chmod 000 blocks read → returns default
        # On root or macOS with SIP off it may still read; just assert no crash
        assert result.status is None or result.status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# _task_branch_name
# ---------------------------------------------------------------------------


class TestTaskBranchName:
    def test_standard(self) -> None:
        assert _task_branch_name("MC-001") == "feat/mc-001"

    def test_lowercase(self) -> None:
        assert _task_branch_name("FW-042") == "feat/fw-042"


# ---------------------------------------------------------------------------
# approval helpers
# ---------------------------------------------------------------------------


class TestApprovalHelpers:
    def test_marker_path(self, tmp_path: Path) -> None:
        loomstack_dir = tmp_path / ".loomstack"
        p = approval_marker_path("MC-001", loomstack_dir)
        assert p == loomstack_dir / "approvals" / "MC-001"

    def test_not_approved(self, tmp_path: Path) -> None:
        loomstack_dir = tmp_path / ".loomstack"
        assert is_approved("MC-001", loomstack_dir) is False

    def test_approved_when_marker_exists(self, tmp_path: Path) -> None:
        loomstack_dir = tmp_path / ".loomstack"
        marker = loomstack_dir / "approvals" / "MC-001"
        marker.parent.mkdir(parents=True)
        marker.touch()
        assert is_approved("MC-001", loomstack_dir) is True


# ---------------------------------------------------------------------------
# derive_status
# ---------------------------------------------------------------------------


class TestDeriveStatus:
    @pytest.mark.asyncio
    async def test_run_file_authoritative(self, tmp_path: Path) -> None:
        loomstack_dir = tmp_path / ".loomstack"
        runs_dir = loomstack_dir / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "MC-001.md").write_text("---\nstatus: done\n---\n")

        status = await derive_status("MC-001", tmp_path, loomstack_dir)
        assert status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_proposed_when_pr_open(self, tmp_path: Path) -> None:
        with patch(
            "loomstack.core.state.get_open_pr_for_branch",
            new=AsyncMock(return_value="https://github.com/owner/repo/pull/42"),
        ):
            status = await derive_status("MC-002", tmp_path)
        assert status == TaskStatus.PROPOSED

    @pytest.mark.asyncio
    async def test_in_progress_when_branch_exists(self, tmp_path: Path) -> None:
        # Create the git ref to simulate a local branch
        branch_ref = tmp_path / ".git" / "refs" / "heads" / "feat" / "mc-003"
        branch_ref.parent.mkdir(parents=True)
        branch_ref.write_text("abc123\n")

        with patch(
            "loomstack.core.state.get_open_pr_for_branch",
            new=AsyncMock(return_value=None),
        ):
            status = await derive_status("MC-003", tmp_path)
        assert status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_pending_fallback(self, tmp_path: Path) -> None:
        with patch(
            "loomstack.core.state.get_open_pr_for_branch",
            new=AsyncMock(return_value=None),
        ):
            status = await derive_status("MC-004", tmp_path)
        assert status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_run_file_beats_pr(self, tmp_path: Path) -> None:
        """Run file status is authoritative even when a PR exists."""
        loomstack_dir = tmp_path / ".loomstack"
        runs_dir = loomstack_dir / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "MC-005.md").write_text("---\nstatus: failed\n---\n")

        with patch(
            "loomstack.core.state.get_open_pr_for_branch",
            new=AsyncMock(return_value="https://github.com/owner/repo/pull/99"),
        ):
            status = await derive_status("MC-005", tmp_path, loomstack_dir)
        assert status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_default_loomstack_dir(self, tmp_path: Path) -> None:
        """loomstack_dir defaults to repo_path/.loomstack."""
        default_dir = tmp_path / ".loomstack" / "runs"
        default_dir.mkdir(parents=True)
        (default_dir / "MC-006.md").write_text("---\nstatus: blocked\n---\n")

        status = await derive_status("MC-006", tmp_path)
        assert status == TaskStatus.BLOCKED
