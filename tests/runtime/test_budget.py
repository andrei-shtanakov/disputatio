"""Учёт бюджета на границе шага: `None` ≠ 0 ([REQ-009], [DESIGN-009]).

[TASK-014]. `budget_used` — единственный вход стоп-условия BUDGET_HIT (§5.2),
и ошибиться в нём можно двумя противоположными способами. Первый — принять
«адаптер не сообщил расход» за «адаптер сообщил ноль»: счётчик замирает, и
сессия на адаптере без token-метрик жжёт бюджет вечно, формально его
соблюдая. Второй — принять сообщённый ноль за отсутствие данных и потерять
уже накопленное. Поэтому пинятся обе половины: значение счётчика И
различимость самого факта «сообщил / не сообщил». Второе живёт только на шве
начисления (`token_delta`): в `BudgetUsed` для неизвестности места нет —
`tokens: int`, и приравнять её к нулю можно молча.

Вторая половина задачи — ПОРЯДОК. `SessionState` frozen, публичного мутатора
бюджета у `SessionFsm` нет, менять ядро нельзя, поэтому начисление — это
пересадка FSM: `store.save(новое состояние)`, затем новый `SessionFsm`.
Пересадка внутри retry-петли обнулила бы счётчик schema-повторов текущего
шага и превратила `schema_retries` в бесконечность (ADR-004); пересадка до
`store.save` сломала бы write-ahead (I2). И то и другое наблюдаемо только
порядком, поэтому он читается по ОДНОМУ общему spy-логу — набор «вызвано/не
вызвано» пропустил бы ровно эту ошибку.

Монотонность `wall_seconds` пинится инжектированными часами, а не реальным
временем: каждый отсчёт двигает их на `_TICK`, поэтому и приращение каждого
шага, и итог заранее известны — реальный `time.monotonic` таких значений не
даёт, и подмена шва часов видна сразу.

`runtime/budget.py` до реализации не существует, поэтому доступ к модулю идёт
через `_budget_attr`: отсутствие символа переводится в `AssertionError`, а не
в ошибку коллекции — red-чекпоинт обязан быть падением assertion'ом.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

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
    ProposalParseError,
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
_SESSION_ID = "s-budget"
_BASE_COMMIT = "0" * 40
_BASE_REV = "resolved-base-rev"

# Шаг инжектированных монотонных часов. Значение нарочно крупное и «круглое»:
# 7.5 представимо в двоичной плавающей точке точно, поэтому суммы и разности
# сравниваются на равенство без допуска, а реальные часы столько на шаге
# фейков не набирают — подмена шва была бы видна сразу.
_TICK = 7.5

# Расход, который сообщают фейковые адаптеры. Ноль и `None` в очереди —
# наравне с обычным числом: обе половины REQ-009 обязаны доехать до
# `session.json` через настоящий цикл, а не только через `accumulate`.
_AUTHOR_TOKENS = 120
_REVIEWER_TOKENS = 7

# Итог двух раундов: 120 (автор, раунд 1) + 7 (ревьюер, раунд 1) +
# «не сообщил» (автор, раунд 2) + сообщённый 0 (ревьюер, раунд 2).
_EXPECTED_TOKENS = _AUTHOR_TOKENS + _REVIEWER_TOKENS

# Начальный бюджет для проверок чистого `accumulate`: ненулевой, иначе
# «счётчик обнулён» и «счётчик не вырос» были бы одним и тем же наблюдением.
_PRIOR_TOKENS = 1234
_PRIOR_WALL = 10.5
_ELAPSED = 2.5

# Потолок обращений к агенту в тесте лимита schema-повторов. Пересадка FSM
# внутри retry-петли обнуляет счётчик и делает петлю бесконечной — без
# потолка тест не упал бы, а завис.
_CALL_CAP = 3

_AUTHOR_CALL = "author.run"
_EXPORT_CALL = "export"
_FSM_NEW = "fsm.new"
_STEP_SUCCESS = "fsm.handle_step_success"


def _budget() -> ModuleType:
    """Модуль `runtime/budget.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.budget")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/budget.py: {exc}") from exc


def _budget_attr(name: str) -> Any:
    """Символ модуля бюджета; отсутствие — `AssertionError`, не `AttributeError`."""
    module = _budget()
    assert hasattr(module, name), f"runtime/budget.py не определяет {name!r}"
    return getattr(module, name)


@dataclass
class SpyStore:
    """`StateStore` поверх настоящего `FileStateStore` + общий лог порядка.

    Настоящий, а не фейковый: REQ-009 говорит про `budget_used` сессии, а
    прочитать его после обрыва можно только с диска. Метка кладётся ПОСЛЕ
    записи — «состояние уже переживёт обрыв», иначе порядок в логе был бы
    слабее, чем на диске.
    """

    inner: FileStateStore
    log: list[str]
    saved: list[SessionState] = field(default_factory=list)

    def load(self, session_id: str) -> SessionState:
        """Читает состояние настоящим хранилищем."""
        return self.inner.load(session_id)

    def save(self, state: SessionState) -> None:
        """Пишет `session.json` и отмечает в логе фазу сохранённого состояния."""
        self.inner.save(state)
        self.saved.append(state)
        self.log.append(f"save:{state.state.value}")


@dataclass
class RecordingSink:
    """`EventSink`-фейк: события в список, диска не касается."""

    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        """Запоминает событие вместо дописывания в `events.jsonl`."""
        self.events.append(event)


@dataclass
class TickingClock:
    """Инжектированные монотонные часы: каждый отсчёт двигает время на `_TICK`.

    Ровно один шаг за отсчёт — значит замер шага, взявший два отсчёта
    (до и после тела), даёт `_TICK` и ничего кроме. Часы неубывающие по
    построению: возвращаемое значение считается из числа уже выданных
    отсчётов, и «время пошло назад» здесь невыразимо.
    """

    readings: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        """Отдаёт следующий отсчёт и запоминает его."""
        reading = _TICK * len(self.readings)
        self.readings.append(reading)
        return reading


@dataclass
class QueueAdapter:
    """`AgentAdapter`-фейк: отдаёт ответы очереди вместе с их расходом токенов.

    Расход задаётся ответом, а не адаптером: одна и та же очередь обязана
    уметь и сообщить число, и сообщить ноль, и промолчать (`None`) — иначе
    различие «ноль против неизвестности» пришлось бы проверять тремя
    прогонами цикла вместо одного.
    """

    name: str
    log: list[str]
    replies: list[tuple[str, int | None]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Отмечает вызов и снимает следующий ответ очереди."""
        self.log.append(f"{self.name}.run")
        self.calls.append(prompt)
        assert len(self.calls) <= _CALL_CAP, (
            f"{self.name}: обращений больше {_CALL_CAP} — лимит schema-повторов "
            "сброшен пересадкой FSM внутри retry-петли"
        )
        assert self.replies, (
            f"QueueAdapter {self.name}: очередь ответов исчерпана, лишний вызов"
        )
        text, tokens = self.replies.pop(0)
        return AgentTurn(text=text, session_ref=session_ref, tokens_used=tokens)


@dataclass
class PassingVerifier:
    """`Verifier`-фейк: гейты всегда зелёные, свой расход не сообщает."""

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
        self.log.append("git.diff_head")
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


def _used(tokens: int, wall_seconds: float) -> BudgetUsed:
    """Уже израсходованный бюджет — вход `accumulate`."""
    return BudgetUsed(tokens=tokens, wall_seconds=wall_seconds)


def _turn(tokens_used: int | None) -> AgentTurn:
    """`AgentTurn` с заданным расходом; текст ответа здесь роли не играет."""
    return AgentTurn(text="ответ агента", tokens_used=tokens_used)


def _state(
    phase: SessionPhase,
    round_no: int,
    *,
    budget: BudgetUsed | None = None,
    schema_retries: int = 1,
) -> SessionState:
    """`SessionState` в фазе `phase` раунда `round_no` с бюджетом `budget`."""
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
            schema_retries=schema_retries,
        ),
        budget_used=budget if budget is not None else BudgetUsed(),
    )


def _fixed_base_rev(root: Path, round_no: int, *, base_commit: str) -> str:
    """Цель сброса без обращения к истории git — одна для всех раундов."""
    return _BASE_REV


@dataclass
class Harness:
    """Собранное окружение цикла: контекст, спаи и общий лог порядка."""

    ctx: StepContext
    store: SpyStore
    author: QueueAdapter
    log: list[str]
    transplanted: list[SessionState]


def _make_harness(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    author_replies: tuple[tuple[str, int | None], ...],
    reviewer_replies: tuple[tuple[str, int | None], ...] = (),
    schema_retries: int = 1,
) -> Harness:
    """Собирает `StepContext` со спаями на всех наблюдаемых границах цикла."""
    bootstrap_session(root)
    steps = import_module("disputatio.runtime.steps")
    monkeypatch.setattr(steps, "base_rev", _fixed_base_rev)

    log: list[str] = []
    store = SpyStore(inner=FileStateStore(root), log=log)
    sink = RecordingSink()
    clock = TickingClock()
    author = QueueAdapter(name="author", log=log, replies=list(author_replies))
    reviewer = QueueAdapter(name="reviewer", log=log, replies=list(reviewer_replies))
    fsm = SessionFsm(
        _state(SessionPhase.IDLE, 0, schema_retries=schema_retries),
        store=store,
        sink=sink,
        now=lambda: _FROZEN_NOW,
    )
    deps = RuntimeDeps(
        workspace_root=root,
        artifact_root=root,
        store=store,
        sink=sink,
        author=author,
        reviewer=reviewer,
        verifier=PassingVerifier(log=log),
        git=SpyGit(log=log),
        now=lambda: _FROZEN_NOW,
        monotonic=clock,
    )
    return Harness(
        ctx=StepContext(deps=deps, fsm=fsm, base_commit=_BASE_COMMIT),
        store=store,
        author=author,
        log=log,
        transplanted=[],
    )


def _two_round_harness(root: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Сессия из двух раундов: `request_changes` → `approve` → `DONE`.

    Расход по ответам подобран так, чтобы за один прогон цикла прошли все
    три случая REQ-009: сообщённое число, `None` («не сообщил») и
    сообщённый ноль.
    """
    harness = _make_harness(
        root,
        monkeypatch,
        author_replies=(
            (_proposal(1), _AUTHOR_TOKENS),
            (_proposal(2), None),
        ),
        reviewer_replies=(
            (_request_changes(1), _REVIEWER_TOKENS),
            (_approve(2), 0),
        ),
    )
    _install_export_step(monkeypatch, harness.log)
    return harness


def _install_export_step(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    """Ставит временный шаг `EXPORTING`, доводящий сессию до `DONE`.

    Заглушка ровно потому, что регистрация настоящего `exporting.export` в
    диспетчере — предмет отдельной задачи: бюджет здесь считается по шагам
    раунда, а не по содержимому `result/`.
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


def _spy_transplant(monkeypatch: pytest.MonkeyPatch, harness: Harness) -> None:
    """Подменяет `SessionFsm` в модуле бюджета фабрикой с отметкой в логе.

    Пересадка наблюдаема только по факту создания нового FSM, а создаётся он
    по имени модуля — иначе подменить его тесту было бы нечем. Класс
    остаётся настоящим: фабрика лишь отмечает вызов и запоминает состояние,
    с которым FSM создан.
    """
    module = _budget()
    assert hasattr(module, "SessionFsm"), (
        "runtime/budget.py обязан создавать FSM по имени модуля SessionFsm"
    )

    def factory(
        state: SessionState,
        *,
        store: Any,
        sink: Any,
        now: Callable[[], datetime],
    ) -> SessionFsm:
        """Отмечает пересадку и создаёт настоящий FSM."""
        harness.log.append(_FSM_NEW)
        harness.transplanted.append(state)
        return SessionFsm(state, store=store, sink=sink, now=now)

    monkeypatch.setattr(module, "SessionFsm", factory)


def _spy_step_success(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    """Отмечает в общем логе каждый `handle_step_success` ядра."""
    original = SessionFsm.handle_step_success

    def spy(self: SessionFsm) -> None:
        """Отмечает успешное завершение шага и зовёт настоящий метод."""
        log.append(_STEP_SUCCESS)
        original(self)

    monkeypatch.setattr(SessionFsm, "handle_step_success", spy)


def _drive(harness: Harness) -> SessionState:
    """Крутит цикл до остановки — `drive` асинхронна, как и шаги в ней."""
    drive: Callable[[StepContext], Awaitable[SessionState]] = loop_module.drive
    final: SessionState = anyio.run(drive, harness.ctx)
    return final


def _walls(harness: Harness) -> list[float]:
    """`wall_seconds` всех сохранений `session.json`, в порядке вызовов."""
    return [state.budget_used.wall_seconds for state in harness.store.saved]


def _increments(values: list[float]) -> list[float]:
    """Ненулевые приращения последовательности, в порядке появления."""
    return [after - before for before, after in pairwise(values) if after != before]


def _verified_transplants(harness: Harness) -> int:
    """Число пересадок, у каждой из которых свой `save` — ДО неё.

    Проверяется не только порядок, но и тождество: новый FSM обязан
    получить РОВНО то состояние, которое уже лежит на диске. Разойдись они,
    и write-ahead выполнялся бы формально — сохранённое состояние не имело
    бы отношения к тому, с которым цикл продолжает работать.
    """
    saves = 0
    verified = 0
    for index, mark in enumerate(harness.log):
        if mark.startswith("save:"):
            saves += 1
        elif mark == _FSM_NEW:
            assert index > 0 and harness.log[index - 1].startswith("save:"), (
                f"пересадка #{verified} не следует за сохранением: {harness.log}"
            )
            assert harness.transplanted[verified] is harness.store.saved[saves - 1], (
                f"пересадка #{verified} получила не то состояние, что сохранено"
            )
            verified += 1
    return verified


def test_reported_zero_tokens_keep_the_previous_count() -> None:
    """`tokens_used == 0` — сообщённый ноль: счётчик прибавляет 0 ([REQ-009]).

    «Прибавил ноль» наблюдаемо тем, что накопленное значение осталось на
    месте: обнулить счётчик или потерять его, приняв ноль за «данных нет», —
    ровно та ошибка, которую §5.2 не заметит, пока лимит не перестанет
    срабатывать вовсе. Исходное состояние frozen и обязано остаться
    прежним — начисление возвращает копию, а не правит вход.
    """
    accumulate = _budget_attr("accumulate")
    before = _state(SessionPhase.PROPOSING, 1, budget=_used(_PRIOR_TOKENS, _PRIOR_WALL))

    after = accumulate(before, turn=_turn(0), elapsed_s=_ELAPSED)

    assert after.budget_used.tokens == _PRIOR_TOKENS
    assert after.budget_used.wall_seconds == _PRIOR_WALL + _ELAPSED
    assert before.budget_used.tokens == _PRIOR_TOKENS
    assert before.budget_used.wall_seconds == _PRIOR_WALL


def test_unknown_tokens_neither_grow_the_counter_nor_pass_for_zero() -> None:
    """`tokens_used is None` — «не сообщил», и это не ноль ([REQ-009]).

    Обе половины требования, потому что ломаются они порознь. Первая —
    счётчик не растёт: приписать неизвестности любое число значило бы
    сочинить расход. Вторая — неизвестность не выдаётся за сообщённый ноль:
    в `BudgetUsed` для неё места нет (`tokens: int`), поэтому различить их
    можно только на шве начисления, и `token_delta` обязан отвечать `None`
    там, где на сообщённом нуле отвечает `0`. Сотри это различие — и
    «адаптер без token-метрик» стал бы «адаптер, который ничего не тратит».

    Время шага при этом идёт: `wall_seconds` растёт независимо от того,
    умеет ли адаптер считать токены.
    """
    accumulate = _budget_attr("accumulate")
    token_delta = _budget_attr("token_delta")
    before = _state(SessionPhase.PROPOSING, 1, budget=_used(_PRIOR_TOKENS, _PRIOR_WALL))

    after = accumulate(before, turn=_turn(None), elapsed_s=_ELAPSED)

    assert after.budget_used.tokens == _PRIOR_TOKENS
    assert after.budget_used.wall_seconds == _PRIOR_WALL + _ELAPSED
    assert token_delta(_turn(None)) is None
    assert token_delta(_turn(0)) == 0


def test_absent_turn_does_not_grow_the_token_counter() -> None:
    """Шаг без разговора с агентом токенов не тратит, а время тратит.

    `VERIFYING` и `DECIDING` адаптера не зовут, и `turn is None` для них —
    штатный вход, а не ошибка: гейты и решение стоят времени, но не токенов.
    """
    accumulate = _budget_attr("accumulate")
    token_delta = _budget_attr("token_delta")
    before = _state(SessionPhase.VERIFYING, 1, budget=_used(_PRIOR_TOKENS, _PRIOR_WALL))

    after = accumulate(before, turn=None, elapsed_s=_ELAPSED)

    assert after.budget_used.tokens == _PRIOR_TOKENS
    assert after.budget_used.wall_seconds == _PRIOR_WALL + _ELAPSED
    assert token_delta(None) is None


def test_wall_seconds_grows_by_the_injected_clock_and_never_shrinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wall_seconds` растёт монотонно между раундами ([REQ-009]).

    Часы инжектированные, поэтому проверяется не «больше нуля», а точное
    приращение: каждый шаг обязан добавить ровно один `_TICK` — то есть
    разницу двух отсчётов ОДНИХ часов, взятых вокруг тела шага. Возьми
    реализация время из `time.monotonic`, `datetime.now` или из числа
    раундов — приращения перестали бы совпадать с шагом инжектированных
    часов.

    Убывание проверяется отдельно от роста: между раундами счётчик обязан
    расти, а внутри — не уменьшаться ни на одном сохранении, иначе
    BUDGET_HIT (§5.2) можно было бы «отмотать» очередным шагом.
    """
    harness = _two_round_harness(tmp_path, monkeypatch)

    final = _drive(harness)

    walls = _walls(harness)
    increments = _increments(walls)
    assert walls == sorted(walls), f"wall_seconds убывает: {walls}"
    assert increments == [_TICK] * len(increments)
    # Четыре шага двух раундов — минимум того, что обязано быть начислено;
    # точное число сохранений зависит от терминальной цепочки §5, и пинить
    # его здесь значило бы пинить чужой инвариант.
    assert len(increments) >= 8
    first_round = [
        wall
        for wall, state in zip(walls, harness.store.saved, strict=True)
        if state.current_round == 1
    ]
    second_round = [
        wall
        for wall, state in zip(walls, harness.store.saved, strict=True)
        if state.current_round == 2
    ]
    assert max(second_round) > max(first_round)
    assert final.budget_used.wall_seconds == max(walls)


def test_reported_tokens_reach_session_json_and_unknown_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Токены обоих агентов доезжают до `session.json` ([REQ-009]).

    Один прогон цикла проходит все три случая: автор раунда 1 сообщил
    число, ревьюер раунда 1 сообщил число, автор раунда 2 не сообщил ничего
    (`None`), ревьюер раунда 2 сообщил ноль. Итог обязан быть суммой только
    сообщённого — и обязан читаться с ДИСКА: спай доказывал бы лишь то, что
    метод звали, а стоп-условие §5.2 работает по состоянию, которое
    переживёт обрыв.
    """
    harness = _two_round_harness(tmp_path, monkeypatch)

    final = _drive(harness)

    assert final.state is SessionPhase.DONE
    assert final.budget_used.tokens == _EXPECTED_TOKENS
    on_disk = FileStateStore(tmp_path).load(_SESSION_ID)
    assert on_disk.budget_used.tokens == _EXPECTED_TOKENS
    assert on_disk.budget_used.wall_seconds == final.budget_used.wall_seconds


