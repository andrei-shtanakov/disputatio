"""Сборка промпта автора как целого: TASK-004 (review-fix), [DESIGN-005].

`test_author.py` пинит содержимое секций — какие issues и гейты попадают в
промпт. Мутационная проба вскрыла, что мимо него проходит ровно то, что
секции не описывает: номер раунда, режим задачи, порядок секций §6.1,
граница artifact-меток вокруг `task.prompt` и отсев пустых частей при
склейке. Шесть мутантов реализации переживали весь `tests/context/` — этот
файл их закрывает.

Второй проход пробы добавил ещё три выживших мутанта: сам разделитель
секций (`"\\n\\n"` → `"\\n"`), разделитель внутри секции задачи и сжатие
условия «замечания без решения оркестратора не показываются» до одного
`prior_review is None` — последнее падало `AttributeError` на комбинации
`prior_review` без `prior_decision`, которую не проверял ни один тест.

Файл отдельный, а не дополнение к `test_author.py`: тот байт-заморожен
red-чекпоинтом TASK-004 и правке не подлежит.
"""

import importlib
from types import ModuleType

from disputatio.contracts.base import Role
from disputatio.contracts.decision import Decision, Outcome
from disputatio.contracts.review import Issue, Review, Severity, Verdict
from disputatio.contracts.session import Mode, TaskSpec
from disputatio.contracts.verification import (
    DiffStats,
    GateResult,
    GateStatus,
    OverallStatus,
    VerificationReport,
)


def _module(name: str) -> ModuleType:
    """Импортирует модуль пакета `context`; отсутствие — assertion."""
    try:
        return importlib.import_module(f"disputatio.context.{name}")
    except ImportError as exc:  # pragma: no cover - только на red-чекпоинте
        raise AssertionError(
            f"disputatio.context.{name} не импортируется: {exc}"
        ) from exc


def _task() -> TaskSpec:
    """Задача пользователя; текст не пересекается с полями артефактов."""
    return TaskSpec(prompt="Почини ретраи клиента.", mode=Mode.DEVELOP)


def _prior_review() -> Review:
    """`review.json` раунда 3 с единственным blocker'ом `R3-1`."""
    return Review(
        round=3,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.7,
        issues=[
            Issue(
                id="R3-1",
                severity=Severity.BLOCKER,
                file="src/client.py",
                claim="retry не сбрасывает backoff",
                evidence="в логе backoff растёт после успеха",
            )
        ],
        checked=["src/client.py"],
        summary="нужны правки",
    )


def _full_round_kwargs() -> dict[str, object]:
    """Раунд 4 со всеми тремя артефактами раунда 3 — все секции непусты."""
    review = _prior_review()
    verification = VerificationReport(
        round=3,
        gates=[
            GateResult(
                name="pytest",
                cmd="uv run pytest -q",
                status=GateStatus.FAIL,
                exit_code=1,
                tail="FAILED tests/test_client.py::test_backoff",
            )
        ],
        overall=OverallStatus.FAIL,
        diff_stats=DiffStats(files=1, insertions=10, deletions=2),
    )
    decision = Decision(
        round=3,
        outcome=Outcome.CONTINUE,
        reason="continue_revise_cycle",
        open_issues_carried=["R3-1"],
        next_round_directive="Сначала почини гейт pytest.",
    )
    return {
        "task": _task(),
        "round": 4,
        "prior_review": review,
        "prior_verification": verification,
        "prior_decision": decision,
    }


def test_prompt_names_the_current_round() -> None:
    """Автор обязан знать, какой сейчас раунд, — номер стоит в промпте."""
    author = _module("author")

    prompt = author.build_author_prompt(
        task=TaskSpec(prompt="Почини ретраи клиента.", mode=Mode.DEVELOP), round=7
    )

    assert "7" in prompt


def test_prompt_carries_task_mode() -> None:
    """`analyze` не даёт править код (§5.1) — режим не должен теряться."""
    author = _module("author")

    develop = author.build_author_prompt(
        task=TaskSpec(prompt="Почини ретраи клиента.", mode=Mode.DEVELOP), round=1
    )
    analyze = author.build_author_prompt(
        task=TaskSpec(prompt="Почини ретраи клиента.", mode=Mode.ANALYZE), round=1
    )

    assert Mode.DEVELOP.value in develop
    assert Mode.ANALYZE.value not in develop
    assert Mode.ANALYZE.value in analyze


def test_user_prompt_sits_inside_artifact_tags() -> None:
    """Текст пользователя — данные: заголовок снаружи меток, prompt внутри."""
    author = _module("author")
    tags = _module("tags")
    task = TaskSpec(prompt="Почини ретраи клиента.", mode=Mode.DEVELOP)

    prompt = author.build_author_prompt(task=task, round=1)

    open_at = prompt.index(tags._OPEN_TAG)
    text_at = prompt.index(task.prompt)
    close_at = prompt.index(tags._CLOSE_TAG)
    assert prompt.index(author.TASK_TITLE) < open_at < text_at < close_at


def test_sections_follow_the_order_of_spec_6_1() -> None:
    """Порядок §6.1: задача → директива → открытые замечания → гейты."""
    author = _module("author")
    sections = _module("sections")

    prompt = author.build_author_prompt(**_full_round_kwargs())

    assert (
        prompt.index(author.TASK_TITLE)
        < prompt.index(sections.DIRECTIVE_TITLE)
        < prompt.index(sections.OPEN_ISSUES_TITLE)
        < prompt.index(sections.FAILED_GATES_TITLE)
    )


def test_absent_sections_leave_no_blank_gap() -> None:
    """Пустая секция выпадает из склейки, а не превращается в дыру из \\n."""
    author = _module("author")
    task = TaskSpec(prompt="Почини ретраи клиента.", mode=Mode.DEVELOP)

    minimal = author.build_author_prompt(task=task, round=1)
    full = author.build_author_prompt(**_full_round_kwargs())

    assert "\n\n\n" not in minimal
    assert "\n\n\n" not in full


def test_blank_line_separates_sections_and_never_splits_one() -> None:
    """Пустая строка — граница секций: внутри секции её нет, между — есть."""
    author = _module("author")
    sections = _module("sections")
    tags = _module("tags")

    prompt = author.build_author_prompt(**_full_round_kwargs())

    for title in (
        author.TASK_TITLE,
        sections.DIRECTIVE_TITLE,
        sections.OPEN_ISSUES_TITLE,
        sections.FAILED_GATES_TITLE,
    ):
        assert f"\n\n{title}" in prompt
    # Заголовок, режим и открывающая метка — сплошной блок одной секции.
    task_head = prompt[prompt.index(author.TASK_TITLE) : prompt.index(tags._OPEN_TAG)]
    assert "\n\n" not in task_head


def test_review_without_decision_renders_no_issues_section() -> None:
    """Какие замечания остались открытыми, знает только `DECIDING` (§6.1)."""
    author = _module("author")
    sections = _module("sections")
    review = _prior_review()

    prompt = author.build_author_prompt(task=_task(), round=4, prior_review=review)

    assert sections.OPEN_ISSUES_TITLE not in prompt
    # Ни id, ни текст замечания: без решения оркестратора неизвестно, какие
    # из них ещё открыты, — и автор не должен чинить уже закрытое.
    assert review.issues[0].id not in prompt
    assert review.issues[0].claim not in prompt
