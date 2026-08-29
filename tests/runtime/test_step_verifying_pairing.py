"""Что шаг VERIFYING берёт из отчёта, а что — из сессии ([REQ-004]).

[TASK-009], follow-up к `test_step_verifying.py`. Тот тест пинит порядок
операций и состав payload'ов на фикстуре, где конфигурация и отчёт совпадают
до последнего имени, а все замороженные значения (часы, sink, номер раунда)
совпадают между собой. Совпадение фикстуры и есть дыра: пять подмен
источника данных проходят её насквозь, потому что оба источника отдают одно
и то же.

* **`gate_finished` идёт по `report.gates`, а не по `ctx.gates`.** Списки
  живут врозь: `RuntimeDeps` поля `gates` не имеет, `Verifier` порт отдаёт
  только `verify()`, и сверить их шаг не может — значит расхождение обязано
  быть наблюдаемым, а не молчаливым. Реализация на `zip(ctx.gates, …)` или
  на срезе `report.gates[: len(ctx.gates)]` теряет результаты гейтов,
  которых нет в конфиге шага, — и теряет молча;
* **каталог артефакта берётся у FSM, а не у отчёта.** `report.round`
  приходит из порта; поверив ему, шаг положил бы `verification.json` в
  чужой раунд, а `session.json` об этом бы не узнал;
* **`null` на диске остаётся `null`.** Round-trip через
  `model_validate_json` слеп к `exclude_none`: pydantic восстановит
  умолчания, и равенство моделей сойдётся. Между тем `verification.json`
  читает шаг REVIEWING, и «`exit_code` неизвестен» обязано отличаться от
  «поля нет» ровно так же, как в событии гейта;
* **`ts` — из инжектированных часов**, а не из любого другого времени,
  лежащего в состоянии сессии (`created_at` — второй такой источник);
* **события уходят в порт `deps.sink`**, а не в sink, розданный FSM: в
  проде это один объект, но одинаковость — свойство composition root'а, а
  не шага.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

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
    Limits,
    Mode,
    OverallStatus,
    Role,
    SessionPhase,
    SessionState,
    TaskSpec,
    VerificationReport,
)
from disputatio.core import SessionFsm
from disputatio.runtime import RuntimeDeps
from disputatio.runtime.layout import VERIFICATION_NAME, round_artifact
from disputatio.runtime.steps import StepContext, verify
from disputatio.verifier import GateSpec

_NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
"""Часы сессии. Отличаются от `_CREATED` — иначе любой источник времени сойдётся."""

_CREATED = datetime(2026, 8, 9, 9, 30, 0, tzinfo=UTC)
"""`created_at` состояния: второе время, до которого шагу дела нет."""

_ROUND = 3
_REPORTED_ROUND = 7
"""Раунд, названный отчётом. Не равен `_ROUND`: чужому числу шаг не верит."""

_SESSION_ID = "s-pairing"

# Конфигурация шага и отчёт расходятся И длиной, И именами, И их порядком:
# на совпадающих списках подмена одного источника другим ненаблюдаема.
_SPECS = (
    GateSpec(name="tests", cmd="uv run pytest -q"),
    GateSpec(name="lint", cmd="ruff check ."),
)

_GATE_FIELDS = frozenset(
    {"name", "cmd", "status", "exit_code", "duration_s", "tail", "reason"}
)
"""Полный набор ключей `GateResult` §4.3 — ни один не вправе исчезнуть с диска."""


def _now() -> datetime:
    """Замороженные часы сессии — событиям нужен детерминированный `ts`."""
    return _NOW


def _monotonic() -> float:
    """Замороженный монотонный счётчик; шаг им не пользуется."""
    return 0.0


@dataclass
class FakeStore:
    """`StateStore`-фейк: сохранения в память, на диск не пишет."""

    saved: list[SessionState] = field(default_factory=list)

    def load(self, session_id: str) -> SessionState:
        """Сессии нет — `KeyError`, как у файловой реализации."""
        raise KeyError(session_id)

    def save(self, state: SessionState) -> None:
        """Запоминает состояние вместо записи `session.json`."""
        self.saved.append(state)


@dataclass
class SpySink:
    """`EventSink`-фейк: складывает события в список."""

    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        """Запоминает событие целиком."""
        self.events.append(event)


@dataclass
class StubVerifier:
    """`Verifier`-фейк: отдаёт заготовленный отчёт, что бы ни спросили."""

    report: VerificationReport

    def verify(self, round_no: int) -> VerificationReport:
        """Гейтов не гоняет: предмет теста — обёртка, а не прогон."""
        return self.report


@dataclass
class NoGit:
    """`GitOps`-фейк: шаг VERIFYING git не трогает — любой вызов ошибка."""

    def diff_head(self) -> str:
        """Не вызывается: патч раунда снят шагом PROPOSING."""
        raise AssertionError("VERIFYING не вправе трогать git")

    def commit_round(self, round_no: int) -> None:
        """Не вызывается: коммит раунда принимает DECIDING."""
        raise AssertionError(f"VERIFYING не вправе коммитить раунд {round_no}")

    def reset_hard(self, rev: str) -> None:
        """Не вызывается: сброс дерева — прерогатива PROPOSING."""
        raise AssertionError(f"VERIFYING не вправе сбрасывать дерево на {rev}")

    def clean(self) -> None:
        """Не вызывается: уборка дерева — прерогатива PROPOSING."""
        raise AssertionError("VERIFYING не вправе убирать дерево")


@dataclass
class NoAgent:
    """`AgentAdapter`-фейк: шаг VERIFYING агентов не зовёт вовсе."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Вызов означает, что шаг спутал детерминированные гейты с агентом."""
        raise AssertionError("VERIFYING не вправе звать агента")


