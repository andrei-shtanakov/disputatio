"""Тесты `reviewer.py` — промпт ревьюера раунда N: TASK-005, [DESIGN-006].

Модуль загружается лениво (`_module`), как в `test_author.py`: на
red-чекпоинте `disputatio.context.reviewer` ещё не существует, а импорт на
уровне модуля сорвал бы collection всего каталога — гейт увидел бы `error`
вместо честного assertion'а.

Чем промпт ревьюера отличается от промпта автора и что здесь пинится:

* REQ-005 — ревьюер получает ПУТИ к `proposal.md`/`changes.patch`, а не их
  содержимое: он read-only (§7) и читает файлы сам, поэтому в сигнатуре нет
  и не может появиться параметра под текст proposal или диффа;
* REQ-006 — `verification.json` отдаётся целиком и обязательным аргументом:
  и `pass`, и `skip` с его `reason`, плюс `overall` и `diff_stats`. Шаг
  `VERIFYING` всегда предшествует `REVIEWING`, поэтому `None` здесь —
  нелегальное состояние, а не «данных пока нет»;
* REQ-007 — из прошлого раунда приходит ДОПОЛНЕНИЕ `open_issues_carried`:
  замечания, которые автор заявил решёнными. Оставшиеся открытыми ревьюеру
  не показываются — он и так смотрит на новый код;
* REQ-008 — статический блок §4.4 и требование `checked` стоят в промпте
  при любых валидных входах, включая минимальный;
* REQ-009 — диалоговая история автора не передаётся; это выражено типами
  сигнатуры, а не комментарием (ADR-004);
* REQ-011 — отсутствие артефактов прошлого раунда не рендерит пустых
  заголовков и не поднимает исключений (ADR-003).

Заголовки секций берутся из `sections`, а не копируются сюда: их буквальный
текст — деталь реализации, тест пинит структуру, а не формулировки.
"""

import importlib
import inspect
from collections.abc import Callable, Sequence
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

TASK_PROMPT = "Почини ретраи HTTP-клиента."
PROPOSAL_PATH = ".disputatio/rounds/003/proposal.md"
PATCH_PATH = ".disputatio/rounds/003/changes.patch"

# Имена, за которыми в промпт просочился бы свободный текст прошлых ответов
# автора или содержимое артефактов вместо путей к ним (REQ-005, REQ-009).
FORBIDDEN_PARAM_TOKENS = (
    "message",
    "history",
    "transcript",
    "dialog",
    "chat",
    "content",
    "text",
    "body",
    "diff",
    "source",
)


def _module(name: str) -> ModuleType:
    """Импортирует модуль пакета `context`; отсутствие — assertion."""
    try:
        return importlib.import_module(f"disputatio.context.{name}")
    except ImportError as exc:  # pragma: no cover - только на red-чекпоинте
        raise AssertionError(
            f"disputatio.context.{name} не импортируется: {exc}"
        ) from exc


def _task(prompt: str = TASK_PROMPT, mode: Mode = Mode.DEVELOP) -> TaskSpec:
    """Задача пользователя; по умолчанию — develop."""
    return TaskSpec(prompt=prompt, mode=mode)


def _issue(
    issue_id: str,
    severity: Severity = Severity.MAJOR,
    *,
    claim: str = "претензия",
    evidence: str = "улика",
    file: str = "src/x.py",
) -> Issue:
    """Issue с осмысленными дефолтами — в тестах задаётся только значимое."""
    return Issue(
        id=issue_id,
        severity=severity,
        file=file,
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
    round_no: int,
    gates: Sequence[GateResult] = (),
    *,
    overall: OverallStatus = OverallStatus.FAIL,
) -> VerificationReport:
    """`verification.json` ТЕКУЩЕГО раунда; числа `diff_stats` уникальны."""
    return VerificationReport(
        round=round_no,
        gates=list(gates),
        overall=overall,
        diff_stats=DiffStats(files=7, insertions=31, deletions=13),
    )


def _minimal_kwargs() -> dict[str, object]:
    """Раунд 1: только обязательные аргументы, ни одного `prior_*`."""
    return {
        "task": _task(),
        "round": 1,
        "proposal_path": PROPOSAL_PATH,
        "patch_path": PATCH_PATH,
        "verification": _verification(1, overall=OverallStatus.PASS),
    }


