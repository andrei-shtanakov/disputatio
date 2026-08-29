"""Шаг DECIDING ([REQ-007], [REQ-011], [REQ-016], [DESIGN-007]).

[TASK-012]. Шаг ничего не решает: единственный источник §5 — `core.decide`,
единственный строитель терминальных цепочек — `SessionFsm.apply_decision`.
Работа runtime сводится к трём вещам, и каждая ломается молча:

* **что подано на вход.** `DecidingInputs` собирается с диска, а не из памяти
  процесса: ревью и отчёт — раунда N, `carried_issues` — замечания раунда
  N−1, отфильтрованные `open_issues_carried` решения N−1, `patch_two_back` —
  патч раунда N−2, `issue_history` — все раунды строго до N. Пять полей,
  четыре разных источника; фикстура, где раунды неразличимы, пропустила бы
  любую подмену одного другим, поэтому каждый раунд здесь помечен своим
  номером в тексте патча, в `file` и в `claim` замечаний.
* **в каком порядке легло на диск.** `decision.json` пишется ДО
  `apply_decision`, финализация и коммит — тоже: переход есть точка, после
  которой resume считает раунд решённым, и всё, что обязано пережить обрыв,
  случается раньше неё. Порядок пинится ОДНИМ общим spy-логом — не набором
  «вызвано/не вызвано»: цена ошибки здесь именно перестановка.
* **кого слушаться.** Решение берётся у `core.decide` целиком, вместе с
  `reason` и директивой. Пин двусторонний: spy, возвращающий исход,
  противоречащий входам, обязан быть исполнен буквально, а сам модуль шага
  не вправе содержать ни одного имени из §5 — ни порогов, ни причин, ни
  предикатов сходимости.

Непринятый раунд (частичный исход — эскалация пользователю, §2.5) не
финализируется и не коммитится: принять такую работу вправе только человек.

`steps.decide_step` и расширения `history` до реализации не существуют,
поэтому доступ идёт через `_steps_attr`/`_history_attr`: отсутствие символа
переводится в `AssertionError`, а не в ошибку коллекции — red-чекпоинт
обязан быть падением assertion'ом.
"""

import ast
import json
from collections.abc import Sequence
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
    Decision,
    DiffStats,
    Event,
    EventType,
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
from disputatio.core import (
    REASON_ANTI_SYCOPHANCY,
    DecidingInputs,
    DecisionDraft,
    SessionFsm,
    decide,
)
from disputatio.events import finalize_round, write_round_artifact
from disputatio.runtime import ROUND_COMMIT_TEMPLATE, RuntimeDeps

from ._fakes import GitOpsFakeBase

_FROZEN_NOW = datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)
_ROUND = 4
_SESSION_ID = "s-deciding"

# Замечания, которые решение раунда n оставило открытыми. У раунда 3 —
# один существующий id и один несуществующий: фильтр обязан отобрать первый
# и промолчать про второй, а не упасть и не подставить пустышку.
_CARRIED_IDS = {
    1: ("I-001-A",),
    2: ("I-002-A",),
    3: ("I-003-B", "I-003-MISSING"),
}

# Сколько замечаний несёт ревью раунда n. Раунд 3 богаче прочих, чтобы
# фильтрация `carried_issues` была наблюдаема, а не совпадала со «взять всё».
_REVIEW_LETTERS = {3: ("A", "B", "C")}

# Ожидаемая последовательность принятого раунда целиком.
_ACCEPTED_ORDER = [
    "decide",
    "write:decision.json",
    "finalize_round",
    "commit_round",
    "apply_decision",
]

# То же для непринятого (частичного) исхода: ни финализации, ни коммита.
_PARTIAL_ORDER = ["decide", "write:decision.json", "apply_decision"]

# Имена §5, которых в модуле шага быть не должно вовсе: пороги, коды причин
# и предикаты сходимости живут в ядре, и второе их упоминание в runtime
# означало бы второй источник правды о порядке стоп-условий.
_STOPPING_RULE_TOKENS = frozenset(
    {
        "REASON_ANTI_SYCOPHANCY",
        "REASON_BUDGET_TOKENS",
        "REASON_BUDGET_WALL",
        "REASON_CONTINUE",
        "REASON_CONVERGED",
        "REASON_MAX_ROUNDS",
        "REASON_OSCILLATION_DIFF",
        "REASON_OSCILLATION_ISSUE",
        "CLAIM_SIMILARITY_THRESHOLD",
        "OSCILLATION_DIFF_THRESHOLD",
        "is_converged",
        "patch_similarity",
        "find_repeated_issue",
    }
)

