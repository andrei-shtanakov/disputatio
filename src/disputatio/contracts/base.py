"""Базовый контракт схемы артефактов disputatio/v1 ([DESIGN-001], [REQ-001]).

Каждый корневой артефакт на диске несёт поле ``"schema": "disputatio/v1"``;
вложенные модели версию не дублируют — она одна на артефакт-файл.
"""

from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_V1: Final = "disputatio/v1"


class Role(StrEnum):
    """Роль агента в debate loop; сериализуется строкой (JSON-совместимо)."""

    AUTHOR = "author"
    REVIEWER = "reviewer"


class ArtifactBase(BaseModel):
    """Общий предок корневых артефактов disputatio/v1.

    Имя атрибута ``schema_`` + alias ``"schema"``: голое ``schema`` в
    pydantic ``BaseModel`` — риск конфликта имён. Три гарантии контракта:

    1. Сериализация (``model_dump``/``model_dump_json`` без аргументов)
       всегда содержит ключ ``"schema" == "disputatio/v1"``
       (``serialize_by_alias=True``).
    2. Десериализация требует ключ ``schema`` (или имя ``schema_`` при
       ``populate_by_name``): payload без него отклоняется как missing,
       чужое значение отклоняется ``Literal`` — оба ``ValidationError``.
    3. Конструктор подставляет версию по умолчанию (ADR-005).
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal["disputatio/v1"] = Field(
        alias="schema", serialization_alias="schema"
    )

    def __init__(self, /, **data: Any) -> None:
        # Конструктор — единственный путь, где версия подставляется по
        # умолчанию: программный код создаёт артефакты текущей версии, а
        # парсинг чужих payload'ов обязан видеть ключ "schema" явно (ADR-005).
        if "schema" not in data and "schema_" not in data:
            data["schema"] = SCHEMA_V1
        super().__init__(**data)

    # Без маркера pydantic-core считает __init__ кастомным и гонит через
    # него и model_validate — подстановка сработала бы и при парсинге.
    # Маркер (как у BaseModel.__init__) возвращает валидацию на прямой
    # путь мимо __init__: парсинг строгий, конструктор удобный.
    __init__.__pydantic_base_init__ = True  # type: ignore[missing-attribute]


class ArtifactChild(BaseModel):
    """Frozen-база вложенных моделей артефактов — без поля ``schema``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
