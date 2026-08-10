"""Тесты `author.py` — промпт автора раунда N: TASK-004, [DESIGN-005].

Модуль загружается лениво (`_load_author`), как в `test_tags.py`/
`test_sections.py`: на red-чекпоинте `disputatio.context.author` ещё не
существует, а импорт на уровне модуля сорвал бы collection всего каталога —
гейт увидел бы `error` вместо честного assertion'а.

Что здесь действительно пинится:

* REQ-001 — холодный старт: раунд 1 без единого `prior_*` собирается без
  исключения, задача пользователя цитируется дословно, ни одной
  необязательной секции нет;
* REQ-002 — в промпт попадают ТОЛЬКО issues из `decision.open_issues_carried`,
  отсортированные по severity; issue вне списка не утекает ни одним своим
  полем;
* REQ-003 — из verification берутся только `fail`-гейты (полного отчёта
  ревьюера здесь нет), директива оркестратора идёт отдельной секцией и
  исчезает целиком при `None` (ADR-003);
* REQ-004 — рассинхрон раунда у любого `prior_*` — `ValueError` с именем
  параметра и ожидавшимся раундом; сама сигнатура типами запрещает передать
  историю, списки артефактов или собственные proposal автора (ADR-004);
* REQ-011 — все опциональные входы `None` одновременно на НЕ первом раунде.

Заголовки секций берутся из `sections`/`author`, а не копируются сюда:
их буквальный текст — деталь реализации, тест пинит структуру, а не формулировки.
"""

import importlib
import inspect
from collections.abc import Sequence
from types import ModuleType
from typing import get_args

import pytest

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

TASK_PROMPT = "Почини ретраи HTTP-клиента.\n\n  - учти таймауты\n  - не трогай API\n"


def _load_author() -> ModuleType:
    """Импортирует `disputatio.context.author`; отсутствие — assertion."""
    try:
        return importlib.import_module("disputatio.context.author")
    except ImportError as exc:  # pragma: no cover - только на red-чекпоинте
        raise AssertionError(
            f"disputatio.context.author не импортируется: {exc}"
        ) from exc


def _load_sections() -> ModuleType:
    """Импортирует `disputatio.context.sections`; отсутствие — assertion."""
    try:
        return importlib.import_module("disputatio.context.sections")
    except ImportError as exc:  # pragma: no cover - только на red-чекпоинте
        raise AssertionError(
            f"disputatio.context.sections не импортируется: {exc}"
        ) from exc


def _task(prompt: str = TASK_PROMPT, mode: Mode = Mode.DEVELOP) -> TaskSpec:
    """Задача пользователя; по умолчанию — develop с многострочным prompt."""
    return TaskSpec(prompt=prompt, mode=mode)


def _issue(
    issue_id: str,
    severity: Severity,
    *,
    claim: str = "претензия",
    evidence: str = "улика",
    file: str = "src/x.py",
    line_hint: int | None = None,
) -> Issue:
    """Issue с осмысленными дефолтами — в тестах задаётся только значимое."""
    return Issue(
        id=issue_id,
        severity=severity,
        file=file,
        line_hint=line_hint,
        claim=claim,
        evidence=evidence,
    )


def _review(round_no: int, issues: Sequence[Issue] = ()) -> Review:
    """`review.json` прошлого раунда с вердиктом `request_changes`."""
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.8,
        issues=list(issues),
        checked=["src/x.py"],
        summary="нужны правки",
    )


def _decision(
    round_no: int,
    *,
    carried: Sequence[str] = (),
    directive: str | None = None,
) -> Decision:
    """`decision.json` прошлого раунда с исходом `continue`."""
    return Decision(
        round=round_no,
        outcome=Outcome.CONTINUE,
        reason="continue_revise_cycle",
        open_issues_carried=list(carried),
        next_round_directive=directive,
    )


