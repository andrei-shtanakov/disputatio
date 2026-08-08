"""Контракты артефактов disputatio/v1: pydantic-модели и чистые валидаторы.

Публичный API волны 1 ([DESIGN-010]): потребители импортируют модели,
enum'ы, функции валидации, порты и константы причин только отсюда,
не из подмодулей.
"""

from disputatio.contracts.base import SCHEMA_V1, ArtifactBase, ArtifactChild, Role
from disputatio.contracts.decision import Decision, Outcome
from disputatio.contracts.events import Event, EventSource, EventType
from disputatio.contracts.ports import (
    AgentAdapter,
    AgentTurn,
    EventSink,
    StateStore,
    Verifier,
)
from disputatio.contracts.proposal import (
    ProposalFrontmatter,
    ProposalParseError,
    SelfDeclaredStatus,
    parse_proposal,
)
from disputatio.contracts.review import Issue, Review, Severity, Verdict
from disputatio.contracts.session import (
    AgentRef,
    BudgetUsed,
    Limits,
    Mode,
    SessionPhase,
    SessionState,
    TaskSpec,
)
from disputatio.contracts.validation import (
    REASON_APPROVE_ON_FAILED_GATES,
    REASON_EMPTY_CHECKED,
    REASON_NO_SUBSTANTIVE_ISSUES,
    ReviewAcceptance,
    check_checked_nonempty,
    check_substantive_issues,
    check_verdict_vs_verification,
    degrade_unevidenced_issues,
    validate_review,
)
from disputatio.contracts.verification import (
    DiffStats,
    GateResult,
    GateStatus,
    OverallStatus,
    VerificationReport,
)

__all__ = [
    "REASON_APPROVE_ON_FAILED_GATES",
    "REASON_EMPTY_CHECKED",
    "REASON_NO_SUBSTANTIVE_ISSUES",
    "SCHEMA_V1",
    "AgentAdapter",
    "AgentRef",
    "AgentTurn",
    "ArtifactBase",
    "ArtifactChild",
    "BudgetUsed",
    "Decision",
    "DiffStats",
    "Event",
    "EventSink",
    "EventSource",
    "EventType",
    "GateResult",
    "GateStatus",
    "Issue",
    "Limits",
    "Mode",
    "Outcome",
    "OverallStatus",
    "ProposalFrontmatter",
    "ProposalParseError",
    "Review",
    "ReviewAcceptance",
    "Role",
    "SelfDeclaredStatus",
    "SessionPhase",
    "SessionState",
    "Severity",
    "StateStore",
    "TaskSpec",
    "Verdict",
    "VerificationReport",
    "Verifier",
    "check_checked_nonempty",
    "check_substantive_issues",
    "check_verdict_vs_verification",
    "degrade_unevidenced_issues",
    "parse_proposal",
    "validate_review",
]