def test_state_is_saved_before_the_new_fsm_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`store.save` предшествует созданию нового `SessionFsm` (I2).

    Пересадка — это смена состояния, с которым работает цикл, и порядок у
    неё тот же, что у перехода: сначала диск, потом память. Создай FSM
    раньше записи — и обрыв между ними оставил бы на диске состояние, из
    которого уже ушли, то есть потерянный расход при следующем resume.
    """
    harness = _two_round_harness(tmp_path, monkeypatch)
    _spy_transplant(monkeypatch, harness)

    _drive(harness)

    assert _verified_transplants(harness) >= 8


def test_fsm_is_transplanted_only_after_handle_step_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пересадка идёт после `handle_step_success`, а не до (ADR-004).

    Счётчик schema-повторов принадлежит экземпляру FSM: новый экземпляр
    начинает считать с нуля. Пересади FSM раньше, чем шаг объявлен
    успешным, — и счётчик обнулялся бы посреди шага, то есть `schema_retries`
    превратился бы в бесконечность. Порядок читается по общему логу, и
    между успехом шага и пересадкой обязано стоять ровно одно сохранение —
    то самое, которым write-ahead и держится.
    """
    harness = _two_round_harness(tmp_path, monkeypatch)
    _spy_transplant(monkeypatch, harness)
    _spy_step_success(monkeypatch, harness.log)

    _drive(harness)

    assert _STEP_SUCCESS in harness.log
    first_success = harness.log.index(_STEP_SUCCESS)
    first_transplant = harness.log.index(_FSM_NEW)
    assert harness.log.index(_AUTHOR_CALL) < first_success
    assert harness.log[first_success : first_transplant + 1] == [
        _STEP_SUCCESS,
        f"save:{SessionPhase.PROPOSING.value}",
        _FSM_NEW,
    ]


