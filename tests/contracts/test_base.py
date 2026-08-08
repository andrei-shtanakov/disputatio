"""Тесты базового контракта схемы disputatio/v1: TASK-001, [DESIGN-001].

Импорты `disputatio.contracts.base` выполняются внутри тестов: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы collection.
Red-селектор (`test_serialization_contains_schema_key`) превращает ImportError
в AssertionError — гейт принимает red только при падении assertion'ом.
"""

import json

import pytest
from pydantic import ValidationError


def test_serialization_contains_schema_key() -> None:
    """Сериализация по алиасам содержит ключ `"schema" == "disputatio/v1"`."""
    try:
        from disputatio.contracts.base import SCHEMA_V1, ArtifactBase
    except ImportError as exc:  # red-фаза: base.py ещё не создан
        raise AssertionError("src/disputatio/contracts/base.py ещё не создан") from exc

    class Sample(ArtifactBase):
        value: int

    payload = json.loads(Sample(value=1).model_dump_json(by_alias=True))
    assert payload["schema"] == SCHEMA_V1 == "disputatio/v1"


def test_foreign_schema_value_rejected() -> None:
    """Десериализация с чужим значением schema отклоняется ValidationError."""
    from disputatio.contracts.base import ArtifactBase

    class Sample(ArtifactBase):
        value: int

    with pytest.raises(ValidationError):
        Sample.model_validate({"schema": "disputatio/v2", "value": 1})


def test_round_trip_by_alias() -> None:
    """model_validate_json(model_dump_json(by_alias=True)) даёт равную модель."""
    from disputatio.contracts.base import ArtifactBase

    class Sample(ArtifactBase):
        value: int

    original = Sample(value=42)
    restored = Sample.model_validate_json(original.model_dump_json(by_alias=True))
    assert restored == original


def test_extra_field_forbidden() -> None:
    """extra="forbid": лишнее поле отклоняется ValidationError."""
    from disputatio.contracts.base import ArtifactBase

    class Sample(ArtifactBase):
        value: int

    with pytest.raises(ValidationError):
        Sample.model_validate({"value": 1, "unexpected": True})


def test_frozen_forbids_mutation() -> None:
    """frozen=True: присваивание в поле сконструированной модели запрещено."""
    from disputatio.contracts.base import ArtifactBase

    class Sample(ArtifactBase):
        value: int

    sample = Sample(value=1)
    with pytest.raises(ValidationError):
        sample.value = 2


def test_construct_by_field_name_and_alias() -> None:
    """populate_by_name: конструирование и по `schema_`, и по `"schema"`."""
    from disputatio.contracts.base import SCHEMA_V1, ArtifactBase

    class Sample(ArtifactBase):
        value: int

    by_name = Sample.model_validate({"schema_": SCHEMA_V1, "value": 1})
    by_alias = Sample.model_validate({"schema": SCHEMA_V1, "value": 1})
    assert by_name == by_alias


def test_artifact_child_has_no_schema_field() -> None:
    """ArtifactChild не несёт поле schema — версия одна на артефакт-файл."""
    from disputatio.contracts.base import ArtifactChild

    class Child(ArtifactChild):
        name: str

    assert "schema_" not in Child.model_fields
    payload = json.loads(Child(name="x").model_dump_json(by_alias=True))
    assert "schema" not in payload


def test_artifact_child_frozen_and_forbids_extra() -> None:
    """ArtifactChild наследует дисциплину frozen + extra="forbid"."""
    from disputatio.contracts.base import ArtifactChild

    class Child(ArtifactChild):
        name: str

    child = Child(name="x")
    with pytest.raises(ValidationError):
        child.name = "y"
    with pytest.raises(ValidationError):
        Child.model_validate({"name": "x", "extra_field": 1})


def test_role_is_str_enum() -> None:
    """Role — StrEnum с ровно двумя значениями: author и reviewer."""
    from disputatio.contracts.base import Role

    assert list(Role) == [Role.AUTHOR, Role.REVIEWER]
    assert Role.AUTHOR == "author"
    assert Role.REVIEWER == "reviewer"
    assert isinstance(Role.AUTHOR, str)
