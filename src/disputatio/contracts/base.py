"""Базовые классы артефакт-моделей disputatio/v1 (DESIGN-001).

`FrozenModel` — общий конфиг всех моделей contract-слоя; `ArtifactModel`
добавляет обязательное валидируемое поле версии схемы для корневых
артефактов (вложенные модели поле версии не несут — §4 SPEC-001).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from disputatio.contracts.version import SCHEMA_V1


class FrozenModel(BaseModel):
    """Общий frozen-конфиг моделей contract-слоя (ADR-004).

    Иммутабельность (`frozen=True`), строгая десериализация
    (`extra="forbid"` — опечатка в ключе артефакта ловится на входе, а не
    молча теряется) и `populate_by_name` — обновления только через
    `model_copy(update=...)`.
    """

    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        extra="forbid",
    )


class ArtifactModel(FrozenModel):
    """Базовый класс всех корневых артефактов disputatio/v1 (REQ-001).

    В JSON поле версии — строго ключ `"schema"`: Python-имя
    `schema_version` с alias обходит shadowing-конфликт с устаревшим
    `BaseModel.schema` в pydantic v2 (ADR-001). Сериализация всюду —
    `model_dump_json(by_alias=True)`; `Literal` отвергает любое иное
    значение версии (включая будущий `"disputatio/v2"`).
    """

    schema_version: Literal["disputatio/v1"] = Field(
        default=SCHEMA_V1,
        alias="schema",
        serialization_alias="schema",
    )