def _verification(
    round_no: int, gates: Sequence[GateResult] = ()
) -> VerificationReport:
    """`verification.json` прошлого раунда; `overall` — `fail`."""
    return VerificationReport(
        round=round_no,
        gates=list(gates),
        overall=OverallStatus.FAIL,
        diff_stats=DiffStats(files=2, insertions=30, deletions=4),
    )


def _optional_titles() -> list[str]:
    """Заголовки всех секций, которых не должно быть в минимальном промпте."""
    sections = _load_sections()
    return [
        sections.OPEN_ISSUES_TITLE,
        sections.RESOLVED_ISSUES_TITLE,
        sections.FAILED_GATES_TITLE,
        sections.DIRECTIVE_TITLE,
        sections.VERIFICATION_TITLE,
    ]


def test_cold_start_round_one_renders_task_only() -> None:
    """REQ-001: раунд 1 без `prior_*` — задача дословно, секций нет."""
    author = _load_author()
    task = _task()

    prompt = author.build_author_prompt(
        task=task,
        round=1,
        prior_review=None,
        prior_verification=None,
        prior_decision=None,
    )

    assert author.TASK_TITLE in prompt
    # Дословно — вместе с пустой строкой, отступами и хвостовым переводом.
    assert task.prompt in prompt
    for title in _optional_titles():
        assert title not in prompt


def test_all_optional_inputs_none_on_later_round() -> None:
    """REQ-011: `None` во всех опциональных входах не первого раунда — не ошибка."""
    author = _load_author()

    prompt = author.build_author_prompt(task=_task(), round=3)

    assert _task().prompt in prompt
    for title in _optional_titles():
        assert title not in prompt


def test_only_carried_issues_reach_prompt_sorted_by_severity() -> None:
    """REQ-002: только `open_issues_carried`, порядок blocker→major→nit."""
    author = _load_author()
    sections = _load_sections()
    dropped = _issue(
        "R1-9",
        Severity.MAJOR,
        claim="замечание снято автором",
        evidence="строка 7 переписана",
        file="src/dropped.py",
    )
    review = _review(
        1,
        [
            _issue("R1-1", Severity.MAJOR),
            _issue("R1-2", Severity.BLOCKER),
            dropped,
            _issue("R1-3", Severity.NIT),
        ],
    )
    decision = _decision(1, carried=["R1-1", "R1-2", "R1-3"])

    prompt = author.build_author_prompt(
        task=_task(), round=2, prior_review=review, prior_decision=decision
    )

    assert sections.OPEN_ISSUES_TITLE in prompt
    assert sections.RESOLVED_ISSUES_TITLE not in prompt
    assert prompt.index("R1-2") < prompt.index("R1-1") < prompt.index("R1-3")
    # Снятый issue не утекает ни одним своим полем — ни id, ни текстом.
    assert dropped.id not in prompt
    assert dropped.claim not in prompt
    assert dropped.evidence not in prompt
    assert dropped.file not in prompt


def test_carried_issue_fields_are_rendered() -> None:
    """REQ-002: у включённого issue видны claim, file, line_hint и evidence."""
    author = _load_author()
    issue = _issue(
        "R2-1",
        Severity.BLOCKER,
        claim="retry не сбрасывает backoff",
        evidence="в логе backoff растёт после успешного ответа",
        file="src/client.py",
        line_hint=42,
    )

    prompt = author.build_author_prompt(
        task=_task(),
        round=3,
        prior_review=_review(2, [issue]),
        prior_decision=_decision(2, carried=["R2-1"]),
    )

    assert issue.claim in prompt
    assert issue.file in prompt
    assert issue.evidence in prompt
    # «42» больше неоткуда взяться: ни в claim, ни в evidence, ни в задаче.
    assert "42" in prompt


