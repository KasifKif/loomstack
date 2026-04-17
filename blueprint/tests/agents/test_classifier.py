"""Tests for blueprint/src/loomstack/agents/classifier.py."""

from __future__ import annotations

import pytest

from loomstack.agents.classifier import ClassificationResult, Classifier
from loomstack.core.plan_parser import AcceptanceBlock, Task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(
    description: str = "Add widget module",
    tags: list[str] | None = None,
    notes: str = "",
) -> Task:
    return Task(
        task_id="LS-001",
        description=description,
        role="code_worker",
        acceptance=AcceptanceBlock(tests_pass="unit"),
        tags=tags or [],
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Default classification
# ---------------------------------------------------------------------------


class TestDefaultClassification:
    @pytest.mark.asyncio
    async def test_plain_task_routes_to_code_worker(self) -> None:
        c = Classifier()
        result = await c.classify(make_task())
        assert result.tier == "code_worker"
        assert result.confidence == 1.0
        assert result.tags == frozenset()

    @pytest.mark.asyncio
    async def test_refactor_task_still_code_worker(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Refactor logger module"))
        assert result.tier == "code_worker"


# ---------------------------------------------------------------------------
# Security detection
# ---------------------------------------------------------------------------


class TestSecurityDetection:
    @pytest.mark.asyncio
    async def test_explicit_security_tag(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(tags=["security"]))
        assert "security" in result.tags
        assert result.tier == "architect"

    @pytest.mark.asyncio
    async def test_security_keyword_in_description(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Fix authentication bypass vulnerability"))
        assert "security" in result.tags
        assert result.tier == "architect"

    @pytest.mark.asyncio
    async def test_security_keyword_in_notes(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(notes="Need to rotate credentials after this change"))
        assert "security" in result.tags

    @pytest.mark.asyncio
    async def test_auth_keyword_triggers_security(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Add auth middleware"))
        assert "security" in result.tags

    @pytest.mark.asyncio
    async def test_encryption_keyword(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Add TLS encryption"))
        assert "security" in result.tags


# ---------------------------------------------------------------------------
# Breaking change detection
# ---------------------------------------------------------------------------


class TestBreakingChangeDetection:
    @pytest.mark.asyncio
    async def test_explicit_breaking_change_tag(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(tags=["breaking_change"]))
        assert "breaking_change" in result.tags
        assert result.tier == "architect"

    @pytest.mark.asyncio
    async def test_breaking_change_keyword(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="This is a breaking change to the API"))
        assert "breaking_change" in result.tags

    @pytest.mark.asyncio
    async def test_deprecate_keyword(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Deprecate the old REST endpoint"))
        assert "breaking_change" in result.tags

    @pytest.mark.asyncio
    async def test_both_tags_combined(self) -> None:
        c = Classifier()
        result = await c.classify(
            make_task(
                description="Breaking change: remove auth token from API",
                tags=["security"],
            )
        )
        assert "security" in result.tags
        assert "breaking_change" in result.tags
        assert result.tier == "architect"


# ---------------------------------------------------------------------------
# Architect routing (by keyword, no escalation tags)
# ---------------------------------------------------------------------------


class TestArchitectRouting:
    @pytest.mark.asyncio
    async def test_architecture_keyword(self) -> None:
        c = Classifier()
        result = await c.classify(
            make_task(description="Design new architecture for plugin system")
        )
        assert result.tier == "architect"
        assert result.confidence < 1.0

    @pytest.mark.asyncio
    async def test_rfc_keyword(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Write RFC for new event bus"))
        assert result.tier == "architect"

    @pytest.mark.asyncio
    async def test_decompose_keyword(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Decompose this task into subtasks"))
        assert result.tier == "architect"

    @pytest.mark.asyncio
    async def test_schema_migration_keyword(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Schema migration for user table"))
        assert result.tier == "architect"


# ---------------------------------------------------------------------------
# Reviewer routing
# ---------------------------------------------------------------------------


class TestReviewerRouting:
    @pytest.mark.asyncio
    async def test_review_keyword(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Review the logging changes"))
        assert result.tier == "reviewer"
        assert result.confidence < 1.0

    @pytest.mark.asyncio
    async def test_audit_keyword(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Audit error handling"))
        assert result.tier == "reviewer"

    @pytest.mark.asyncio
    async def test_validate_keyword(self) -> None:
        c = Classifier()
        result = await c.classify(make_task(description="Validate config parsing logic"))
        assert result.tier == "reviewer"


# ---------------------------------------------------------------------------
# ClassificationResult
# ---------------------------------------------------------------------------


class TestClassificationResult:
    def test_immutable(self) -> None:
        r = ClassificationResult(tier="code_worker")
        with pytest.raises(AttributeError):
            r.tier = "reviewer"  # type: ignore[misc]

    def test_default_tags_empty(self) -> None:
        r = ClassificationResult(tier="code_worker")
        assert r.tags == frozenset()

    def test_default_confidence(self) -> None:
        r = ClassificationResult(tier="code_worker")
        assert r.confidence == 1.0


# ---------------------------------------------------------------------------
# Role attribute
# ---------------------------------------------------------------------------


class TestRole:
    def test_role_is_classifier(self) -> None:
        c = Classifier()
        assert c.role == "classifier"
