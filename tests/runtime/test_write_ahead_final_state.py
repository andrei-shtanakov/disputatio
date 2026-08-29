"""Что именно возвращает `drive`, дойдя до терминальной фазы ([REQ-008]).

[TASK-013], дополнение к `test_write_ahead.py`. Тот наблюдает результат
`drive` только на входе в УЖЕ терминальную фазу, где тело цикла не
исполняется ни разу: там состояние на входе и состояние на выходе — один и
тот же объект, и «возвращает финальное» проверяется вакуумно. Диспетчер,
снявший `ctx.fsm.state` до цикла и вернувший этот снимок, проходит весь
набор целиком (проверено подменой). Здесь пинится вторая половина
контракта: возвращается фаза, до которой цикл ДОШЁЛ, а не та, с которой
начал, — иначе вызывающая сторона отчиталась бы о сессии её прошлым
состоянием.

Шаг `EXPORTING` приходит своей задачей ([DESIGN-017]), поэтому единственный
путь до `DONE` — временная запись в `STEP_BY_PHASE`. Таблица подменяется
целиком (`setattr`), а не правится по ключу: объявлена она `Mapping`, и
мутация на месте утверждала бы про диспетчер то, чего он не обещает.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

import anyio
import pytest

from disputatio.contracts import (
    AgentRef,
    AgentTurn,
    BudgetUsed,
    DiffStats,
    Event,
    GateResult,
    GateStatus,
    Issue,
    Limits,
    Mode,
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
from disputatio.events import FileStateStore, bootstrap_session
from disputatio.runtime import RuntimeDeps
from disputatio.runtime import loop as loop_module
from disputatio.runtime.steps import StepContext

_FROZEN_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "s-final-state"
_BASE_COMMIT = "0" * 40
_BASE_REV = "resolved-base-rev"

_EXPORT_CALL = "export"


@dataclass
class RecordingSink:
    """`EventSink`-фейк: события в список, диска не касается."""

    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        """Запоминает событие вместо дописывания в `events.jsonl`."""
        self.events.append(event)


@dataclass
class QueueAdapter:
    """`AgentAdapter`-фейк: отдаёт ответы очереди по одному."""

    name: str
    log: list[str]
    replies: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Отмечает вызов и снимает следующий ответ очереди."""
        self.log.append(f"{self.name}.run")
        assert self.replies, (
            f"QueueAdapter {self.name}: очередь ответов исчерпана, лишний вызов"
        )
        return AgentTurn(text=self.replies.pop(0), session_ref=session_ref)


@dataclass
class PassingVerifier:
    """`Verifier`-фейк: гейты всегда зелёные."""

    log: list[str]
    rounds: list[int] = field(default_factory=list)

    def verify(self, round_no: int) -> VerificationReport:
        """Отмечает прогон и отдаёт зелёный отчёт раунда."""
        self.log.append("verifier.verify")
        self.rounds.append(round_no)
        return _verification(round_no)


@dataclass
class SpyGit:
    """`GitOps`-фейк: журналирует операции, рабочего дерева не трогает."""

    log: list[str]
    commits: list[int] = field(default_factory=list)

    def diff_head(self) -> str:
        """Отдаёт патч раунда — содержимое здесь роли не играет."""
        return _PATCH

    def commit_round(self, round_no: int) -> None:
        """Журналирует фиксацию принятого раунда."""
        self.log.append("git.commit_round")
        self.commits.append(round_no)

    def reset_hard(self, rev: str) -> None:
        """Журналирует сброс дерева на цель раунда."""
        self.log.append("git.reset_hard")

    def clean(self) -> None:
        """Журналирует уборку untracked-файлов прерванной попытки."""
        self.log.append("git.clean")


_PATCH = (
    "--- a/feature.py\n+++ b/feature.py\n@@ -1 +1 @@\n-СТАРАЯ-СТРОКА\n+НОВАЯ-СТРОКА\n"
)


def _proposal(round_no: int) -> str:
    """Валидный `proposal.md` раунда `round_no` — ответ автора."""
    return (
        "---\n"
        "schema: disputatio/v1\n"
        f"round: {round_no}\n"
        "role: author\n"
        "responds_to: null\n"
        "files_touched:\n"
        "  - feature.py\n"
        "self_declared_status: complete\n"
        "---\n"
        f"Работа раунда {round_no:03d}.\n"
    )


def _request_changes(round_no: int) -> str:
    """Ответ ревьюера: `request_changes` с major-замечанием и evidence."""
    marker = f"{round_no:03d}"
    review = Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.8,
        issues=[
            Issue(
                id=f"I-{marker}",
                severity=Severity.MAJOR,
                file=f"feature{marker}.py",
                claim=f"замечание раунда {marker}",
                evidence=f"feature{marker}.py:1 — свидетельство раунда {marker}",
            )
        ],
        checked=[f"feature{marker}.py"],
        summary=f"свод раунда {marker}",
    )
    return review.model_dump_json(by_alias=True)


def _approve(round_no: int) -> str:
    """Ответ ревьюера: `approve` — раунд `round_no` сходится (§5.1)."""
    review = Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.APPROVE,
        confidence=0.9,
        issues=[],
        checked=[f"feature{round_no:03d}.py"],
        summary=f"замечания раунда {round_no - 1:03d} закрыты",
    )
    return review.model_dump_json(by_alias=True)


