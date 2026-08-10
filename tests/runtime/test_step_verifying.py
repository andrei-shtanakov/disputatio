"""Шаг VERIFYING ([REQ-004], [DESIGN-004]).

[TASK-009]. Шаг тонкий — прогнать гейты и записать отчёт, — но обёртка
вокруг `Verifier.verify` держит два обещания, которые ломаются молча:

* **гейт-события обрамляют прогон.** `VerifierRunner` синхронен и об
  `EventSink` не знает: пакет `verifier` от порта событий не зависит вовсе.
  Значит `gate_started` эмитит runtime — по каждому `GateSpec` **до**
  вызова, а `gate_finished` — по каждому `GateResult` после. Пиньётся это
  не только общим spy-логом, но и снимком журнала, снятым спай-верификатором
  ИЗНУТРИ `verify`: реализация, эмитящая всё скопом после прогона, даёт
  правильный итоговый список и неправильный снимок;
* **пара «спека → результат» держится индексом и именем.** Порядок
  `report.gates` = порядок конфигурации — гарантия `VerifierRunner`; runtime
  обязан не переставить и не отсортировать. Имена гейтов нарочно не
  алфавитны (`tests`, `lint`, `types`), иначе сортировка прошла бы мимо
  теста;
* **`overall == fail` ничего не отменяет** ([REQ-004]). Ребро
  `VERIFYING → REVIEWING` безусловно, и правило §4 выражено ОТСУТСТВИЕМ
  кода: `verify` не содержит ни ветвления, ни `raise`, ни `assert` — ни сам,
  ни в вызываемых из него хелперах (AST-скан транзитивного замыкания).
  Проверять «ветки нет» списком известных мутаций бессмысленно, поэтому
  запрещена сама форма.

`by_alias=True` при записи отчёта семантически неубиваем: `ArtifactBase`
объявляет `serialize_by_alias=True`, и `model_dump_json()` без аргументов
даёт тот же ключ `schema`. Поэтому пинов два — поведенческий (файл читается
обратно `model_validate_json`, ключ схемы называется `schema`, а не
`schema_`) и синтаксический (вызов в `steps.py` несёт `by_alias=True`):
первый переживёт смену формы вызова, второй — смену конфига contracts.

`runtime.steps` до реализации `verify` не имеет нужного символа, поэтому
доступ идёт через `_steps`/`_layout`/`_steps_attr`: отсутствие модуля или
символа переводится в `AssertionError`, а не в ошибку коллекции.
"""

