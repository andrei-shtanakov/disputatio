"""Жизненный цикл хода автора: `SessionLifecyclePolicy` (SPEC-002 §7.1, P9).

P9 говорит про **ход автора**, а ход — это один вызов адаптера, а не один
раунд: внутри одного `PROPOSING` их несколько, если вывод не прошёл схему и
сработал `schema_retries`. Отсюда три утверждения набора.

* **Счёт идёт по вызовам адаптера, а не по раундам и не по агентам.**
  Ревьюер лицензии на запись не имеет вовсе (§7), и обрамлять его ход
  снапшотом control plane нечего.
* **Пары обрамляют КАЖДУЮ попытку.** Обрамление шага целиком дало бы одну
  пару на раунд, и подмена управляющих файлов между retry-попытками осталась
  бы незамеченной: вторая попытка успела бы вернуть байты на место, а сверка
  после шага увидела бы исходный снапшот. Порядок пинится общим spy-логом:
  «before второй пары» обязан стоять ДО второго вызова адаптера, а не после.
* **Ошибка политики закрывает сессию fail-closed.** Проверяется не
  исключение, а `session.json`: durable-состояние обязано стать `FAILED`,
  иначе следующий `resume` счёл бы сессию активной — и подмену control plane
  никто не заметил бы во второй раз. Причина уходит в журнал событием
  `error` с кодом `invariant_violation`.

Порты подменены по тем же причинам, что и в `test_round_boundary.py`: автор,
ревьюер и верификатор — фейки, git настоящий во временном репозитории (цель
сброса раунда 2 вычисляется по истории), журнал и хранилище настоящие —
утверждения третьего теста именно о них.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any

import anyio
import pytest

from disputatio.contracts import (
    AgentRef,
    AgentTurn,
    BudgetUsed,
    DiffStats,
    Event,
    EventType,
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
from disputatio.events import (
    FileStateStore,
    JsonlEventSink,
    bootstrap_session,
    write_config_snapshot,
)
from disputatio.events.paths import events_jsonl_path, session_dir
from disputatio.runtime import (
    AgentConfig,
    GitCli,
    LimitsConfig,
    RuntimeConfig,
    RuntimeDeps,
)
from disputatio.runtime.loop import drive, resume_session
from disputatio.runtime.steps import StepContext
from disputatio.verifier import GateSpec

_SESSION_ID = "20260828-130000-lif0"
_CREATED_AT = datetime(2026, 8, 28, 13, 0, 0, tzinfo=UTC)
_CLOCK_STEP = timedelta(seconds=1)
_MONOTONIC_STEP = 0.25

_GATE = GateSpec(name="pytest", cmd="uv run pytest -q", enabled=True)

# Имя фейкового адаптера в реестре композиции — нужно только `resume_session`,
# который собирает порты сам, из снапшота конфига.
_ADAPTER_NAME = "lifecycle_fake"

_INVARIANT_VIOLATION = "invariant_violation"


class ControlPlaneTampered(Exception):
    """Отказ политики P9: снапшот не сошёлся со сверкой после хода."""


@dataclass
class ScriptedAgent:
    """`AgentAdapter`-фейк: очередь ответов и общий журнал порядка вызовов.

    Автор вдобавок правит рабочее дерево, как правил бы настоящий: без этого
    `commit_round` не создал бы коммита, и раунду 2 не на что было бы
    сбрасываться.
    """

    role: Role
    root: Path
    replies: list[str]
    log: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует вызов в общий лог и отдаёт следующий ответ очереди."""
        self.prompts.append(prompt)
        self.log.append(f"run:{self.role.value}:{len(self.prompts)}")
        assert self.replies, (
            f"{self.role.value}: очередь ответов исчерпана на вызове "
            f"{len(self.prompts)}"
        )
        if self.role is Role.AUTHOR:
            (self.root / f"feature_{len(self.prompts)}.py").write_text(
                f"# работа автора, вызов {len(self.prompts)}\n"
                f"VALUE = {len(self.prompts)}\n",
                encoding="utf-8",
            )
        return AgentTurn(text=self.replies.pop(0), session_ref=session_ref)


