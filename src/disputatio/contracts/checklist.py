"""Чеклист сходимости doc-сессий: EvidenceRef, ChecklistItem (SPEC-002 §5.2).

Только `disputatio/v2` несёт эти модели во вложенном `Review.checklist`
(§5.1); привязка тега к допустимости поля живёт в `review.py`, не здесь.
Evidence — дискриминированный union двух закрытых форм: ссылка на артефакт
с диапазоном строк или ссылка на результат гейта. Одна модель с
опциональным `lines` пропускала бы `artifact` без строк и `gate` со
строками — union с `discriminator="kind"` закрывает обе дыры.

**Непустота проверяется по содержимому, а не по длине контейнера.**
`evidence=[{"kind": "gate", "ref": ""}]` — список из одного элемента, то
есть `min_length=1` доволен; при этом пункт не называет ни одного
проверенного объекта, а именно это V2 и запрещает. Пустая, пробельная и
невидимая (Cf) ссылка отвергается схемой обеих форм (`semantic_text`), а
`lines` обязан быть настоящим диапазоном: строки нумеруются с единицы, и
``"0"`` указывает не на строку, а в никуда.
"""

import re
from typing import Annotated, Literal

from pydantic import Field, field_validator

from disputatio.contracts.base import ArtifactChild, semantic_text

_LINES_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def _require_substantive_ref(value: str) -> str:
    """Общий валидатор `ref` обеих форм evidence: ссылка обязана быть ссылкой."""
    if not semantic_text(value):
        raise ValueError(
            "ref обязан называть проверенный объект: пустая, пробельная "
            f"или невидимая ссылка доказательством не является: {value!r}"
        )
    return value


class ArtifactEvidence(ArtifactChild):
    """Evidence-ссылка на артефакт с диапазоном строк (§5.2 SPEC-002)."""

    kind: Literal["artifact"]
    ref: str
    lines: str

    _validate_ref = field_validator("ref")(_require_substantive_ref)

    @field_validator("lines")
    @classmethod
    def _validate_lines_format(cls, value: str) -> str:
        match = _LINES_RE.match(value)
        if match is None:
            raise ValueError(f'lines обязан быть в формате "N" или "N-M": {value!r}')
        start, end = match.group(1), match.group(2)
        if int(start) < 1:
            raise ValueError(f"lines: строки нумеруются с единицы: {value!r}")
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

    _validate_ref = field_validator("ref")(_require_substantive_ref)


EvidenceRef = Annotated[ArtifactEvidence | GateEvidence, Field(discriminator="kind")]


class ChecklistItem(ArtifactChild):
    """Один пункт чеклиста сходимости doc-сессии (§5.2, §5.3 SPEC-002).

    `evidence` непуст независимо от статуса (V2, §5.2): pass и
    not_applicable без указания, что именно проверено или почему
    неприменимо, — голословны. Непустота здесь двойная: длина списка
    (`min_length=1`) И содержательность каждой ссылки (валидаторы
    `EvidenceRef`) — первого без второго хватало ровно на то, чтобы
    голословный approve выглядел обоснованным. Кросс-артефактные правила V1, V3–V8
    (покрытие набора id, согласованность с issues) — вне схемной
    валидации, живут в слое протокольной валидации review.json.
    """

    id: str
    status: Literal["pass", "fail", "not_applicable"]
    evidence: list[EvidenceRef] = Field(min_length=1)
    issue_ids: list[str] = Field(default_factory=list)