import ast
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from disputatio.contracts import (
    AgentRef,
    AgentTurn,
    BudgetUsed,
    DiffStats,
    Event,
    EventSource,
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
from disputatio.core import TRANSITIONS, SessionFsm
from disputatio.events import write_round_artifact
from disputatio.runtime import RuntimeDeps
from disputatio.verifier import GateSpec

_FROZEN_NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
_ROUND = 3
_SESSION_ID = "s-verifying"

# Имена нарочно не в алфавитном порядке: сортировка гейтов — самая дешёвая
# ошибка обёртки, и на алфавитном списке она осталась бы невидимой.
_SPECS = (
    GateSpec(name="tests", cmd="uv run pytest -q"),
    GateSpec(name="lint", cmd="ruff check ."),
    GateSpec(name="types", cmd="pyrefly check", enabled=False),
)


def _steps() -> ModuleType:
    """Модуль `runtime/steps.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.steps")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/steps.py: {exc}") from exc


def _layout() -> ModuleType:
    """Модуль `runtime/layout.py` — read-side зеркало раскладки (ADR-002)."""
    try:
        return import_module("disputatio.runtime.layout")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/layout.py: {exc}") from exc


def _steps_attr(name: str) -> Any:
    """Символ модуля шагов; отсутствие — `AssertionError`, не `AttributeError`."""
    steps = _steps()
    assert hasattr(steps, name), f"runtime/steps.py не определяет {name!r}"
    return getattr(steps, name)


def _steps_tree() -> ast.Module:
    """AST модуля шагов — для проверок «код такой формы отсутствует»."""
    path = _steps().__file__
    assert path is not None, "у runtime/steps.py нет файла на диске"
    return ast.parse(Path(path).read_text(encoding="utf-8"))


def _now() -> datetime:
    """Замороженные часы сессии — событиям нужен детерминированный `ts`."""
    return _FROZEN_NOW


def _monotonic() -> float:
    """Замороженный монотонный счётчик; шаг им не пользуется."""
    return 0.0


@dataclass
class FakeStore:
    """`StateStore`-фейк: журналирует сохранения, на диск не пишет."""

    saved: list[SessionState] = field(default_factory=list)

    def load(self, session_id: str) -> SessionState:
        """Сессии нет — `KeyError`, как у файловой реализации."""
        raise KeyError(session_id)

    def save(self, state: SessionState) -> None:
        """Запоминает состояние вместо записи `session.json`."""
        self.saved.append(state)


@dataclass
class SpySink:
    """`EventSink`-фейк: событие целиком в список и строка в общий лог."""

    log: list[str]
    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        """Запоминает событие и отмечает его в общем логе порядка."""
        self.events.append(event)
        self.log.append(f"{event.type.value}:{event.payload.get('name', '')}")


@dataclass
class SpyVerifier:
    """`Verifier`-фейк со снимком журнала, снятым ИЗНУТРИ прогона.

    Снимок — единственный способ отличить «эмитим `gate_started` до
    прогона» от «эмитим оба списка после»: итоговый лог у обеих реализаций
    одинаков, а снимок — нет.
    """

    log: list[str]
    report: VerificationReport
    rounds: list[int] = field(default_factory=list)
    log_at_call: list[str] = field(default_factory=list)

    def verify(self, round_no: int) -> VerificationReport:
        """Снимает журнал на момент прогона и отдаёт заготовленный отчёт."""
        self.rounds.append(round_no)
        self.log_at_call.extend(self.log)
        self.log.append("verify")
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


@dataclass(frozen=True)
class Written:
    """Один вызов `write_round_artifact`, пойманный спаем."""

    round_no: int
    name: str
    content: str
    path: Path


@dataclass
class Harness:
    """Собранное окружение шага: контекст, спаи и общий лог порядка."""

    root: Path
    ctx: Any
    fsm: SessionFsm
    sink: SpySink
    verifier: SpyVerifier
    report: VerificationReport
    log: list[str]
    writes: list[Written]


def _state(phase: SessionPhase = SessionPhase.VERIFYING) -> SessionState:
    """`SessionState` раунда `_ROUND` в фазе `phase`."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=phase,
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


def _results(*, failing: bool) -> list[GateResult]:
    """Результаты гейтов в порядке `_SPECS`; `failing` роняет первый из них.

    Третий гейт — `skip` без `exit_code`: `gate_finished` обязан нести поле
    и для него, иначе «кода нет» стало бы неотличимо от «поля нет».
    """
    return [
        GateResult(
            name="tests",
            cmd=_SPECS[0].cmd,
            status=GateStatus.FAIL if failing else GateStatus.PASS,
            exit_code=1 if failing else 0,
            duration_s=1.5,
            tail="1 failed" if failing else "3 passed",
        ),
        GateResult(
            name="lint",
            cmd=_SPECS[1].cmd,
            status=GateStatus.PASS,
            exit_code=0,
            duration_s=0.25,
        ),
        GateResult(
            name="types",
            cmd=_SPECS[2].cmd,
            status=GateStatus.SKIP,
            reason="disabled in config",
        ),
    ]


def _report(*, failing: bool) -> VerificationReport:
    """`VerificationReport` раунда `_ROUND` с гейтами в порядке `_SPECS`."""
    return VerificationReport(
        round=_ROUND,
        gates=_results(failing=failing),
        overall=OverallStatus.FAIL if failing else OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=4, deletions=2),
    )


def _spy_writer(
    monkeypatch: pytest.MonkeyPatch, log: list[str], writes: list[Written]
) -> None:
    """Подменяет `write_round_artifact` в модуле шага, сохраняя запись."""
    steps = _steps()
    assert hasattr(steps, "write_round_artifact"), (
        "runtime/steps.py обязан писать артефакты через events.write_round_artifact"
    )

    def spy(root: Path, round_no: int, name: str, content: str) -> Path:
        log.append(f"write:{name}")
        path = write_round_artifact(root, round_no, name, content)
        writes.append(Written(round_no=round_no, name=name, content=content, path=path))
        return path

    monkeypatch.setattr(steps, "write_round_artifact", spy)


