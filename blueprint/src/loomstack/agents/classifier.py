"""
Keyword-based task classifier.

Scans a task description and tags to decide which agent tier should handle it,
and whether to apply escalation tags (``security``, ``breaking_change``).

No LLM calls — this is a fast, free, deterministic classifier.
LLM-based classification is a future enhancement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from loomstack.core.plan_parser import Task

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassificationResult:
    """Output of the classifier: which tier, what tags, and confidence."""

    tier: str
    tags: frozenset[str] = field(default_factory=frozenset)
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Keyword tables
# ---------------------------------------------------------------------------

# Keywords that push a task toward the architect tier.
_ARCHITECT_KEYWORDS: frozenset[str] = frozenset(
    {
        "architecture",
        "architect",
        "design",
        "rfc",
        "breaking change",
        "breaking_change",
        "api change",
        "schema migration",
        "data migration",
        "decompose",
        "split into",
    }
)

# Keywords that push a task toward the reviewer tier.
_REVIEWER_KEYWORDS: frozenset[str] = frozenset(
    {
        "review",
        "audit",
        "code review",
        "pr review",
        "inspect",
        "verify",
        "validate",
    }
)

# Keywords that trigger the "security" escalation tag.
_SECURITY_KEYWORDS: frozenset[str] = frozenset(
    {
        "security",
        "auth",
        "authentication",
        "authorization",
        "credential",
        "secret",
        "token",
        "vulnerability",
        "cve",
        "injection",
        "xss",
        "csrf",
        "encryption",
        "tls",
        "ssl",
    }
)

# Keywords that trigger the "breaking_change" escalation tag.
_BREAKING_KEYWORDS: frozenset[str] = frozenset(
    {
        "breaking change",
        "breaking_change",
        "backwards incompatible",
        "backward incompatible",
        "remove api",
        "drop support",
        "deprecate",
        "migration required",
    }
)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def _text_matches(text: str, keywords: frozenset[str]) -> bool:
    """Check whether any keyword appears in the lowered text."""
    lower = text.lower()
    return any(kw in lower for kw in keywords)


class Classifier:
    """
    Keyword-based task classifier.

    Determines the target agent tier and applicable escalation tags by scanning
    the task description, notes, and existing tags.
    """

    role: str = "classifier"

    async def classify(self, task: Task) -> ClassificationResult:
        """
        Classify a task into a tier and tag set.

        Priority order:
        1. Explicit tags on the task (``security``, ``breaking_change``) always
           escalate to architect.
        2. Keyword scan of description + notes for architect/reviewer keywords.
        3. Default: ``code_worker``.
        """
        combined_text = f"{task.description} {task.notes}"
        explicit_tags = set(task.tags)
        inferred_tags: set[str] = set()

        # Check for security keywords
        if "security" in explicit_tags or _text_matches(combined_text, _SECURITY_KEYWORDS):
            inferred_tags.add("security")

        # Check for breaking change keywords
        if "breaking_change" in explicit_tags or _text_matches(combined_text, _BREAKING_KEYWORDS):
            inferred_tags.add("breaking_change")

        # Tier determination
        if inferred_tags:
            # Any escalation tag → architect
            tier = "architect"
            confidence = 0.9
        elif _text_matches(combined_text, _ARCHITECT_KEYWORDS):
            tier = "architect"
            confidence = 0.8
        elif _text_matches(combined_text, _REVIEWER_KEYWORDS):
            tier = "reviewer"
            confidence = 0.8
        else:
            tier = "code_worker"
            confidence = 1.0

        result = ClassificationResult(
            tier=tier,
            tags=frozenset(inferred_tags),
            confidence=confidence,
        )

        log.info(
            "classifier.result",
            task_id=task.task_id,
            tier=result.tier,
            tags=sorted(result.tags),
            confidence=result.confidence,
        )
        return result
