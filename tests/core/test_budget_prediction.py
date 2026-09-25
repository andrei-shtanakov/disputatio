"""Прогноз бюджета перед следующим раундом: SPEC-001 §5.2.

Прогноз — последнее условие `decide()`: он заменяет только исход,
который иначе был бы `CONTINUE` (включая принудительный цикл
анти-сикофантии §5.1), и не перебивает ни одну уже найденную причину
остановки. Наблюдение стоимости раунда k — разность снимков k и k−1;
снимок текущего раунда — текущий `budget_used`, снимок «раунда 0» — ноль,
но только у сессии с `tracked_from_start`. Отсутствующий снимок нулём не
заменяется и через пропуск разность не берётся; токенное наблюдение
дополнительно требует, чтобы `unreported_turns` за раунд не вырос.
"""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from disputatio.contracts import (
    BudgetSnapshot,
    BudgetUsed,
    Limits,
    Mode,
    Outcome,
    OverallStatus,
    Verdict,
)
from disputatio.core import (
    REASON_ANTI_SYCOPHANCY,
    REASON_BUDGET_PREDICTED,
    REASON_BUDGET_TOKENS,
    REASON_CONTINUE,
    REASON_CONVERGED,
    REASON_MAX_ROUNDS,
    REASON_OSCILLATION_DIFF,
    REASON_VERIFICATION_INDETERMINATE,
    DecidingInputs,
    decide,
)

from .test_deciding import make_inputs, make_review, make_verification

_TOKENS_LIMIT = 1_000
_WALL_LIMIT = 100


def _snap(tokens: int, wall: float, unreported: int = 0) -> BudgetSnapshot:
    return BudgetSnapshot(tokens=tokens, wall_seconds=wall, unreported_turns=unreported)


def _used(
    tokens: int, wall: float, unreported: int = 0, *, tracked: bool = True
) -> BudgetUsed:
    return BudgetUsed(
        tokens=tokens,
        wall_seconds=wall,
        unreported_turns=unreported,
        tracked_from_start=tracked,
    )


def _limits(max_rounds: int = 8) -> Limits:
    return Limits(
        max_rounds=max_rounds,
        max_total_tokens=_TOKENS_LIMIT,
        max_wall_seconds=_WALL_LIMIT,
        schema_retries=2,
    )


def _continue_inputs(
    *,
    round: int,
    used: BudgetUsed,
    snapshots: Mapping[int, BudgetSnapshot] | None = None,
    **overrides: Any,
) -> DecidingInputs:
    """Раунд, который без прогноза решился бы обычным `CONTINUE`."""
    base: dict[str, Any] = {
        "round": round,
        "review": make_review(verdict=Verdict.REQUEST_CHANGES),
        "budget_used": used,
        "limits": _limits(),
    }
    base.update(overrides)
    inputs: DecidingInputs = make_inputs(**base)
    return replace(inputs, budget_snapshots={} if snapshots is None else snapshots)


def test_reason_budget_predicted_is_pinned() -> None:
    """Код причины — ровно тот, что назван в §4.5 и §5.2."""
    assert REASON_BUDGET_PREDICTED == "budget_hit: predicted"


