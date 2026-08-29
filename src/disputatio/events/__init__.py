"""disputatio.events: файловое хранилище артефактов сессии `.disputatio/`.

Публичное API workstream'а: единственная точка импорта для оркестратора
([DESIGN §5]). Модуль ничего не реализует — только re-export; поведение живёт
в `atomic`/`bootstrap`/`state_store`/`event_sink`/`rounds`/`export`, а
`paths` остаётся внутренней деталью раскладки `.disputatio/` и наружу не
экспортируется.

`FileStateStore` и `JsonlEventSink` — реализации портов
`disputatio.contracts.ports.StateStore`/`EventSink`; сериализация артефактов
идёт только через публичные методы pydantic-моделей contracts ([REQ-011]).

Слой пайплайна (SPEC-002 §4.1, §4.2, P8, P9) добавляет
`FilePipelineStateStore` (порт `PipelineStateStore`), журнал
`PipelineEventSink` с парным читателем `read_pipeline_events` и
`IntegrityAnchor`. `pipeline_paths` — такая же внутренняя деталь раскладки,
как `paths`, и наружу не экспортируется.
"""

from disputatio.events.atomic import atomic_write
from disputatio.events.bootstrap import bootstrap_session, write_config_snapshot
from disputatio.events.event_sink import JsonlEventSink
from disputatio.events.export import write_result
from disputatio.events.integrity_anchor import AnchorRecord, IntegrityAnchor
from disputatio.events.pipeline_events import (
    PipelineEvent,
    PipelineEventSink,
    PipelineEventType,
    read_pipeline_events,
)
from disputatio.events.pipeline_store import FilePipelineStateStore
from disputatio.events.rounds import (
    RoundImmutableError,
    finalize_round,
    write_round_artifact,
)
from disputatio.events.state_store import FileStateStore

__all__ = [
    "AnchorRecord",
    "FilePipelineStateStore",
    "FileStateStore",
    "IntegrityAnchor",
    "JsonlEventSink",
    "PipelineEvent",
    "PipelineEventSink",
    "PipelineEventType",
    "RoundImmutableError",
    "atomic_write",
    "bootstrap_session",
    "finalize_round",
    "read_pipeline_events",
    "write_config_snapshot",
    "write_result",
    "write_round_artifact",
]