def _full_kwargs() -> dict[str, object]:
    """Раунд 4: verification раунда 4 и артефакты раунда 3 — все секции есть."""
    return {
        "task": _task(),
        "round": 4,
        "proposal_path": PROPOSAL_PATH,
        "patch_path": PATCH_PATH,
        "verification": _verification(
            4,
            [
                GateResult(
                    name="pytest",
                    cmd="uv run pytest -q",
                    status=GateStatus.FAIL,
                    exit_code=1,
                    tail="FAILED tests/test_client.py::test_backoff",
                )
            ],
        ),
        "prior_review": _review(3, [_issue("R3-1", Severity.BLOCKER)]),
        "prior_decision": _decision(3, carried=[]),
    }


def _inside_artifact_block(prompt: str, needle: str) -> bool:
    """Находится ли первое вхождение `needle` между метками artifact-данных.

    Проверка по позициям, а не по эвристике: внутри блока число открытых
    меток слева строго больше числа закрытых.
    """
    tags = _module("tags")
    before = prompt[: prompt.index(needle)]
    return before.count(tags._OPEN_TAG) > before.count(tags._CLOSE_TAG)


def test_artifact_paths_are_rendered_verbatim() -> None:
    """REQ-005: ревьюер получает пути к артефактам раунда, а не их содержимое."""
    reviewer = _module("reviewer")

    prompt = reviewer.build_reviewer_prompt(
        task=_task(),
        round=3,
        proposal_path=PROPOSAL_PATH,
        patch_path=PATCH_PATH,
        verification=_verification(3),
    )

    assert PROPOSAL_PATH in prompt
    assert PATCH_PATH in prompt


def test_artifact_paths_stay_outside_artifact_tags() -> None:
    """Пути — статический текст оркестратора, а не данные агента (§6.3)."""
    reviewer = _module("reviewer")

    prompt = reviewer.build_reviewer_prompt(**_minimal_kwargs())

    assert not _inside_artifact_block(prompt, PROPOSAL_PATH)
    assert not _inside_artifact_block(prompt, PATCH_PATH)
    # Текст пользователя, наоборот, обязан быть внутри меток.
    assert _inside_artifact_block(prompt, TASK_PROMPT)


def test_signature_carries_paths_not_artifact_content() -> None:
    """REQ-005: в сигнатуре пути (`str`), а `verification` — обязательная модель."""
    reviewer = _module("reviewer")
    params = inspect.signature(reviewer.build_reviewer_prompt).parameters

    assert set(params) == {
        "task",
        "round",
        "proposal_path",
        "patch_path",
        "verification",
        "prior_review",
        "prior_decision",
    }
    assert params["proposal_path"].annotation is str
    assert params["patch_path"].annotation is str
    # `VERIFYING` всегда предшествует `REVIEWING`: `None` — нелегальное
    # состояние, поэтому ни Optional, ни значения по умолчанию тут нет.
    assert params["verification"].annotation is VerificationReport
    assert params["verification"].default is inspect.Parameter.empty
    assert get_args(params["prior_review"].annotation) == (Review, type(None))
    assert get_args(params["prior_decision"].annotation) == (Decision, type(None))


def test_signature_admits_no_free_text_of_author_answers() -> None:
    """REQ-009: диалоговая история автора не передаётся — это запрет типов."""
    reviewer = _module("reviewer")
    params = inspect.signature(reviewer.build_reviewer_prompt).parameters

    for name in params:
        for token in FORBIDDEN_PARAM_TOKENS:
            assert token not in name, f"параметр {name!r} протаскивает {token!r}"


