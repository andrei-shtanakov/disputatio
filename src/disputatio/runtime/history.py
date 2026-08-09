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

from disputatio.contracts import Decision, Issue, Review, VerificationReport
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
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


def load_verification(root: Path, round_no: int) -> VerificationReport | None:
    """Отчёт проверок раунда `round_no`; нет файла — `None`.

    Отдельно от `load_prior_round`, потому что читается отчёт ТЕКУЩЕГО
    раунда: шаг REVIEWING выносит вердикт по тем же гейтам, что прогнал
    шаг VERIFYING (§6.2), и подмена «взять отчёт прошлого раунда» — самая
    дешёвая ошибка сборки промпта. Разные функции делают её видимой.
    """
    return _load(root, round_no, VERIFICATION_NAME, VerificationReport)


def load_review(root: Path, round_no: int) -> Review | None:
    """Ревью раунда `round_no`; нет файла — `None`.

    Отдельно от `load_prior_round` по той же причине, что и
    `load_verification`: шаг DECIDING судит по ревью ТЕКУЩЕГО раунда, и
    подмена «взять ревью прошлого» — самая дешёвая ошибка сборки снимка.
    Разные функции делают её видимой.
    """
    return _load(root, round_no, REVIEW_NAME, Review)


def load_decision(root: Path, round_no: int) -> Decision | None:
    """Решение раунда `round_no`; нет файла — `None`.

    Отдельно от `load_prior_round` по той же причине, что и остальные
    одиночные читатели: решение ТЕКУЩЕГО раунда спрашивают, когда раунд уже
    финализирован и переписать его нельзя, — а «взять решение прошлого»
    выглядело бы там правдоподобно и молча.
    """
    return _load(root, round_no, DECISION_NAME, Decision)


def load_patch(root: Path, round_no: int) -> str | None:
    """Текст `rounds/NNN/changes.patch`; нет раунда или файла — `None`.

    `None`, а не пустая строка: «автор ничего не менял» и «сравнивать не с
    чем» — разные факты, и §5.3 отвечает на них по-разному (два пустых
    патча считаются идентичными по определению). Кому нужна именно строка,
    тот и подставляет пустую — здесь отсутствие остаётся отсутствием.

    `errors="replace"` симметричен записи патча: дифф чужого репозитория
    вправе нести байты не в UTF-8, и упасть на чтении собственного
    артефакта раунд не должен.
    """
    if round_no < 1:
        return None
    path = round_artifact(root, round_no, CHANGES_PATCH_NAME)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def carried_issues(root: Path, round_no: int) -> tuple[Issue, ...]:
    """Замечания раунда `round_no`, оставленные открытыми его решением.

    Пересечение двух артефактов ОДНОГО раунда: `review.json` знает сами
    замечания, `decision.json` — какие из них остались незакрытыми. Ни
    один из двух в одиночку ответа не даёт, поэтому нет и половинчатого
    результата: нет любого из артефактов — пустой кортеж.

    Неизвестный `open_issues_carried` id пропускается молча: замечание,
    которого в ревью нет, материализовать не из чего, а падение здесь
    остановило бы сессию из-за записи, которую уже никто не исправит.

    Порядок — порядок ревью, а не порядок списка id: он уходит в
    `open_issues_carried` следующего решения, и перестановка сделала бы
    историю раундов невоспроизводимой.
    """
    prior = load_prior_round(root, round_no)
    if prior.review is None or prior.decision is None:
        return ()
    open_ids = set(prior.decision.open_issues_carried)
    return tuple(issue for issue in prior.review.issues if issue.id in open_ids)


def issue_history(root: Path, round_no: int) -> dict[int, tuple[Issue, ...]]:
    """`{n: замечания ревью n}` по всем раундам строго до `round_no`.

    Строго до — часть контракта, а не оптимизация: попади сюда сам раунд
    `round_no`, каждое его замечание совпало бы само с собой, и повтор
    объявлялся бы на ровном месте.

    Раунд без ревью в словарь не попадает вовсе: пустой кортеж означал бы
    «ревьюер замечаний не нашёл», а оборванный раунд не находил их не
    потому, что их не было.
    """
    history: dict[int, tuple[Issue, ...]] = {}
    for prior in range(1, round_no):
        review = load_review(root, prior)
        if review is not None:
            history[prior] = tuple(review.issues)
    return history


def _load[T: (Review, VerificationReport, Decision)](
    root: Path, round_no: int, name: str, model: type[T]
) -> T | None:
    """Разбирает `rounds/NNN/name` моделью `model`; нет файла — `None`."""
    path = round_artifact(root, round_no, name)
    if not path.is_file():
        return None
    return model.model_validate_json(path.read_text(encoding="utf-8"))
