"""Tests for blueprint/src/loomstack/core/github.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from loomstack.core.github import (
    GitError,
    checkout_branch,
    commit_and_push,
    create_branch,
    get_pr_diff,
    get_pr_status,
    list_open_prs,
    open_pr,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> AsyncMock:
    """Build a mock process matching asyncio.create_subprocess_exec return."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    return proc


# ---------------------------------------------------------------------------
# create_branch
# ---------------------------------------------------------------------------


class TestCreateBranch:
    @pytest.mark.asyncio
    async def test_calls_git_checkout(self) -> None:
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc()
            await create_branch(Path("/repo"), "feat/test")
            mock_exec.assert_called_once()
            args = mock_exec.call_args[0]
            assert args == ("git", "checkout", "-b", "feat/test")

    @pytest.mark.asyncio
    async def test_raises_on_failure(self) -> None:
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(128, stderr="already exists")
            with pytest.raises(GitError) as exc_info:
                await create_branch(Path("/repo"), "feat/test")
            assert exc_info.value.exit_code == 128
            assert "already exists" in exc_info.value.stderr


# ---------------------------------------------------------------------------
# checkout_branch
# ---------------------------------------------------------------------------


class TestCheckoutBranch:
    @pytest.mark.asyncio
    async def test_calls_git_checkout(self) -> None:
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc()
            await checkout_branch(Path("/repo"), "develop")
            args = mock_exec.call_args[0]
            assert args == ("git", "checkout", "develop")

    @pytest.mark.asyncio
    async def test_raises_on_failure(self) -> None:
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(1, stderr="did not match")
            with pytest.raises(GitError):
                await checkout_branch(Path("/repo"), "no-such-branch")


# ---------------------------------------------------------------------------
# commit_and_push
# ---------------------------------------------------------------------------


class TestCommitAndPush:
    @pytest.mark.asyncio
    async def test_stages_commits_and_pushes(self) -> None:
        calls: list[tuple[str, ...]] = []

        async def fake_exec(*args: str, **kwargs: object) -> AsyncMock:
            calls.append(args)
            return _make_proc(returncode=1 if args[1:3] == ("diff", "--cached") else 0)

        with patch("loomstack.core.github.asyncio.create_subprocess_exec", side_effect=fake_exec):
            await commit_and_push(Path("/repo"), "feat/x", "msg")

        # Expect: git add, git diff --cached --quiet, git commit, git push
        cmds = [c[0:3] for c in calls]
        assert ("git", "add", "-A") in cmds
        assert ("git", "commit", "-m") in cmds
        assert ("git", "push", "-u") in cmds

    @pytest.mark.asyncio
    async def test_skips_commit_when_nothing_staged(self) -> None:
        calls: list[tuple[str, ...]] = []

        async def fake_exec(*args: str, **kwargs: object) -> AsyncMock:
            calls.append(args)
            return _make_proc(returncode=0)  # diff --cached --quiet → 0 means clean

        with patch("loomstack.core.github.asyncio.create_subprocess_exec", side_effect=fake_exec):
            await commit_and_push(Path("/repo"), "feat/x", "msg")

        cmd_strs = [" ".join(c) for c in calls]
        assert not any("commit" in s for s in cmd_strs)

    @pytest.mark.asyncio
    async def test_raises_on_push_failure(self) -> None:
        call_count = 0

        async def fake_exec(*args: str, **kwargs: object) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            if "push" in args:
                return _make_proc(1, stderr="rejected")
            if args[1:3] == ("diff", "--cached"):
                return _make_proc(returncode=1)  # has staged changes
            return _make_proc()

        with (
            patch("loomstack.core.github.asyncio.create_subprocess_exec", side_effect=fake_exec),
            pytest.raises(GitError, match="rejected"),
        ):
            await commit_and_push(Path("/repo"), "feat/x", "msg")


# ---------------------------------------------------------------------------
# open_pr
# ---------------------------------------------------------------------------


class TestOpenPr:
    @pytest.mark.asyncio
    async def test_returns_pr_url(self) -> None:
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(stdout="https://github.com/org/repo/pull/42")
            url = await open_pr(Path("/repo"), "feat/x", "Title", "Body")
            assert url == "https://github.com/org/repo/pull/42"

    @pytest.mark.asyncio
    async def test_passes_correct_args(self) -> None:
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(stdout="url")
            await open_pr(Path("/repo"), "feat/x", "Title", "Body", base="develop")
            args = mock_exec.call_args[0]
            assert "--base" in args
            idx = args.index("--base")
            assert args[idx + 1] == "develop"

    @pytest.mark.asyncio
    async def test_raises_on_failure(self) -> None:
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(1, stderr="no remote")
            with pytest.raises(GitError):
                await open_pr(Path("/repo"), "feat/x", "T", "B")


# ---------------------------------------------------------------------------
# get_pr_status
# ---------------------------------------------------------------------------


class TestGetPrStatus:
    @pytest.mark.asyncio
    async def test_returns_parsed_json(self) -> None:
        payload = {"state": "OPEN", "mergeable": "MERGEABLE", "statusCheckRollup": []}
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(stdout=json.dumps(payload))
            result = await get_pr_status(Path("/repo"), "https://github.com/org/repo/pull/1")
            assert result == payload

    @pytest.mark.asyncio
    async def test_raises_on_failure(self) -> None:
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(1, stderr="not found")
            with pytest.raises(GitError):
                await get_pr_status(Path("/repo"), "https://github.com/org/repo/pull/999")


# ---------------------------------------------------------------------------
# get_pr_diff
# ---------------------------------------------------------------------------


class TestGetPrDiff:
    @pytest.mark.asyncio
    async def test_returns_diff_string(self) -> None:
        diff_text = "diff --git a/file.py b/file.py\n+new line"
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(stdout=diff_text)
            result = await get_pr_diff(Path("/repo"), "https://github.com/org/repo/pull/1")
            assert result == diff_text


# ---------------------------------------------------------------------------
# list_open_prs
# ---------------------------------------------------------------------------


class TestListOpenPrs:
    @pytest.mark.asyncio
    async def test_returns_parsed_list(self) -> None:
        prs = [{"number": 1, "title": "PR1", "headRefName": "feat/a", "url": "u", "state": "OPEN"}]
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(stdout=json.dumps(prs))
            result = await list_open_prs(Path("/repo"))
            assert len(result) == 1
            assert result[0]["number"] == 1

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_output(self) -> None:
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(stdout="")
            result = await list_open_prs(Path("/repo"))
            assert result == []

    @pytest.mark.asyncio
    async def test_raises_on_failure(self) -> None:
        with patch("loomstack.core.github.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _make_proc(1, stderr="auth required")
            with pytest.raises(GitError):
                await list_open_prs(Path("/repo"))


# ---------------------------------------------------------------------------
# GitError
# ---------------------------------------------------------------------------


class TestGitError:
    def test_fields(self) -> None:
        err = GitError("git push", 1, "rejected")
        assert err.cmd == "git push"
        assert err.exit_code == 1
        assert err.stderr == "rejected"
        assert "git push" in str(err)
        assert "rejected" in str(err)
