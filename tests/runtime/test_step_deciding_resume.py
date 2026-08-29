"""DECIDING после обрыва: повторный запуск шага ([REQ-015], [DESIGN-007]).

Ревью [TASK-012]. Файл рядом с байт-locked `test_step_deciding.py` и
закрывает окно, которое пережило полный suite: шаг ставит маркер I3
(`finalize_round`) ДО перехода, а перехода — точка, после которой resume
считает раунд решённым. Между ними лежат два вызова, каждый из которых
вправе не дойти до конца: `commit_round` (git-сбой) и сам обрыв процесса.

Состояние на диске после такого обрыва — `session.json` всё ещё в
`DECIDING`, раунд N уже финализирован. Диспетчер ([DESIGN-008]) на resume
исполнит шаг фазы заново, и запись `decision.json` упрётся в собственный
маркер раунда: `RoundImmutableError`. Каждая следующая попытка даёт то же
самое — сессия встаёт намертво в фазе, из которой нет выхода, хотя
`commit_round` для ровно этого случая сделан идемпотентным ([DESIGN-011]).

REQ-015 требует обратного: «повторное выполнение того же шага дважды подряд
даёт эквивалентное состояние». Решение раунда детерминировано входами с
диска, поэтому повтор обязан пройти шаг до конца — но и переписать
финализированный артефакт не вправе (I3, [REQ-016]). Единственный
допустимый ответ — довести шаг, убедившись, что на диске лежит ровно то же
решение; разойдись они, честнее упасть.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from disputatio.contracts import (
    AgentRef,
    AgentTurn,
    BudgetUsed,
    Decision,
    DiffStats,
    Event,
    GateResult,
    GateStatus,
    Issue,
    Limits,
    Mode,
    Outcome,
    OverallStatus,
    Review,
    Role,
    SessionPhase,
    SessionState,
    Severity,
    TaskSpec,
    Verdict,
    VerificationReport,
)
from disputatio.core import SessionFsm
from disputatio.events import (
    RoundImmutableError,
    finalize_round,
    write_round_artifact,
)
from disputatio.runtime import RuntimeDeps
from disputatio.runtime.layout import DECISION_NAME, round_artifact
from disputatio.runtime.steps import StepContext, decide_step

from ._fakes import GitOpsFakeBase

_FROZEN_NOW = datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)
_ROUND = 2
_SESSION_ID = "s-deciding-resume"


class CommitFailed(Exception):
    """Сбой `git commit` — то, что рвёт шаг между маркером и переходом."""


@dataclass
class FakeStore:
    """`StateStore`-фейк: сохранения журналируются, на диск не пишутся."""

    saved: list[SessionState] = field(default_factory=list)

    def load(self, session_id: str) -> SessionState:
        """Сессии нет — `KeyError`, как у файловой реализации."""
        raise KeyError(session_id)

    def save(self, state: SessionState) -> None:
        """Запоминает состояние вместо записи `session.json`."""
        self.saved.append(state)


@dataclass
class FakeSink:
    """`EventSink`-фейк: складывает события в список."""

    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        """Запоминает событие вместо дописывания в `events.jsonl`."""
        self.events.append(event)


@dataclass
class NoAgent:
    """`AgentAdapter`-фейк: DECIDING не разговаривает ни с кем (§7)."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Вызов означает, что шаг спросил агента вместо ядра."""
        raise AssertionError("DECIDING не вправе звать агентов")


@dataclass
class NoVerifier:
    """`Verifier`-фейк: гейты прогнаны шагом VERIFYING, повтора не бывает."""

    def verify(self, round_no: int) -> VerificationReport:
        """Вызов означает, что шаг перепрогнал гейты раунда."""
        raise AssertionError(f"DECIDING не вправе гонять гейты (раунд {round_no})")


@dataclass
class SpyGit(GitOpsFakeBase):
    """`GitOps`-фейк: коммит журналируется и вправе сорваться однажды.

    `fail_once` воспроизводит именно обрыв между маркером I3 и переходом:
    попытка фиксации была, следа в истории не осталось, фаза на диске всё
    ещё `DECIDING`.
    """

    commits: list[int] = field(default_factory=list)
    fail_once: bool = False

    def diff_head(self) -> str:
        """Не вызывается: патч раунда снят шагом PROPOSING."""
        raise AssertionError("DECIDING не вправе снимать дифф")

    def commit_round(self, round_no: int) -> None:
        """Фиксирует раунд либо срывается ровно один раз."""
        if self.fail_once:
            self.fail_once = False
            raise CommitFailed(f"git commit раунда {round_no} не прошёл")
        self.commits.append(round_no)

    def reset_hard(self, rev: str) -> None:
        """Не вызывается: сброс дерева — прерогатива PROPOSING."""
        raise AssertionError(f"DECIDING не вправе сбрасывать дерево на {rev}")

    def clean(self) -> None:
        """Не вызывается: уборка дерева — прерогатива PROPOSING."""
        raise AssertionError("DECIDING не вправе убирать дерево")