# Поля, чтение которых означало бы, что шаг сам судит о сходимости, бюджете
# или лимитах. `budget_used`/`limits` целиком передаются в `DecidingInputs`
# и потому разрешены — запрещён именно разбор их содержимого.
_RULE_ATTRIBUTES = frozenset(
    {
        "issues",
        "max_rounds",
        "max_total_tokens",
        "max_wall_seconds",
        "overall",
        "severity",
        "tokens",
        "verdict",
        "wall_seconds",
    }
)

# Имена enum'ов, по которым шаг мог бы ветвиться вместо ядра.
_RULE_NAMES = frozenset({"OverallStatus", "Outcome", "Severity", "Verdict"})


def _steps() -> ModuleType:
    """Модуль `runtime/steps.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.steps")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/steps.py: {exc}") from exc


def _history() -> ModuleType:
    """Модуль `runtime/history.py` — чтение артефактов прошлых раундов."""
    try:
        return import_module("disputatio.runtime.history")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/history.py: {exc}") from exc


def _layout() -> ModuleType:
    """Модуль `runtime/layout.py` — read-side зеркало раскладки (ADR-002)."""
    try:
        return import_module("disputatio.runtime.layout")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/layout.py: {exc}") from exc


def _attr(module: ModuleType, name: str) -> Any:
    """Символ модуля; отсутствие — `AssertionError`, не `AttributeError`."""
    assert hasattr(module, name), f"{module.__name__} не определяет {name!r}"
    return getattr(module, name)


def _steps_attr(name: str) -> Any:
    """Символ модуля шагов."""
    return _attr(_steps(), name)


def _history_attr(name: str) -> Any:
    """Символ модуля чтения истории."""
    return _attr(_history(), name)


def _source(module: ModuleType) -> str:
    """Исходник модуля с диска — для проверок «код такой формы отсутствует»."""
    path = module.__file__
    assert path is not None, f"у {module.__name__} нет файла на диске"
    return Path(path).read_text(encoding="utf-8")


def _functions_reachable_from(
    tree: ast.Module, root: str
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Функция `root` модуля и все функции модуля, вызываемые из неё.

    Замыкание транзитивное: правило, вынесенное в хелпер, скан обязан
    видеть так же, как правило в теле, — иначе запрет обходится одной
    лишней функцией.
    """
    defined: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert root in defined, f"модуль не определяет функцию {root!r}"

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


def _attribute_names(nodes: Sequence[ast.AST]) -> set[str]:
    """Имена всех атрибутов, читаемых в `nodes`."""
    names: set[str] = set()
    for scope in nodes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def _referenced_names(nodes: Sequence[ast.AST]) -> set[str]:
    """Имена всех голых идентификаторов, упомянутых в `nodes`."""
    names: set[str] = set()
    for scope in nodes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Name):
                names.add(node.id)
    return names


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
    """`GitOps`-фейк: коммит журналируется, прочие операции запрещены."""

    log: list[str]
    commits: list[int] = field(default_factory=list)

    def diff_head(self) -> str:
        """Не вызывается: патч раунда снят шагом PROPOSING."""
        raise AssertionError("DECIDING не вправе снимать дифф")

    def commit_round(self, round_no: int) -> None:
        """Журналирует фиксацию принятого раунда ([REQ-011])."""
        self.log.append("commit_round")
        self.commits.append(round_no)

    def reset_hard(self, rev: str) -> None:
        """Не вызывается: сброс дерева — прерогатива PROPOSING."""
        raise AssertionError(f"DECIDING не вправе сбрасывать дерево на {rev}")

    def clean(self) -> None:
        """Не вызывается: уборка дерева — прерогатива PROPOSING."""
        raise AssertionError("DECIDING не вправе убирать дерево")


@dataclass
class SpyDecide:
    """Спай на `core.decide`: журналирует вход и отдаёт исход.

    По умолчанию делегирует настоящему `decide` — тест смотрит, ЧТО подано
    на вход. С заданным `draft` он, наоборот, отвечает вопреки входам: так
    видно, следует ли шаг ядру или пересчитывает §5 сам.
    """

    log: list[str]
    draft: DecisionDraft | None = None
    calls: list[DecidingInputs] = field(default_factory=list)

    def __call__(self, inputs: DecidingInputs) -> DecisionDraft:
        """Записывает вход и возвращает решение."""
        self.log.append("decide")
        self.calls.append(inputs)
        return decide(inputs) if self.draft is None else self.draft


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
    sink: FakeSink
    git: SpyGit
    decide_spy: SpyDecide
    log: list[str]
    writes: list[Written]
    applied: list[Decision]