def test_full_verification_report_reaches_the_reviewer() -> None:
    """REQ-006: все гейты независимо от статуса, `reason` у skip, overall, diff."""
    reviewer = _module("reviewer")
    sections = _module("sections")
    failed = GateResult(
        name="pytest",
        cmd="uv run pytest -q",
        status=GateStatus.FAIL,
        exit_code=1,
        tail="FAILED tests/test_client.py::test_backoff",
    )
    passed = GateResult(
        name="lint-ruff", cmd="ruff check .", status=GateStatus.PASS, exit_code=0
    )
    skipped = GateResult(
        name="typecheck-pyrefly",
        cmd="pyrefly check",
        status=GateStatus.SKIP,
        reason="pyrefly не сконфигурирован",
    )
    verification = _verification(3, [failed, passed, skipped])

    prompt = reviewer.build_reviewer_prompt(
        task=_task(),
        round=3,
        proposal_path=PROPOSAL_PATH,
        patch_path=PATCH_PATH,
        verification=verification,
    )

    assert sections.VERIFICATION_TITLE in prompt
    # Отчёт вставлен целиком, а не пересказан своими словами.
    assert sections.render_verification_section(verification) in prompt
    for gate in (failed, passed, skipped):
        assert gate.name in prompt
    assert skipped.reason in prompt
    assert failed.tail in prompt
    assert OverallStatus.FAIL.value in prompt
    # diff_stats: files=7, insertions=31, deletions=13 — больше нигде не встречаются.
    for number in ("7", "31", "13"):
        assert number in prompt


def test_only_issues_claimed_resolved_reach_the_reviewer() -> None:
    """REQ-007: в промпте дополнение `open_issues_carried`, а не сам список."""
    reviewer = _module("reviewer")
    sections = _module("sections")
    resolved_first = _issue("R2-1", Severity.BLOCKER, file="src/first.py")
    still_open = _issue(
        "R2-2",
        Severity.MAJOR,
        claim="утечка соединения в пуле",
        evidence="в логе растёт число сокетов",
        file="src/pool.py",
    )
    resolved_last = _issue("R2-3", Severity.NIT, file="src/last.py")

    prompt = reviewer.build_reviewer_prompt(
        task=_task(),
        round=3,
        proposal_path=PROPOSAL_PATH,
        patch_path=PATCH_PATH,
        verification=_verification(3),
        prior_review=_review(2, [resolved_first, still_open, resolved_last]),
        prior_decision=_decision(2, carried=["R2-2"]),
    )

    assert sections.RESOLVED_ISSUES_TITLE in prompt
    assert sections.OPEN_ISSUES_TITLE not in prompt
    assert "R2-1" in prompt
    assert "R2-3" in prompt
    # Оставшееся открытым замечание не утекает ни одним своим полем: ревьюер
    # смотрит на новый код, а не перечитывает то, что автор ещё не трогал.
    assert still_open.id not in prompt
    assert still_open.claim not in prompt
    assert still_open.evidence not in prompt
    assert still_open.file not in prompt


def test_resolved_issue_fields_are_rendered_unchanged() -> None:
    """REQ-007: `claim`/`file`/`evidence` доходят до ревьюера дословно."""
    reviewer = _module("reviewer")
    issue = _issue(
        "R2-1",
        Severity.BLOCKER,
        claim="retry не сбрасывает backoff",
        evidence="в логе backoff растёт после успешного ответа",
        file="src/client.py",
    )

    prompt = reviewer.build_reviewer_prompt(
        task=_task(),
        round=3,
        proposal_path=PROPOSAL_PATH,
        patch_path=PATCH_PATH,
        verification=_verification(3),
        prior_review=_review(2, [issue]),
        prior_decision=_decision(2, carried=[]),
    )

    assert issue.claim in prompt
    assert issue.file in prompt
    assert issue.evidence in prompt


@pytest.mark.parametrize(
    ("prior_review", "prior_decision"),
    [
        (None, None),
        (None, _decision(2, carried=[])),
        (_review(2, [_issue("R2-1")]), None),
    ],
    ids=["neither", "decision-only", "review-only"],
)
def test_missing_prior_artifacts_remove_the_resolved_section(
    prior_review: Review | None, prior_decision: Decision | None
) -> None:
    """REQ-007/011: без обоих артефактов прошлого раунда секции нет, ошибки нет."""
    reviewer = _module("reviewer")
    sections = _module("sections")

    prompt = reviewer.build_reviewer_prompt(
        task=_task(),
        round=3,
        proposal_path=PROPOSAL_PATH,
        patch_path=PATCH_PATH,
        verification=_verification(3),
        prior_review=prior_review,
        prior_decision=prior_decision,
    )

    assert sections.RESOLVED_ISSUES_TITLE not in prompt
    # Какие замечания автор заявил решёнными, известно только из пары
    # «ревью + решение оркестратора»: половины пары недостаточно.
    assert "R2-1" not in prompt


