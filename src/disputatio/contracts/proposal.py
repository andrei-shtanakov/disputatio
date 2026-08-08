"""Фронтматтер `proposal.md` и его парсер ([DESIGN-003], [REQ-003]).

Схема §4.2 SPEC-001: proposal — свободный markdown с YAML-фронтматтером,
несущим машиночитаемые поля. `parse_proposal` — чистая функция без I/O:
вход — строка документа, выход — `(ProposalFrontmatter, тело)`; тело
возвращается без изменений. Структурные ошибки (нет фронтматтера,
невалидный YAML, YAML не-словарь) — `ProposalParseError`; нарушение схемы
полей — pydantic `ValidationError`. Ничего не «пропускается молча».
"""

from enum import StrEnum
from typing import Literal

import yaml
from pydantic import Field

from disputatio.contracts.base import ArtifactBase, Role

_OPENING = "---\n"
_DELIMITER = "---"


class SelfDeclaredStatus(StrEnum):
    """Само-заявленный автором статус результата раунда (§4.2)."""

    COMPLETE = "complete"
    PARTIAL = "partial"


class ProposalFrontmatter(ArtifactBase):
    """Машиночитаемый YAML-фронтматтер `proposal.md` (§4.2 SPEC-001).

    `responds_to` — путь к review предыдущего раунда; `None` в раунде 1.
    `role` — Literal: proposal пишет только автор, иная роль отклоняется
    на валидации.
    """

    round: int = Field(ge=1)
    role: Literal[Role.AUTHOR]
    responds_to: str | None
    files_touched: list[str]
    self_declared_status: SelfDeclaredStatus


class ProposalParseError(ValueError):
    """Структурная ошибка разбора `proposal.md` (до валидации схемы полей)."""


def parse_proposal(text: str) -> tuple[ProposalFrontmatter, str]:
    """Разобрать proposal.md: валидированный фронтматтер + тело markdown.

    Документ обязан начинаться с ``---\\n``; блок до следующей строки,
    равной ``---``, читается ``yaml.safe_load``; остаток возвращается как
    тело байт-в-байт. Строки ``---`` внутри тела границей не считаются.
    """
    if not text.startswith(_OPENING):
        raise ProposalParseError("proposal.md обязан начинаться с '---'")
    lines = text[len(_OPENING) :].split("\n")
    for i, line in enumerate(lines):
        if line == _DELIMITER:
            block = "\n".join(lines[:i])
            body = "\n".join(lines[i + 1 :])
            break
    else:
        raise ProposalParseError("фронтматтер не закрыт строкой '---'")
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise ProposalParseError(f"невалидный YAML во фронтматтере: {exc}") from exc
    if not isinstance(data, dict):
        raise ProposalParseError("фронтматтер обязан быть YAML-словарём")
    return ProposalFrontmatter.model_validate(data), body
