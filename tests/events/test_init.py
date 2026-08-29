"""Тесты публичного API пакета `disputatio.events`: TASK-008, [REQ-011], [DESIGN §5].

`__init__.py` не добавляет поведения — он только re-export'ит имена модулей
workstream'а. Поэтому проверяется четыре вещи: имена доступны с уровня пакета,
`__all__` перечисляет ровно их (пин против утечки внутренних символов модулей),
каждое имя — тот же объект, что и в своём модуле (re-export, а не вторая
реализация в `__init__.py`), и `FileStateStore`/`JsonlEventSink` удовлетворяют
портам contracts именно через публичный путь импорта.

Имена читаются через `importlib`/`getattr`, а не статическим `from ... import`
на уровне модуля: на red-чекпоинте `__init__.py` ещё пуст, и статический импорт
сломал бы collection — гейт принимает red только при падении assertion'ом.
"""

import importlib
from pathlib import Path

from disputatio.contracts import ports
from disputatio.events import (
    atomic,
    bootstrap,
    event_sink,
    export,
    integrity_anchor,
    pipeline_events,
    pipeline_store,
    rounds,
    state_store,
)

PACKAGE_NAME = "disputatio.events"

WAVE1_PUBLIC_NAMES = (
    "atomic_write",
    "bootstrap_session",
    "write_config_snapshot",
    "FileStateStore",
    "JsonlEventSink",
    "write_round_artifact",
    "finalize_round",
    "RoundImmutableError",
    "write_result",
)

PIPELINE_PUBLIC_NAMES = (
    "FilePipelineStateStore",
    "PipelineEvent",
    "PipelineEventSink",
    "PipelineEventType",
    "read_pipeline_events",
    "IntegrityAnchor",
    "AnchorRecord",
)
"""Имена, добавленные слоем пайплайна SPEC-002 (§4.1, §4.2, P8, P9).

Держатся отдельным кортежем, а не дописаны к волне 1: пин девяти имён —
факт приёмки волны, и растворять его в общем списке значило бы потерять,
что именно было принято. Утечку внутренних символов проверка ловит
по-прежнему — сравнивается объединение, а не «хотя бы эти».
`pipeline_paths` наружу не идёт: раскладка каталога — такая же внутренняя
деталь, как `paths` (см. `__init__.py`).
"""

PUBLIC_NAMES = WAVE1_PUBLIC_NAMES + PIPELINE_PUBLIC_NAMES

ORIGINS = {
    "atomic_write": atomic.atomic_write,
    "bootstrap_session": bootstrap.bootstrap_session,
    "write_config_snapshot": bootstrap.write_config_snapshot,
    "FileStateStore": state_store.FileStateStore,
    "JsonlEventSink": event_sink.JsonlEventSink,
    "write_round_artifact": rounds.write_round_artifact,
    "finalize_round": rounds.finalize_round,
    "RoundImmutableError": rounds.RoundImmutableError,
    "write_result": export.write_result,
    "FilePipelineStateStore": pipeline_store.FilePipelineStateStore,
    "PipelineEvent": pipeline_events.PipelineEvent,
    "PipelineEventSink": pipeline_events.PipelineEventSink,
    "PipelineEventType": pipeline_events.PipelineEventType,
    "read_pipeline_events": pipeline_events.read_pipeline_events,
    "IntegrityAnchor": integrity_anchor.IntegrityAnchor,
    "AnchorRecord": integrity_anchor.AnchorRecord,
}

MISSING = object()


def test_public_names_importable_from_package() -> None:
    package = importlib.import_module(PACKAGE_NAME)

    missing = [name for name in PUBLIC_NAMES if not hasattr(package, name)]

    assert missing == [], f"{PACKAGE_NAME} не экспортирует: {missing}"


def test_all_lists_exactly_the_public_names() -> None:
    package = importlib.import_module(PACKAGE_NAME)

    exported = getattr(package, "__all__", MISSING)

    assert exported is not MISSING, f"{PACKAGE_NAME}.__all__ не определён"
    assert isinstance(exported, list | tuple)
    assert sorted(exported) == sorted(PUBLIC_NAMES)


def test_wave1_public_names_still_exported() -> None:
    """Девять имён приёмки волны 1 остаются на месте — слой пайплайна аддитивен."""
    package = importlib.import_module(PACKAGE_NAME)

    exported = set(package.__all__)

    assert len(WAVE1_PUBLIC_NAMES) == 9
    assert set(WAVE1_PUBLIC_NAMES) <= exported


def test_star_import_exposes_exactly_the_public_names() -> None:
    namespace: dict[str, object] = {}

    exec(f"from {PACKAGE_NAME} import *", namespace)  # noqa: S102

    leaked = {name for name in namespace if not name.startswith("__")}
    assert leaked == set(PUBLIC_NAMES)


def test_reexported_objects_are_the_module_originals() -> None:
    package = importlib.import_module(PACKAGE_NAME)

    rebound = [
        name
        for name, origin in ORIGINS.items()
        if getattr(package, name, MISSING) is not origin
    ]

    assert rebound == [], f"не re-export, а другой объект: {rebound}"


def test_store_and_sink_satisfy_contracts_ports_via_package(session_root: Path) -> None:
    try:
        from disputatio.events import FileStateStore, JsonlEventSink
    except ImportError as exc:  # red-фаза: `__init__.py` ещё не re-export'ит
        raise AssertionError("disputatio/events/__init__.py ещё не собран") from exc

    assert isinstance(FileStateStore(session_root), ports.StateStore)
    assert isinstance(JsonlEventSink(session_root), ports.EventSink)