@dataclass
class Harness:
    """Окружение шага: контекст, отчёт и два РАЗНЫХ sink'а."""

    root: Path
    ctx: StepContext
    report: VerificationReport
    step_sink: SpySink
    fsm_sink: SpySink


def _state() -> SessionState:
    """`SessionState` раунда `_ROUND` в фазе VERIFYING."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_CREATED,
        state=SessionPhase.VERIFYING,
        current_round=_ROUND,
        task=TaskSpec(prompt="Почини экспорт CSV", attachments=[], mode=Mode.DEVELOP),
        agents={
            Role.AUTHOR: AgentRef(adapter="claude_code", model="opus"),
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


def _report() -> VerificationReport:
    """Отчёт, разошедшийся с `_SPECS`: три гейта, чужие имена, чужой раунд."""
    return VerificationReport(
        round=_REPORTED_ROUND,
        gates=[
            GateResult(
                name="lint",
                cmd="ruff check .",
                status=GateStatus.PASS,
                exit_code=0,
                duration_s=0.25,
                tail="All checks passed",
            ),
            GateResult(
                name="types",
                cmd="pyrefly check",
                status=GateStatus.FAIL,
                exit_code=2,
                duration_s=3.5,
                tail="1 error",
            ),
            GateResult(
                name="docs",
                cmd="mkdocs build",
                status=GateStatus.SKIP,
                reason="disabled in config",
            ),
        ],
        overall=OverallStatus.FAIL,
        diff_stats=DiffStats(files=1, insertions=4, deletions=2),
    )


def _make_harness(root: Path) -> Harness:
    """Собирает `StepContext`, у которого каждый источник данных различим."""
    report = _report()
    step_sink = SpySink()
    fsm_sink = SpySink()
    store = FakeStore()
    deps = RuntimeDeps(
        workspace_root=root,
        artifact_root=root,
        store=store,
        sink=step_sink,
        author=NoAgent(),
        reviewer=NoAgent(),
        verifier=StubVerifier(report=report),
        git=NoGit(),
        now=_now,
        monotonic=_monotonic,
    )
    fsm = SessionFsm(_state(), store=store, sink=fsm_sink, now=_now)
    return Harness(
        root=root,
        ctx=StepContext(deps=deps, fsm=fsm, base_commit="0" * 40, gates=_SPECS),
        report=report,
        step_sink=step_sink,
        fsm_sink=fsm_sink,
    )


def _payload_names(harness: Harness, kind: EventType) -> list[str]:
    """Имена гейтов в событиях типа `kind`, в порядке эмиссии."""
    return [
        event.payload["name"]
        for event in harness.step_sink.events
        if event.type is kind
    ]


def _written(harness: Harness) -> dict[str, object]:
    """Разобранный `verification.json` раунда сессии."""
    path = round_artifact(harness.root, _ROUND, VERIFICATION_NAME)
    return json.loads(path.read_text(encoding="utf-8"))


def test_gate_finished_follows_the_report_not_the_configured_specs(
    tmp_path: Path,
) -> None:
    """Одно `gate_finished` на `GateResult`, даже когда отчёт шире конфига."""
    harness = _make_harness(tmp_path)

    verify(harness.ctx)

    assert _payload_names(harness, EventType.GATE_STARTED) == ["tests", "lint"]
    assert _payload_names(harness, EventType.GATE_FINISHED) == [
        "lint",
        "types",
        "docs",
    ], "gate_finished перечисляет результаты прогона, а не конфигурацию шага"


def test_gate_finished_carries_the_result_status_not_the_spec_position(
    tmp_path: Path,
) -> None:
    """Статус и `exit_code` берутся у того же `GateResult`, что и имя."""
    harness = _make_harness(tmp_path)

    verify(harness.ctx)

    finished = [
        event
        for event in harness.step_sink.events
        if event.type is EventType.GATE_FINISHED
    ]
    assert [event.payload for event in finished] == [
        {"name": "lint", "status": "pass", "exit_code": 0},
        {"name": "types", "status": "fail", "exit_code": 2},
        {"name": "docs", "status": "skip", "exit_code": None},
    ]


def test_artifact_lands_in_the_session_round_not_the_reported_one(
    tmp_path: Path,
) -> None:
    """Каталог раунда называет FSM; `report.round` — данные, не адрес."""
    harness = _make_harness(tmp_path)

    verify(harness.ctx)

    assert round_artifact(tmp_path, _ROUND, VERIFICATION_NAME).is_file()
    assert not round_artifact(tmp_path, _REPORTED_ROUND, VERIFICATION_NAME).exists()
    # Содержимое при этом не переписывается: отчёт лёг на диск как есть.
    assert _written(harness)["round"] == _REPORTED_ROUND


def test_verifier_is_asked_for_the_session_round(tmp_path: Path) -> None:
    """Раунд прогона — из состояния сессии, а не из отчёта прошлого вызова."""
    harness = _make_harness(tmp_path)
    asked: list[int] = []

    @dataclass
    class RecordingVerifier:
        """`Verifier`-фейк, запоминающий номер раунда прогона."""

        report: VerificationReport

        def verify(self, round_no: int) -> VerificationReport:
            """Запоминает запрошенный раунд и отдаёт заготовленный отчёт."""
            asked.append(round_no)
            return self.report

    ctx = StepContext(
        deps=RuntimeDeps(
            workspace_root=harness.root,
            artifact_root=harness.root,
            store=FakeStore(),
            sink=harness.step_sink,
            author=NoAgent(),
            reviewer=NoAgent(),
            verifier=RecordingVerifier(report=harness.report),
            git=NoGit(),
            now=_now,
            monotonic=_monotonic,
        ),
        fsm=harness.ctx.fsm,
        base_commit="0" * 40,
        gates=_SPECS,
    )

    verify(ctx)

    assert asked == [_ROUND]


def test_written_report_keeps_explicit_nulls_and_defaults(tmp_path: Path) -> None:
    """`null` и умолчания доживают до диска: их читает шаг REVIEWING."""
    harness = _make_harness(tmp_path)

    verify(harness.ctx)

    written = _written(harness)
    assert set(written) == {"schema", "round", "gates", "overall", "diff_stats"}
    gates = written["gates"]
    assert isinstance(gates, list)
    for gate in gates:
        assert set(gate) == _GATE_FIELDS, (
            f"гейт {gate.get('name')!r} потерял поля "
            f"{sorted(_GATE_FIELDS - set(gate))} по дороге на диск"
        )
    skipped = gates[2]
    assert skipped["exit_code"] is None
    assert skipped["duration_s"] is None
    assert skipped["tail"] == ""


def test_gate_event_ts_comes_from_the_injected_clock(tmp_path: Path) -> None:
    """`ts` — часы сессии; `created_at` состояния временем события не бывает."""
    harness = _make_harness(tmp_path)

    verify(harness.ctx)

    assert harness.step_sink.events, "шаг обязан эмитить гейт-события"
    for event in harness.step_sink.events:
        assert event.ts == _NOW
        assert event.ts != _CREATED


def test_gate_events_go_to_the_injected_sink(tmp_path: Path) -> None:
    """События уходят в порт `deps.sink`; чужой sink шаг не ищет."""
    harness = _make_harness(tmp_path)

    verify(harness.ctx)

    assert len(harness.step_sink.events) == len(_SPECS) + len(harness.report.gates)
    assert harness.fsm_sink.events == [], (
        "шаг эмитит через deps.sink, а не через sink, розданный FSM"
    )


def test_step_does_not_transition_or_touch_the_store(tmp_path: Path) -> None:
    """Переход `VERIFYING → REVIEWING` — работа диспетчера, не шага."""
    harness = _make_harness(tmp_path)

    verify(harness.ctx)

    assert harness.ctx.fsm.state.state is SessionPhase.VERIFYING
    assert harness.ctx.fsm.state.current_round == _ROUND


def test_empty_gate_config_still_reports_every_result(tmp_path: Path) -> None:
    """`gates=()` не глушит `gate_finished`: результаты приходят из отчёта."""
    harness = _make_harness(tmp_path)
    ctx = StepContext(
        deps=harness.ctx.deps, fsm=harness.ctx.fsm, base_commit="0" * 40, gates=()
    )

    verify(ctx)

    assert _payload_names(harness, EventType.GATE_STARTED) == []
    assert _payload_names(harness, EventType.GATE_FINISHED) == [
        "lint",
        "types",
        "docs",
    ]


@pytest.mark.parametrize("kind", [EventType.GATE_STARTED, EventType.GATE_FINISHED])
def test_gate_events_are_stamped_with_the_session_round(
    tmp_path: Path, kind: EventType
) -> None:
    """Раунд события — раунд сессии, а не раунд, названный отчётом."""
    harness = _make_harness(tmp_path)

    verify(harness.ctx)

    rounds = {event.round for event in harness.step_sink.events if event.type is kind}
    assert rounds == {_ROUND}