@dataclass
class SpyLifecycle:
    """`SessionLifecyclePolicy`-фейк: журнал хуков в общий лог порядка.

    `raise_on` — точка отказа: `"before"`/`"after"`. Отказ моделирует именно
    несошедшуюся сверку P9, а не падение постороннего порта, поэтому тип
    исключения свой.
    """

    log: list[str]
    raise_on: str | None = None
    phases: list[str] = field(default_factory=list)

    def before_author_turn(self, state: SessionState) -> None:
        """Снапшот перед ходом; журналирует фазу, в которой его позвали."""
        self._record("before", state)

    def after_author_turn(self, state: SessionState) -> None:
        """Сверка после хода — до чтения артефактов хода."""
        self._record("after", state)

    def _record(self, point: str, state: SessionState) -> None:
        """Общая половина обоих хуков: журнал, затем — возможный отказ."""
        self.log.append(point)
        self.phases.append(state.state.value)
        if self.raise_on == point:
            raise ControlPlaneTampered(
                f"снапшот control plane не сошёлся на {point}_author_turn"
            )

    @property
    def pairs(self) -> int:
        """Число завершённых пар before/after."""
        return min(self.log.count("before"), self.log.count("after"))


@dataclass
class GreenVerifier:
    """`Verifier`-фейк: зелёный отчёт по каждому запрошенному раунду."""

    def verify(self, round_no: int) -> VerificationReport:
        """Отдаёт `overall == pass` — гейты не предмет этого набора."""
        return VerificationReport(
            round=round_no,
            gates=[
                GateResult(
                    name=_GATE.name,
                    cmd=_GATE.cmd,
                    status=GateStatus.PASS,
                    exit_code=0,
                    duration_s=0.5,
                    tail=f"1 passed (раунд {round_no:03d})",
                )
            ],
            overall=OverallStatus.PASS,
            diff_stats=DiffStats(files=1, insertions=2, deletions=0),
        )


@dataclass
class Clocks:
    """Инжектированные часы сессии: детерминированные и монотонные."""

    ticks: int = 0
    monotonic_ticks: int = 0

    def now(self) -> datetime:
        """Стенные часы: каждый вызов на шаг позже предыдущего."""
        self.ticks += 1
        return _CREATED_AT + _CLOCK_STEP * self.ticks

    def monotonic(self) -> float:
        """Монотонные часы бюджета."""
        self.monotonic_ticks += 1
        return self.monotonic_ticks * _MONOTONIC_STEP


def test_lifecycle_called_per_adapter_run(git_repo: Path) -> None:
    """Пар ровно столько, сколько вызовов АВТОРА: не раундов и не агентов."""
    log: list[str] = []
    author = ScriptedAgent(
        role=Role.AUTHOR,
        root=git_repo,
        replies=[_proposal(1), _proposal(2)],
        log=log,
    )
    reviewer = ScriptedAgent(
        role=Role.REVIEWER,
        root=git_repo,
        replies=[_request_changes(1), _approve(2)],
        log=log,
    )
    policy = SpyLifecycle(log=log)
    ctx = _context(git_repo, author=author, reviewer=reviewer)

    final = anyio.run(lambda: drive(ctx, lifecycle=policy))

    assert final.state is SessionPhase.DONE
    assert len(author.prompts) == 2
    assert len(reviewer.prompts) == 2
    assert policy.pairs == len(author.prompts)
    # Обрамляй цикл каждый вызов адаптера — пар было бы четыре: ход ревьюера
    # писателем не бывает никогда (§7), и снапшота control plane ему не надо.
    assert policy.log.count("before") == 2
    assert policy.phases == [SessionPhase.PROPOSING.value] * 4
    assert [entry for entry in log if entry.startswith("run:author")] == [
        "run:author:1",
        "run:author:2",
    ]