def _limits(*, max_rounds: int = 9, max_total_tokens: int = 100_000) -> Limits:
    """Лимиты сессии; раунд `_ROUND` заведомо не упирается в них."""
    return Limits(
        max_rounds=max_rounds,
        max_total_tokens=max_total_tokens,
        max_wall_seconds=600,
        schema_retries=1,
    )


def _budget(*, tokens: int = 4242) -> BudgetUsed:
    """Израсходованный бюджет; значения ненулевые — дефолт был бы вакуумен."""
    return BudgetUsed(tokens=tokens, wall_seconds=7.5, cost_usd_est=0.25)


def _state(
    *,
    round_no: int = _ROUND,
    mode: Mode = Mode.DEVELOP,
    limits: Limits | None = None,
    budget: BudgetUsed | None = None,
) -> SessionState:
    """`SessionState` раунда `round_no` в фазе `DECIDING`."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=SessionPhase.DECIDING,
        current_round=round_no,
        task=TaskSpec(
            prompt="ЗАДАЧА-ПОЛЬЗОВАТЕЛЯ: почини экспорт CSV",
            attachments=[],
            mode=mode,
        ),
        agents={
            Role.AUTHOR: AgentRef(
                adapter="claude_code", model="opus", session_ref="ref-author"
            ),
            Role.REVIEWER: AgentRef(
                adapter="claude_code", model="sonnet", session_ref="ref-reviewer"
            ),
        },
        limits=_limits() if limits is None else limits,
        budget_used=_budget() if budget is None else budget,
    )


def _patch(round_no: int) -> str:
    """Патч раунда `round_no`; изменённые строки помечены его номером.

    Помечены нарочно: `patch_similarity` считает по множествам изменённых
    строк, и патчи разных раундов обязаны быть различимы и по содержимому,
    и глазами теста.
    """
    marker = f"{round_no:03d}"
    return (
        f"--- a/mod{marker}.py\n"
        f"+++ b/mod{marker}.py\n"
        "@@ -1 +1 @@\n"
        f"-СТАРАЯ-СТРОКА-РАУНДА-{marker}\n"
        f"+НОВАЯ-СТРОКА-РАУНДА-{marker}\n"
    )


def _issue(round_no: int, letter: str) -> Issue:
    """Замечание `letter` раунда `round_no`; файл помечен номером раунда.

    Разные `file` у разных раундов делают `find_repeated_issue` заведомо
    отрицательным: осцилляция — предмет собственных тестов ядра, а здесь
    она была бы шумом, срабатывающим от совпадения текстов фикстуры.
    """
    marker = f"{round_no:03d}"
    return Issue(
        id=f"I-{marker}-{letter}",
        severity=Severity.MAJOR,
        file=f"mod{marker}.py",
        claim=f"замечание {letter} раунда {marker}",
        evidence=f"mod{marker}.py:1 — свидетельство {letter} раунда {marker}",
    )


def _review(
    round_no: int,
    *,
    verdict: Verdict = Verdict.REQUEST_CHANGES,
    letters: tuple[str, ...] | None = None,
) -> Review:
    """Ревью раунда `round_no` с помеченными раундом замечаниями."""
    marker = f"{round_no:03d}"
    chosen = _REVIEW_LETTERS.get(round_no, ("A",)) if letters is None else letters
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=verdict,
        confidence=0.9,
        issues=[_issue(round_no, letter) for letter in chosen],
        checked=[f"mod{marker}.py"],
        summary=f"свод раунда {marker}",
    )


def _decision(round_no: int) -> Decision:
    """Решение раунда `round_no` с его собственным `open_issues_carried`."""
    marker = f"{round_no:03d}"
    return Decision(
        round=round_no,
        outcome=Outcome.CONTINUE,
        reason=f"причина раунда {marker}",
        open_issues_carried=list(_CARRIED_IDS.get(round_no, ())),
        next_round_directive=f"директива раунда {marker}",
    )


def _verification(round_no: int, *, overall: OverallStatus) -> VerificationReport:
    """Отчёт проверок раунда `round_no`; имена гейтов помечены раундом."""
    marker = f"{round_no:03d}"
    failing = overall is OverallStatus.FAIL
    return VerificationReport(
        round=round_no,
        gates=[
            GateResult(
                name=f"gate-{marker}",
                cmd="uv run pytest -q",
                status=GateStatus.FAIL if failing else GateStatus.PASS,
                exit_code=1 if failing else 0,
                duration_s=1.5,
                tail=f"вывод гейта раунда {marker}",
            )
        ],
        overall=overall,
        diff_stats=DiffStats(files=1, insertions=4, deletions=2),
    )


def _write(
    root: Path,
    round_no: int,
    name: str,
    artifact: Review | Decision | VerificationReport,
) -> None:
    """Сериализует артефакт в раунд `round_no` как это делает оркестратор."""
    write_round_artifact(root, round_no, name, artifact.model_dump_json(by_alias=True))


def _seed(
    root: Path,
    *,
    round_no: int,
    overall: OverallStatus,
    verdict: Verdict,
    letters: tuple[str, ...] | None,
    write_patch: bool,
) -> None:
    """Кладёт на диск историю раундов `1…round_no−1` и артефакты раунда N.

    Прошлые раунды финализируются, как в живой сессии: шаг обязан читать их
    и не пытаться переписать ([REQ-016]). Их отчёты всегда `pass` — при
    красном отчёте раунда N это делает наблюдаемой подмену источника.
    """
    for prior in range(1, round_no):
        _write(root, prior, "review.json", _review(prior))
        _write(root, prior, "decision.json", _decision(prior))
        _write(
            root,
            prior,
            "verification.json",
            _verification(prior, overall=OverallStatus.PASS),
        )
        write_round_artifact(root, prior, "changes.patch", _patch(prior))
        finalize_round(root, prior)

    _write(
        root,
        round_no,
        "review.json",
        _review(round_no, verdict=verdict, letters=letters),
    )
    _write(
        root, round_no, "verification.json", _verification(round_no, overall=overall)
    )
    if write_patch:
        write_round_artifact(root, round_no, "changes.patch", _patch(round_no))


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


def _spy_finalize(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> list[int]:
    """Подменяет `finalize_round` в модуле шага, сохраняя номера раундов."""
    steps = _steps()
    assert hasattr(steps, "finalize_round"), (
        "runtime/steps.py обязан финализировать раунд через events.finalize_round"
    )
    finalized: list[int] = []

    def spy(root: Path, round_no: int) -> None:
        log.append("finalize_round")
        finalized.append(round_no)
        finalize_round(root, round_no)

    monkeypatch.setattr(steps, "finalize_round", spy)
    return finalized


def _spy_decide(
    monkeypatch: pytest.MonkeyPatch, log: list[str], draft: DecisionDraft | None
) -> SpyDecide:
    """Подменяет `decide` в модуле шага — решение обязано идти оттуда."""
    steps = _steps()
    assert hasattr(steps, "decide"), (
        "runtime/steps.py обязан импортировать core.decide именем decide — "
        "исход раунда берётся у ядра, а не выводится на месте"
    )
    spy = SpyDecide(log=log, draft=draft)
    monkeypatch.setattr(steps, "decide", spy)
    return spy


def _spy_apply_decision(
    monkeypatch: pytest.MonkeyPatch, fsm: SessionFsm, log: list[str]
) -> list[Decision]:
    """Оборачивает `fsm.apply_decision`, сохраняя материализованное решение."""
    applied: list[Decision] = []
    original = fsm.apply_decision

    def spy(draft: DecisionDraft) -> tuple[Decision, SessionState]:
        log.append("apply_decision")
        decision, state = original(draft)
        applied.append(decision)
        return decision, state

    monkeypatch.setattr(fsm, "apply_decision", spy)
    return applied


def _make_harness(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    round_no: int = _ROUND,
    mode: Mode = Mode.DEVELOP,
    verdict: Verdict = Verdict.REQUEST_CHANGES,
    letters: tuple[str, ...] | None = None,
    overall: OverallStatus = OverallStatus.FAIL,
    write_patch: bool = True,
    limits: Limits | None = None,
    budget: BudgetUsed | None = None,
    draft: DecisionDraft | None = None,
    seed: bool = True,
) -> Harness:
    """Собирает `StepContext` со спаями на всех наблюдаемых границах шага."""
    if seed:
        _seed(
            root,
            round_no=round_no,
            overall=overall,
            verdict=verdict,
            letters=letters,
            write_patch=write_patch,
        )
    log: list[str] = []
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(
        _state(round_no=round_no, mode=mode, limits=limits, budget=budget),
        store=store,
        sink=sink,
        now=lambda: _FROZEN_NOW,
    )
    git = SpyGit(log=log)
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
    writes: list[Written] = []
    decide_spy = _spy_decide(monkeypatch, log, draft)
    _spy_writer(monkeypatch, log, writes)
    _spy_finalize(monkeypatch, log)
    applied = _spy_apply_decision(monkeypatch, fsm, log)
    ctx = _steps_attr("StepContext")(deps=deps, fsm=fsm, base_commit="0" * 40)
    return Harness(
        root=root,
        ctx=ctx,
        fsm=fsm,
        sink=sink,
        git=git,
        decide_spy=decide_spy,
        log=log,
        writes=writes,
        applied=applied,
    )


def _decide_step(harness: Harness) -> None:
    """Выполняет шаг DECIDING — он синхронен, агентов не зовёт."""
    _steps_attr("decide_step")(harness.ctx)


def _decision_path(root: Path, round_no: int = _ROUND) -> Path:
    """Путь `rounds/NNN/decision.json` по read-side зеркалу раскладки."""
    layout = _layout()
    return layout.round_artifact(root, round_no, layout.DECISION_NAME)


def _on_disk(root: Path, round_no: int = _ROUND) -> Decision:
    """`decision.json` раунда, прочитанное обратно моделью contracts."""
    return Decision.model_validate_json(
        _decision_path(root, round_no).read_text(encoding="utf-8")
    )


def _phases(harness: Harness) -> list[tuple[str, str]]:
    """Пары `(from, to)` всех переходов, попавших в журнал событий."""
    return [
        (event.payload["from"], event.payload["to"])
        for event in harness.sink.events
        if event.type is EventType.STATE_CHANGE
    ]


def _inputs(harness: Harness) -> DecidingInputs:
    """Единственный снимок, поданный ядру."""
    assert len(harness.decide_spy.calls) == 1, (
        f"core.decide вызван {len(harness.decide_spy.calls)} раз — исход "
        "раунда выносится ровно одним вызовом"
    )
    return harness.decide_spy.calls[0]


def test_accepted_round_order_is_decide_write_finalize_commit_then_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порядок принятого раунда фиксирован общим spy-логом ([DESIGN-007]).

    Всё, что обязано пережить обрыв, случается ДО `apply_decision`: переход
    и есть точка, после которой resume считает раунд решённым.
    """
    harness = _make_harness(tmp_path, monkeypatch)

    _decide_step(harness)

    assert harness.log == _ACCEPTED_ORDER


