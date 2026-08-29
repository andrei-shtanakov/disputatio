"""Хранилище пайплайна, пути, журнал событий и анкер целостности (задача 6).

Трассируемость: SPEC-002 §4.1 (раскладка, словарь событий), §4.2 (манифест,
append-only как prefix-equality), P8 (гарантии журнала), P9 (анкер).

Импорты модулей задачи 6 выполняются внутри тестов: на red-чекпоинте их ещё
нет, и импорт на уровне модуля сломал бы collection всего каталога.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from disputatio.contracts.pipeline import (
    AppendOnlyEntry,
    IntegritySnapshot,
    OperatorDecision,
    PipelinePhase,
    PipelineState,
    SessionOutcome,
    SessionRecord,
    Transition,
    TransitionReason,
)
from disputatio.events import atomic

_SHA = "a" * 64
_SLUG = "pipe-20260828-01"


def make_pipeline_state(**overrides: Any) -> PipelineState:
    """Минимальный валидный `PipelineState` с `pipeline_id == _SLUG`."""
    payload: dict[str, Any] = {
        "schema": "disputatio/pipeline/v1",
        "pipeline_id": _SLUG,
        "created_at": "2026-08-28T12:00:00+00:00",
        "phase": "IDLE",
        "task": {"path": "task.md", "sha256": _SHA},
        "config": {"path": "config.toml", "sha256": _SHA},
        "checklists": {"path": "checklists.toml", "sha256": _SHA},
        "documents": {"spec_path": "spec/design.md", "plan_path": "spec/tasks.md"},
        "spec_sessions": [],
        "pair_sessions": [],
        "transitions": [],
        "budget_used": {"tokens": 0, "wall_seconds": 0.0, "cost_usd_est": 0.0},
        "operator_decisions": [],
        "anchor_id": _SLUG,
        "next_action": None,
    }
    payload.update(overrides)
    return PipelineState.model_validate(payload)


def make_session_record(revision: int = 1, **overrides: Any) -> SessionRecord:
    """Запись о ревизии сессии — груз append-only списков манифеста."""
    fields: dict[str, Any] = {
        "revision": revision,
        "session_id": f"spec-r{revision}",
        "path": f"sessions/spec-r{revision}",
        "entry_hashes": {"spec/design.md": _SHA, "spec/tasks.md": "absent"},
        "outcome": None,
        "superseded_by": None,
    }
    fields.update(overrides)
    return SessionRecord.model_validate(fields)


def make_transition(reason: TransitionReason = TransitionReason.STARTED) -> Transition:
    """Переход `IDLE → SPEC_LOOP` — груз append-only списка `transitions`."""
    return Transition(
        from_=PipelinePhase.IDLE,
        to=PipelinePhase.SPEC_LOOP,
        reason=reason,
        at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
    )


def make_decision(operation_id: str = "op-1") -> OperatorDecision:
    """Решение оператора — груз append-only списка `operator_decisions`."""
    return OperatorDecision(
        operation_id=operation_id,
        kind="discard_round",
        at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        worktree_diff_sha256=_SHA,
    )


@pytest.fixture
def store(session_root: Path) -> Any:
    """`FilePipelineStateStore` над готовым каталогом `pipelines/<slug>/`."""
    from disputatio.events.pipeline_paths import pipeline_dir
    from disputatio.events.pipeline_store import FilePipelineStateStore

    pipeline_dir(session_root, _SLUG).mkdir(parents=True)
    return FilePipelineStateStore(session_root)


# --- пути и грамматика слага (§4.1) ------------------------------------


def test_pipeline_paths_follow_layout(session_root: Path) -> None:
    """Все производные пути строятся от `.disputatio/pipelines/<slug>` (§4.1)."""
    from disputatio.events import pipeline_paths as pp

    base = session_root / ".disputatio" / "pipelines" / _SLUG
    assert pp.pipeline_dir(session_root, _SLUG) == base
    assert pp.manifest_path(session_root, _SLUG) == base / "pipeline.json"
    assert pp.events_path(session_root, _SLUG) == base / "events.jsonl"
    assert pp.sessions_dir(session_root, _SLUG) == base / "sessions"
    assert pp.adoptions_dir(session_root, _SLUG) == base / "adoptions"
    assert pp.result_dir(session_root, _SLUG) == base / "result"


def test_slug_grammar_rejected(session_root: Path) -> None:
    """Слаг вне грамматики `[a-z0-9][a-z0-9._-]{0,63}` отвергается на входе."""
    from disputatio.events.pipeline_paths import pipeline_dir

    for bad in ("", "-leading", "Upper", "with space", "sla/sh", "..", "a" * 65):
        with pytest.raises(ValueError):
            pipeline_dir(session_root, bad)


def test_slug_grammar_accepts_boundary_values(session_root: Path) -> None:
    """Граница грамматики: 64 символа и допустимая пунктуация проходят."""
    from disputatio.events.pipeline_paths import pipeline_dir

    for good in ("a", "0", "a" * 64, "pipe.v1_2-3"):
        assert pipeline_dir(session_root, good).name == good


# --- FilePipelineStateStore: порт, атомарность (§4.2) -------------------


def test_store_satisfies_pipeline_state_store_port(store: Any) -> None:
    """`FilePipelineStateStore` структурно соответствует порту (ADR-004)."""
    from disputatio.contracts.ports import PipelineStateStore

    port: PipelineStateStore = store
    state = make_pipeline_state()
    port.save(state)
    assert port.load(_SLUG) == state


def test_load_missing_manifest_raises_key_error(store: Any) -> None:
    """Отсутствующий манифест — `KeyError(pipeline_id)`, не `FileNotFoundError`."""
    with pytest.raises(KeyError):
        store.load(_SLUG)


def test_load_foreign_pipeline_id_raises_key_error(store: Any) -> None:
    """Чужой `pipeline_id` в файле — `KeyError`, как и отсутствие файла."""
    store.save(make_pipeline_state())
    with pytest.raises(KeyError):
        store.load("pipe-other")


def test_save_failure_at_rename_leaves_manifest_intact(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой на `os.replace` не оставляет частичного манифеста (temp+rename)."""
    store.save(make_pipeline_state())

    monkeypatch.setattr(atomic.os, "replace", Mock(side_effect=OSError("boom")))
    with pytest.raises(OSError):
        store.save(make_pipeline_state(phase="SPEC_LOOP"))

    assert store.load(_SLUG).phase is PipelinePhase.IDLE


