"""Файловая реализация порта `PipelineStateStore` (SPEC-002 §4.2, §9).

`pipeline.json` — текущее состояние, перезаписываемое целиком через
`atomic_write` (temp-file + rename), ровно как `session.json`. Отличие от
`FileStateStore` одно, и оно существенное: внутри манифеста живут
append-only коллекции, и **проверять их — обязанность хранилища**, а не
схемы (§4.2). Схема видит один документ и не знает, что было записано
раньше; знание о предыдущей записи есть только здесь.

**Append-only = prefix-equality, а не «длина не уменьшается».** Уже
записанные элементы обязаны совпадать поэлементно, новое допускается только
в хвост. Переписывание элемента на месте — при той же длине коллекции — та
же порча истории, что и усечение, и отвергается так же. Ровно две правки
прежнего элемента разрешены, и обе — поля, объявленные заполняемыми позже:
`outcome` (с `null` на значение, однократно, P3) и `superseded_by`.

Отсюда же второе отличие: сверка с предыдущим состоянием — это
read-check-write, и атомарности одной записи ему мало. `save` целиком идёт
под эксклюзивной блокировкой (`events.file_lock`), иначе два процесса,
продолжающих один пайплайн, прочитали бы одно и то же прежнее состояние,
оба прошли бы guard и второй затёр бы добавление первого молча.

`pipeline_id` совпадает со слагом каталога (§4.1) и с `anchor_id` (§4.2),
поэтому путь манифеста выводится из самого состояния — отдельного входа
хранилищу не нужно.
"""

import json
from pathlib import Path
from typing import Any

from disputatio.contracts.pipeline import (
    OperatorDecision,
    PipelineState,
    SessionRecord,
    Transition,
)
from disputatio.events.atomic import atomic_write
from disputatio.events.file_lock import exclusive_lock
from disputatio.events.pipeline_paths import manifest_path

# Поля SessionRecord, законно заполняемые задним числом (§4.2). Остальные —
# неизменяемы с момента записи.
_LATE_FIELDS = ("outcome", "superseded_by")


def _stable_fields(record: SessionRecord) -> dict[str, Any]:
    """Поля записи сессии, неизменяемые после записи, — всё кроме `_LATE_FIELDS`."""
    payload = record.model_dump(mode="json")
    for field in _LATE_FIELDS:
        payload.pop(field, None)
    return payload


def _guard_length(previous: list[Any], current: list[Any], name: str) -> None:
    """Отвергает усечение коллекции: история только растёт."""
    if len(current) < len(previous):
        raise ValueError(
            f"{name}: append-only коллекция усечена "
            f"({len(previous)} → {len(current)}), история неприкосновенна (§4.2)"
        )


def _guard_immutable(
    previous: list[Transition] | list[OperatorDecision],
    current: list[Transition] | list[OperatorDecision],
    name: str,
) -> None:
    """Prefix-equality для коллекций без заполняемых позже полей."""
    _guard_length(previous, current, name)
    for index, recorded in enumerate(previous):
        if current[index] != recorded:
            raise ValueError(
                f"{name}[{index}] переписан на месте: append-only означает "
                "prefix-equality, а не «длина не уменьшается» (§4.2)"
            )


def _guard_late_field(
    previous: SessionRecord, current: SessionRecord, field: str, index: int, name: str
) -> None:
    """`outcome`/`superseded_by` заполняются однократно: `null` → значение."""
    recorded = getattr(previous, field)
    proposed = getattr(current, field)
    if recorded is None or proposed == recorded:
        return
    raise ValueError(
        f"{name}[{index}].{field} уже записан ({recorded!r} → {proposed!r}): "
        "поле заполняется однократно, с null на значение (§4.2, P3)"
    )


def _guard_sessions(
    previous: list[SessionRecord], current: list[SessionRecord], name: str
) -> None:
    """Prefix-equality для списков сессий с двумя разрешёнными правками."""
    _guard_length(previous, current, name)
    for index, recorded in enumerate(previous):
        proposed = current[index]
        if _stable_fields(proposed) != _stable_fields(recorded):
            raise ValueError(
                f"{name}[{index}] переписан на месте: изменять прежнюю запись "
                f"можно только через {' и '.join(_LATE_FIELDS)} (§4.2)"
            )
        for field in _LATE_FIELDS:
            _guard_late_field(recorded, proposed, field, index, name)