def _make_harness(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failing: bool = False,
    phase: SessionPhase = SessionPhase.VERIFYING,
) -> Harness:
    """Собирает `StepContext` со спаями на всех наблюдаемых границах шага."""
    log: list[str] = []
    store = FakeStore()
    sink = SpySink(log=log)
    report = _report(failing=failing)
    verifier = SpyVerifier(log=log, report=report)
    fsm = SessionFsm(_state(phase), store=store, sink=sink, now=_now)
    deps = RuntimeDeps(
        root=root,
        store=store,
        sink=sink,
        author=NoAgent(),
        reviewer=NoAgent(),
        verifier=verifier,
        git=NoGit(),
        now=_now,
        monotonic=_monotonic,
    )
    writes: list[Written] = []
    _spy_writer(monkeypatch, log, writes)
    ctx = _steps_attr("StepContext")(
        deps=deps, fsm=fsm, base_commit="0" * 40, gates=_SPECS
    )
    return Harness(
        root=root,
        ctx=ctx,
        fsm=fsm,
        sink=sink,
        verifier=verifier,
        report=report,
        log=log,
        writes=writes,
    )


def _verify(harness: Harness) -> None:
    """Выполняет шаг VERIFYING; шаг синхронен ([DESIGN-004])."""
    _steps_attr("verify")(harness.ctx)


def _gate_events(harness: Harness, kind: EventType) -> list[Event]:
    """События гейтов типа `kind` в порядке эмиссии."""
    return [event for event in harness.sink.events if event.type is kind]