def test_save_replaces_manifest_by_rename_without_leftovers(
    store: Any, session_root: Path
) -> None:
    """Успешный `save` подменяет файл целиком и не сорит `.tmp`."""
    from disputatio.events.pipeline_paths import manifest_path, pipeline_dir

    store.save(make_pipeline_state())
    inode_before = manifest_path(session_root, _SLUG).stat().st_ino

    store.save(
        make_pipeline_state(transitions=[make_transition().model_dump(by_alias=True)])
    )

    assert manifest_path(session_root, _SLUG).stat().st_ino != inode_before
    assert list(pipeline_dir(session_root, _SLUG).glob("*.tmp*")) == []


# --- guard истории: append-only = prefix-equality (§4.2) ----------------


def test_transitions_shrink_rejected(store: Any) -> None:
    """Усечение `transitions` — порча истории, `ValueError`."""
    store.save(
        make_pipeline_state(transitions=[make_transition().model_dump(by_alias=True)])
    )

    with pytest.raises(ValueError):
        store.save(make_pipeline_state(transitions=[]))


def test_transition_edited_in_place_rejected(store: Any) -> None:
    """Правка прежнего перехода при ТОЙ ЖЕ длине списка — отказ.

    Это и есть отличие prefix-equality от «длина не уменьшается»: длина
    сохранена, а первый элемент подменён.
    """
    first = make_transition()
    store.save(make_pipeline_state(transitions=[first.model_dump(by_alias=True)]))

    edited = first.model_copy(update={"to": PipelinePhase.FAILED}).model_dump(
        by_alias=True
    )
    edited["reason"] = TransitionReason.SESSION_FAILED.value
    with pytest.raises(ValueError):
        store.save(make_pipeline_state(transitions=[edited]))


def test_transition_append_to_tail_allowed(store: Any) -> None:
    """Новый переход в хвост при неизменённом префиксе — принимается."""
    first = make_transition()
    store.save(make_pipeline_state(transitions=[first.model_dump(by_alias=True)]))

    second = Transition(
        from_=PipelinePhase.SPEC_LOOP,
        to=PipelinePhase.PAIR_LOOP,
        reason=TransitionReason.SPEC_CONVERGED,
        at=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
    )
    store.save(
        make_pipeline_state(
            transitions=[
                first.model_dump(by_alias=True),
                second.model_dump(by_alias=True),
            ]
        )
    )
    assert len(store.load(_SLUG).transitions) == 2