def _state() -> SessionState:
    """`SessionState` раунда `_ROUND` в фазе `DECIDING`."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=SessionPhase.DECIDING,
        current_round=_ROUND,
        task=TaskSpec(
            prompt="ЗАДАЧА-ПОЛЬЗОВАТЕЛЯ: почини экспорт CSV",
            attachments=[],
            mode=Mode.DEVELOP,
        ),
        agents={
            Role.AUTHOR: AgentRef(adapter="claude_code", model="opus"),
            Role.REVIEWER: AgentRef(adapter="claude_code", model="sonnet"),
        },
        limits=Limits(
            max_rounds=9,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        budget_used=BudgetUsed(tokens=4242, wall_seconds=7.5, cost_usd_est=0.25),
    )


def _review(round_no: int) -> Review:
    """Ревью раунда `round_no` — правки требуются, раунд продолжается."""
    marker = f"{round_no:03d}"
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.9,
        issues=[
            Issue(
                id=f"I-{marker}-A",
                severity=Severity.MAJOR,
                file=f"mod{marker}.py",
                claim=f"замечание A раунда {marker}",
                evidence=f"mod{marker}.py:1 — свидетельство A раунда {marker}",
            )
        ],
        checked=[f"mod{marker}.py"],
        summary=f"свод раунда {marker}",
    )


def _verification(round_no: int) -> VerificationReport:
    """Отчёт проверок раунда `round_no`; гейты зелёные."""
    marker = f"{round_no:03d}"
    return VerificationReport(
        round=round_no,
        gates=[
            GateResult(
                name=f"gate-{marker}",
                cmd="uv run pytest -q",
                status=GateStatus.PASS,
                exit_code=0,
                duration_s=1.5,
                tail=f"вывод гейта раунда {marker}",
            )
        ],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=4, deletions=2),
    )


def _seed(root: Path) -> None:
    """Кладёт на диск раунд 1 целиком и артефакты раунда `_ROUND`."""
    write_round_artifact(
        root, 1, "review.json", _review(1).model_dump_json(by_alias=True)
    )
    write_round_artifact(
        root,
        1,
        "decision.json",
        Decision(
            round=1,
            outcome=Outcome.CONTINUE,
            reason="причина раунда 001",
            open_issues_carried=["I-001-A"],
            next_round_directive="директива раунда 001",
        ).model_dump_json(by_alias=True),
    )
    write_round_artifact(
        root, 1, "verification.json", _verification(1).model_dump_json(by_alias=True)
    )
    write_round_artifact(root, 1, "changes.patch", "--- a/mod001.py\n")
    finalize_round(root, 1)

    write_round_artifact(
        root,
        _ROUND,
        "review.json",
        _review(_ROUND).model_dump_json(by_alias=True),
    )
    write_round_artifact(
        root,
        _ROUND,
        "verification.json",
        _verification(_ROUND).model_dump_json(by_alias=True),
    )
    write_round_artifact(root, _ROUND, "changes.patch", "--- a/mod002.py\n")


def _context(root: Path, git: SpyGit) -> StepContext:
    """`StepContext` шага DECIDING поверх свежесобранного состояния.

    Свежая `SessionFsm` на каждый вызов — это и есть resume: состояние
    поднимается из `session.json`, а не переживает обрыв в памяти.
    """
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(_state(), store=store, sink=sink, now=lambda: _FROZEN_NOW)
    deps = RuntimeDeps(
        workspace_root=root,
        artifact_root=root,
        store=store,
        sink=sink,
        author=NoAgent(),
        reviewer=NoAgent(),
        verifier=NoVerifier(),
        git=git,
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    return StepContext(deps=deps, fsm=fsm, base_commit="0" * 40)


def _decision_on_disk(root: Path) -> Decision:
    """`decision.json` раунда `_ROUND`, прочитанное моделью contracts."""
    return Decision.model_validate_json(
        round_artifact(root, _ROUND, DECISION_NAME).read_text(encoding="utf-8")
    )


def test_step_resumes_after_a_crash_between_the_marker_and_the_transition(
    tmp_path: Path,
) -> None:
    """Обрыв после `finalize_round` не запирает раунд ([REQ-015]).

    Первый проход ставит маркер I3 и срывается на коммите: `decision.json`
    уже на диске, фаза — всё ещё `DECIDING`. Второй проход обязан пройти
    шаг целиком — иначе сессия не выходит из DECIDING ни одной попыткой.
    """
    _seed(tmp_path)
    git = SpyGit(fail_once=True)

    with pytest.raises(CommitFailed):
        decide_step(_context(tmp_path, git))

    written_before = _decision_on_disk(tmp_path)
    ctx = _context(tmp_path, git)

    decide_step(ctx)

    assert git.commits == [_ROUND]
    assert ctx.fsm.state.state is SessionPhase.PROPOSING
    assert ctx.fsm.state.current_round == _ROUND + 1
    assert _decision_on_disk(tmp_path) == written_before


def test_resume_does_not_rewrite_a_decision_that_disagrees_with_the_round(
    tmp_path: Path,
) -> None:
    """Чужое решение на диске финализированного раунда — отказ, не правка.

    Терпимость к повтору держится ровно на детерминизме: раз входы те же,
    и решение то же. Артефакт, который решением этого раунда быть не может,
    означает, что предположение сломалось, — и переписать его шаг не вправе
    (I3, [REQ-016]), и промолчать тоже: переход ушёл бы в фазу, о которой
    `decision.json` рассказывает другое.
    """
    _seed(tmp_path)
    write_round_artifact(
        tmp_path,
        _ROUND,
        DECISION_NAME,
        Decision(
            round=_ROUND,
            outcome=Outcome.CONVERGED,
            reason="чужое решение",
            open_issues_carried=[],
            next_round_directive=None,
        ).model_dump_json(by_alias=True),
    )
    finalize_round(tmp_path, _ROUND)
    git = SpyGit()

    with pytest.raises(RoundImmutableError):
        decide_step(_context(tmp_path, git))

    assert git.commits == []
    assert _decision_on_disk(tmp_path).reason == "чужое решение"