def _functions_reachable_from(tree: ast.Module, root: str) -> list[ast.FunctionDef]:
    """Функция `root` модуля шагов и все функции модуля, вызываемые из неё.

    Замыкание транзитивное: ветку, вынесенную в хелпер, скан обязан видеть
    так же, как ветку в теле — иначе запрет формы обходился бы одной
    лишней функцией.
    """
    defined = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert root in defined, f"runtime/steps.py не определяет функцию {root!r}"

    seen: set[str] = set()
    queue = [root]
    while queue:
        current = queue.pop()
        if current in seen or current not in defined:
            continue
        seen.add(current)
        for node in ast.walk(defined[current]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                queue.append(node.func.id)
    return [defined[name] for name in sorted(seen)]


def test_gate_started_precedes_the_run_and_gate_finished_follows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гейт-события обрамляют прогон, по одному на гейт ([REQ-004])."""
    harness = _make_harness(tmp_path, monkeypatch)

    _verify(harness)

    assert harness.log == [
        "gate_started:tests",
        "gate_started:lint",
        "gate_started:types",
        "verify",
        "gate_finished:tests",
        "gate_finished:lint",
        "gate_finished:types",
        "write:verification.json",
    ]
    assert harness.verifier.log_at_call == [
        "gate_started:tests",
        "gate_started:lint",
        "gate_started:types",
    ], "gate_started обязан уйти в журнал ДО прогона гейтов, а не после"
    assert harness.verifier.rounds == [_ROUND]


def test_gate_events_are_attributed_to_the_verifier_round_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гейт-события несут раунд, сессию и источник `verifier` (§8)."""
    harness = _make_harness(tmp_path, monkeypatch)

    _verify(harness)

    assert len(harness.sink.events) == 2 * len(_SPECS)
    for event in harness.sink.events:
        assert event.session == _SESSION_ID
        assert event.round == _ROUND
        assert event.source is EventSource.VERIFIER
        assert event.ts == _FROZEN_NOW


def test_gate_finished_carries_name_status_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gate_finished` несёт имя, статус и `exit_code` ([REQ-004])."""
    harness = _make_harness(tmp_path, monkeypatch, failing=True)

    _verify(harness)

    finished = _gate_events(harness, EventType.GATE_FINISHED)
    payloads = [event.payload for event in finished]
    assert payloads == [
        {"name": "tests", "status": "fail", "exit_code": 1},
        {"name": "lint", "status": "pass", "exit_code": 0},
        {"name": "types", "status": "skip", "exit_code": None},
    ]


def test_gate_started_carries_the_spec_not_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gate_started` описывает то, что запускается: имя и команду спеки."""
    harness = _make_harness(tmp_path, monkeypatch, failing=True)

    _verify(harness)

    started = _gate_events(harness, EventType.GATE_STARTED)
    assert [event.payload for event in started] == [
        {"name": spec.name, "cmd": spec.cmd} for spec in _SPECS
    ]


def test_specs_pair_to_results_by_index_and_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порядок `report.gates` = порядок `GateSpec`; пара — индекс и имя."""
    harness = _make_harness(tmp_path, monkeypatch, failing=True)

    _verify(harness)

    started = _gate_events(harness, EventType.GATE_STARTED)
    finished = _gate_events(harness, EventType.GATE_FINISHED)
    spec_names = [spec.name for spec in _SPECS]
    assert [event.payload["name"] for event in started] == spec_names
    assert [gate.name for gate in harness.report.gates] == spec_names
    for index, spec in enumerate(_SPECS):
        assert harness.report.gates[index].name == spec.name
        assert finished[index].payload["name"] == spec.name

    # Порядок обязан дожить и до диска: пересортировка гейтов перед записью
    # разошлась бы с гейт-событиями, которые уже ушли в UI.
    written = json.loads(harness.writes[0].path.read_text(encoding="utf-8"))
    assert [gate["name"] for gate in written["gates"]] == spec_names


def test_verification_json_is_written_by_alias_and_reads_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отчёт лёг в `rounds/003/verification.json` и читается обратно."""
    layout = _layout()
    harness = _make_harness(tmp_path, monkeypatch, failing=True)

    _verify(harness)

    assert [write.name for write in harness.writes] == ["verification.json"]
    written = harness.writes[0]
    assert written.round_no == _ROUND
    mirrored = layout.round_artifact(tmp_path, _ROUND, layout.VERIFICATION_NAME)
    assert written.path == mirrored
    assert layout.VERIFICATION_NAME == "verification.json"

    raw = mirrored.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["schema"] == "disputatio/v1"
    assert "schema_" not in payload
    assert VerificationReport.model_validate_json(raw) == harness.report


def test_report_is_serialized_with_by_alias_explicitly() -> None:
    """`model_dump_json(by_alias=True)` — явный вызов, а не дефолт конфига.

    `ArtifactBase` включает `serialize_by_alias=True`, поэтому убрать
    аргумент поведенчески незаметно; пин синтаксический и намеренно такой.
    """
    tree = _steps_tree()
    functions = _functions_reachable_from(tree, "verify")

    aliased = [
        node
        for function in functions
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "model_dump_json"
        and any(
            keyword.arg == "by_alias"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
    ]
    assert len(aliased) == 1, (
        "verify обязан сериализовать отчёт model_dump_json(by_alias=True)"
    )


def test_overall_fail_neither_raises_nor_blocks_reviewing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`overall == fail` не роняет шаг и не отменяет `REVIEWING` ([REQ-004])."""
    harness = _make_harness(tmp_path, monkeypatch, failing=True)

    _verify(harness)

    raw = harness.writes[0].path.read_text(encoding="utf-8")
    assert json.loads(raw)["overall"] == "fail"
    assert harness.fsm.state.state is SessionPhase.VERIFYING

    harness.fsm.transition(SessionPhase.REVIEWING)

    assert harness.fsm.state.state is SessionPhase.REVIEWING
    assert SessionPhase.REVIEWING in TRANSITIONS[SessionPhase.VERIFYING]


def test_step_has_no_branch_able_to_cancel_the_transition() -> None:
    """Правило §4 выражено отсутствием кода: ни ветки, ни `raise` в `verify`."""
    tree = _steps_tree()
    functions = _functions_reachable_from(tree, "verify")

    for function in functions:
        for node in ast.walk(function):
            # Формы, которыми выражается «отчёт отменил шаг». Запрещены
            # целиком: у прямолинейного `verify` законных ветвлений нет.
            assert not isinstance(
                node, ast.If | ast.IfExp | ast.Match | ast.Raise | ast.Assert
            ), (
                f"{function.name} содержит {type(node).__name__} — "
                "в шаге VERIFYING нет ветки, способной отменить REVIEWING"
            )