@pytest.mark.parametrize(
    "make_kwargs", [_minimal_kwargs, _full_kwargs], ids=["minimal", "full"]
)
def test_schema_requirements_present_for_any_valid_input(
    make_kwargs: Callable[[], dict[str, object]],
) -> None:
    """REQ-008: блок §4.4 и требование `checked` — при любых валидных входах."""
    reviewer = _module("reviewer")
    schema_rules = _module("schema_rules")

    prompt = reviewer.build_reviewer_prompt(**make_kwargs())

    assert schema_rules.REVIEW_SCHEMA_REQUIREMENTS in prompt
    assert "checked" in prompt


def test_verification_from_another_round_is_rejected() -> None:
    """`verification.round != round` — `ValueError` с параметром и раундом."""
    reviewer = _module("reviewer")

    with pytest.raises(ValueError) as excinfo:
        reviewer.build_reviewer_prompt(
            task=_task(),
            round=5,
            proposal_path=PROPOSAL_PATH,
            patch_path=PATCH_PATH,
            verification=_verification(4),
        )

    message = str(excinfo.value)
    assert "verification" in message
    assert "5" in message


@pytest.mark.parametrize("param", ["prior_review", "prior_decision"])
def test_prior_artifact_from_wrong_round_is_rejected(param: str) -> None:
    """`prior_*.round != round - 1` — `ValueError` с параметром и раундом."""
    reviewer = _module("reviewer")
    stale: dict[str, object] = {
        "prior_review": _review(1),
        "prior_decision": _decision(1),
    }

    with pytest.raises(ValueError) as excinfo:
        reviewer.build_reviewer_prompt(
            task=_task(),
            round=5,
            proposal_path=PROPOSAL_PATH,
            patch_path=PATCH_PATH,
            verification=_verification(5),
            **{param: stale[param]},
        )

    message = str(excinfo.value)
    assert param in message
    # Ожидавшийся раунд назван явно: 5 - 1 == 4.
    assert "4" in message


def test_sections_follow_the_order_of_spec_6_2() -> None:
    """Порядок §6.2: задача → пути → verification → решённые → схема вывода."""
    reviewer = _module("reviewer")
    sections = _module("sections")
    schema_rules = _module("schema_rules")

    prompt = reviewer.build_reviewer_prompt(**_full_kwargs())

    assert (
        prompt.index(sections.TASK_TITLE)
        < prompt.index(PROPOSAL_PATH)
        < prompt.index(sections.VERIFICATION_TITLE)
        < prompt.index(sections.RESOLVED_ISSUES_TITLE)
        < prompt.index(schema_rules.REVIEW_SCHEMA_REQUIREMENTS)
    )


def test_prompt_names_the_current_round_and_task_mode() -> None:
    """Ревьюер обязан знать раунд и режим задачи: `analyze` меняет ожидания."""
    reviewer = _module("reviewer")

    prompt = reviewer.build_reviewer_prompt(
        task=_task(mode=Mode.ANALYZE),
        round=7,
        proposal_path=PROPOSAL_PATH,
        patch_path=PATCH_PATH,
        verification=_verification(7),
    )

    assert "7" in prompt
    assert Mode.ANALYZE.value in prompt


def test_absent_sections_leave_no_blank_gap() -> None:
    """Пустая секция выпадает из склейки, а не превращается в дыру из \\n."""
    reviewer = _module("reviewer")

    minimal = reviewer.build_reviewer_prompt(**_minimal_kwargs())
    full = reviewer.build_reviewer_prompt(**_full_kwargs())

    assert "\n\n\n" not in minimal
    assert "\n\n\n" not in full
    # Пустая строка — граница секций, а не украшение внутри одной.
    assert "\n\n" in minimal