def test_only_failed_gates_reach_prompt() -> None:
    """REQ-003: из verification берутся только `fail`; отчёта целиком нет."""
    author = _load_author()
    sections = _load_sections()
    failed = GateResult(
        name="pytest",
        cmd="uv run pytest -q",
        status=GateStatus.FAIL,
        exit_code=1,
        tail="FAILED tests/test_client.py::test_backoff\n1 failed, 12 passed",
    )
    passed = GateResult(
        name="lint-ruff", cmd="ruff check .", status=GateStatus.PASS, exit_code=0
    )
    skipped = GateResult(
        name="typecheck-pyrefly",
        cmd="pyrefly check",
        status=GateStatus.SKIP,
        reason="not configured",
    )

    prompt = author.build_author_prompt(
        task=_task(),
        round=2,
        prior_verification=_verification(1, [failed, passed, skipped]),
    )

    assert sections.FAILED_GATES_TITLE in prompt
    assert sections.VERIFICATION_TITLE not in prompt
    assert failed.tail in prompt
    assert passed.name not in prompt
    assert skipped.name not in prompt
    assert skipped.reason not in prompt


def test_directive_is_rendered_verbatim_in_its_own_section() -> None:
    """REQ-003: `next_round_directive` — отдельная секция, текст дословно."""
    author = _load_author()
    sections = _load_sections()
    directive = "Сначала почини гейт pytest, только потом трогай публичный API."

    prompt = author.build_author_prompt(
        task=_task(),
        round=2,
        prior_decision=_decision(1, directive=directive),
    )

    assert sections.DIRECTIVE_TITLE in prompt
    assert directive in prompt


def test_absent_directive_removes_the_whole_section() -> None:
    """REQ-003: `next_round_directive is None` — ни секции, ни пустого заголовка."""
    author = _load_author()
    sections = _load_sections()

    prompt = author.build_author_prompt(
        task=_task(),
        round=2,
        prior_decision=_decision(1, carried=["R1-1"], directive=None),
    )

    assert sections.DIRECTIVE_TITLE not in prompt


@pytest.mark.parametrize(
    "param", ["prior_review", "prior_verification", "prior_decision"]
)
def test_prior_artifact_from_wrong_round_is_rejected(param: str) -> None:
    """REQ-004: `prior_*.round != round - 1` — `ValueError` с параметром и раундом."""
    author = _load_author()
    stale: dict[str, object] = {
        "prior_review": _review(1),
        "prior_verification": _verification(1),
        "prior_decision": _decision(1),
    }

    with pytest.raises(ValueError) as excinfo:
        author.build_author_prompt(task=_task(), round=5, **{param: stale[param]})

    message = str(excinfo.value)
    assert param in message
    # Ожидавшийся раунд назван явно: 5 - 1 == 4.
    assert "4" in message


@pytest.mark.parametrize(
    "param", ["prior_review", "prior_verification", "prior_decision"]
)
def test_round_one_rejects_any_prior_artifact(param: str) -> None:
    """ADR-002: холодный старт — не ветка, а `round - 1 == 0` при схемном `ge=1`."""
    author = _load_author()
    stale: dict[str, object] = {
        "prior_review": _review(1),
        "prior_verification": _verification(1),
        "prior_decision": _decision(1),
    }

    with pytest.raises(ValueError):
        author.build_author_prompt(task=_task(), round=1, **{param: stale[param]})


def test_signature_forbids_history_and_own_proposals() -> None:
    """REQ-004: типы запрещают историю, списки артефактов и свои proposal."""
    author = _load_author()
    params = inspect.signature(author.build_author_prompt).parameters

    assert set(params) == {
        "task",
        "round",
        "prior_review",
        "prior_verification",
        "prior_decision",
    }
    assert params["task"].annotation is TaskSpec
    assert params["round"].annotation is int
    # Единичная модель или None — `list[Review] | None` дал бы другие get_args.
    assert get_args(params["prior_review"].annotation) == (Review, type(None))
    assert get_args(params["prior_verification"].annotation) == (
        VerificationReport,
        type(None),
    )
    assert get_args(params["prior_decision"].annotation) == (Decision, type(None))