def test_tokens_prediction_fires_instead_of_continue() -> None:
    """Остаток токенов меньше медианы наблюдений → `BUDGET_HIT` (прогноз).

    Раунды стоили 300, 300 и 350 токенов; использовано 950 из 1000 —
    остаток 50 меньше медианы 300. По секундам остаток велик.
    """
    inputs = _continue_inputs(
        round=3,
        used=_used(950, 30.0),
        snapshots={1: _snap(300, 10.0), 2: _snap(600, 20.0)},
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.BUDGET_HIT
    assert draft.reason == REASON_BUDGET_PREDICTED
    assert draft.next_round_directive is None
    assert draft.forced_review is False


def test_wall_prediction_fires_alone() -> None:
    """Достаточно одного ресурса: прогноз по секундам при запасе токенов."""
    inputs = _continue_inputs(
        round=2,
        used=_used(20, 80.0),
        snapshots={1: _snap(10, 40.0)},
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.BUDGET_HIT
    assert draft.reason == REASON_BUDGET_PREDICTED


def test_remaining_equal_to_median_continues() -> None:
    """Остаток, равный медиане, прогноза не даёт: сравнение строгое.

    Одно наблюдение (без нулевой базы): 250 токенов и 25 секунд; остаток
    ровно столько же по обоим ресурсам.
    """
    inputs = _continue_inputs(
        round=2,
        used=_used(750, 75.0, tracked=False),
        snapshots={1: _snap(500, 50.0)},
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE
    assert draft.reason == REASON_CONTINUE


def test_median_not_mean_decides() -> None:
    """Медиана, а не среднее: 10, 10, 800 → 10; среднее 273 > остатка 180."""
    inputs = _continue_inputs(
        round=3,
        used=_used(820, 3.0),
        snapshots={1: _snap(10, 1.0), 2: _snap(20, 2.0)},
    )

    assert decide(inputs).outcome is Outcome.CONTINUE


def test_no_usable_observations_continue() -> None:
    """Без пригодных наблюдений прогноз не применяется, даже у края лимита."""
    inputs = _continue_inputs(
        round=3,
        used=_used(999, 99.0, tracked=False),
        snapshots={},
    )

    assert decide(inputs).outcome is Outcome.CONTINUE


def test_round_one_untracked_gives_no_observation() -> None:
    """Раунд 1 без `tracked_from_start` — нулевой базы нет, наблюдения нет."""
    inputs = _continue_inputs(round=1, used=_used(900, 90.0, tracked=False))
    inputs = _anti_sycophancy_free(inputs)

    assert decide(inputs).outcome is Outcome.CONTINUE


def test_current_round_counts_when_tracked() -> None:
    """Текущий раунд — наблюдение: прогноз возможен уже в конце раунда 1."""
    inputs = _continue_inputs(round=1, used=_used(600, 10.0, tracked=True))
    inputs = _anti_sycophancy_free(inputs)

    draft = decide(inputs)

    assert draft.outcome is Outcome.BUDGET_HIT
    assert draft.reason == REASON_BUDGET_PREDICTED


def test_missing_middle_snapshot_is_not_skipped_over() -> None:
    """Отсутствующий снимок нулём не заменяется и не перешагивается (§5.2).

    Снимка раунда 2 нет, нулевой базы нет (`tracked_from_start == False`):
    пригодных интервалов нет вовсе → `CONTINUE`. Нуль вместо снимка дал бы
    интервалы −100 и 800 (медиана 350 > остатка 200), перешагивание —
    интервал 1→3 = 700; оба прочтения выдали бы прогноз.
    """
    inputs = _continue_inputs(
        round=3,
        used=_used(800, 2.0, tracked=False),
        snapshots={1: _snap(100, 1.0)},
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE, (
        "интервал через отсутствующий снимок попал в наблюдения"
    )


def test_missing_snapshot_breaks_only_adjacent_intervals() -> None:
    """Пропуск рвёт только два соседних интервала; остальные пригодны.

    Снимка 2 нет; пригодны 0→1 (300) и 3→4 (250) — медиана 275 больше
    остатка 50, прогноз срабатывает.
    """
    inputs = _continue_inputs(
        round=4,
        used=_used(950, 4.0),
        snapshots={1: _snap(300, 1.0), 3: _snap(700, 3.0)},
    )

    assert decide(inputs).reason == REASON_BUDGET_PREDICTED


def test_token_observation_dropped_when_unreported_grew() -> None:
    """Раунд с неотчитавшимся вызовом о токенах не говорит ничего.

    Единственный интервал 1→2 нарастил `unreported_turns`: токенного
    наблюдения нет, и остаток токенов 100 при «стоимости» 400 прогноза не
    даёт. Секундное наблюдение (10) при этом пригодно и тоже прогноза не
    даёт — а при остатке секунд меньше 10 дало бы (второй тест).
    """
    inputs = _continue_inputs(
        round=2,
        used=_used(900, 20.0, unreported=1, tracked=False),
        snapshots={1: _snap(500, 10.0, unreported=0)},
    )

    assert decide(inputs).outcome is Outcome.CONTINUE


def test_wall_observation_kept_when_unreported_grew() -> None:
    """Рост `unreported_turns` отбрасывает только токены, не секунды."""
    inputs = _continue_inputs(
        round=2,
        used=_used(10, 95.0, unreported=1, tracked=False),
        snapshots={1: _snap(5, 50.0, unreported=0)},
    )

    draft = decide(inputs)

    assert draft.reason == REASON_BUDGET_PREDICTED


def test_prediction_replaces_forced_review_continue() -> None:
    """Принудительный цикл анти-сикофантии — тоже `CONTINUE`: прогноз сильнее."""
    inputs = _continue_inputs(
        round=1,
        used=_used(600, 10.0, tracked=True),
        review=make_review(verdict=Verdict.APPROVE),
        patch_current="--- a/x.py\n",
    )
    unpredicted = _continue_inputs(
        round=1,
        used=_used(100, 10.0, tracked=True),
        review=make_review(verdict=Verdict.APPROVE),
        patch_current="--- a/x.py\n",
    )

    assert decide(unpredicted).reason == REASON_ANTI_SYCOPHANCY
    draft = decide(inputs)
    assert draft.outcome is Outcome.BUDGET_HIT
    assert draft.reason == REASON_BUDGET_PREDICTED
    assert draft.forced_review is False


_PREDICTING: dict[str, Any] = {
    "used": _used(950, 95.0),
    "snapshots": {1: _snap(300, 30.0)},
}


@pytest.mark.parametrize(
    ("overrides", "outcome", "reason"),
    [
        (
            {"review": make_review(verdict=Verdict.APPROVE)},
            Outcome.CONVERGED,
            REASON_CONVERGED,
        ),
        (
            {
                "verification": make_verification(
                    overall=OverallStatus.INDETERMINATE, gates=[]
                )
            },
            Outcome.DEADLOCK,
            REASON_VERIFICATION_INDETERMINATE,
        ),
        (
            {"patch_current": "--- a/x.py\n+1\n", "patch_two_back": "--- a/x.py\n+1\n"},
            Outcome.DEADLOCK,
            REASON_OSCILLATION_DIFF,
        ),
        ({"limits": _limits(max_rounds=2)}, Outcome.DEADLOCK, REASON_MAX_ROUNDS),
    ],
    ids=["converged", "indeterminate", "oscillation", "max_rounds"],
)
def test_prediction_never_overrides_an_earlier_stop(
    overrides: dict[str, Any], outcome: Outcome, reason: str
) -> None:
    """Прогноз проверяется последним: установленную причину он не заменяет."""
    inputs = _continue_inputs(round=2, **{**_PREDICTING, **overrides})

    draft = decide(inputs)

    assert (draft.outcome, draft.reason) == (outcome, reason)


def test_actual_exceed_keeps_its_own_reason() -> None:
    """Фактическое превышение — своя причина на своём месте порядка."""
    inputs = _continue_inputs(
        round=2, used=_used(1_001, 10.0), snapshots={1: _snap(300, 5.0)}
    )

    assert decide(inputs).reason == REASON_BUDGET_TOKENS


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"review": make_review(verdict=Verdict.APPROVE)},
        {"limits": _limits(max_rounds=2)},
        {"used": _used(950, 95.0)},
    ],
    ids=["continue", "converged", "max_rounds", "predicted"],
)
def test_draft_carries_entry_snapshot(overrides: dict[str, Any]) -> None:
    """Каждое решение несёт снимок `budget_used` на входе в DECIDING (§4.5)."""
    params: dict[str, Any] = {
        "used": _used(200, 20.0, unreported=2),
        "snapshots": {1: _snap(100, 10.0, unreported=2)},
    }
    params.update(overrides)
    inputs = _continue_inputs(round=2, **params)

    draft = decide(inputs)

    used = inputs.budget_used
    assert draft.budget_snapshot == BudgetSnapshot(
        tokens=used.tokens,
        wall_seconds=used.wall_seconds,
        unreported_turns=used.unreported_turns,
    )


def _anti_sycophancy_free(inputs: DecidingInputs) -> DecidingInputs:
    """Раунд 1 без approve: анти-сикофантия не вмешивается, мод — develop."""
    assert inputs.mode is Mode.DEVELOP
    assert inputs.review.verdict is Verdict.REQUEST_CHANGES
    return inputs