def test_operator_decision_edited_rejected(store: Any) -> None:
    """Правка прежнего `operator_decisions[i]` — отказ (у него нет поздних полей)."""
    first = make_decision()
    store.save(make_pipeline_state(operator_decisions=[first.model_dump()]))

    edited = first.model_copy(update={"kind": "adopt_external"}).model_dump()
    with pytest.raises(ValueError):
        store.save(make_pipeline_state(operator_decisions=[edited]))


def test_session_record_edited_in_place_rejected(store: Any) -> None:
    """Правка полей записи сессии, кроме `outcome`/`superseded_by`, — отказ."""
    first = make_session_record()
    store.save(make_pipeline_state(spec_sessions=[first.model_dump()]))

    edited = first.model_copy(update={"session_id": "spec-r1-подменённый"}).model_dump()
    with pytest.raises(ValueError):
        store.save(make_pipeline_state(spec_sessions=[edited]))


def test_pair_sessions_guarded_too(store: Any) -> None:
    """`pair_sessions` под тем же guard'ом, что и `spec_sessions`."""
    first = make_session_record()
    store.save(make_pipeline_state(pair_sessions=[first.model_dump()]))

    with pytest.raises(ValueError):
        store.save(make_pipeline_state(pair_sessions=[]))


def test_superseded_by_and_first_outcome_allowed(store: Any) -> None:
    """`outcome` с `null` на значение и `superseded_by` — разрешённые правки."""
    first = make_session_record()
    store.save(make_pipeline_state(spec_sessions=[first.model_dump()]))

    filled = first.model_copy(
        update={"outcome": SessionOutcome.CONVERGED, "superseded_by": "spec-r2"}
    )
    store.save(make_pipeline_state(spec_sessions=[filled.model_dump()]))

    loaded = store.load(_SLUG).spec_sessions[0]
    assert loaded.outcome is SessionOutcome.CONVERGED
    assert loaded.superseded_by == "spec-r2"


def test_outcome_rewrite_rejected(store: Any) -> None:
    """`outcome` заполняется однократно: смена значения на значение — отказ (P3)."""
    first = make_session_record(outcome=SessionOutcome.CONVERGED)
    store.save(make_pipeline_state(spec_sessions=[first.model_dump()]))

    rewritten = first.model_copy(update={"outcome": SessionOutcome.FAILED})
    with pytest.raises(ValueError):
        store.save(make_pipeline_state(spec_sessions=[rewritten.model_dump()]))


def test_outcome_cleared_back_to_null_rejected(store: Any) -> None:
    """Обнуление уже записанного `outcome` — та же порча истории."""
    first = make_session_record(outcome=SessionOutcome.CONVERGED)
    store.save(make_pipeline_state(spec_sessions=[first.model_dump()]))

    cleared = first.model_copy(update={"outcome": None})
    with pytest.raises(ValueError):
        store.save(make_pipeline_state(spec_sessions=[cleared.model_dump()]))


def test_superseded_by_set_once(store: Any) -> None:
    """Повторная смена `superseded_by` (`r2` → `r3`) — отказ."""
    first = make_session_record(superseded_by="spec-r2")
    store.save(make_pipeline_state(spec_sessions=[first.model_dump()]))

    rewritten = first.model_copy(update={"superseded_by": "spec-r3"})
    with pytest.raises(ValueError):
        store.save(make_pipeline_state(spec_sessions=[rewritten.model_dump()]))


def test_save_over_foreign_manifest_rejected(store: Any, session_root: Path) -> None:
    """Чужой манифест на пути пайплайна не переписывается: истории не смешиваются."""
    from disputatio.events.pipeline_paths import manifest_path

    foreign = make_pipeline_state(pipeline_id="pipe-чужой")
    manifest_path(session_root, _SLUG).write_text(
        foreign.model_dump_json(by_alias=True), encoding="utf-8"
    )

    with pytest.raises(ValueError):
        store.save(make_pipeline_state())


