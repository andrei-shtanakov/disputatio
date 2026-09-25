"""Все попытки шага в расходе: §4.1 SPEC-001 (I4, `unreported_turns`).

До этой правки граница шага начисляла только `turn` принятой попытки, и
токены schema-невалидных попыток терялись: агент, трижды вернувший мусор,
выглядел в `budget_used` втрое дешевле, чем обошёлся. Теперь шаг отдаёт
все свои попытки, начисление суммирует их токены, а попытка без отчёта о
расходе вместо нуля прибавляет единицу к `unreported_turns`. Шаг без
вызова агента (`VERIFYING`, `DECIDING`) о токенах не отчитывается вовсе —
и к счётчику ничего не прибавляет.

Проверка идёт через `loop._run_step` — ту самую границу, где расход
начисляется в цикле, — а не через голое `accumulate`: мутация «начислять
только последнюю попытку» живёт в шаге, и поймать её можно только там.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from disputatio.contracts import AgentTurn, BudgetUsed, SessionPhase
from disputatio.core import SessionFsm
from disputatio.events import write_round_artifact
from disputatio.runtime import loop, steps
from disputatio.runtime.budget import accumulate
from disputatio.runtime.layout import CHANGES_PATCH_NAME, PROPOSAL_NAME

from .test_schema_retry import (
    _FROZEN_NOW,
    _INVALID_PROPOSAL,
    _UNPARSABLE_REVIEW,
    FakeSink,
    FakeStore,
    NoAgent,
    NoGit,
    SpyGit,
    _deps,
    _head,
    _proposal,
    _review_reply,
    _seed_verification,
    _state,
)


@dataclass
class MeteredAdapter:
    """`AgentAdapter`-фейк: ответ и сообщённый расход — по сценарию."""

    replies: list[tuple[str, int | None]]
    calls: int = 0
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Отдаёт очередной ответ сценария с его расходом токенов."""
        self.prompts.append(prompt)
        text, tokens = self.replies[self.calls]
        self.calls += 1
        return AgentTurn(text=text, session_ref=session_ref, tokens_used=tokens)


def _ctx(phase: SessionPhase, root: Path, *, author: Any, reviewer: Any) -> Any:
    """`StepContext` фазы `phase` раунда 1 с лимитом в два повтора."""
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(
        _state(phase, schema_retries=2, round_no=1),
        store=store,
        sink=sink,
        now=lambda: _FROZEN_NOW,
    )
    git = SpyGit() if phase is SessionPhase.PROPOSING else NoGit()
    deps = _deps(
        root, store=store, sink=sink, author=author, reviewer=reviewer, git=git
    )
    base = _head(root) if phase is SessionPhase.PROPOSING else "0" * 40
    return steps.StepContext(deps=deps, fsm=fsm, base_commit=base)


def _charged(step: Any, ctx: Any) -> BudgetUsed:
    """Прогоняет шаг через границу цикла и отдаёт начисленный бюджет."""

    async def call() -> Any:
        return await loop._run_step(step, ctx)

    charged_ctx = anyio.run(call)
    used: BudgetUsed = charged_ctx.fsm.state.budget_used
    return used


def test_failed_and_accepted_author_attempts_are_both_charged(git_repo: Path) -> None:
    """Токены невалидной попытки и принятой складываются (§4.1)."""
    author = MeteredAdapter(
        replies=[(_INVALID_PROPOSAL, 100), (_proposal(1, body="исправился"), 30)]
    )
    ctx = _ctx(SessionPhase.PROPOSING, git_repo, author=author, reviewer=NoAgent())

    used = _charged(steps.propose, ctx)

    assert author.calls == 2
    assert used.tokens == 130, "расход отвергнутой попытки потерян"
    assert used.unreported_turns == 0


def test_attempt_without_token_report_counts_as_unreported(git_repo: Path) -> None:
    """Попытка без отчёта не прибавляет токенов, но растит `unreported_turns`."""
    author = MeteredAdapter(
        replies=[(_INVALID_PROPOSAL, None), (_proposal(1, body="исправился"), 30)]
    )
    ctx = _ctx(SessionPhase.PROPOSING, git_repo, author=author, reviewer=NoAgent())

    used = _charged(steps.propose, ctx)

    assert used.tokens == 30
    assert used.unreported_turns == 1


def test_reviewer_attempts_are_summed_with_unreported(tmp_path: Path) -> None:
    """REVIEWING: сообщённое суммируется, несообщённое считается отдельно."""
    _seed_verification(tmp_path, 1)
    write_round_artifact(tmp_path, 1, PROPOSAL_NAME, _proposal(1, body="тело"))
    write_round_artifact(tmp_path, 1, CHANGES_PATCH_NAME, "ДИФФ\n")
    reviewer = MeteredAdapter(
        replies=[(_UNPARSABLE_REVIEW, 7), (_review_reply(1), None)]
    )
    ctx = _ctx(SessionPhase.REVIEWING, tmp_path, author=NoAgent(), reviewer=reviewer)

    used = _charged(steps.review, ctx)

    assert used.tokens == 7
    assert used.unreported_turns == 1


def test_step_without_agent_call_does_not_count_as_unreported(tmp_path: Path) -> None:
    """Шаг без вызова агента (`VERIFYING`, `DECIDING`) счётчик не трогает.

    Такой шаг возвращает `None`: о токенах он не отчитывается, потому что
    их не тратит, и «не отчитался» здесь было бы ложью.
    """
    ctx = _ctx(SessionPhase.VERIFYING, tmp_path, author=NoAgent(), reviewer=NoAgent())

    def no_agent_step(_ctx: Any) -> None:
        return None

    used = _charged(no_agent_step, ctx)

    assert used.unreported_turns == 0
    assert used.tokens == 0


def test_accumulate_sums_reported_and_counts_unreported() -> None:
    """`accumulate`: сумма сообщённого, `None` — в счётчик, ноль — это ноль."""
    state = _state(SessionPhase.PROPOSING, schema_retries=2, round_no=1)
    turns = (
        AgentTurn(text="a", tokens_used=5),
        AgentTurn(text="b", tokens_used=None),
        AgentTurn(text="c", tokens_used=0),
    )

    after = accumulate(state, turns=turns, elapsed_s=1.0)

    assert after.budget_used.tokens == 5
    assert after.budget_used.unreported_turns == 1
    assert after.budget_used.wall_seconds == 1.0