def _verification(round_no: int) -> VerificationReport:
    """Зелёный отчёт проверок раунда `round_no`."""
    return VerificationReport(
        round=round_no,
        gates=[
            GateResult(
                name="pytest",
                cmd="uv run pytest -q",
                status=GateStatus.PASS,
                exit_code=0,
                duration_s=1.25,
                tail=f"вывод гейта раунда {round_no:03d}",
            )
        ],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=1, deletions=1),
    )


def _state(phase: SessionPhase, round_no: int) -> SessionState:
    """`SessionState` в фазе `phase` раунда `round_no`."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=phase,
        current_round=round_no,
        task=TaskSpec(prompt="Почини экспорт CSV", attachments=[], mode=Mode.DEVELOP),
        agents={
            Role.AUTHOR: AgentRef(
                adapter="claude_code", model="opus", session_ref="ref-author"
            ),
            Role.REVIEWER: AgentRef(adapter="claude_code", model="sonnet"),
        },
        limits=Limits(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        budget_used=BudgetUsed(),
    )


def _fixed_base_rev(root: Path, round_no: int, *, base_commit: str) -> str:
    """Цель сброса без обращения к истории git — одна для всех раундов."""
    return _BASE_REV


@dataclass
class Harness:
    """Собранное окружение цикла и общий лог вызовов портов."""

    ctx: StepContext
    fsm: SessionFsm
    log: list[str]
    git: SpyGit


def _make_harness(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    phase: SessionPhase,
    round_no: int,
    author_replies: tuple[str, ...] = (),
    reviewer_replies: tuple[str, ...] = (),
) -> Harness:
    """Собирает `StepContext` с фейками на всех границах цикла."""
    bootstrap_session(root)
    steps = import_module("disputatio.runtime.steps")
    monkeypatch.setattr(steps, "base_rev", _fixed_base_rev)

    log: list[str] = []
    store = FileStateStore(root)
    sink = RecordingSink()
    git = SpyGit(log=log)
    fsm = SessionFsm(
        _state(phase, round_no), store=store, sink=sink, now=lambda: _FROZEN_NOW
    )
    deps = RuntimeDeps(
        workspace_root=root,
        artifact_root=root,
        store=store,
        sink=sink,
        author=QueueAdapter(name="author", log=log, replies=list(author_replies)),
        reviewer=QueueAdapter(name="reviewer", log=log, replies=list(reviewer_replies)),
        verifier=PassingVerifier(log=log),
        git=git,
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    return Harness(
        ctx=StepContext(deps=deps, fsm=fsm, base_commit=_BASE_COMMIT),
        fsm=fsm,
        log=log,
        git=git,
    )


def _install_export_step(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    """Ставит временный шаг `EXPORTING`, доводящий сессию до `DONE`.

    Заглушка ровно потому, что настоящий `exporting.export` — предмет
    отдельной задачи: цикл здесь проверяется на возвращаемом значении, а не
    на содержимом `result/`.
    """

    def fake_export(ctx: StepContext) -> None:
        """Отмечает экспорт и переводит сессию в `DONE`."""
        log.append(_EXPORT_CALL)
        ctx.fsm.transition(SessionPhase.DONE)

    monkeypatch.setattr(
        loop_module,
        "STEP_BY_PHASE",
        {**loop_module.STEP_BY_PHASE, SessionPhase.EXPORTING: fake_export},
    )


def _drive(harness: Harness) -> SessionState:
    """Крутит цикл до остановки — `drive` асинхронна, как и шаги в ней."""
    drive: Callable[[StepContext], Awaitable[SessionState]] = loop_module.drive
    final: SessionState = anyio.run(drive, harness.ctx)
    return final


def test_drive_returns_the_phase_it_reached_not_the_one_it_started_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Результат — состояние ПОСЛЕ шага, а не снимок, снятый до цикла.

    Фаза, чей шаг уводит сессию сам (как `DECIDING` через `apply_decision`),
    — единственное место, где эти два состояния различимы за одну итерацию:
    в `NEXT_PHASE` её нет, и вернуть тут прошлое состояние можно молча.
    """
    harness = _make_harness(
        tmp_path, monkeypatch, phase=SessionPhase.EXPORTING, round_no=2
    )
    _install_export_step(monkeypatch, harness.log)
    before = harness.fsm.state

    result = _drive(harness)

    assert harness.log == [_EXPORT_CALL]
    assert before.state is SessionPhase.EXPORTING
    assert result is not before
    assert result.state is SessionPhase.DONE
    assert result is harness.fsm.state


def test_drive_returns_the_final_state_of_a_two_round_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сессия IDLE → два раунда → `DONE`: в результате оба итога цикла.

    И фаза, и номер раунда обязаны быть теми, до которых цикл дошёл:
    вернувший начальное состояние диспетчер отчитался бы фазой `IDLE`
    раунда 0 о сессии, которая свела два раунда и зафиксировала работу.
    """
    harness = _make_harness(
        tmp_path,
        monkeypatch,
        phase=SessionPhase.IDLE,
        round_no=0,
        author_replies=(_proposal(1), _proposal(2)),
        reviewer_replies=(_request_changes(1), _approve(2)),
    )
    _install_export_step(monkeypatch, harness.log)

    result = _drive(harness)

    assert harness.log[-1] == _EXPORT_CALL
    assert harness.git.commits == [1, 2]
    assert result.state is SessionPhase.DONE
    assert result.current_round == 2
