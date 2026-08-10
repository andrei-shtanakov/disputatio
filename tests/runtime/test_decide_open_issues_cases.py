"""Regression к DISP-DEF-001: как `decide()` формирует `open_issues_carried`.

Дефект: исходящее множество приравнивалось входящему
(`open_issues_carried = tuple(i.id for i in inputs.carried_issues)`), из-за чего
множество никогда не пополнялось и сходилось в неподвижную точку на нуле —
раунд 1 приходит без прошлого, значит пусто, и дальше пусто на каждом раунде.
Обнаружено интеграционным E2E TASK-025 (REQ-025: манифест эскалированной сессии
обязан нести непустой список открытых замечаний), исправлено операторски
санкционированной cross-scope правкой `src/disputatio/core/deciding.py` внутри
`421ce8c`/`d608c10`.

Юнит-тесты волны 1 дефект не ловили: они подавали `carried_issues` напрямую,
то есть проверяли функцию на искусственно корректном входе, но не происхождение
этого входа. Цепочка происхождения покрыта отдельно — `test_carry_chain.py`.

Случай «замечание разрешено» здесь выражен так, как его видит `decide()`:
разрешённое не приходит во входящем множестве и не названо ревью этого раунда.
Само вычёркивание живёт слоем ниже (`history.carried_issues` пересекает список
id с ревью раунда) и проверяется в интеграционном тесте.
"""

from __future__ import annotations

from disputatio.contracts import (
    BudgetUsed,
    DiffStats,
    GateResult,
    GateStatus,
    Issue,
    Limits,
    Mode,
    OverallStatus,
    Review,
    Role,
    Severity,
    Verdict,
    VerificationReport,
)
from disputatio.core.deciding import DecidingInputs, decide

_CARRIED = Issue(
    id="R1-1", severity=Severity.BLOCKER, file="a.py", claim="упало на границе"
)
_FRESH_MAJOR = Issue(
    id="R2-1", severity=Severity.MAJOR, file="b.py", claim="потерянная ветка"
)
_FRESH_MINOR = Issue(
    id="R2-2", severity=Severity.MINOR, file="c.py", claim="имя переменной"
)


def _review(*, verdict: Verdict, issues: list[Issue]) -> Review:
    return Review(
        round=2,
        role=Role.REVIEWER,
        verdict=verdict,
        confidence=0.9,
        issues=issues,
        checked=["a.py"],
        summary="итог раунда",
    )


def _verification() -> VerificationReport:
    return VerificationReport(
        round=2,
        gates=[GateResult(name="tests", cmd="pytest -q", status=GateStatus.PASS)],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=1, deletions=0),
    )


def _inputs(
    *, carried: tuple[Issue, ...], review_issues: list[Issue], verdict: Verdict
) -> DecidingInputs:
    return DecidingInputs(
        round=2,
        mode=Mode.DEVELOP,
        review=_review(verdict=verdict, issues=review_issues),
        verification=_verification(),
        carried_issues=carried,
        patch_current="diff --git a/a.py b/a.py\n",
        patch_two_back=None,
        issue_history={},
        budget_used=BudgetUsed(),
        limits=Limits(
            max_rounds=8,
            max_total_tokens=1_000_000,
            max_wall_seconds=3600,
            schema_retries=2,
        ),
    )


def test_a_fresh_significant_issue_is_admitted_into_the_carried_set() -> None:
    """Новое `major`-замечание раунда попадает в исходящее множество.

    Ровно этого не делал дефектный код: без впуска новых замечаний множество
    оставалось пустым навсегда.
    """
    draft = decide(
        _inputs(
            carried=(),
            review_issues=[_FRESH_MAJOR],
            verdict=Verdict.REQUEST_CHANGES,
        )
    )

    assert draft.open_issues_carried == ("R2-1",)


def test_an_already_carried_issue_stays_and_is_not_duplicated() -> None:
    """Перенесённое и всё ещё открытое остаётся ровно один раз.

    Ревьюер называет то же замечание снова — повтор id сделал бы список
    растущим на каждом раунде и испортил бы и манифест, и промпт автора.
    """
    draft = decide(
        _inputs(
            carried=(_CARRIED,),
            review_issues=[_CARRIED, _FRESH_MAJOR],
            verdict=Verdict.REQUEST_CHANGES,
        )
    )

    assert draft.open_issues_carried == ("R1-1", "R2-1")


def test_a_resolved_issue_is_absent_from_the_carried_set() -> None:
    """Разрешённое не приходит входящим и не названо ревью — значит не выходит.

    `decide()` вычёркиванием не занимается: он не изобретает id, которых ему
    не дали. Само вычёркивание — в `history.carried_issues`.
    """
    draft = decide(
        _inputs(
            carried=(),
            review_issues=[_FRESH_MAJOR],
            verdict=Verdict.REQUEST_CHANGES,
        )
    )

    assert "R1-1" not in draft.open_issues_carried


def test_a_clean_approve_carries_nothing() -> None:
    """Approve без замечаний — пустое множество, а не пустота по недосмотру.

    Отличать «нечего переносить» от «перенос сломан» иначе нечем: обе
    ситуации выглядят как `()`, и без этого случая починка дефекта могла бы
    начать переносить несуществующее.
    """
    draft = decide(_inputs(carried=(), review_issues=[], verdict=Verdict.APPROVE))

    assert draft.open_issues_carried == ()


def test_a_fresh_minor_issue_is_not_admitted() -> None:
    """Порог существенности: свежий `minor` открытым не объявляется.

    Порог несущий, а не косметический — замечание, которого никто не обязан
    закрывать, попав в множество, крутило бы цикл и врало бы в манифесте.
    """
    draft = decide(
        _inputs(
            carried=(),
            review_issues=[_FRESH_MINOR],
            verdict=Verdict.REQUEST_CHANGES,
        )
    )

    assert draft.open_issues_carried == ()
