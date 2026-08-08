"""Базовый контракт схемы артефактов disputatio/v1 ([DESIGN-001], [REQ-001]).

Каждый корневой артефакт на диске несёт поле ``"schema": "disputatio/v1"``;
вложенные модели версию не дублируют — она одна на артефакт-файл.
"""

from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_V1: Final = "disputatio/v1"


class Role(StrEnum):
    """Роль агента в debate loop; сериализуется строкой (JSON-совместимо)."""

    AUTHOR = "author"
    REVIEWER = "reviewer"


class ArtifactBase(BaseModel):
    """Общий предок корневых артефактов disputatio/v1.

    Имя атрибута ``schema_`` + alias ``"schema"``: голое ``schema`` в
    pydantic ``BaseModel`` — риск конфликта имён, alias гарантирует, что в
    JSON поле называется ровно ``"schema"``. ``Literal`` с дефолтом даёт обе
    гарантии без кастомных валидаторов: сериализация всегда содержит поле,
    десериализация с чужим значением падает ``ValidationError``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_: Literal["disputatio/v1"] = Field(
        default=SCHEMA_V1, alias="schema", serialization_alias="schema"
    )


class ArtifactChild(BaseModel):
    """Frozen-база вложенных моделей артефактов — без поля ``schema``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
