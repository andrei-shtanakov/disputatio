"""DECIDING и бюджет: снимок §4.5, прогноз с диска §5.2, повтор прежней версии.

Три обязательства шага, и каждое наблюдаемо только на диске:

* **снимок** — каждое решение, терминальное тоже, несёт `budget_snapshot`,
  равный `budget_used` на ВХОДЕ в `DECIDING`: время самого шага начисляется
  после него и в снимок не попадает;
* **прогноз читает снимки прошлых раундов с диска** — из `decision.json`, а
  не из памяти процесса: после resume он обязан прийти к тому же;
* **повтор над решением прежней версии** (без снимка) — такое решение
  стоит только в сессии без учёта с рождения и только если совпадает с
  ядром С прогнозом: снимок в него не дописывается, переход идёт по
  записанному исходу. Повтор над решением новой версии воспроизводит его
  байт-в-байт.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic import ValidationError

from disputatio.contracts import (
    BudgetSnapshot,
    BudgetUsed,
    Decision,
    Outcome,
    SessionPhase,
    SessionState,
)
from disputatio.core import REASON_BUDGET_PREDICTED, SessionFsm
from disputatio.events import (
    RoundImmutableError,
    finalize_round,
    write_round_artifact,
)
from disputatio.runtime import loop
from disputatio.runtime.layout import DECISION_NAME, round_artifact
from disputatio.runtime.steps import StepContext, decide_step

from .test_step_deciding_resume import (
    _FROZEN_NOW,
    _ROUND,
    CommitFailed,
    SpyGit,
    _context,
    _decision_on_disk,
    _seed,
    _state,
)

# Бюджет у края лимита (100_000 токенов): прогноз срабатывает, как только
# у раунда 2 есть база — снимок раунда 1.
_NEAR_LIMIT = BudgetUsed(tokens=95_000, wall_seconds=7.5)
_ROUND_ONE_SNAPSHOT = BudgetSnapshot(
    tokens=50_000, wall_seconds=5.0, unreported_turns=0
)


def _ctx_with(
    root: Path, git: SpyGit, *, budget: BudgetUsed, clock: list[float] | None = None
) -> StepContext:
    """Контекст DECIDING с заданным бюджетом и, по желанию, идущими часами."""
    base = _context(root, git)
    state: SessionState = base.fsm.state.model_copy(update={"budget_used": budget})
    fsm = SessionFsm(
        state, store=base.deps.store, sink=base.deps.sink, now=lambda: _FROZEN_NOW
    )
    deps = base.deps
    if clock is not None:
        ticks = iter(clock)
        deps = replace(deps, monotonic=lambda: next(ticks))
    return StepContext(deps=deps, fsm=fsm, base_commit=base.base_commit)


def _seed_round_one_snapshot(root: Path) -> None:
    """Переписывает решение раунда 1 решением новой версии со снимком."""
    decision_path = round_artifact(root, 1, DECISION_NAME)
    recorded = Decision.model_validate_json(decision_path.read_text(encoding="utf-8"))
    marker = decision_path.parent / ".finalized"
    marker.unlink()
    write_round_artifact(
        root,
        1,
        DECISION_NAME,
        recorded.model_copy(
            update={"budget_snapshot": _ROUND_ONE_SNAPSHOT}
        ).model_dump_json(by_alias=True),
    )
    finalize_round(root, 1)


def _write_prior_version_decision(root: Path, *, finalized: bool, **fields: Any) -> str:
    """Кладёт `decision.json` раунда `_ROUND` прежней версии — без снимка."""
    payload: dict[str, Any] = {
        "round": _ROUND,
        "outcome": Outcome.CONTINUE,
        "reason": "continue_revise_cycle",
        "open_issues_carried": ["I-002-A"],
        # Ровно то, что ядро выносит этому раунду без прогноза.
        "next_round_directive": "mod002.py: замечание A раунда 002",
    }
    payload.update(fields)
    text = Decision(**payload).model_dump_json(by_alias=True)
    assert '"budget_snapshot"' not in text, "фикстура обязана быть без снимка"
    write_round_artifact(root, _ROUND, DECISION_NAME, text)
    if finalized:
        finalize_round(root, _ROUND)
    return round_artifact(root, _ROUND, DECISION_NAME).read_text(encoding="utf-8")


def test_continue_decision_carries_the_entry_snapshot(tmp_path: Path) -> None:
    """`CONTINUE` несёт снимок `budget_used` на входе в шаг (§4.5)."""
    _seed(tmp_path)
    ctx = _context(tmp_path, SpyGit())
    entry = ctx.fsm.state.budget_used

    decide_step(ctx)

    decision = _decision_on_disk(tmp_path)
    assert decision.outcome is Outcome.CONTINUE
    assert decision.budget_snapshot == BudgetSnapshot(
        tokens=entry.tokens,
        wall_seconds=entry.wall_seconds,
        unreported_turns=entry.unreported_turns,
    )


def test_snapshot_excludes_the_time_of_deciding_itself(tmp_path: Path) -> None:
    """Время `DECIDING` начисляется после шага и в снимок не входит (§4.5)."""
    _seed(tmp_path)
    ctx = _ctx_with(
        tmp_path,
        SpyGit(),
        budget=BudgetUsed(tokens=10, wall_seconds=7.5),
        clock=[100.0, 103.0],
    )

    async def run() -> StepContext:
        return await loop._run_step(decide_step, ctx)

    charged = anyio.run(run)

    decision = _decision_on_disk(tmp_path)
    assert decision.budget_snapshot is not None
    assert decision.budget_snapshot.wall_seconds == 7.5
    assert charged.fsm.state.budget_used.wall_seconds == 10.5


def test_prediction_reads_prior_snapshots_from_disk(tmp_path: Path) -> None:
    """Снимок раунда 1 с диска даёт наблюдение: терминальный прогноз §5.2.

    Раунд 2 стоил 45_000 токенов, остаток — 5_000. Решение терминальное и
    всё равно несёт снимок; частичный исход не финализируется.
    """
    _seed(tmp_path)
    _seed_round_one_snapshot(tmp_path)
    git = SpyGit()
    ctx = _ctx_with(tmp_path, git, budget=_NEAR_LIMIT)

    decide_step(ctx)

    decision = _decision_on_disk(tmp_path)
    assert decision.outcome is Outcome.BUDGET_HIT
    assert decision.reason == REASON_BUDGET_PREDICTED
    assert decision.budget_snapshot == BudgetSnapshot(
        tokens=95_000, wall_seconds=7.5, unreported_turns=0
    )
    assert ctx.fsm.state.state is SessionPhase.EXPORTING
    assert git.commits == []


def test_prior_version_round_one_gives_no_base(tmp_path: Path) -> None:
    """Решение раунда 1 прежней версии (без снимка) базой не служит."""
    _seed(tmp_path)
    ctx = _ctx_with(tmp_path, SpyGit(), budget=_NEAR_LIMIT)

    decide_step(ctx)

    assert _decision_on_disk(tmp_path).outcome is Outcome.CONTINUE


@pytest.mark.parametrize("finalized", [True, False], ids=["finalized", "open"])
def test_prior_version_decision_is_authoritative_on_replay(
    tmp_path: Path, finalized: bool
) -> None:
    """Согласное с ядром решение прежней версии остаётся как записано (§4.5).

    Сессия прежняя (`tracked_from_start` снят) и снимков на диске нет: у
    прогноза нет наблюдений, и ядро с прогнозом выносит тот же `CONTINUE`,
    даже при бюджете у края. Ни `RoundImmutableError`, ни дописанного снимка.
    """
    _seed(tmp_path)
    before = _write_prior_version_decision(tmp_path, finalized=finalized)
    git = SpyGit()
    ctx = _ctx_with(tmp_path, git, budget=_NEAR_LIMIT)

    decide_step(ctx)

    after = round_artifact(tmp_path, _ROUND, DECISION_NAME).read_text(encoding="utf-8")
    assert after == before, "авторитетное решение прежней версии переписано"
    assert ctx.fsm.state.state is SessionPhase.PROPOSING
    assert ctx.fsm.state.current_round == _ROUND + 1
    assert git.commits == [_ROUND]
    assert round_artifact(tmp_path, _ROUND, ".finalized").exists()


def _assert_refused(root: Path, ctx: StepContext, git: SpyGit, before: str) -> None:
    """Отказ шага: файл цел, раунд не закрыт и не закоммичен, фаза — DECIDING."""
    with pytest.raises(RoundImmutableError):
        decide_step(ctx)

    after = round_artifact(root, _ROUND, DECISION_NAME).read_text(encoding="utf-8")
    assert after == before
    assert ctx.fsm.state.state is SessionPhase.DECIDING
    assert git.commits == []
    assert not round_artifact(root, _ROUND, ".finalized").exists()


def test_snapshotless_decision_refused_in_tracked_session(tmp_path: Path) -> None:
    """Сессия с учётом с рождения: решение без снимка не бывает законным.

    Новая версия пишет снимок всегда, поэтому `decision.json` без него в
    сессии с `tracked_from_start` — подложен. Бюджет мал, и подложенный
    `CONTINUE` совпадает с ядром и без прогноза, и с ним — отказ даёт только
    сам признак учёта.
    """
    _seed(tmp_path)
    before = _write_prior_version_decision(tmp_path, finalized=False)
    git = SpyGit()
    budget = BudgetUsed(tokens=10, wall_seconds=1.0, tracked_from_start=True)
    ctx = _ctx_with(tmp_path, git, budget=budget)

    _assert_refused(tmp_path, ctx, git, before)


def test_planted_decision_cannot_suppress_prediction(tmp_path: Path) -> None:
    """Подложенный `CONTINUE` без снимка не отключает прогноз §5.2.

    Сессия прежняя, но раунд 1 уже записан новой версией со снимком, а
    бюджет у края: ядро с прогнозом выносит `budget_hit: predicted`.
    `CONTINUE`, совпадающий с ядром без прогноза, — расхождение и отказ.
    """
    _seed(tmp_path)
    _seed_round_one_snapshot(tmp_path)
    before = _write_prior_version_decision(tmp_path, finalized=False)
    git = SpyGit()
    ctx = _ctx_with(tmp_path, git, budget=_NEAR_LIMIT)

    _assert_refused(tmp_path, ctx, git, before)


@pytest.mark.parametrize(
    "fields",
    [
        {
            "outcome": Outcome.CONVERGED,
            "reason": "approve_with_gates_pass",
            "next_round_directive": None,
        },
        {"round": 1},
        {"outcome": Outcome.DEADLOCK, "reason": "max_rounds"},
    ],
    ids=["planted-converged", "wrong-round", "other-terminal"],
)
def test_planted_prior_version_decision_is_refused(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    """Решение без снимка, несогласное с ядром, — отказ, а не сходимость.

    Сценарий ревью PR #132: автор (дерево пишет он) или соседняя сессия с
    общим `rounds/` подкладывает `decision.json` без снимка. Ревью раунда —
    `request_changes`, а подложено `CONVERGED`: шаг обязан упасть
    `RoundImmutableError`, не сойтись, не финализировать и не коммитить
    раунд, и сам файл не трогать. Так же — чужой `round` при прочих
    совпадениях и терминальный исход, которого ядро не выносит.
    """
    _seed(tmp_path)
    before = _write_prior_version_decision(tmp_path, finalized=False, **fields)
    git = SpyGit()
    ctx = _context(tmp_path, git)

    # Чужой `round` — повреждённый артефакт (SPEC-001 §4): отказ приходит
    # ещё от загрузчика, `ValidationError`; прочие расхождения — от сверки.
    expected = ValidationError if "round" in fields else RoundImmutableError
    with pytest.raises(expected):
        decide_step(ctx)

    after = round_artifact(tmp_path, _ROUND, DECISION_NAME).read_text(encoding="utf-8")
    assert after == before
    assert ctx.fsm.state.state is SessionPhase.DECIDING
    assert git.commits == []
    assert not round_artifact(tmp_path, _ROUND, ".finalized").exists()


def test_new_version_decision_replays_identically(tmp_path: Path) -> None:
    """Повтор после обрыва над решением новой версии воспроизводит его.

    Снимок стабилен: расход начисляется после шага, и повтор получает на
    вход то же сохранённое значение. Файл — байт-в-байт тот же.
    """
    _seed(tmp_path)
    git = SpyGit(fail_once=True)

    with pytest.raises(CommitFailed):
        decide_step(_context(tmp_path, git))

    path = round_artifact(tmp_path, _ROUND, DECISION_NAME)
    written = path.read_text(encoding="utf-8")
    assert _decision_on_disk(tmp_path).budget_snapshot is not None
    ctx = _context(tmp_path, git)

    decide_step(ctx)

    assert path.read_text(encoding="utf-8") == written
    assert ctx.fsm.state.state is SessionPhase.PROPOSING
    assert git.commits == [_ROUND]


def test_state_fixture_is_untracked() -> None:
    """Контроль фикстуры: у прежнего `session.json` признака учёта нет."""
    assert _state().budget_used.tracked_from_start is False
