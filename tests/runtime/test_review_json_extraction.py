"""Границы JSON-объекта в тексте ревьюера ([REQ-005], [DESIGN-005]).

[TASK-010], дыра, найденная мутационной пробой уже после red-чекпоинта
`test_step_reviewing.py` (тот файл байт-locked, поэтому пин живёт здесь).

Выживший мутант — «от первой `{` до ПОСЛЕДНЕЙ `}`»: на ответах, где после
объекта нет ни одной скобки, он неотличим от честного счётчика. Отличим он
ровно там, где агенты и ошибаются: реплика вида «…вот JSON… а если нужно
{подробнее} — спрашивайте» превращается у жадного разбора в мусор, потому
что в объект уезжает вся болтовня хвоста.

`extract_json_object` документирует «первый top-level объект» — не «самый
длинный» и не «всё между крайними скобками», — и здесь это пиньётся тремя
формами хвоста: скобки в прозе, второй объект и незакрытая `{`.
"""

import json
from importlib import import_module
from types import ModuleType
from typing import Any

import pytest

_PAYLOAD: dict[str, Any] = {
    "schema": "disputatio/v1",
    "round": 3,
    "role": "reviewer",
    "verdict": "approve",
    "confidence": 0.7,
    "issues": [],
    "checked": ["feature.py"],
    "summary": "свод",
}


def _parsing() -> ModuleType:
    """Модуль `runtime/parsing.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.parsing")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/parsing.py: {exc}") from exc


def _extract(text: str) -> str:
    """`extract_json_object` из модуля парсинга."""
    parsing = _parsing()
    assert hasattr(parsing, "extract_json_object"), (
        "runtime/parsing.py не определяет extract_json_object"
    )
    return parsing.extract_json_object(text)


def _body() -> str:
    """Сериализованный `review.json`-payload одной строкой."""
    return json.dumps(_PAYLOAD, ensure_ascii=False)


def test_braces_in_the_trailing_prose_do_not_extend_the_object() -> None:
    """Скобки в хвосте ответа не уезжают внутрь объекта ([REQ-005])."""
    text = f"{_body()}\nЕсли нужно {{подробнее}} — спрашивайте."

    extracted = _extract(text)

    assert json.loads(extracted) == _PAYLOAD
    assert "спрашивайте" not in extracted


def test_first_object_wins_when_the_reply_carries_two() -> None:
    """Берётся ПЕРВЫЙ top-level объект, а не последний и не их склейка."""
    tail = json.dumps({"note": "черновик, не используйте"}, ensure_ascii=False)
    text = f"Ревью:\n{_body()}\nА вот черновик, который я не отправляю:\n{tail}"

    extracted = _extract(text)

    assert json.loads(extracted) == _PAYLOAD
    assert "черновик" not in extracted


def test_an_unclosed_brace_in_the_prose_does_not_swallow_the_object() -> None:
    """Незакрытая `{` перед объектом не делает разбор невозможным."""
    text = f"Поставьте {{ в начало файла. Ревью:\n{_body()}"

    extracted = _extract(text)

    assert json.loads(extracted) == _PAYLOAD


def test_fenced_object_with_a_trailing_fence_and_braces() -> None:
    """```json-фенс + скобки после фенса — объект всё ещё ровно один."""
    text = (
        "Готово.\n"
        f"```json\n{_body()}\n```\n"
        'Дальше по стилю: используйте f-строки вида f"{{value}}".'
    )

    extracted = _extract(text)

    assert json.loads(extracted) == _PAYLOAD
    assert "```" not in extracted
    assert "f-строки" not in extracted


def test_reply_without_any_closing_brace_is_a_parse_error() -> None:
    """Незакрытый объект — `ReviewParseError`, а не обрезанная строка."""
    runtime = import_module("disputatio.runtime")
    assert hasattr(runtime, "ReviewParseError"), (
        "disputatio.runtime не экспортирует ReviewParseError"
    )

    with pytest.raises(runtime.ReviewParseError):
        _extract('Ревью: {"verdict": "approve"')