def test_schema_retry_limit_is_not_reset_by_the_budget_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`schema_retries = 1` даёт ровно два обращения к автору ([REQ-006]).

    Прямое следствие ADR-004, проверенное поведением: адаптер сообщает
    расход на каждой попытке, поэтому начисление внутри retry-петли было бы
    «законным» — и обнулило бы счётчик повторов вместе с пересадкой FSM.
    Наблюдаемо это числом обращений (при сброшенном счётчике петля не
    кончается) и отсутствием пересадки вовсе: шаг, не дошедший до конца,
    бюджета не начисляет — начислять его в `FAILED`-сессию уже некому.
    """
    harness = _make_harness(
        tmp_path,
        monkeypatch,
        author_replies=(
            ("ЭТО НЕ ФРОНТМАТТЕР ПРЕДЛОЖЕНИЯ", _AUTHOR_TOKENS),
            ("И ЭТО ТОЖЕ НЕ ОН", _AUTHOR_TOKENS),
            ("И ЭТО", _AUTHOR_TOKENS),
        ),
    )
    _spy_transplant(monkeypatch, harness)

    with pytest.raises(ProposalParseError):
        _drive(harness)

    assert len(harness.author.calls) == 2
    assert _FSM_NEW not in harness.log
    assert FileStateStore(tmp_path).load(_SESSION_ID).state is SessionPhase.FAILED
