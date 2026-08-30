"""Общие приватные хелперы разбора TOML: `config.py` и `pipeline_config.py`
(фикс-раунд 1 задачи 13, Important-2).

`config.py` (снапшот `[session]`/`[task]`/`[limits]`/`[agents]`/`[[gates]]`)
и `pipeline_config.py` (снапшот `[pipeline]`) — два независимых загрузчика
одного и того же `config.toml`, и оба переводят синтаксически некорректный
TOML в `KeyError`/`TypeError`, которые вызывающий модуль ловит и превращает
в `ConfigError` ([DESIGN-020]). До этого модуля пять функций были
продублированы между ними побайтово (`table`/`text`/`texts`) либо с одной
отличающейся строкой (`gate`/`integer` — разный `where` в тексте ошибки).
Разойдись дубли при следующей правке одного из двух мест, диагностика
конфига стала бы зависеть от того, какая секция упала первой, а не от
природы ошибки.

Оба потребителя — приватные модули внутри `runtime`, и общий модуль их
границ владения не пересекает (§9 SPEC-002: и `[session]`/`[limits]`, и
`[pipeline]` — оркестрация/composition, зона `runtime`).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from disputatio.verifier import GateSpec


def table(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Обязательная таблица верхнего уровня; иначе `KeyError`/`TypeError`."""
    if name not in raw:
        raise KeyError(f"нет обязательной таблицы [{name}]")
    value = raw[name]
    if not isinstance(value, Mapping):
        raise TypeError(f"[{name}] обязана быть таблицей, а не {type(value).__name__}")
    return value


def text(container: Mapping[str, Any], key: str, *, where: str) -> str:
    """Обязательное строковое значение таблицы `where`."""
    if key not in container:
        raise KeyError(f"нет обязательного ключа {where}.{key}")
    value = container[key]
    if not isinstance(value, str):
        raise TypeError(f"{where}.{key} обязан быть строкой")
    return value


def texts(container: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Необязательный массив строк; отсутствие — пустой кортеж."""
    value = container.get(key, [])
    if not isinstance(value, list):
        raise TypeError(f"{key} обязан быть массивом строк")
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"элемент {key} обязан быть строкой")
    return tuple(value)


def integer(container: Mapping[str, Any], key: str, *, where: str) -> int:
    """Обязательное целое `where.key`; `bool` — подкласс `int`, не считается.

    `bool` — подкласс `int` в Python, поэтому `max_rounds = true` прошёл бы
    проверку «это целое» молча и превратил бы лимит раундов в единицу.
    """
    if key not in container:
        raise KeyError(f"нет обязательного ключа {where}.{key}")
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{where}.{key} обязан быть целым числом")
    return value


def table_array(container: Mapping[str, Any], key: str) -> Sequence[Mapping[str, Any]]:
    """Массив таблиц `[[<container>.key]]`; отсутствие — пустой список, не ошибка.

    Пустой массив — законное состояние обоих потребителей: сессия без
    дополнительных гейтов законна ([REQ-010]), пайплайн без `extra_gates`
    — тоже (§3.2); требовать хотя бы один элемент значило бы завести здесь
    второе мнение о том, что уже решено на уровне вызывающего модуля.
    """
    value = container.get(key, [])
    if not isinstance(value, list):
        raise TypeError(f"[[{key}]] обязан быть массивом таблиц")
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError(f"элемент [[{key}]] обязан быть таблицей")
    return value


def gate(item: Mapping[str, Any], *, where: str) -> GateSpec:
    """Один `GateSpec` из элемента массива таблиц `[[<where>]]`.

    `enabled` необязателен и по умолчанию `True` — тем же дефолтом, что у
    самого `GateSpec`: два разных ответа на «гейт без флага включён?»
    разошлись бы молча, и разошлись бы в сторону пропущенной проверки.
    """
    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TypeError(f"{where}.enabled обязан быть true/false")
    return GateSpec(
        name=text(item, "name", where=where),
        cmd=text(item, "cmd", where=where),
        enabled=enabled,
    )
