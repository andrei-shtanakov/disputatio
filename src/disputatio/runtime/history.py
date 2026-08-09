"""Чтение артефактов прошлых раундов ([REQ-003], [DESIGN-003]).

Единственный источник контекста для шага: §6.1 запрещает передавать в промпт
историю сессии, поэтому «что было в прошлом раунде» собирается не из памяти
процесса и не из чата агента, а с диска — из артефактов, которые пережили бы
перезапуск оркестратора. Resume и холодный старт читают одно и то же.

Отсутствующий артефакт — `None`, а не ошибка: раунд 1 прошлого не имеет
вовсе, а раунд, оборванный до `REVIEWING`, честно не оставил `review.json`.
Битый артефакт, наоборот, поднимает `ValidationError` — молча подставленный
`None` превратил бы повреждённую историю в «замечаний не было».
"""

from dataclasses import dataclass
from pathlib import Path

from disputatio.contracts import Decision, Review, VerificationReport
from disputatio.runtime.layout import (
    DECISION_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    round_artifact,
)


@dataclass(frozen=True, slots=True)
class PriorRound:
    """Артефакты одного прошлого раунда — материал промпта автора (§6.1).

    Ровно три артефакта, ровно те, что принимает `build_author_prompt`.
    `proposal.md` среди них нет: прошлые предложения автора в промпт не
    попадают, и отсутствие поля — то же структурное выражение запрета, что и
    отсутствие параметра у сборщика промпта. Списка раундов здесь тоже нет —
    «никогда не передавать полную историю» выражено типом контейнера.
    """

    round: int
    review: Review | None = None
    verification: VerificationReport | None = None
    decision: Decision | None = None


def load_prior_round(root: Path, round_no: int) -> PriorRound:
    """Читает артефакты раунда `round_no` (== N−1 для шага раунда N).

    `round_no < 1` — законный вход, а не ошибка: так выглядит холодный старт
    раунда 1, и отдельной ветки «первый раунд» ни у шага, ни у сборщика
    промпта не появляется.
    """
    if round_no < 1:
        return PriorRound(round=round_no)
    return PriorRound(
        round=round_no,
        review=_load(root, round_no, REVIEW_NAME, Review),
        verification=_load(root, round_no, VERIFICATION_NAME, VerificationReport),
        decision=_load(root, round_no, DECISION_NAME, Decision),
    )


def _load[T: (Review, VerificationReport, Decision)](
    root: Path, round_no: int, name: str, model: type[T]
) -> T | None:
    """Разбирает `rounds/NNN/name` моделью `model`; нет файла — `None`."""
    path = round_artifact(root, round_no, name)
    if not path.is_file():
        return None
    return model.model_validate_json(path.read_text(encoding="utf-8"))