def test_lifecycle_wraps_schema_retry_attempts(git_repo: Path) -> None:
    """Невалидный вывод автора: две пары в ОДНОМ раунде, вторая — до попытки."""
    log: list[str] = []
    author = ScriptedAgent(
        role=Role.AUTHOR,
        root=git_repo,
        replies=["ответ без фронтматтера — схема не пройдена", _proposal(1)],
        log=log,
    )
    reviewer = ScriptedAgent(
        role=Role.REVIEWER, root=git_repo, replies=[_request_changes(1)], log=log
    )
    policy = SpyLifecycle(log=log)
    ctx = _context(
        git_repo,
        author=author,
        reviewer=reviewer,
        limits=_limits(max_rounds=1),
    )

    final = anyio.run(lambda: drive(ctx, lifecycle=policy))

    assert final.state is SessionPhase.DONE
    assert len(author.prompts) == 2
    assert policy.pairs == 2
    # Порядок, а не только счёт: вторая сверка обязана стоять ДО второго
    # вызова адаптера. Обрамление шага целиком дало бы одну пару вокруг обеих
    # попыток, и подмена между ними осталась бы невидимой.
    assert log[:6] == [
        "before",
        "run:author:1",
        "after",
        "before",
        "run:author:2",
        "after",
    ]


@pytest.mark.parametrize("point", ["before", "after"])
def test_lifecycle_error_fails_session(
    git_repo: Path, point: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отказ политики: `session.json` — `FAILED`, причина в журнале, resume мимо."""
    log: list[str] = []
    author = ScriptedAgent(
        role=Role.AUTHOR, root=git_repo, replies=[_proposal(1)], log=log
    )
    reviewer = ScriptedAgent(
        role=Role.REVIEWER, root=git_repo, replies=[_request_changes(1)], log=log
    )
    policy = SpyLifecycle(log=log, raise_on=point)
    ctx = _context(git_repo, author=author, reviewer=reviewer)
    write_config_snapshot(git_repo, _config().render_toml())

    with pytest.raises(ControlPlaneTampered):
        anyio.run(lambda: drive(ctx, lifecycle=policy))

    # Durable-состояние, а не только исключение: сессия, оставшаяся в
    # PROPOSING, считалась бы для resume активной, и подмену control plane
    # никто не заметил бы во второй раз.
    assert _session_json(git_repo)["state"] == SessionPhase.FAILED.value
    assert _reason_events(git_repo) == [_INVARIANT_VIOLATION]
    assert _phases(git_repo)[-1] == ("PROPOSING", "FAILED")
    assert len(reviewer.prompts) == 0

    seen_before_resume = len(author.prompts)
    _register_agents(monkeypatch, author=author, reviewer=reviewer)
    resumed = anyio.run(lambda: _resume(git_repo))
    assert resumed.state is SessionPhase.FAILED
    assert len(author.prompts) == seen_before_resume


async def _resume(root: Path) -> SessionState:
    """Поднимает сессию штатным `resume_session` — тем же, что зовёт CLI."""
    clocks = Clocks()
    return await resume_session(
        root,
        _SESSION_ID,
        git=GitCli(root),
        verifier=GreenVerifier(),
        now=clocks.now,
        monotonic=clocks.monotonic,
    )


def _register_agents(
    monkeypatch: pytest.MonkeyPatch,
    *,
    author: ScriptedAgent,
    reviewer: ScriptedAgent,
) -> None:
    """Ставит фейки в реестр композиции — единственный шов подмены агента.

    Нужен только `resume_session`: он собирает порты сам, из снапшота
    конфига, и подсунуть ему готовый адаптер больше негде.
    """
    composition = import_module("disputatio.runtime.composition")
    agents = {Role.AUTHOR: author, Role.REVIEWER: reviewer}

    def factory(
        *, role: Role, session_dir: Path, event_sink: object, session: str
    ) -> ScriptedAgent:
        """Отдаёт заранее заготовленный фейк для роли."""
        return agents[role]

    monkeypatch.setitem(composition.ADAPTER_FACTORIES, _ADAPTER_NAME, factory)


def _context(
    root: Path,
    *,
    author: ScriptedAgent,
    reviewer: ScriptedAgent,
    limits: Limits | None = None,
) -> StepContext:
    """Контекст холодного старта на фейковых портах."""
    bootstrap_session(root)
    clocks = Clocks()
    state = _state(limits=limits)
    deps = RuntimeDeps(
        workspace_root=root.resolve(),
        artifact_root=root.resolve(),
        store=FileStateStore(root),
        sink=JsonlEventSink(root),
        author=author,
        reviewer=reviewer,
        verifier=GreenVerifier(),
        git=GitCli(root),
        now=clocks.now,
        monotonic=clocks.monotonic,
    )
    deps.store.save(state)
    return StepContext(
        deps=deps,
        fsm=SessionFsm(state, store=deps.store, sink=deps.sink, now=deps.now),
        base_commit="HEAD",
        gates=(_GATE,),
    )


def _limits(*, max_rounds: int = 5) -> Limits:
    """Лимиты сессии; `schema_retries=1` — ровно один повтор попытки."""
    return Limits(
        max_rounds=max_rounds,
        max_total_tokens=100_000,
        max_wall_seconds=600,
        schema_retries=1,
    )


def _config() -> RuntimeConfig:
    """Снапшот конфига сессии — вход `resume_session`."""
    return RuntimeConfig(
        session_id=_SESSION_ID,
        mode=Mode.DEVELOP,
        base_commit="HEAD",
        task_prompt="ЗАДАЧА-ПОЛЬЗОВАТЕЛЯ: почини экспорт CSV",
        author=AgentConfig(adapter=_ADAPTER_NAME, model="opus"),
        reviewer=AgentConfig(adapter=_ADAPTER_NAME, model="sonnet"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        gates=(_GATE,),
        attachments=(),
    )


def _state(*, limits: Limits | None = None) -> SessionState:
    """Стартовое состояние сессии: `IDLE`, раунд 0."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_CREATED_AT,
        state=SessionPhase.IDLE,
        current_round=0,
        task=TaskSpec(
            prompt="ЗАДАЧА-ПОЛЬЗОВАТЕЛЯ: почини экспорт CSV",
            attachments=[],
            mode=Mode.DEVELOP,
        ),
        agents={
            Role.AUTHOR: AgentRef(
                adapter=_ADAPTER_NAME, model="opus", session_ref=None
            ),
            Role.REVIEWER: AgentRef(
                adapter=_ADAPTER_NAME, model="sonnet", session_ref=None
            ),
        },
        limits=_limits() if limits is None else limits,
        budget_used=BudgetUsed(tokens=0, wall_seconds=0.0, cost_usd_est=0.0),
    )


