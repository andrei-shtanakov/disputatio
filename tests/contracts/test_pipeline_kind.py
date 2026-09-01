"""Вид пайплайна в схеме манифеста: SPEC-002 v0.2 §1, §2, §4.2.

Файл закрывает ровно то, что редакция v0.2 добавила к семейству
`disputatio/pipeline/*`: дискриминатор `documents.kind`, коллекцию
`doc_sessions`, фазу `DOC_LOOP` с её рёбрами и две версии тега схемы.
Инварианты, общие обоим видам, живут в `test_pipeline_state.py` и здесь не
дублируются.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from disputatio.contracts.pipeline import (
    ALLOWED_TRANSITIONS,
    CONTOURS_BY_KIND,
    EDGES_BY_KIND,
    ENTRY_PHASE,
    SCHEMA_PIPELINE_V1,
    SCHEMA_PIPELINE_V2,
    TERMINAL_CONTOUR,
    FileRef,
    PairDocuments,
    PipelineKind,
    PipelinePhase,
    PipelineState,
    SingleDocument,
    TransitionReason,
)
from disputatio.contracts.session import BudgetUsed

_SHA = "a" * 64

#: Манифест пары, записанный реализацией v0.1 — без `documents.kind` и без
#: `doc_sessions`. Читается тестами совместимости К2 (§4.2) и интеграционным
#: набором задачи 8, поэтому лежит файлом, а не собирается кодом: «манифест,
#: записанный ДО этой ветки» обязан быть байтами, а не тем, что нынешняя
#: модель считает старым форматом.
_V1_FIXTURE = Path(__file__).parent.parent / "fixtures" / "pipeline_v1_pair.json"


def _file_ref(path: str) -> dict[str, str]:
    return {"path": path, "sha256": _SHA}


def _payload(
    schema: str, documents: dict[str, Any], **overrides: Any
) -> dict[str, Any]:
    """Минимальный валидный payload манифеста нужной формы."""
    payload: dict[str, Any] = {
        "schema": schema,
        "pipeline_id": "pipe-1",
        "created_at": "2026-08-31T12:00:00+00:00",
        "phase": "IDLE",
        "task": _file_ref("task.md"),
        "config": _file_ref("config.toml"),
        "checklists": _file_ref("checklists.toml"),
        "documents": documents,
        "transitions": [],
        "budget_used": {"tokens": 0, "wall_seconds": 0.0, "cost_usd_est": 0.0},
        "operator_decisions": [],
        "anchor_id": "pipe-1",
        "next_action": None,
    }
    payload.update(overrides)
    return payload


def _pair_payload(schema: str = SCHEMA_PIPELINE_V2, **overrides: Any) -> dict[str, Any]:
    documents: dict[str, Any] = {
        "spec_path": "docs/spec.md",
        "plan_path": "docs/plan.md",
    }
    if schema == SCHEMA_PIPELINE_V2:
        documents["kind"] = "pair"
    return _payload(schema, documents, **overrides)


def _document_payload(
    schema: str = SCHEMA_PIPELINE_V2, **overrides: Any
) -> dict[str, Any]:
    return _payload(
        schema,
        {"kind": "document", "document_path": "docs/charter.md"},
        **overrides,
    )


# --- совместимость версий (§4.2, три обязательных теста) --------------


def test_v1_payload_without_kind_reads_as_pair() -> None:
    """Манифест v0.1 поля kind не несёт — нормализуется по тегу, не дефолтом."""
    state = PipelineState.model_validate(_pair_payload(SCHEMA_PIPELINE_V1))
    assert state.kind is PipelineKind.PAIR


def test_v1_payload_carrying_kind_is_rejected() -> None:
    """Файл, лгущий о своей форме, проходить не должен."""
    payload = _pair_payload(SCHEMA_PIPELINE_V1)
    payload["documents"]["kind"] = "pair"
    with pytest.raises(ValidationError, match="v1"):
        PipelineState.model_validate(payload)


def test_every_write_carries_v2_tag() -> None:
    """Пара, заведённая как v1, при первой же записи объявляет форму честно."""
    state = PipelineState.model_validate(_pair_payload(SCHEMA_PIPELINE_V1))
    assert state.model_dump(mode="json")["schema"] == SCHEMA_PIPELINE_V2


# --- union документов (§4.2) ------------------------------------------


def test_single_document_rejects_plan_path() -> None:
    """«Документный пайплайн с планом» невыразим схемой, а не редок."""
    with pytest.raises(ValidationError):
        SingleDocument.model_validate(
            {
                "kind": "document",
                "document_path": "docs/charter.md",
                "plan_path": "docs/plan.md",
            }
        )


def test_pair_documents_paths_keep_declared_order() -> None:
    """Общий аксессор пары отдаёт спеку и план в том же порядке, что и раньше."""
    documents = PairDocuments(spec_path="docs/spec.md", plan_path="docs/plan.md")
    assert documents.paths() == ("docs/spec.md", "docs/plan.md")


def test_single_document_paths_is_the_document_alone() -> None:
    documents = SingleDocument(kind="document", document_path="docs/charter.md")
    assert documents.paths() == ("docs/charter.md",)


# --- таблицы вида (§1, §2) --------------------------------------------


def test_document_kind_has_own_entry_phase_and_terminal_contour() -> None:
    assert CONTOURS_BY_KIND[PipelineKind.DOCUMENT] == ("doc",)
    assert ENTRY_PHASE[PipelineKind.DOCUMENT] is PipelinePhase.DOC_LOOP
    assert TERMINAL_CONTOUR[PipelineKind.DOCUMENT] == "doc"


def test_pair_kind_keeps_its_own_contours() -> None:
    """Регрессия: вид pair не потерял ни контура, ни входной фазы."""
    assert CONTOURS_BY_KIND[PipelineKind.PAIR] == ("spec", "pair")
    assert ENTRY_PHASE[PipelineKind.PAIR] is PipelinePhase.SPEC_LOOP
    assert TERMINAL_CONTOUR[PipelineKind.PAIR] == "pair"


def test_doc_loop_converged_edge_exists_and_is_document_only() -> None:
    edge = (PipelinePhase.DOC_LOOP, PipelinePhase.EXPORTING)
    assert TransitionReason.DOCUMENT_CONVERGED in ALLOWED_TRANSITIONS[edge]
    assert edge in EDGES_BY_KIND[PipelineKind.DOCUMENT]
    assert edge not in EDGES_BY_KIND[PipelineKind.PAIR]


def test_doc_loop_escalation_excludes_architectural_returns() -> None:
    """Причина, которая не может наступить, в наборе — приглашение к ошибке."""
    reasons = ALLOWED_TRANSITIONS[(PipelinePhase.DOC_LOOP, PipelinePhase.ESCALATED)]
    assert TransitionReason.MAX_ARCHITECTURAL_RETURNS not in reasons
    assert TransitionReason.SESSION_DEADLOCK in reasons


def test_failed_edges_are_narrowed_to_own_phases() -> None:
    """Сужение по виду доходит до FAILED: чужая фаза ребра не даёт (§2).

    Общий список всех `(phase, FAILED)` пропускал бы документный манифест с
    историей `SPEC_LOOP → FAILED` — целый класс переходов мимо fail-closed
    проверки.
    """
    document = EDGES_BY_KIND[PipelineKind.DOCUMENT]
    pair = EDGES_BY_KIND[PipelineKind.PAIR]
    assert (PipelinePhase.DOC_LOOP, PipelinePhase.FAILED) in document
    assert (PipelinePhase.SPEC_LOOP, PipelinePhase.FAILED) not in document
    assert (PipelinePhase.PAIR_LOOP, PipelinePhase.FAILED) not in document
    assert (PipelinePhase.DOC_LOOP, PipelinePhase.FAILED) not in pair
    # Общие нетерминальные фазы остаются у обоих видов.
    assert (PipelinePhase.IDLE, PipelinePhase.FAILED) in document
    assert (PipelinePhase.IDLE, PipelinePhase.FAILED) in pair


# --- инварианты вида в состоянии (§2, §4.2) ---------------------------


def test_state_rejects_transition_of_foreign_kind() -> None:
    """Ребро, допустимое общей таблицей, но чужое виду, отвергается (§2).

    Проверка членства в `EDGES_BY_KIND` доказывала бы только объявление
    таблицы. Здесь проверяется её ПРИМЕНЕНИЕ: документный манифест с ребром
    pair-механики не должен читаться вовсе — иначе таблица остаётся мёртвой
    при зелёном тесте.
    """
    with pytest.raises(ValidationError, match="чужое виду"):
        PipelineState.model_validate(
            _document_payload(
                phase="PAIR_LOOP",
                transitions=[
                    {
                        "from": "SPEC_LOOP",
                        "to": "PAIR_LOOP",
                        "reason": "spec_converged",
                        "evidence": [],
                        "at": "2026-08-31T00:00:00+00:00",
                    }
                ],
            )
        )


@pytest.mark.parametrize(
    "payload_of_kind, foreign_phase",
    [
        (_document_payload, "SPEC_LOOP"),
        (_document_payload, "PAIR_LOOP"),
        (_pair_payload, "DOC_LOOP"),
    ],
)
def test_state_rejects_failed_edge_from_foreign_phase(
    payload_of_kind: Any, foreign_phase: str
) -> None:
    """Сужение по виду доходит и до FAILED-рёбер (§2)."""
    with pytest.raises(ValidationError, match="чужое виду"):
        PipelineState.model_validate(
            payload_of_kind(
                phase="FAILED",
                transitions=[
                    {
                        "from": foreign_phase,
                        "to": "FAILED",
                        "reason": "invariant_violation",
                        "evidence": [],
                        "at": "2026-08-31T00:00:00+00:00",
                    }
                ],
            )
        )


def test_pair_state_accepts_its_own_edges() -> None:
    """Регрессия: у пары те же рёбра принимаются как раньше."""
    state = PipelineState.model_validate(
        _pair_payload(
            phase="PAIR_LOOP",
            transitions=[
                {
                    "from": "IDLE",
                    "to": "SPEC_LOOP",
                    "reason": "started",
                    "evidence": [],
                    "at": "2026-08-31T00:00:00+00:00",
                },
                {
                    "from": "SPEC_LOOP",
                    "to": "PAIR_LOOP",
                    "reason": "spec_converged",
                    "evidence": [],
                    "at": "2026-08-31T00:00:01+00:00",
                },
            ],
        )
    )
    assert state.transitions[-1].to is PipelinePhase.PAIR_LOOP


def test_document_state_accepts_its_own_converged_edge() -> None:
    state = PipelineState.model_validate(
        _document_payload(
            phase="EXPORTING",
            transitions=[
                {
                    "from": "IDLE",
                    "to": "DOC_LOOP",
                    "reason": "started",
                    "evidence": [],
                    "at": "2026-08-31T00:00:00+00:00",
                },
                {
                    "from": "DOC_LOOP",
                    "to": "EXPORTING",
                    "reason": "document_converged",
                    "evidence": [],
                    "at": "2026-08-31T00:00:01+00:00",
                },
            ],
        )
    )
    assert state.kind is PipelineKind.DOCUMENT
    assert state.transitions[-1].reason is TransitionReason.DOCUMENT_CONVERGED


def test_state_rejects_sessions_of_foreign_kind() -> None:
    """Непустая коллекция чужого вида — invariant_violation, не «лишние данные»."""
    with pytest.raises(ValidationError, match="чужого вида"):
        PipelineState.model_validate(
            _document_payload(
                pair_sessions=[
                    {
                        "revision": 1,
                        "session_id": "pair-r1",
                        "path": "sessions/pair-r1",
                        "entry_hashes": {},
                        "outcome": None,
                        "superseded_by": None,
                    }
                ]
            )
        )


def test_pair_state_rejects_doc_sessions() -> None:
    """Симметрия: коллекция контура `doc` чужая виду pair."""
    with pytest.raises(ValidationError, match="чужого вида"):
        PipelineState.model_validate(
            _pair_payload(
                doc_sessions=[
                    {
                        "revision": 1,
                        "session_id": "doc-r1",
                        "path": "sessions/doc-r1",
                        "entry_hashes": {},
                        "outcome": None,
                        "superseded_by": None,
                    }
                ]
            )
        )


def test_document_payload_under_v1_tag_is_rejected_as_lying() -> None:
    """Документный манифест под тегом v1 отвергается — но по форме, не по виду.

    Порядок guard'ов здесь не случаен и стоит проверки: документная форма
    невыразима без `kind`, а `kind` под тегом v1 запрещён §4.2 как ложь о
    форме. Поэтому payload-путь до проверки вида не доходит — его закрывает
    нормализация, и это правильный, более ранний отказ.
    """
    with pytest.raises(ValidationError, match="v1"):
        PipelineState.model_validate(_document_payload(SCHEMA_PIPELINE_V1))


def test_document_state_requires_v2_schema() -> None:
    """Вид document несовместим с тегом v1 и на конструкторском пути.

    Нормализация тут не срабатывает (`documents` приходит моделью, а не
    dict'ом), поэтому проверку делает инвариант вида — и без неё
    программно собранный документный манифест записался бы под тегом,
    строгий читатель которого не знает ни `DOC_LOOP`, ни
    `document_converged`.
    """
    with pytest.raises(ValidationError, match="disputatio/pipeline/v2"):
        PipelineState(
            schema=SCHEMA_PIPELINE_V1,
            pipeline_id="pipe-1",
            created_at="2026-08-31T12:00:00+00:00",
            phase=PipelinePhase.IDLE,
            task=FileRef(path="task.md", sha256=_SHA),
            config=FileRef(path="config.toml", sha256=_SHA),
            checklists=FileRef(path="checklists.toml", sha256=_SHA),
            documents=SingleDocument(kind="document", document_path="docs/c.md"),
            budget_used=BudgetUsed(),
            anchor_id="pipe-1",
        )


# --- миграция пары на v2 целиком (§4.2) -------------------------------

#: Всё, что редакция v0.2 добавляет в сериализацию pair-манифеста. Список
#: закрытый: тест ниже требует, чтобы других отличий не было ни одного.
_V2_ADDITIONS: dict[tuple[str, ...], Any] = {
    ("schema",): SCHEMA_PIPELINE_V2,
    ("documents", "kind"): "pair",
    ("doc_sessions",): [],
}


def test_v1_fixture_saves_as_v2_and_keeps_everything_else() -> None:
    """Пара переходит на v2 ровно тремя полями и ничем больше.

    `doc_sessions` попадает в дамп наравне с двумя существующими
    коллекциями — `default_factory=list` сериализуется как обычное поле.
    Забыть его в списке ожидаемых отличий значило бы написать тест, который
    невозможно выполнить.
    """
    before = json.loads(_V1_FIXTURE.read_text(encoding="utf-8"))
    after = PipelineState.model_validate(before).model_dump(mode="json", by_alias=True)

    assert after["schema"] == _V2_ADDITIONS[("schema",)]
    assert after["documents"]["kind"] == _V2_ADDITIONS[("documents", "kind")]
    assert after["doc_sessions"] == _V2_ADDITIONS[("doc_sessions",)]

    stripped = {k: v for k, v in after.items() if k != "doc_sessions"}
    stripped["schema"] = before["schema"]
    stripped["documents"] = {k: v for k, v in after["documents"].items() if k != "kind"}
    assert stripped == before
