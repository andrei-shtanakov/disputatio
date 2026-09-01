"""CLI различает вид: SPEC-002 v0.2 §3.1 C1/C2/C4; задача 7 плана.

Команды у обоих видов общие, а вид выводится из конфига — цена решения —
невидимость второго вида в `--help`. Гасится она нормативными требованиями
C1–C4, а не документацией, поэтому здесь они и проверяются как требования.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from disputatio.cli import main, render_status
from disputatio.contracts import (
    BudgetUsed,
    FileRef,
    PairDocuments,
    PipelinePhase,
    PipelineState,
    SessionRecord,
    SingleDocument,
    Transition,
    TransitionReason,
)

_AT = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


def _state(
    documents: PairDocuments | SingleDocument, **overrides: Any
) -> PipelineState:
    payload: dict[str, Any] = {
        "pipeline_id": "charter",
        "created_at": _AT,
        "phase": PipelinePhase.IDLE,
        "task": FileRef(path="task.md", sha256="a" * 64),
        "config": FileRef(path="config.toml", sha256="b" * 64),
        "checklists": FileRef(path="checklists.toml", sha256="c" * 64),
        "documents": documents,
        "budget_used": BudgetUsed(),
        "anchor_id": "charter",
    }
    payload.update(overrides)
    return PipelineState(**payload)


def _document_state() -> PipelineState:
    return _state(
        SingleDocument(kind="document", document_path="docs/charter.md"),
        phase=PipelinePhase.DOC_LOOP,
        doc_sessions=[
            SessionRecord(
                revision=1,
                session_id="doc-r1",
                path="sessions/doc-r1",
                entry_hashes={"docs/charter.md": "absent"},
            )
        ],
        transitions=[
            Transition(
                from_=PipelinePhase.IDLE,
                to=PipelinePhase.DOC_LOOP,
                reason=TransitionReason.STARTED,
                at=_AT,
            )
        ],
    )


def _pair_state() -> PipelineState:
    return _state(PairDocuments(spec_path="docs/spec.md", plan_path="docs/plan.md"))


# --- C4: вид первой строкой ------------------------------------------


def test_status_prints_kind_first(tmp_path: Path) -> None:
    lines = render_status(_document_state(), tmp_path / "a.jsonl").splitlines()
    assert lines[0] == "kind: document"
    assert "documents: docs/charter.md" in lines


def test_status_of_pair_prints_kind_too(tmp_path: Path) -> None:
    lines = render_status(_pair_state(), tmp_path / "a.jsonl").splitlines()
    assert lines[0] == "kind: pair"
    assert "documents: docs/spec.md + docs/plan.md" in lines


def test_status_lists_doc_revisions(tmp_path: Path) -> None:
    """Ревизии контура `doc` видны в статусе — иначе он молчит о работе."""
    rendered = render_status(_document_state(), tmp_path / "a.jsonl")
    assert "doc r1 doc-r1: активна" in rendered


# --- C1/C2: обе формы в справке --------------------------------------


def test_run_help_shows_both_config_forms(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["pipeline", "run", "--help"])
    out = capsys.readouterr().out
    assert "document_path" in out
    assert "spec_path" in out
    assert "plan_path" in out


def test_pipeline_help_names_both_forms(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["pipeline", "--help"])
    out = capsys.readouterr().out
    assert "одиночн" in out
    assert "пар" in out