def test_guard_rejects_before_touching_file(store: Any, session_root: Path) -> None:
    """Отвергнутый `save` не переписывает манифест: на диске прежняя история."""
    from disputatio.events.pipeline_paths import manifest_path

    store.save(
        make_pipeline_state(transitions=[make_transition().model_dump(by_alias=True)])
    )
    before = manifest_path(session_root, _SLUG).read_bytes()

    with pytest.raises(ValueError):
        store.save(make_pipeline_state(transitions=[]))

    assert manifest_path(session_root, _SLUG).read_bytes() == before


def test_first_save_has_no_history_to_guard(store: Any) -> None:
    """Первая запись сравнивать не с чем — непустые коллекции проходят."""
    state = make_pipeline_state(
        transitions=[make_transition().model_dump(by_alias=True)],
        spec_sessions=[make_session_record().model_dump()],
    )
    store.save(state)
    assert store.load(_SLUG) == state


# --- журнал событий пайплайна: словарь, sink, читатель (§4.1, P8) -------


def make_event(event_type: Any = "phase_change", operation_id: str = "op-1") -> Any:
    """Строка журнала пайплайна с обязательным `operation_id` в payload."""
    from disputatio.events.pipeline_events import PipelineEvent

    return PipelineEvent(
        ts=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        pipeline=_SLUG,
        type=event_type,
        payload={"operation_id": operation_id},
    )


def test_event_vocabulary_closed() -> None:
    """Словарь §4.1 закрыт: ровно шесть типов, седьмой — `ValueError`."""
    from disputatio.events.pipeline_events import PipelineEventType

    assert {member.value for member in PipelineEventType} == {
        "phase_change",
        "session_started",
        "session_finished",
        "return_recorded",
        "exported",
        "error",
    }
    for name in PipelineEventType:
        assert make_event(name).type is name

    with pytest.raises(ValueError):
        make_event("state_change")


def test_event_requires_operation_id_in_payload() -> None:
    """Без `operation_id` в payload событие не собирается (P8: дедупликация)."""
    from disputatio.events.pipeline_events import PipelineEvent, PipelineEventType

    with pytest.raises(ValueError):
        PipelineEvent(
            ts=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
            pipeline=_SLUG,
            type=PipelineEventType.EXPORTED,
            payload={},
        )