def test_decision_json_is_written_before_apply_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`decision.json` лёг на диск раньше первого перехода ([REQ-007])."""
    harness = _make_harness(tmp_path, monkeypatch)

    _decide_step(harness)

    assert harness.log.index("write:decision.json") < harness.log.index(
        "apply_decision"
    )
    assert [write.name for write in harness.writes] == ["decision.json"]
    written = harness.writes[0]
    assert written.round_no == _ROUND
    assert written.path == _decision_path(tmp_path)


def test_deciding_inputs_are_assembled_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каждое поле `DecidingInputs` пришло из своего источника ([DESIGN-007]).

    Раунды фикстуры различимы во всём — в отчёте, в патче, в `file` и
    `claim` замечаний, — поэтому подмена «взять ревью N−1», «взять патч
    N−1», «взять решение N» наблюдаема каждая по отдельности.
    """
    harness = _make_harness(tmp_path, monkeypatch)

    _decide_step(harness)

    inputs = _inputs(harness)
    state = _state()
    assert inputs.round == _ROUND
    assert inputs.mode is Mode.DEVELOP
    assert inputs.review == _review(_ROUND)
    assert inputs.verification == _verification(_ROUND, overall=OverallStatus.FAIL)
    assert inputs.budget_used == state.budget_used
    assert inputs.limits == state.limits
    assert inputs.patch_current == _patch(_ROUND)
    assert inputs.patch_two_back == _patch(_ROUND - 2)
    assert inputs.carried_issues == (_issue(_ROUND - 1, "B"),)
    assert dict(inputs.issue_history) == {
        1: (_issue(1, "A"),),
        2: (_issue(2, "A"),),
        3: (_issue(3, "A"), _issue(3, "B"), _issue(3, "C")),
    }