def _proposal(round_no: int) -> str:
    """`proposal.md` раунда `round_no` — ответ автора с YAML-фронтматтером."""
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
        f"Работа раунда {round_no:03d}: разделитель вынесен в конфиг.\n"
    )


def _request_changes(round_no: int) -> str:
    """Ревью раунда `round_no`: `request_changes` с major-замечанием."""
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.7,
        issues=[
            Issue(
                id=f"I-{round_no:03d}-1",
                severity=Severity.MAJOR,
                file="feature.py",
                claim="разделитель всё ещё читается из локали процесса",
                evidence="feature.py:2 — VALUE зависит от окружения",
                suggestion="взять разделитель из конфига сессии",
            )
        ],
        checked=["feature.py", f"rounds/{round_no:03d}/changes.patch"],
        summary="правка в нужном месте, но источник разделителя не изменился",
    ).model_dump_json(by_alias=True)


def _approve(round_no: int) -> str:
    """Ревью раунда `round_no`: `approve` при зелёных гейтах."""
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.APPROVE,
        confidence=0.9,
        issues=[],
        checked=["feature.py", f"rounds/{round_no:03d}/changes.patch"],
        summary="замечание раунда 1 закрыто, гейты зелёные",
    ).model_dump_json(by_alias=True)


def _events(root: Path) -> list[Event]:
    """Все события журнала сессии в порядке записи."""
    lines = events_jsonl_path(root).read_text(encoding="utf-8").splitlines()
    return [Event.model_validate_json(line) for line in lines if line]


def _reason_events(root: Path) -> list[str]:
    """Коды причин из событий `error` — machine-readable, не человеческий текст."""
    return [
        str(event.payload["reason"])
        for event in _events(root)
        if event.type is EventType.ERROR and "reason" in event.payload
    ]


def _phases(root: Path) -> list[tuple[str, str]]:
    """Последовательность переходов из `events.jsonl` — пары «откуда, куда»."""
    return [
        (str(event.payload["from"]), str(event.payload["to"]))
        for event in _events(root)
        if event.type is EventType.STATE_CHANGE
    ]


def _session_json(root: Path) -> dict[str, Any]:
    """`session.json` как он лежит на диске — байты, а не объект в памяти."""
    payload = (session_dir(root) / "session.json").read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(payload)
    return parsed
