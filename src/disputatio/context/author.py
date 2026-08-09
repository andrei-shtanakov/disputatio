"""Промпт автора раунда N (§6.1, [DESIGN-005]).

Тонкий композитор: сначала сверка раундов у каждого непустого `prior_*`,
затем сборка секций через `sections.py`, затем склейка непустых частей.
Правил «что и в каком порядке попадает в промпт» здесь нет — они живут в
`sections.py` и переиспользуются промптом ревьюера.

Два решения, которые проще нарушить, чем восстановить:

1. Холодный старт — не отдельная ветка ([ADR-002]). Раунд 1 даёт
   `round - 1 == 0`, а схемное `round: ge=1` делает такой раунд
   недостижимым для любого артефакта: первый раунд просто не находит,
   что подставить, а подсунутый ему `prior_*` честно падает `ValueError`.
2. Сигнатура принимает единичные `Review`/`VerificationReport`/`Decision`,
   не списки и не `SessionState` ([ADR-004]). Запрет «никогда не передавать
   полную историю сессии» и запрет отдавать автору его собственные прошлые
   proposal выражены типами, а не комментарием: код в рабочей директории —
   источник истины, история чата — нет (§6.1).

`task.attachments` в промпт не попадают сознательно: способ их доставки
агентам — открытый вопрос §10.5 SPEC-001, и v1 его не фиксирует.

Модуль чист: ни I/O, ни времени, ни случайности (NFR-001, NFR-002).
"""

from typing import Final

from disputatio.context.sections import (
    OPEN_ISSUES_TITLE,
    render_directive_section,
    render_failed_gates_section,
    render_issues_section,
    select_open_issues,
)
from disputatio.context.tags import wrap_artifact_data
from disputatio.contracts.decision import Decision
from disputatio.contracts.review import Review
from disputatio.contracts.session import TaskSpec
from disputatio.contracts.verification import VerificationReport

TASK_TITLE: Final = "## Задача пользователя"

_INTRO_TEMPLATE: Final = (
    "# Раунд {round}: работа автора\n"
    "Источник истины — файлы рабочей директории, а не история диалога: "
    "прошлые proposal сюда не передаются намеренно (§6.1). Ниже — задача "
    "пользователя и, если раунд не первый, материалы прошлого раунда."
)


def build_author_prompt(
    *,
    task: TaskSpec,
    round: int,
    prior_review: Review | None = None,
    prior_verification: VerificationReport | None = None,
    prior_decision: Decision | None = None,
) -> str:
    """Собирает промпт автора для раунда `round` (§6.1).

    Порядок секций фиксирован §6.1: задача пользователя (всегда), директива
    оркестратора, открытые замечания ревьюера, проваленные гейты. Каждая
    необязательная секция исчезает целиком, если данных нет ([ADR-003]):
    заголовок над пустотой автор читает как факт «проверено, замечаний нет».

    Все `prior_*` относятся к раунду `round - 1` — иначе `ValueError`: молча
    подставленный чужой раунд увёл бы автора чинить уже исправленное.
    Открытые замечания — пересечение `prior_review.issues` с
    `prior_decision.open_issues_carried`, поэтому без решения оркестратора
    (`prior_decision is None`) секции замечаний нет: какие из них остались
    открытыми, знает только `DECIDING`.

    Результат зависит только от аргументов — байт-в-байт воспроизводим
    (NFR-002).
    """
    prior_round = round - 1
    _check_prior_round(prior_review, param="prior_review", expected=prior_round)
    _check_prior_round(
        prior_verification, param="prior_verification", expected=prior_round
    )
    _check_prior_round(prior_decision, param="prior_decision", expected=prior_round)

    parts = [
        _INTRO_TEMPLATE.format(round=round),
        _render_task_section(task),
        render_directive_section(
            prior_decision.next_round_directive if prior_decision else None
        ),
        _render_open_issues_section(prior_review, prior_decision),
        render_failed_gates_section(
            prior_verification.gates if prior_verification else []
        ),
    ]
    return "\n\n".join(part for part in parts if part)


def _check_prior_round(
    artifact: Review | VerificationReport | Decision | None,
    *,
    param: str,
    expected: int,
) -> None:
    """Артефакт обязан быть из раунда `expected`; `None` — законный вход.

    Сообщение называет и параметр, и ожидавшийся раунд: без этого вызывающая
    сторона видит только «не тот раунд» и не знает, какой из трёх `prior_*`
    разъехался.
    """
    if artifact is None:
        return
    if artifact.round != expected:
        raise ValueError(
            f"{param}.round == {artifact.round}: ожидался раунд {expected} "
            f"(round - 1); полная история раундов в промпт не передаётся (§6.1)"
        )


def _render_task_section(task: TaskSpec) -> str:
    """Задача пользователя дословно; режим — сводка оркестратора вне меток."""
    return "\n".join(
        [TASK_TITLE, f"режим: {task.mode}", wrap_artifact_data(task.prompt)]
    )


def _render_open_issues_section(
    prior_review: Review | None, prior_decision: Decision | None
) -> str:
    """Замечания прошлого раунда, оставшиеся открытыми после `DECIDING`."""
    if prior_review is None or prior_decision is None:
        return ""
    open_issues = select_open_issues(
        prior_review.issues, prior_decision.open_issues_carried
    )
    return render_issues_section(open_issues, title=OPEN_ISSUES_TITLE)
