"""Чеклист сходимости doc-сессий: EvidenceRef, ChecklistItem (SPEC-002 §5.2).

Только `disputatio/v2` несёт эти модели во вложенном `Review.checklist`
(§5.1); привязка тега к допустимости поля живёт в `review.py`, не здесь.
Evidence — дискриминированный union двух закрытых форм: ссылка на артефакт
с диапазоном строк или ссылка на результат гейта. Одна модель с
опциональным `lines` пропускала бы `artifact` без строк и `gate` со
строками — union с `discriminator="kind"` закрывает обе дыры.
"""

import re
from typing import Annotated, Literal

from pydantic import Field, field_validator

from disputatio.contracts.base import ArtifactChild

_LINES_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


class ArtifactEvidence(ArtifactChild):
    """Evidence-ссылка на артефакт с диапазоном строк (§5.2 SPEC-002)."""

    kind: Literal["artifact"]
    ref: str
    lines: str

    @field_validator("lines")
    @classmethod
    def _validate_lines_format(cls, value: str) -> str:
        match = _LINES_RE.match(value)
        if match is None:
            raise ValueError(f'lines обязан быть в формате "N" или "N-M": {value!r}')
        start, end = match.group(1), match.group(2)
        if end is not None and int(end) < int(start):
            raise ValueError(f"lines: конец диапазона меньше начала: {value!r}")
        return value


class GateEvidence(ArtifactChild):
    """Evidence-ссылка на результат гейта — без диапазона строк (§5.2).

    Поля ``lines`` в модели нет: `extra="forbid"` базовой модели отвергает
    его как лишнее, если payload его несёт.
    """

    kind: Literal["gate"]
    ref: str


EvidenceRef = Annotated[ArtifactEvidence | GateEvidence, Field(discriminator="kind")]


class ChecklistItem(ArtifactChild):
    """Один пункт чеклиста сходимости doc-сессии (§5.2, §5.3 SPEC-002).

    `evidence` непуст независимо от статуса (V2, §5.2): pass и
    not_applicable без указания, что именно проверено или почему
    неприменимо, — голословны. Кросс-артефактные правила V1, V3–V8
    (покрытие набора id, согласованность с issues) — вне схемной
    валидации, живут в слое протокольной валидации review.json.
    """

    id: str
    status: Literal["pass", "fail", "not_applicable"]
    evidence: list[EvidenceRef] = Field(min_length=1)
    issue_ids: list[str] = Field(default_factory=list)
