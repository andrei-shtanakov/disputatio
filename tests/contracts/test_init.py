"""Тесты __init__.py — публичный API contracts: TASK-012, [DESIGN-010].

Пакет `disputatio.contracts` существует с самого начала (докстринг), поэтому
его импорт на уровне модуля безопасен и на red-чекпоинте. Red-селектор
(`test_public_names_importable_from_package`) падает assertion'ом, пока
реэкспорт не написан: `hasattr` возвращает False без ImportError.
Потребители волны 1 импортируют только из `disputatio.contracts`,
не из подмодулей — тесты фиксируют и состав `__all__`, и тождественность
реэкспортированных объектов оригиналам в подмодулях.
"""

import importlib
from typing import Final

from disputatio import contracts

# Публичные имена по подмодулям — источник ожиданий для всех тестов ниже.
EXPECTED_BY_MODULE: Final[dict[str, tuple[str, ...]]] = {
    "base": ("SCHEMA_V1", "SCHEMA_V2", "ArtifactBase", "ArtifactChild", "Role"),
    "checklist": (
        "ArtifactEvidence",
        "GateEvidence",
        "EvidenceRef",
        "ChecklistItem",
    ),
    "checklists_catalog": (
        "SPEC_CHECKLIST",
        "PAIR_CHECKLIST",
        "CHECKLIST_BY_CONTOUR",
        "CHECKLIST_TEXT",
    ),
    "session": (
        "SessionPhase",
        "Mode",
        "TaskSpec",
        "AgentRef",
        "Limits",
        "BudgetUsed",
        "SessionState",
    ),
    "verification": (
        "GateStatus",
        "OverallStatus",
        "GateResult",
        "DiffStats",
        "VerificationReport",
    ),
    "proposal": (
        "SelfDeclaredStatus",
        "ProposalFrontmatter",
        "ProposalParseError",
        "parse_proposal",
    ),
    "review": ("Verdict", "Severity", "Issue", "Review"),
    "decision": ("Outcome", "Decision"),
    "events": ("EventSource", "EventType", "Event"),
    "validation": (
        "REASON_NO_SUBSTANTIVE_ISSUES",
        "REASON_APPROVE_ON_FAILED_GATES",
        "REASON_EMPTY_CHECKED",
        "REASON_CHECKLIST_ID_MISMATCH",
        "REASON_APPROVE_WITH_CHECKLIST_FAIL",
        "REASON_CHECKLIST_FAIL_WITHOUT_ISSUE_IDS",
        "REASON_CHECKLIST_FAIL_UNKNOWN_ISSUE_ID",
        "REASON_CHECKLIST_FAIL_ISSUE_SEVERITY_TOO_LOW",
        "REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS",
        "REASON_APPROVE_WITH_SUBSTANTIVE_ISSUE",
        "REASON_CHECKLIST_PASS_CONTRADICTS_S1",
        "ReviewAcceptance",
        "validate_review",
        "validate_doc_review",
        "degrade_unevidenced_issues",
        "check_substantive_issues",
        "check_verdict_vs_verification",
        "check_checked_nonempty",
    ),
    "ports": (
        "StateStore",
        "EventSink",
        "AgentAdapter",
        "Verifier",
        "AgentTurn",
        "PipelineStateStore",
        "BoundaryVerdict",
        "RoundBoundaryPolicy",
        "SessionLifecyclePolicy",
    ),
    "pipeline": (
        "SCHEMA_PIPELINE_V1",
        "SCHEMA_PIPELINE_V2",
        "ALLOWED_TRANSITIONS",
        "CONTOURS_BY_KIND",
        "TERMINAL_CONTOUR",
        "ENTRY_PHASE",
        "SESSIONS_FIELD_BY_CONTOUR",
        "PHASES_BY_KIND",
        "EDGES_BY_KIND",
        "PipelineKind",
        "PipelinePhase",
        "TransitionReason",
        "SessionOutcome",
        "PipelineArtifactBase",
        "FileRef",
        "PairDocuments",
        "SingleDocument",
        "Documents",
        "EvidenceLink",
        "SessionRecord",
        "Transition",
        "OperatorDecision",
        "NextAction",
        "AppendOnlyEntry",
        "IntegritySnapshot",
        "PipelineState",
        # Правило путей манифеста нужно и снаружи схемы: конфиг проверяет им
        # пару документов ДО создания durable-состояния (§4.2, D2).
        "validate_relative_path",
    ),
}

EXPECTED_PUBLIC_NAMES: Final[tuple[str, ...]] = tuple(
    name for names in EXPECTED_BY_MODULE.values() for name in names
)


def test_public_names_importable_from_package() -> None:
    """Каждое публичное имя доступно как атрибут `disputatio.contracts`."""
    missing = [name for name in EXPECTED_PUBLIC_NAMES if not hasattr(contracts, name)]
    assert missing == []


def test_all_consistent_with_reexports() -> None:
    """`__all__` без дубликатов и поимённо совпадает с реэкспортом."""
    exported = getattr(contracts, "__all__", None)
    assert exported is not None
    assert len(exported) == len(set(exported))
    assert set(exported) == set(EXPECTED_PUBLIC_NAMES)


def test_reexports_are_submodule_objects() -> None:
    """Реэкспорт — те же объекты, что в подмодулях (не копии и не тени)."""
    mismatched = [
        name
        for module_name, names in EXPECTED_BY_MODULE.items()
        for name in names
        if getattr(contracts, name, None)
        is not getattr(
            importlib.import_module(f"disputatio.contracts.{module_name}"), name
        )
    ]
    assert mismatched == []