def test_missing_changes_patch_is_an_empty_string_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Раунд без правок даёт `patch_current == ""`, а не исключение.

    Пустая строка — не то же, что «шаг упал»: analyze-режим правок не
    делает вовсе, и падение здесь останавливало бы законную сессию.
    """
    harness = _make_harness(tmp_path, monkeypatch, write_patch=False)

    _decide_step(harness)

    assert _inputs(harness).patch_current == ""


def test_patch_two_back_is_none_when_that_round_left_no_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нет патча раунда N−2 → `None`, а не пустая строка ([DESIGN-007]).

    Разница не косметическая: два пустых патча ядро считает идентичными
    (`patch_similarity == 1.0`), и подмена `None → ""` объявила бы
    осцилляцию там, где сравнивать не с чем.
    """
    harness = _make_harness(tmp_path, monkeypatch)
    (tmp_path / ".disputatio" / "rounds" / "002" / "changes.patch").unlink()

    _decide_step(harness)

    assert _inputs(harness).patch_two_back is None


def test_round_one_has_no_prior_rounds_to_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Раунд 1: пустая история, пустой `carried`, `patch_two_back is None`."""
    harness = _make_harness(
        tmp_path,
        monkeypatch,
        round_no=1,
        verdict=Verdict.APPROVE,
        letters=(),
        overall=OverallStatus.PASS,
    )

    _decide_step(harness)

    inputs = _inputs(harness)
    assert inputs.carried_issues == ()
    assert dict(inputs.issue_history) == {}
    assert inputs.patch_two_back is None
    assert inputs.patch_current == _patch(1)


def test_revise_directive_opens_the_next_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`continue` ведёт в `PROPOSING` раунда N+1 ([REQ-007])."""
    harness = _make_harness(tmp_path, monkeypatch)

    _decide_step(harness)

    assert _phases(harness) == [("DECIDING", "PROPOSING")]
    assert harness.fsm.state.state is SessionPhase.PROPOSING
    assert harness.fsm.state.current_round == _ROUND + 1

    decision = _on_disk(tmp_path)
    assert decision.outcome is Outcome.CONTINUE
    assert decision.round == _ROUND
    assert decision.next_round_directive