def _guard_history(previous: PipelineState, current: PipelineState) -> None:
    """Сверяет все четыре append-only коллекции манифеста (§4.2)."""
    _guard_sessions(previous.spec_sessions, current.spec_sessions, "spec_sessions")
    _guard_sessions(previous.pair_sessions, current.pair_sessions, "pair_sessions")
    _guard_immutable(previous.transitions, current.transitions, "transitions")
    _guard_immutable(
        previous.operator_decisions, current.operator_decisions, "operator_decisions"
    )


class FilePipelineStateStore:
    """`ports.PipelineStateStore`: `pipeline.json` под `pipelines/<slug>/`.

    Корень — `workspace_root` (git-репозиторий): каталог пайплайна лежит в
    репозитории, а `artifact_root` каждой ревизии сессии — уже внутри него
    (§4.1). Слаг равен `pipeline_id`, поэтому `save` находит файл по самому
    состоянию.

    Предусловие `save`: каталог `pipelines/<slug>/` уже создан — `atomic_write`
    кладёт временный файл рядом с целевым, поэтому без каталога вызов упадёт
    `FileNotFoundError`. Как и у `FileStateStore`, хранилище не делает `mkdir`
    ([REQ-002]).
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root

    def load(self, pipeline_id: str) -> PipelineState:
        """Читает манифест пайплайна `pipeline_id`.

        `KeyError(pipeline_id)`, если файла нет или `pipeline_id` в нём не
        совпадает с запрошенным — как у `FileStateStore`. Схемно невалидный
        payload — `pydantic.ValidationError` наружу без перехвата: повреждённый
        манифест не маскируется под «пайплайна нет».
        """
        state = self._read(pipeline_id)
        if state is None or state.pipeline_id != pipeline_id:
            raise KeyError(pipeline_id)
        return state

    def save(self, state: PipelineState) -> None:
        """Атомарно перезаписывает манифест, сверив append-only коллекции.

        Guard идёт **до** записи: отвергнутый `save` оставляет файл на диске
        нетронутым, иначе отказ сам был бы порчей истории.

        Чтение, сверка и запись идут под эксклюзивной блокировкой
        (`events.file_lock`) — иначе они не образуют одной операции. Два
        `disp pipeline resume` над одним пайплайном прочитали бы ОДНО и то
        же прежнее состояние, оба прошли бы guard (каждый сверяется с тем,
        что прочитал сам), и `os.replace` второго стёр бы добавление
        первого — молча, потому что append-only коллекция снаружи выглядит
        целой. Под блокировкой проигравший перечитывает уже обновлённое
        состояние, и его снимок отвергает тот же guard: усечение или правка
        префикса — громкий отказ вместо потерянной записи.
        """
        path = manifest_path(self._workspace_root, state.pipeline_id)
        with exclusive_lock(path):
            previous = self._read(state.pipeline_id)
            if previous is not None:
                if previous.pipeline_id != state.pipeline_id:
                    # Путь выводится из state.pipeline_id, так что несовпадение
                    # означает чужой манифест на этом месте. Записать поверх —
                    # значит смешать истории двух пайплайнов молча.
                    raise ValueError(
                        f"манифест по пути пайплайна {state.pipeline_id!r} "
                        f"принадлежит {previous.pipeline_id!r}"
                    )
                _guard_history(previous, state)

            payload = json.dumps(
                state.model_dump(mode="json", by_alias=True), ensure_ascii=False
            )
            atomic_write(path, payload)

    def _read(self, pipeline_id: str) -> PipelineState | None:
        """Прежнее состояние с диска либо `None`, если манифеста ещё нет."""
        path = manifest_path(self._workspace_root, pipeline_id)
        try:
            payload = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return PipelineState.model_validate(json.loads(payload))