def test_sink_appends_one_line_per_event(store: Any, session_root: Path) -> None:
    """`emit` дописывает ровно одну JSON-строку, не переписывая прежние."""
    from disputatio.events.pipeline_events import PipelineEventSink
    from disputatio.events.pipeline_paths import events_path

    sink = PipelineEventSink(session_root, _SLUG)
    sink.emit(make_event(operation_id="op-1"))
    sink.emit(make_event(operation_id="op-2"))

    lines = events_path(session_root, _SLUG).read_text("utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["payload"]["operation_id"] == "op-1"


def test_sink_rejects_foreign_event_type(store: Any, session_root: Path) -> None:
    """Тип вне словаря §4.1 не попадает в журнал даже в обход валидации модели."""
    from disputatio.events.pipeline_events import PipelineEvent, PipelineEventSink
    from disputatio.events.pipeline_paths import events_path

    sink = PipelineEventSink(session_root, _SLUG)
    smuggled = PipelineEvent.model_construct(
        ts=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        pipeline=_SLUG,
        type="state_change",
        payload={"operation_id": "op-1"},
    )
    with pytest.raises(ValueError):
        sink.emit(smuggled)

    assert not events_path(session_root, _SLUG).exists()


def test_tail_repair_truncates_partial_line(store: Any, session_root: Path) -> None:
    """Оборванный kill'ом хвост усекается при открытии sink'а (P8)."""
    from disputatio.events.pipeline_events import (
        PipelineEventSink,
        read_pipeline_events,
    )
    from disputatio.events.pipeline_paths import events_path

    path = events_path(session_root, _SLUG)
    good = make_event(operation_id="op-1")
    path.write_text(
        good.model_dump_json() + "\n" + '{"ts": "2026-08-28T12:00:00+00:00", "pip',
        encoding="utf-8",
    )

    sink = PipelineEventSink(session_root, _SLUG)
    assert path.read_text("utf-8").endswith("\n")
    sink.emit(make_event(operation_id="op-2"))

    events = read_pipeline_events(path)
    assert [event.payload["operation_id"] for event in events] == ["op-1", "op-2"]


def test_tail_repair_truncates_invalid_json_line(
    store: Any, session_root: Path
) -> None:
    """Целая, но невалидная JSON-строка в хвосте тоже усекается."""
    from disputatio.events.pipeline_events import PipelineEventSink
    from disputatio.events.pipeline_paths import events_path

    path = events_path(session_root, _SLUG)
    path.write_text(make_event().model_dump_json() + "\nне json\n", encoding="utf-8")

    PipelineEventSink(session_root, _SLUG)

    assert path.read_text("utf-8").splitlines() == [make_event().model_dump_json()]


def test_reader_dedupes_by_operation_id(store: Any, session_root: Path) -> None:
    """Читатель подавляет дубли по `(operation_id, type)` — P8, а не потребитель."""
    from disputatio.events.pipeline_events import (
        PipelineEventSink,
        PipelineEventType,
        read_pipeline_events,
    )
    from disputatio.events.pipeline_paths import events_path

    sink = PipelineEventSink(session_root, _SLUG)
    sink.emit(make_event(PipelineEventType.PHASE_CHANGE, "op-1"))
    sink.emit(make_event(PipelineEventType.PHASE_CHANGE, "op-1"))
    sink.emit(make_event(PipelineEventType.SESSION_STARTED, "op-1"))
    sink.emit(make_event(PipelineEventType.PHASE_CHANGE, "op-2"))

    events = read_pipeline_events(events_path(session_root, _SLUG))

    assert [(event.type.value, event.payload["operation_id"]) for event in events] == [
        ("phase_change", "op-1"),
        ("session_started", "op-1"),
        ("phase_change", "op-2"),
    ]


def test_reader_skips_torn_tail(store: Any, session_root: Path) -> None:
    """Читатель молча пропускает оборванный хвост, не бросая исключение."""
    from disputatio.events.pipeline_events import read_pipeline_events
    from disputatio.events.pipeline_paths import events_path

    path = events_path(session_root, _SLUG)
    path.write_text(make_event().model_dump_json() + '\n{"ts": "2026', encoding="utf-8")

    assert len(read_pipeline_events(path)) == 1


def test_reader_on_missing_log_returns_empty(session_root: Path) -> None:
    """Журнал best-effort: отсутствие файла — пустой список, не ошибка (P8)."""
    from disputatio.events.pipeline_events import read_pipeline_events

    assert read_pipeline_events(session_root / "нет.jsonl") == []


# --- IntegrityAnchor: append-only журнал P9 -----------------------------


def make_snapshot(
    session_id: str = "spec-r1", round_no: int = 1, operation_id: str = "op-1"
) -> IntegritySnapshot:
    """Pre-turn снапшот: хеш неизменяемого файла + префикс журнала (§4.2)."""
    return IntegritySnapshot(
        session_id=session_id,
        round=round_no,
        operation_id=operation_id,
        immutable={"pipeline.json": _SHA},
        append_only={
            "events.jsonl": AppendOnlyEntry(prefix_bytes=128, prefix_sha256=_SHA)
        },
    )


@pytest.fixture
def anchor(tmp_path: Path) -> Any:
    """Созданный пустой анкер над отдельным `anchor_root` вне рабочего дерева."""
    from disputatio.events.integrity_anchor import IntegrityAnchor

    instance = IntegrityAnchor(
        anchor_root=tmp_path / "anchors",
        workspace_root=tmp_path / "repo",
        anchor_id=_SLUG,
    )
    instance.create_empty()
    return instance


def test_anchor_path_includes_workspace_fingerprint(tmp_path: Path) -> None:
    """Два репозитория с одним `anchor_id` не делят журнал: путь несёт отпечаток."""
    from disputatio.events.integrity_anchor import IntegrityAnchor

    anchor_root = tmp_path / "anchors"
    first = IntegrityAnchor(anchor_root, tmp_path / "repo-a", _SLUG)
    second = IntegrityAnchor(anchor_root, tmp_path / "repo-b", _SLUG)

    assert first.path != second.path
    assert first.path.name == f"{_SLUG}.jsonl" == second.path.name

    fingerprint = hashlib.sha256(
        str((tmp_path / "repo-a").resolve()).encode("utf-8")
    ).hexdigest()[:16]
    assert first.path.parent.name == fingerprint

    first.create_empty()
    second.create_empty()
    first.append_pre_turn(make_snapshot(operation_id="op-a"))
    second.append_pre_turn(make_snapshot(operation_id="op-b"))

    first_last = first.last_record()
    second_last = second.last_record()
    assert first_last is not None and first_last.operation_id == "op-a"
    assert second_last is not None and second_last.operation_id == "op-b"


def test_anchor_id_follows_slug_grammar(tmp_path: Path) -> None:
    """`anchor_id` идёт прямо в имя файла — та же грамматика §4.1, тот же отказ."""
    from disputatio.events.integrity_anchor import IntegrityAnchor

    for bad in ("../побег", "Upper", ""):
        with pytest.raises(ValueError):
            IntegrityAnchor(tmp_path / "anchors", tmp_path / "repo", bad)


def test_create_empty_refuses_existing(anchor: Any) -> None:
    """Повторный `create_empty` не усекает журнал, а отказывает (fail-closed)."""
    anchor.append_pre_turn(make_snapshot())

    with pytest.raises(FileExistsError):
        anchor.create_empty()

    assert anchor.last_record() is not None


def test_last_record_on_empty_journal_is_none(anchor: Any) -> None:
    """Пустой существующий журнал — `None`: сверять нечего (§8.1 шаг 0)."""
    assert anchor.last_record() is None


def test_last_record_without_journal_raises(tmp_path: Path) -> None:
    """Отсутствие файла отличимо от пустого журнала — отказ, а не `None` (§8.1)."""
    from disputatio.events.integrity_anchor import IntegrityAnchor

    anchor = IntegrityAnchor(tmp_path / "anchors", tmp_path / "repo", _SLUG)
    with pytest.raises(FileNotFoundError):
        anchor.last_record()


def test_anchor_last_record_without_args(anchor: Any) -> None:
    """`last_record()` без аргументов отдаёт полную identity незавершённого хода.

    Вызывающему не нужно знать `session_id`/`round`: §8.1 требует сверку ДО
    чтения манифеста, откуда их иначе пришлось бы взять.
    """
    anchor.append_pre_turn(make_snapshot("spec-r2", 3, "op-42"))

    record = anchor.last_record()

    assert record is not None
    assert record.kind == "pre_turn"
    assert (record.session_id, record.round, record.operation_id) == (
        "spec-r2",
        3,
        "op-42",
    )
    assert record.immutable == {"pipeline.json": _SHA}
    assert record.append_only["events.jsonl"].prefix_bytes == 128


def test_anchor_append_idempotent(anchor: Any) -> None:
    """Повтор той же записи после краха не удваивает строку (ключ P9)."""
    snapshot = make_snapshot()
    anchor.append_pre_turn(snapshot)
    anchor.append_pre_turn(snapshot)

    assert len(anchor.path.read_text("utf-8").splitlines()) == 1

    anchor.append_completion(snapshot)
    anchor.append_completion(snapshot)

    assert len(anchor.path.read_text("utf-8").splitlines()) == 2


def test_anchor_completion_after_pre_turn(anchor: Any) -> None:
    """`turn_completed` дописывается, `pre_turn` остаётся на месте (append-only)."""
    snapshot = make_snapshot("spec-r1", 2, "op-7")
    anchor.append_pre_turn(snapshot)
    before = anchor.path.read_bytes()

    anchor.append_completion(snapshot)

    after = anchor.path.read_bytes()
    assert after.startswith(before)

    lines = anchor.path.read_text("utf-8").splitlines()
    assert json.loads(lines[0])["kind"] == "pre_turn"

    last = anchor.last_record()
    assert last is not None
    assert last.kind == "turn_completed"
    assert (last.session_id, last.round, last.operation_id) == ("spec-r1", 2, "op-7")
    assert last.immutable == {} and last.append_only == {}


def test_anchor_records_of_different_turns_accumulate(anchor: Any) -> None:
    """Разные ходы дают разные записи: идемпотентность — по ключу, не по виду."""
    anchor.append_pre_turn(make_snapshot(round_no=1, operation_id="op-1"))
    anchor.append_completion(make_snapshot(round_no=1, operation_id="op-1"))
    anchor.append_pre_turn(make_snapshot(round_no=2, operation_id="op-2"))

    assert len(anchor.path.read_text("utf-8").splitlines()) == 3
    last = anchor.last_record()
    assert last is not None and last.kind == "pre_turn" and last.round == 2
