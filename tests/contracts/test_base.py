"""Тесты TASK-001: `SCHEMA_V1` и базовая модель `ArtifactModel` (DESIGN-001)."""

import importlib.util
import json
from types import ModuleType

import pytest
from pydantic import ValidationError


def _import_contracts() -> tuple[ModuleType, ModuleType]:
    """Возвращает модули `(version, base)` пакета `disputatio.contracts`.

    Отсутствие пакета фиксируется assertion'ом (а не `ImportError`):
    red-фаза TDD-гейта требует падения именно assertion'ом до появления
    реализации.
    """
    assert importlib.util.find_spec("disputatio.contracts") is not None, (
        "пакет disputatio.contracts ещё не реализован"
    )
    from disputatio.contracts import base, version

    return version, base


def test_model_dump_json_contains_schema_key() -> None:
    """`model_dump_json(by_alias=True)` несёт ключ `"schema": "disputatio/v1"`."""
    version, base = _import_contracts()
    dumped = json.loads(base.ArtifactModel().model_dump_json(by_alias=True))
    assert dumped == {"schema": "disputatio/v1"}
    assert dumped["schema"] == version.SCHEMA_V1


def test_reject_future_schema_version() -> None:
    """Десериализация JSON со `schema: "disputatio/v2"` → `ValidationError`."""
    _, base = _import_contracts()
    with pytest.raises(ValidationError):
        base.ArtifactModel.model_validate_json('{"schema": "disputatio/v2"}')


def test_reject_unknown_key() -> None:
    """Неизвестный ключ в JSON отвергается (`extra="forbid"`)."""
    _, base = _import_contracts()
    with pytest.raises(ValidationError):
        base.ArtifactModel.model_validate_json(
            '{"schema": "disputatio/v1", "unexpected": 1}'
        )


def test_frozen_mutation_rejected() -> None:
    """Присваивание полю frozen-модели поднимает `ValidationError`.

    Мутация выполняется через `setattr` с именем поля в переменной: попытка
    записи идёт через pydantic'овский `__setattr__` (как и обычное
    присваивание), но остаётся видимой рантайму, а не статике.
    """
    _, base = _import_contracts()
    artifact = base.ArtifactModel()
    field_name = "schema_version"
    with pytest.raises(ValidationError):
        setattr(artifact, field_name, "disputatio/v2")