@pytest.mark.parametrize(
    ("outcome", "expected_phases"),
    [
        (Outcome.CONVERGED, [("DECIDING", "CONVERGED"), ("CONVERGED", "EXPORTING")]),
        (
            Outcome.DEADLOCK,
            [
                ("DECIDING", "DEADLOCK"),
                ("DEADLOCK", "ESCALATED"),
                ("ESCALATED", "EXPORTING"),
            ],
        ),
        (
            Outcome.BUDGET_HIT,
            [
                ("DECIDING", "BUDGET_HIT"),
                ("BUDGET_HIT", "ESCALATED"),
                ("ESCALATED", "EXPORTING"),
            ],
        ),
    ],
)
def test_terminal_chains_are_built_by_apply_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Outcome,
    expected_phases: list[tuple[str, str]],
) -> None:
    """Терминальные причины ведут в свои цепочки, и строит их ядро.

    Исход подан спаем вопреки входам (ревью просит правок): шаг обязан
    отдать его `apply_decision` как есть — ни одного собственного перехода,
    ни одной собственной причины.
    """
    draft = DecisionDraft(
        outcome=outcome,
        reason=f"причина-{outcome.value}",
        open_issues_carried=("I-003-B",),
        next_round_directive=None,
        forced_review=False,
    )
    harness = _make_harness(tmp_path, monkeypatch, draft=draft)

    _decide_step(harness)

    assert _phases(harness) == expected_phases
    decision = _on_disk(tmp_path)
    assert decision.outcome is outcome
    assert decision.reason == draft.reason
    assert decision.open_issues_carried == ["I-003-B"]


