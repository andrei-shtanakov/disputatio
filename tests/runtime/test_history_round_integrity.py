"""Артефакт раунда с чужим `round` — повреждён (SPEC-001 §4, §5.2).

Загрузчики истории проверяли схему, но не то, что поле `round` артефакта
совпадает с номером его каталога `rounds/NNN/`. Подложенный
`rounds/001/decision.json` с `round=99` и выдуманным снимком становился
снимком раунда 1 и переключал прогноз; `rounds/002/review.json` и
`verification.json` с `round=99` принимались за ревью и отчёт текущего
раунда и сводили сессию. Теперь такой артефакт поднимает ту же
`ValidationError`, что и схемно-невалидный: молча пропустить его значило бы
превратить подмену в «наблюдения нет».
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from disputatio.contracts import (
    BudgetSnapshot,
    Decision,
    DiffStats,
    GateResult,
    GateStatus,
    OverallStatus,
    Review,
    Role,
    SessionPhase,
    Verdict,
    VerificationReport,
)
from disputatio.events import write_round_artifact
from disputatio.runtime.history import (
    budget_snapshots,
    load_decision,
    load_prior_round,
    load_review,
    load_verification,
)
from disputatio.runtime.layout import (
    DECISION_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    round_artifact,
)
from disputatio.runtime.steps import decide_step

from .test_step_deciding_budget import _NEAR_LIMIT, _ctx_with
from .test_step_deciding_resume import _ROUND, SpyGit, _seed

_FOREIGN_ROUND = 99


def _decision(round_no: int) -> Decision:
    return Decision(
        round=round_no,
        outcome="continue",
        reason="continue_revise_cycle",
        open_issues_carried=[],
        next_round_directive="директива",
        budget_snapshot=BudgetSnapshot(tokens=1, wall_seconds=0.1, unreported_turns=0),
    )


def _review(round_no: int, verdict: Verdict = Verdict.APPROVE) -> Review:
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=verdict,
        confidence=0.9,
        issues=[],
        checked=["mod002.py"],
        summary="свод",
    )


def _verification(round_no: int) -> VerificationReport:
    return VerificationReport(
        round=round_no,
        gates=[
            GateResult(
                name="gate",
                cmd="uv run pytest -q",
                status=GateStatus.PASS,
                exit_code=0,
            )
        ],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=1, deletions=0),
    )


_ARTIFACTS: dict[str, tuple[str, Callable[[int], Any]]] = {
    "decision": (DECISION_NAME, _decision),
    "review": (REVIEW_NAME, _review),
    "verification": (VERIFICATION_NAME, _verification),
}

_LOADERS: dict[str, Callable[[Path, int], Any]] = {
    "decision": load_decision,
    "review": load_review,
    "verification": load_verification,
}


def _put(root: Path, round_no: int, kind: str, artifact_round: int) -> None:
    """Кладёт в `rounds/round_no/` артефакт `kind` с полем `round=artifact_round`."""
    name, build = _ARTIFACTS[kind]
    write_round_artifact(
        root, round_no, name, build(artifact_round).model_dump_json(by_alias=True)
    )


@pytest.mark.parametrize("kind", sorted(_ARTIFACTS))
def test_loader_rejects_foreign_round(tmp_path: Path, kind: str) -> None:
    """Каждый загрузчик раунда отвергает `round`, не равный каталогу."""
    _put(tmp_path, 1, kind, _FOREIGN_ROUND)

    with pytest.raises(ValidationError, match="round"):
        _LOADERS[kind](tmp_path, 1)


@pytest.mark.parametrize("kind", sorted(_ARTIFACTS))
def test_loader_accepts_matching_round(tmp_path: Path, kind: str) -> None:
    """Законный артефакт по-прежнему читается."""
    _put(tmp_path, 1, kind, 1)

    loaded = _LOADERS[kind](tmp_path, 1)

    assert loaded is not None
    assert loaded.round == 1


@pytest.mark.parametrize("kind", sorted(_ARTIFACTS))
def test_prior_round_rejects_foreign_round(tmp_path: Path, kind: str) -> None:
    """Сборка прошлого раунда для промпта — тот же отказ."""
    _put(tmp_path, 1, kind, _FOREIGN_ROUND)

    with pytest.raises(ValidationError):
        load_prior_round(tmp_path, 1)


def test_snapshot_reader_rejects_foreign_round(tmp_path: Path) -> None:
    """Снимок из решения с чужим `round` — ошибка, а не «наблюдения нет»."""
    _put(tmp_path, 1, "decision", _FOREIGN_ROUND)

    with pytest.raises(ValidationError):
        budget_snapshots(tmp_path, 2)


def test_planted_snapshot_does_not_flip_the_prediction(tmp_path: Path) -> None:
    """Сценарий QA: `rounds/001/decision.json` с `round=99` и снимком.

    Без проверки выдуманный снимок раунда 1 дал бы наблюдение и прогноз
    `BUDGET_HIT`; теперь `DECIDING` падает на повреждённой истории.
    """
    _seed(tmp_path)
    (round_artifact(tmp_path, 1, DECISION_NAME).parent / ".finalized").unlink()
    fake = _decision(_FOREIGN_ROUND).model_copy(
        update={
            "budget_snapshot": BudgetSnapshot(
                tokens=50_000, wall_seconds=5.0, unreported_turns=0
            )
        }
    )
    write_round_artifact(
        tmp_path, 1, DECISION_NAME, fake.model_dump_json(by_alias=True)
    )
    git = SpyGit()
    ctx = _ctx_with(tmp_path, git, budget=_NEAR_LIMIT)

    with pytest.raises(ValidationError):
        decide_step(ctx)

    assert not round_artifact(tmp_path, _ROUND, DECISION_NAME).exists()
    assert ctx.fsm.state.state is SessionPhase.DECIDING
    assert git.commits == []


@pytest.mark.parametrize(
    "kinds",
    [("review",), ("verification",), ("review", "verification")],
    ids=["review", "verification", "both"],
)
def test_foreign_current_round_artifacts_do_not_converge(
    tmp_path: Path, kinds: tuple[str, ...]
) -> None:
    """Сценарий QA: ревью `approve` и отчёт `pass` с `round=99` в раунде 2.

    Принятые за артефакты текущего раунда, они свели бы сессию; теперь
    `DECIDING` падает, решение не пишется, раунд не коммитится.
    """
    _seed(tmp_path)
    for kind in kinds:
        _put(tmp_path, _ROUND, kind, _FOREIGN_ROUND)
    if "review" not in kinds:
        _put(tmp_path, _ROUND, "review", _ROUND)
    git = SpyGit()
    ctx = _ctx_with(tmp_path, git, budget=_NEAR_LIMIT)

    with pytest.raises(ValidationError):
        decide_step(ctx)

    assert not round_artifact(tmp_path, _ROUND, DECISION_NAME).exists()
    assert ctx.fsm.state.state is SessionPhase.DECIDING
    assert git.commits == []
