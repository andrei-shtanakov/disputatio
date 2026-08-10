"""Regression к DISP-DEF-001: цепочка переноса замечаний между раундами.

Дефект жил не в одной функции, а в шве трёх слоёв, и потому не ловился ничем:
`history.carried_issues` читает открытое ПОСЛЕ раунда, `steps` подаёт это в
`decide` как открытое ДО раунда, а `decide` приравнивал исходящее множество
входящему. Замкнутый круг на нуле: раунд 1 приходит без прошлого — пусто, и
дальше пусто на каждом раунде, сколько бы замечаний ревьюер ни выставил.
Следствие шире REQ-025: промпты автора и ревьюера тоже всегда получали пустой
список, то есть дебат-цикл не переносил вперёд ничего.

Юнит-тесты каждого слоя оставались зелёными, потому что подавали корректный
вход руками. Здесь проверяется ПРОИСХОЖДЕНИЕ этого входа — по кругу, теми же
вызовами, которыми ходит рантайм (`steps.py` подаёт `carried_issues(root, N-1)`,
`exporting.py` пересекает `carried_issues(root, N)`).

Покрывает четыре звена цепочки: замечание появляется в ревью раунда 1 → попадает
в `decision.open_issues_carried` → доступно автору и ревьюеру раунда 2 →
исчезает после разрешения.
"""

from __future__ import annotations

from pathlib import Path

from disputatio.context.sections import select_open_issues
from disputatio.contracts import (
    BudgetUsed,
    Decision,
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
from disputatio.events import write_round_artifact
from disputatio.runtime.history import carried_issues
from disputatio.runtime.layout import DECISION_NAME, REVIEW_NAME

_BUG = Issue(
    id="R1-1", severity=Severity.BLOCKER, file="a.py", claim="падает на границе"
)
_SECOND = Issue(
    id="R2-1", severity=Severity.MAJOR, file="b.py", claim="потерянная ветка"
)


def _review(round_no: int, issues: list[Issue]) -> Review:
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES if issues else Verdict.APPROVE,
        confidence=0.9,
        issues=issues,
        checked=["a.py"],
        summary=f"итог раунда {round_no}",
    )


def _verification(round_no: int) -> VerificationReport:
    return VerificationReport(
        round=round_no,
        gates=[GateResult(name="tests", cmd="pytest -q", status=GateStatus.PASS)],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=1, deletions=0),
    )


def _decide_round(root: Path, round_no: int, review: Review) -> Decision:
    """Проводит раунд ровно так, как это делает рантайм, и пишет артефакты.

    Вход `carried_issues(root, round_no - 1)` — дословно `steps.py`: именно
    этот вызов делал вход пустым навсегда, и подменять его здесь фикстурой
    значило бы снова проверять функцию, а не шов.
    """
    draft = decide(
        DecidingInputs(
            round=round_no,
            mode=Mode.DEVELOP,
            review=review,
            verification=_verification(round_no),
            carried_issues=carried_issues(root, round_no - 1),
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
    )
    decision = Decision(
        round=round_no,
        outcome=draft.outcome,
        reason=draft.reason,
        open_issues_carried=list(draft.open_issues_carried),
        next_round_directive=draft.next_round_directive,
    )
    write_round_artifact(
        root, round_no, REVIEW_NAME, review.model_dump_json(by_alias=True)
    )
    write_round_artifact(
        root, round_no, DECISION_NAME, decision.model_dump_json(by_alias=True)
    )
    return decision


def test_an_issue_is_carried_across_rounds_and_drops_when_resolved(
    tmp_path: Path,
) -> None:
    """Полный круг: появилось → перенеслось → доступно промптам → исчезло."""
    root = tmp_path

    # Звено 1: замечание названо ревью раунда 1 и объявлено открытым.
    decision_one = _decide_round(root, 1, _review(1, [_BUG]))
    assert decision_one.open_issues_carried == ["R1-1"], (
        "решение раунда 1 обязано объявить свежее замечание открытым; пустой "
        "список здесь — та самая неподвижная точка DISP-DEF-001"
    )

    # Звено 2: история отдаёт его следующему раунду — тем же вызовом, что steps.
    assert [issue.id for issue in carried_issues(root, 1)] == ["R1-1"]

    # Звено 3: раунд 2 видит старое и добавляет новое, не дублируя.
    decision_two = _decide_round(root, 2, _review(2, [_BUG, _SECOND]))
    assert decision_two.open_issues_carried == ["R1-1", "R2-1"]

    # Звено 3b: то же множество доступно промптам автора и ревьюера раунда 3.
    for_prompt = select_open_issues(
        [_BUG, _SECOND], decision_two.open_issues_carried
    )
    assert [issue.id for issue in for_prompt] == ["R1-1", "R2-1"], (
        "сборка контекста читает open_issues_carried решения предыдущего "
        "раунда; пустой список оставлял бы автора без единого замечания"
    )

    # Звено 4: раунд 3 больше не называет R1-1 — оно вычёркивается историей.
    _decide_round(root, 3, _review(3, [_SECOND]))
    assert [issue.id for issue in carried_issues(root, 3)] == ["R2-1"], (
        "разрешённое замечание обязано выпасть из переносимого множества, "
        "иначе оно тянулось бы до конца сессии"
    )