def test_round_one_approve_outside_analyze_does_not_converge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Анти-сикофантия: раунд 1 `approve` в `develop` не сходится ([REQ-007]).

    Правило целиком принадлежит ядру, и это видно по причине: runtime её не
    формулирует, а переносит.
    """
    harness = _make_harness(
        tmp_path,
        monkeypatch,
        round_no=1,
        verdict=Verdict.APPROVE,
        letters=(),
        overall=OverallStatus.PASS,
    )

    _decide_step(harness)

    assert ("DECIDING", "CONVERGED") not in _phases(harness)
    assert _phases(harness) == [("DECIDING", "PROPOSING")]
    decision = _on_disk(tmp_path, round_no=1)
    assert decision.outcome is Outcome.CONTINUE
    assert decision.reason == REASON_ANTI_SYCOPHANCY


def test_round_one_approve_in_analyze_without_changes_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обратная половина: `analyze` без правок раунда 1 сходится сразу.

    Без неё предыдущий тест был бы вакуумен — «раунд 1 не сходится никогда»
    прошёл бы его, нарушив §5.
    """
    harness = _make_harness(
        tmp_path,
        monkeypatch,
        round_no=1,
        mode=Mode.ANALYZE,
        verdict=Verdict.APPROVE,
        letters=(),
        overall=OverallStatus.PASS,
        write_patch=False,
    )

    _decide_step(harness)

    assert _phases(harness) == [
        ("DECIDING", "CONVERGED"),
        ("CONVERGED", "EXPORTING"),
    ]
    assert _on_disk(tmp_path, round_no=1).outcome is Outcome.CONVERGED


def test_accepted_round_is_finalized_then_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Принятый раунд: маркер I3, затем ровно один коммит ([REQ-011]).

    Номер коммита совпадает с именем директории раунда — иначе `git reset`
    следующего раунда искал бы цель, которой в истории нет ([DESIGN-012]).
    """
    harness = _make_harness(tmp_path, monkeypatch)

    _decide_step(harness)

    assert harness.log.index("finalize_round") < harness.log.index("commit_round")
    assert harness.git.commits == [_ROUND]

    layout = _layout()
    subject = ROUND_COMMIT_TEMPLATE.format(round=_ROUND)
    assert subject.endswith(layout.round_dir(tmp_path, _ROUND).name)
    assert (
        tmp_path / ".disputatio" / "rounds" / f"{_ROUND:03d}" / ".finalized"
    ).exists()


def test_partial_outcome_neither_finalizes_nor_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Непринятый раунд коммита не создаёт ([REQ-011]).

    Частичный исход — эскалация пользователю (§2.5): принять такую работу
    вправе только он, и коммит от его имени оркестратор не делает. Маркер
    I3 не ставится по той же причине — раунд ещё вправе быть дорешён.
    """
    draft = DecisionDraft(
        outcome=Outcome.DEADLOCK,
        reason="причина-тупика",
        open_issues_carried=(),
        next_round_directive=None,
        forced_review=False,
    )
    harness = _make_harness(tmp_path, monkeypatch, draft=draft)

    _decide_step(harness)

    assert harness.log == _PARTIAL_ORDER
    assert harness.git.commits == []
    assert not (
        tmp_path / ".disputatio" / "rounds" / f"{_ROUND:03d}" / ".finalized"
    ).exists()


