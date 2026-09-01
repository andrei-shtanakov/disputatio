"""Тесты семейства схемы `disputatio/pipeline/v1`: TASK-003, SPEC-002 §2, §4.2.

Импорты `disputatio.contracts.pipeline` выполняются на уровне модуля:
red-чекпоинт этой задачи — файл `pipeline.py` ещё не существует, и все тесты
файла падают ImportError на collection, что pytest репортит как ошибку
сбора, а не как проходящий red — это и есть ожидаемый red для TDD-цикла
задачи (аналогично паттерну `test_init.py`, где красный селектор через
`hasattr` не нужен, т.к. вся задача — новый модуль, а не расширение
существующего).
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from disputatio.contracts.pipeline import (
    ALLOWED_TRANSITIONS,
    SCHEMA_PIPELINE_V1,
    SCHEMA_PIPELINE_V2,
    AppendOnlyEntry,
    EvidenceLink,
    FileRef,
    IntegritySnapshot,
    NextAction,
    OperatorDecision,
    PairDocuments,
    PipelinePhase,
    PipelineState,
    SessionOutcome,
    SessionRecord,
    Transition,
    TransitionReason,
)

_SHA = "a" * 64


def _file_ref(path: str = "spec/task.md") -> dict[str, str]:
    return {"path": path, "sha256": _SHA}


def _session_record(
    *,
    revision: int = 1,
    session_id: str = "sess-1",
    path: str = "sessions/spec-r1",
    entry_hashes: dict[str, str] | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    return {
        "revision": revision,
        "session_id": session_id,
        "path": path,
        "entry_hashes": entry_hashes or {"spec/design.md": _SHA},
        "outcome": outcome,
        "superseded_by": None,
    }


def _transition(
    *, from_: str, to: str, reason: str, at: str = "2026-08-28T12:00:00+00:00"
) -> dict[str, Any]:
    return {"from": from_, "to": to, "reason": reason, "evidence": [], "at": at}


def _pipeline_state_payload() -> dict[str, Any]:
    return {
        "schema": SCHEMA_PIPELINE_V1,
        "pipeline_id": "pipe-1",
        "created_at": "2026-08-28T12:00:00+00:00",
        "phase": "SPEC_LOOP",
        "task": _file_ref("task.md"),
        "config": _file_ref("config.toml"),
        "checklists": _file_ref("checklists.toml"),
        "documents": {"spec_path": "spec/design.md", "plan_path": "spec/tasks.md"},
        "spec_sessions": [_session_record()],
        "pair_sessions": [],
        "transitions": [
            _transition(from_="IDLE", to="SPEC_LOOP", reason="started"),
        ],
        "budget_used": {"tokens": 100, "wall_seconds": 1.5, "cost_usd_est": 0.01},
        "operator_decisions": [],
        "anchor_id": "pipe-1",
        "next_action": None,
    }


# --- round-trip -------------------------------------------------------


def test_round_trip_serialization() -> None:
    """model_validate_json(model_dump_json(by_alias=True)) даёт равную модель."""
    original = PipelineState.model_validate(_pipeline_state_payload())
    restored = PipelineState.model_validate_json(
        original.model_dump_json(by_alias=True)
    )
    assert restored == original


def test_serialization_contains_schema_and_from_keys() -> None:
    """Сериализация несёт `"schema"` и алиас `"from"` у переходов.

    Тег на выходе — v2, а не тот v1, с которым payload прочитан: редакция
    v0.2 пишет v2 для ОБОИХ видов, а v1 только читает (§4.2). Подробности
    миграции — `test_pipeline_kind.py`.
    """
    state = PipelineState.model_validate(_pipeline_state_payload())
    dumped = state.model_dump(by_alias=True)
    assert dumped["schema"] == SCHEMA_PIPELINE_V2
    assert dumped["transitions"][0]["from"] == "IDLE"
    assert "from_" not in dumped["transitions"][0]


def test_model_validate_without_schema_key_rejected() -> None:
    """`pipeline.json` читается через `model_validate` на каждом resume (§8):
    отсутствующий ключ `schema` обязан падать `ValidationError`, а не тихо
    доопределяться значением по умолчанию — иначе повреждённый/усечённый
    манифест без тега схемы читался бы как валидный."""
    payload = _pipeline_state_payload()
    del payload["schema"]
    with pytest.raises(ValidationError):
        PipelineState.model_validate(payload)


def test_model_validate_with_schema_key_accepted() -> None:
    """С явным корректным тегом `model_validate` проходит и отдаёт данные."""
    payload = _pipeline_state_payload()
    state = PipelineState.model_validate(payload)
    assert state.schema_ == SCHEMA_PIPELINE_V2
    assert state.pipeline_id == "pipe-1"
    assert state.phase == PipelinePhase.SPEC_LOOP


def test_constructor_still_defaults_schema() -> None:
    """Конструктор (не `model_validate`) по-прежнему подставляет схему сам —
    удобство для программного кода пайплайна сохранено (§4.2 PipelineState).

    `documents` здесь несёт дискриминатор явно: подстановка тега — про
    `schema`, а не про форму документов, и «манифест без тега» после неё
    объявлен как v2, где `kind` обязателен. Совместимость v1 чинит
    нормализация по тегу, и подменять её дефолтом внутри union'а нельзя
    (§4.2) — иначе payload без дискриминатора проходил бы под любым тегом.
    """
    payload = _pipeline_state_payload()
    del payload["schema"]
    payload["documents"] = {**payload["documents"], "kind": "pair"}
    state = PipelineState(**payload)
    assert state.schema_ == SCHEMA_PIPELINE_V2


# --- transition table ---------------------------------------------------


_VALID_EDGES: tuple[tuple[PipelinePhase, PipelinePhase, TransitionReason], ...] = (
    (PipelinePhase.IDLE, PipelinePhase.SPEC_LOOP, TransitionReason.STARTED),
    (
        PipelinePhase.SPEC_LOOP,
        PipelinePhase.PAIR_LOOP,
        TransitionReason.SPEC_CONVERGED,
    ),
    (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.EXPORTING,
        TransitionReason.PAIR_CONVERGED,
    ),
    (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.SPEC_LOOP,
        TransitionReason.ARCHITECTURAL_DEFECT,
    ),
    (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.SPEC_LOOP,
        TransitionReason.EXTERNAL_SPEC_ADOPT,
    ),
    (
        PipelinePhase.SPEC_LOOP,
        PipelinePhase.ESCALATED,
        TransitionReason.SESSION_DEADLOCK,
    ),
    (
        PipelinePhase.SPEC_LOOP,
        PipelinePhase.ESCALATED,
        TransitionReason.SESSION_BUDGET_HIT,
    ),
    (
        PipelinePhase.SPEC_LOOP,
        PipelinePhase.ESCALATED,
        TransitionReason.MAX_ARCHITECTURAL_RETURNS,
    ),
    (
        PipelinePhase.SPEC_LOOP,
        PipelinePhase.ESCALATED,
        TransitionReason.PIPELINE_BUDGET_HIT,
    ),
    (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.ESCALATED,
        TransitionReason.SESSION_DEADLOCK,
    ),
    (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.ESCALATED,
        TransitionReason.SESSION_BUDGET_HIT,
    ),
    (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.ESCALATED,
        TransitionReason.MAX_ARCHITECTURAL_RETURNS,
    ),
    (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.ESCALATED,
        TransitionReason.PIPELINE_BUDGET_HIT,
    ),
    (
        PipelinePhase.ESCALATED,
        PipelinePhase.EXPORTING,
        TransitionReason.EXPORT_PARTIAL,
    ),
    (PipelinePhase.EXPORTING, PipelinePhase.DONE, TransitionReason.EXPORTED),
)

_NON_TERMINAL = (
    PipelinePhase.IDLE,
    PipelinePhase.SPEC_LOOP,
    PipelinePhase.PAIR_LOOP,
    PipelinePhase.EXPORTING,
    PipelinePhase.ESCALATED,
)

_FAILURE_REASONS = (
    TransitionReason.SESSION_FAILED,
    TransitionReason.INVARIANT_VIOLATION,
)


@pytest.mark.parametrize("from_, to, reason", _VALID_EDGES)
def test_each_table_edge_accepted(
    from_: PipelinePhase, to: PipelinePhase, reason: TransitionReason
) -> None:
    """Каждая пара §2 со своей причиной проходит валидацию."""
    Transition.model_validate(
        _transition(from_=from_.value, to=to.value, reason=reason.value)
    )


@pytest.mark.parametrize("phase", _NON_TERMINAL)
@pytest.mark.parametrize("reason", _FAILURE_REASONS)
def test_failure_edges_accepted(phase: PipelinePhase, reason: TransitionReason) -> None:
    """Любая нетерминальная фаза → FAILED с одной из двух причин отказа."""
    Transition.model_validate(
        _transition(
            from_=phase.value, to=PipelinePhase.FAILED.value, reason=reason.value
        )
    )


def test_transition_out_of_table_rejected() -> None:
    """Несуществующее ребро таблицы отклоняется ValidationError."""
    with pytest.raises(ValidationError):
        Transition.model_validate(
            _transition(from_="SPEC_LOOP", to="DONE", reason="exported")
        )


def test_transition_reason_bound_to_edge() -> None:
    """Существующее ребро с чужой причиной отвергается — не только пара from/to."""
    with pytest.raises(ValidationError):
        Transition.model_validate(
            _transition(from_="IDLE", to="SPEC_LOOP", reason="exported")
        )
    with pytest.raises(ValidationError):
        Transition.model_validate(
            _transition(from_="PAIR_LOOP", to="SPEC_LOOP", reason="spec_converged")
        )


def test_done_has_no_outgoing() -> None:
    """`DONE` не встречается в ключах-источниках закрытой таблицы."""
    sources = {edge[0] for edge in ALLOWED_TRANSITIONS}
    assert PipelinePhase.DONE not in sources


def test_failed_has_no_outgoing() -> None:
    """`FAILED` терминален так же, как `DONE` — переходов из него нет."""
    sources = {edge[0] for edge in ALLOWED_TRANSITIONS}
    assert PipelinePhase.FAILED not in sources


# --- outcome / SessionRecord ---------------------------------------------


def test_outcome_immutable_by_convention() -> None:
    """SessionOutcome — закрытый enum; поле `outcome` — Optional и frozen."""
    assert set(SessionOutcome) == {
        SessionOutcome.CONVERGED,
        SessionOutcome.ESCALATED,
        SessionOutcome.FAILED,
        SessionOutcome.ARCHITECTURAL_DEFECT,
        SessionOutcome.ABANDONED,
    }
    record = SessionRecord.model_validate(_session_record())
    assert record.outcome is None
    with pytest.raises(ValidationError):
        # setattr, а не прямое присваивание: последнее статически типизировано
        # как запись в read-only поле frozen-модели, и pyrefly ловит это на
        # уровне типов раньше, чем тест успевает проверить рантайм-поведение
        # (DESIGN-015 — новые тест-файлы строгий pyrefly не послабляют).
        setattr(record, "outcome", SessionOutcome.CONVERGED)  # noqa: B010


# --- entry_hashes ----------------------------------------------------------


def test_entry_hashes_absent_marker() -> None:
    """`entry_hashes` принимает sha256 либо явный маркер `absent`."""
    record = SessionRecord.model_validate(
        _session_record(
            entry_hashes={"spec/design.md": _SHA, "spec/tasks.md": "absent"}
        )
    )
    assert record.entry_hashes["spec/tasks.md"] == "absent"


def test_entry_hashes_rejects_garbage_value() -> None:
    """Значение, не являющееся ни sha256, ни `absent`, отклоняется."""
    with pytest.raises(ValidationError):
        SessionRecord.model_validate(
            _session_record(entry_hashes={"spec/design.md": "not-a-hash"})
        )


# --- relative paths only ----------------------------------------------------


def test_relative_paths_only_file_ref() -> None:
    """Абсолютный путь в `FileRef.path` — ValidationError."""
    with pytest.raises(ValidationError):
        FileRef.model_validate({"path": "/etc/passwd", "sha256": _SHA})


def test_relative_paths_only_session_record() -> None:
    """Абсолютный путь в `SessionRecord.path` — ValidationError."""
    with pytest.raises(ValidationError):
        SessionRecord.model_validate(_session_record(path="/tmp/sessions/spec-r1"))


def test_relative_paths_only_documents() -> None:
    """Абсолютный путь в `PairDocuments` — ValidationError."""
    with pytest.raises(ValidationError):
        PairDocuments.model_validate(
            {"spec_path": "/abs/spec.md", "plan_path": "spec/tasks.md"}
        )


@pytest.mark.parametrize(
    "bad",
    [
        "../outside/spec.md",  # выход за корень в лоб
        "spec/../../outside.md",  # выход после нормализации
        "spec/../design.md",  # внутри, но неоднозначно: `spec` может быть symlink
        "..",
        ".",
        "",
        "   ",
        "spec//design.md",  # пустой сегмент — не канонический вид
        "spec/",  # каталог, а не файл
        "./spec/design.md",
        "C:/repo/spec.md",  # абсолютный в Windows-форме
        "C:spec.md",  # относительный диску, а не корню репозитория
        "\\\\server\\share\\spec.md",
        "spec\\design.md",  # разделитель другой ОС — машинно-зависимый вид
    ],
)
def test_paths_leaving_the_root_or_non_canonical_rejected(bad: str) -> None:
    """Манифест несёт пути ВНУТРЬ корня и в каноническом виде (§4.2).

    `is_absolute()` ловил только первую форму из списка: `spec_path=
    "../outside/spec.md"` проходил как относительный, а runner склеивал его
    с `workspace_root`, читал и хешировал файл за пределами репозитория.
    """
    with pytest.raises(ValidationError):
        PairDocuments.model_validate({"spec_path": bad, "plan_path": "spec/tasks.md"})


@pytest.mark.parametrize(
    "good", ["spec.md", "spec/design.md", "docs/plans/2026-08-30-план.md", "a/b/c/d.md"]
)
def test_ordinary_relative_paths_accepted(good: str) -> None:
    """Обычный путь внутрь репозитория проверку проходит — она не глухая."""
    assert (
        PairDocuments.model_validate(
            {"spec_path": good, "plan_path": "spec/tasks.md"}
        ).spec_path
        == good
    )


def test_escaping_path_rejected_everywhere_it_appears() -> None:
    """Правило действует на всех полях-путях манифеста, не только на паре."""
    with pytest.raises(ValidationError):
        FileRef.model_validate({"path": "../outside/task.md", "sha256": _SHA})
    with pytest.raises(ValidationError):
        SessionRecord.model_validate(_session_record(path="../outside/sessions/1"))


def test_relative_paths_only_pipeline_state() -> None:
    """Абсолютный путь где-либо в манифесте отклоняется на уровне поля."""
    payload = _pipeline_state_payload()
    payload["task"] = _file_ref("/abs/task.md")
    with pytest.raises(ValidationError):
        PipelineState.model_validate(payload)


# --- extra models: smoke round-trips ---------------------------------------


def test_evidence_link_round_trip() -> None:
    link = EvidenceLink.model_validate(
        {"session_id": "sess-1", "round": 2, "finding_id": "F-1"}
    )
    assert link.round == 2


def test_operator_decision_round_trip() -> None:
    decision = OperatorDecision.model_validate(
        {
            "operation_id": "op-1",
            "kind": "discard_round",
            "at": datetime(2026, 8, 28, tzinfo=UTC),
            "worktree_diff_sha256": _SHA,
        }
    )
    assert decision.kind == "discard_round"


def test_operator_decision_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        OperatorDecision.model_validate(
            {
                "operation_id": "op-1",
                "kind": "bogus",
                "at": datetime(2026, 8, 28, tzinfo=UTC),
                "worktree_diff_sha256": _SHA,
            }
        )


def test_next_action_round_trip() -> None:
    action = NextAction.model_validate(
        {
            "operation_id": "op-2",
            "kind": "create_session",
            "args": {"revision": 1},
            "predecessor_operation_id": None,
        }
    )
    assert action.kind == "create_session"


def test_next_action_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        NextAction.model_validate(
            {
                "operation_id": "op-2",
                "kind": "bogus",
                "args": {},
                "predecessor_operation_id": None,
            }
        )


def test_integrity_snapshot_round_trip() -> None:
    snapshot = IntegritySnapshot.model_validate(
        {
            "session_id": "sess-1",
            "round": 1,
            "operation_id": "op-3",
            "immutable": {"session.json": _SHA},
            "append_only": {
                "events.jsonl": {"prefix_bytes": 128, "prefix_sha256": _SHA}
            },
        }
    )
    assert snapshot.append_only["events.jsonl"] == AppendOnlyEntry(
        prefix_bytes=128, prefix_sha256=_SHA
    )


def test_pipeline_state_not_in_manifest_forbids_snapshot_field() -> None:
    """IntegritySnapshot не входит в PipelineState (§4.2: живёт только в анкере)."""
    payload = _pipeline_state_payload()
    payload["integrity_snapshot"] = {
        "session_id": "sess-1",
        "round": 1,
        "operation_id": "op-1",
        "immutable": {},
        "append_only": {},
    }
    with pytest.raises(ValidationError):
        PipelineState.model_validate(payload)
