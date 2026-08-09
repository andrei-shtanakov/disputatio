"""disputatio.events: файловое хранилище артефактов сессии `.disputatio/`.

Публичное API workstream'а: единственная точка импорта для оркестратора
([DESIGN §5]). Модуль ничего не реализует — только re-export; поведение живёт
в `atomic`/`bootstrap`/`state_store`/`event_sink`/`rounds`/`export`, а
`paths` остаётся внутренней деталью раскладки `.disputatio/` и наружу не
экспортируется.

`FileStateStore` и `JsonlEventSink` — реализации портов
`disputatio.contracts.ports.StateStore`/`EventSink`; сериализация артефактов
идёт только через публичные методы pydantic-моделей contracts ([REQ-011]).
"""

from disputatio.events.atomic import atomic_write
from disputatio.events.bootstrap import bootstrap_session, write_config_snapshot
from disputatio.events.event_sink import JsonlEventSink
from disputatio.events.export import write_result
from disputatio.events.rounds import (
    RoundImmutableError,
    finalize_round,
    write_round_artifact,
)
from disputatio.events.state_store import FileStateStore

__all__ = [
    "FileStateStore",
    "JsonlEventSink",
    "RoundImmutableError",
    "atomic_write",
    "bootstrap_session",
    "finalize_round",
    "write_config_snapshot",
    "write_result",
    "write_round_artifact",
]