def test_step_follows_the_core_verdict_against_the_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ядро сказало `converged` — шаг сошёлся, хотя входы просят правок.

    Самый прямой пин «правила §5 не продублированы»: любая собственная
    проверка сходимости в runtime отменила бы этот исход.
    """
    draft = DecisionDraft(
        outcome=Outcome.CONVERGED,
        reason="ядро-сказало-сошлись",
        open_issues_carried=(),
        next_round_directive=None,
        forced_review=False,
    )
    harness = _make_harness(tmp_path, monkeypatch, draft=draft)

    _decide_step(harness)

    assert _inputs(harness).review.verdict is Verdict.REQUEST_CHANGES
    assert _inputs(harness).verification.overall is OverallStatus.FAIL
    assert _phases(harness) == [
        ("DECIDING", "CONVERGED"),
        ("CONVERGED", "EXPORTING"),
    ]
    assert _on_disk(tmp_path).reason == "ядро-сказало-сошлись"


def test_decision_on_disk_equals_the_one_apply_decision_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Записанное решение и решение ядра — одна и та же модель.

    `decision.json` пишется ДО `apply_decision`, поэтому материализаций
    формально две; разъедься они — история сессии на диске рассказывала бы
    не о том переходе, который случился.
    """
    harness = _make_harness(tmp_path, monkeypatch)

    _decide_step(harness)

    assert len(harness.applied) == 1
    assert _on_disk(tmp_path) == harness.applied[0]

    raw = json.loads(_decision_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["schema"] == "disputatio/v1"
    assert "schema_" not in raw
    assert raw["round"] == _ROUND


def test_missing_review_of_the_round_is_an_ordering_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нет `review.json` раунда N → `AssertionError`, а не тихий пропуск.

    Сюда приводит только сломанная диспетчеризация цикла: write-ahead не
    даёт войти в DECIDING раньше, чем REVIEWING положил ревью на диск.
    """
    harness = _make_harness(tmp_path, monkeypatch)
    (tmp_path / ".disputatio" / "rounds" / f"{_ROUND:03d}" / "review.json").unlink()

    with pytest.raises(AssertionError):
        _decide_step(harness)

    assert harness.writes == []
    assert not _decision_path(tmp_path).exists()


def test_carried_issues_come_from_the_prior_round_decision(tmp_path: Path) -> None:
    """`carried_issues` — пересечение ревью N−1 и его `open_issues_carried`.

    Три пина в одном: берётся ревью именно раунда N−1, фильтр по id решения
    того же раунда, а неизвестный id решение не ломает — материализовать
    замечание, которого в ревью нет, всё равно не из чего.
    """
    _seed(
        tmp_path,
        round_no=_ROUND,
        overall=OverallStatus.FAIL,
        verdict=Verdict.REQUEST_CHANGES,
        letters=None,
        write_patch=True,
    )
    carried_issues = _history_attr("carried_issues")

    assert carried_issues(tmp_path, _ROUND - 1) == (_issue(3, "B"),)
    assert carried_issues(tmp_path, _ROUND - 2) == (_issue(2, "A"),)
    assert carried_issues(tmp_path, 0) == ()
    assert carried_issues(tmp_path, _ROUND) == ()


def test_issue_history_covers_every_round_before_n(tmp_path: Path) -> None:
    """`issue_history` — все раунды строго до N, текущего среди них нет.

    Ядро считает по нему повторные замечания: попади туда раунд N, каждое
    его замечание совпало бы само с собой и осцилляция объявлялась бы на
    ровном месте.
    """
    _seed(
        tmp_path,
        round_no=_ROUND,
        overall=OverallStatus.FAIL,
        verdict=Verdict.REQUEST_CHANGES,
        letters=None,
        write_patch=True,
    )
    issue_history = _history_attr("issue_history")

    history = issue_history(tmp_path, _ROUND)

    assert set(history) == {1, 2, 3}
    assert history[3] == (_issue(3, "A"), _issue(3, "B"), _issue(3, "C"))
    assert issue_history(tmp_path, 1) == {}


def test_load_patch_reads_the_named_round_or_reports_absence(tmp_path: Path) -> None:
    """`load_patch` отдаёт патч своего раунда, а его отсутствие — `None`."""
    _seed(
        tmp_path,
        round_no=_ROUND,
        overall=OverallStatus.FAIL,
        verdict=Verdict.REQUEST_CHANGES,
        letters=None,
        write_patch=False,
    )
    load_patch = _history_attr("load_patch")

    assert load_patch(tmp_path, 2) == _patch(2)
    assert load_patch(tmp_path, _ROUND) is None
    assert load_patch(tmp_path, 0) is None


def test_step_does_not_duplicate_the_stopping_rules() -> None:
    """§5 в модуле шага отсутствует как форма, а не как поведение ([REQ-007]).

    Запрещены сами имена: пороги, коды причин, предикаты сходимости — и
    чтение полей, по которым ветвление о сходимости, бюджете и лимитах
    только и возможно. Перечень поведений можно обойти новой веткой,
    отсутствующее имя обойти нечем.
    """
    steps = _steps()
    source = _source(steps)
    leaked = sorted(token for token in _STOPPING_RULE_TOKENS if token in source)
    assert not leaked, f"runtime/steps.py повторяет §5 именами: {leaked}"

    functions = _functions_reachable_from(ast.parse(source), "decide_step")
    attributes = _attribute_names(list(functions)) & _RULE_ATTRIBUTES
    assert not attributes, (
        f"шаг DECIDING сам разбирает входы §5: {sorted(attributes)} — "
        "решение принимает core.decide, а шаг только собирает снимок"
    )
    names = _referenced_names(list(functions)) & _RULE_NAMES
    assert not names, f"шаг DECIDING ветвится по enum'ам §5: {sorted(names)}"
