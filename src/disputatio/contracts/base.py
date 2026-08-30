"""Базовый контракт схемы артефактов disputatio/v1|v2 ([DESIGN-001], [REQ-001]).

Каждый корневой артефакт на диске несёт поле ``"schema"``; вложенные модели
версию не дублируют — она одна на артефакт-файл. `disputatio/v2` (SPEC-002
§5.1) — строгое надмножество v1: новые поля (`Mode.DOCUMENT`,
`Review.checklist`, `Issue.defect_class`) допустимы только под тегом v2 —
привязку проверяют `model_validator`'ы в `session.py` и `review.py`, не сам
`Literal` версии.
"""

import unicodedata
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_V1: Final = "disputatio/v1"
SCHEMA_V2: Final = "disputatio/v2"


def semantic_text(text: str) -> str:
    """Семантическое содержимое строки: без Cf-символов и краевых пробелов.

    Удаляет все символы Unicode-категории Cf (U+200B, U+FEFF и др.) ДО
    strip: строка из одних невидимых и/или пробельных символов
    нормализуется в "".

    Живёт в `base`, а не в `validation`: критерий «содержательно ли это
    поле» нужен и схемному слою (`checklist.py`, evidence-ссылки), и
    протокольному (`validation.py`, evidence issue и `checked`), а
    `validation` импортирует `review`, который импортирует `checklist`, —
    вторая копия критерия разошлась бы с первой ровно в том месте, где
    расхождение никто не заметит.
    """
    visible = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return visible.strip()


class Role(StrEnum):
    """Роль агента в debate loop; сериализуется строкой (JSON-совместимо)."""

    AUTHOR = "author"
    REVIEWER = "reviewer"


class ArtifactBase(BaseModel):
    """Общий предок корневых артефактов disputatio/v1|v2.

    Имя атрибута ``schema_`` + alias ``"schema"``: голое ``schema`` в
    pydantic ``BaseModel`` — риск конфликта имён. Три гарантии контракта:

    1. Сериализация (``model_dump``/``model_dump_json`` без аргументов)
       всегда содержит ключ ``"schema"`` со значением, с которым артефакт
       был создан (``serialize_by_alias=True``).
    2. Десериализация требует ключ ``schema`` (или имя ``schema_`` при
       ``populate_by_name``): payload без него отклоняется как missing,
       значение вне ``{v1, v2}`` отклоняется ``Literal`` — оба
       ``ValidationError``.
    3. Конструктор подставляет версию по умолчанию — ``disputatio/v1``
       (ADR-005): программный код develop/analyze-сессий не меняется,
       v2 ставит явно только писатель doc-сессии.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal["disputatio/v1", "disputatio/v2"] = Field(
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
