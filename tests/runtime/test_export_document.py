"""Экспорт документного пайплайна: SPEC-002 v0.2 §8.2; задача 7 плана.

Рендер идёт по виду, и оба утверждения здесь про это. Заголовок называет
документы ВИДА, а не склеивает два пути; тело перечисляет контуры вида, а не
все существующие. Пустая секция «pair-сессии» в результате документного
пайплайна была бы не безобидной: читатель экспорта не обязан знать, означает
ли пустота «не было» или «потерялось».
"""

from datetime import UTC, datetime
from pathlib import Path

from disputatio.contracts import (
    BudgetUsed,
    FileRef,
    PipelinePhase,
    PipelineState,
    SessionOutcome,
    SessionRecord,
    SingleDocument,
    Transition,
    TransitionReason,
)
from disputatio.events.pipeline_paths import result_dir
from disputatio.runtime.pipeline_export import (
    PR_BODY_NAME,
    PR_TITLE_NAME,
    export_pipeline,
)

_CREATED_AT = datetime(2026, 8, 31, 9, 0, 0, tzinfo=UTC)
_AT = datetime(2026, 8, 31, 9, 1, 0, tzinfo=UTC)
_PIPELINE_ID = "charter"
DOCUMENT_PATH = "docs/charter.md"


def _document_state(*, converged: bool = True) -> PipelineState:
    """Документный пайплайн, дошедший до `EXPORTING` (§2)."""
    transitions = [
        Transition(
            from_=PipelinePhase.IDLE,
            to=PipelinePhase.DOC_LOOP,
            reason=TransitionReason.STARTED,
            at=_AT,
        )
    ]
    if converged:
        transitions.append(
            Transition(
                from_=PipelinePhase.DOC_LOOP,
                to=PipelinePhase.EXPORTING,
                reason=TransitionReason.DOCUMENT_CONVERGED,
                at=_AT,
            )
        )
    else:
        transitions += [
            Transition(
                from_=PipelinePhase.DOC_LOOP,
                to=PipelinePhase.ESCALATED,
                reason=TransitionReason.SESSION_DEADLOCK,
                at=_AT,
            ),
            Transition(
                from_=PipelinePhase.ESCALATED,
                to=PipelinePhase.EXPORTING,
                reason=TransitionReason.EXPORT_PARTIAL,
                at=_AT,
            ),
        ]
    return PipelineState(
        pipeline_id=_PIPELINE_ID,
        created_at=_CREATED_AT,
        phase=PipelinePhase.EXPORTING,
        task=FileRef(path="task.md", sha256="a" * 64),
        config=FileRef(path="config.toml", sha256="b" * 64),
        checklists=FileRef(path="checklists.toml", sha256="c" * 64),
        documents=SingleDocument(kind="document", document_path=DOCUMENT_PATH),
        doc_sessions=[
            SessionRecord(
                revision=1,
                session_id="doc-r1",
                path="sessions/doc-r1",
                entry_hashes={DOCUMENT_PATH: "absent"},
                outcome=SessionOutcome.CONVERGED,
            )
        ],
        transitions=transitions,
        budget_used=BudgetUsed(tokens=100, wall_seconds=1.0, cost_usd_est=0.0),
        anchor_id=_PIPELINE_ID,
    )


def _export(tmp_path: Path, state: PipelineState) -> tuple[str, str]:
    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url=None,
        branch="docs/charter",
        partial=False,
    )
    directory = result_dir(tmp_path, _PIPELINE_ID)
    return (
        (directory / PR_TITLE_NAME).read_text(encoding="utf-8"),
        (directory / PR_BODY_NAME).read_text(encoding="utf-8"),
    )


def test_export_titles_the_single_document(tmp_path: Path) -> None:
    title, _ = _export(tmp_path, _document_state())
    assert title == f"docs: {DOCUMENT_PATH}\n"


def test_export_body_names_the_document(tmp_path: Path) -> None:
    _, body = _export(tmp_path, _document_state())
    assert f"Документ: `{DOCUMENT_PATH}`" in body


def test_export_body_omits_foreign_contour_sections(tmp_path: Path) -> None:
    """Пустая секция чужого контура неотличима от потерянной."""
    _, body = _export(tmp_path, _document_state())
    assert "Спека:" not in body
    assert "План:" not in body
    assert "pair" not in body.lower()
    assert "spec" not in body.lower()


def test_export_body_lists_the_doc_session(tmp_path: Path) -> None:
    _, body = _export(tmp_path, _document_state())
    assert "doc r1 `doc-r1`: converged" in body


def test_partial_export_names_the_escalation(tmp_path: Path) -> None:
    title, body = _export(tmp_path, _document_state(converged=False))
    assert title.startswith("[partial] ")
    assert "session_deadlock" in body
