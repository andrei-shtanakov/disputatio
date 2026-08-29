"""Честный частичный экспорт и эскалация ([REQ-018], [DESIGN-018]).

[TASK-019]. Ветка без сходимости отличается от сходимости СОДЕРЖИМЫМ
манифеста, а не отдельным кодовым путём: `apply_decision` уже провёл сессию
`DEADLOCK|BUDGET_HIT → ESCALATED → EXPORTING`, и дальше работает тот же
`export`, что и при `CONVERGED`. Проверяется здесь ровно то, что этой веткой
можно соврать:

* **чем сессия кончилась.** Терминал эскалации — `DONE`, а не `FAILED`:
  остановка по деадлоку или бюджету штатна для протокола, и `FAILED` объявил
  бы её сбоем оркестратора. Обе причины гоняются отдельно — одна ветка,
  зелёная за обе, пропустила бы цепочку, собранную под единственный исход.
* **честен ли манифест.** `converged` обязан быть `False`, а `outcome` и
  `stop_reason` — теми же, что записаны в `decision.json` раунда-источника:
  вторая формулировка причины разошлась бы с историей сессии.
* **что осталось открытым.** `open_issues` — замечания последнего ревью,
  отфильтрованные `open_issues_carried` его решения, и в ПОЛНОМ виде
  (`id`, `severity`, `file`, `claim`): манифест читает человек, и список
  голых идентификаторов не сказал бы ему ничего. Порядок — порядок ревью,
  а не порядок списка id: в фикстуре они нарочно разные.
* **одна ли реализация у двух веток.** Пин двусторонний: тот же `export` на
  тех же артефактах раунда даёт побайтово тот же `result/`, а манифесты
  расходятся ровно в трёх полях честности; отдельной функции под частичный
  исход и ветки по исходу в модуле нет вовсе.

Стоп-условия здесь настоящие — `core.decide` вызывается без подмены: раунд 3
несёт два замечания, открытых с раунда 1 (осцилляция §5.3 ⇒ `DEADLOCK`), а
вторая половина параметризации переполняет бюджет токенов (`BUDGET_HIT`
проверяется раньше осцилляции, §5).

Отступление от [DESIGN-018], зафиксированное явно: `stop_reason` строкой в
payload события `state_change` не попадает. Payload перехода собирает
`core.SessionFsm.transition` и он равен `{"from", "to"}` — форма запинена
побайтово тестами ядра, а второе событие `state_change` от runtime запрещено
locked-тестом [TASK-018] (`_state_changes == [("EXPORTING", "DONE")]`).
Поэтому журналу причина остановки известна фазой (`DECIDING → DEADLOCK`,
`DECIDING → BUDGET_HIT`), а machine-readable код живёт в манифесте, и тест
пинит, что эти два рассказа об остановке — один и тот же.
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
    REASON_BUDGET_TOKENS,
    REASON_OSCILLATION_ISSUE,
    SessionFsm,
)
from disputatio.events import bootstrap_session, finalize_round, write_round_artifact
from disputatio.runtime import RuntimeDeps
from disputatio.runtime.layout import session_dir

from ._fakes import GitOpsFakeBase

_FROZEN_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "20260810-120000-c3d4"
_ROUND = 3
_TOKENS = 4242
_WALL_SECONDS = 7.5

_RESULT_MD = "result.md"
_RESULT_PATCH = "result.patch"
_MANIFEST = "manifest.json"

# Два замечания, открытых с раунда 1: ревьюер повторяет их под теми же id в
# каждом раунде, и к раунду 3 §5.3 объявляет осцилляцию. Файлы разные —
# `_claims_match` сверяет `file` и claim, поэтому совпасть друг с другом они
# не могут, а «то же замечание из прошлого раунда» распознаётся по каждому.
_OPEN_A = ("I-OPEN-A", "csv/export.py", Severity.MAJOR)
_OPEN_B = ("I-OPEN-B", "csv/reader.py", Severity.MINOR)

# Замечание, поднятое ревью раунда 3 и решением НЕ перенесённое: без него
# «отфильтровать по решению» было бы неотличимо от «взять весь список».
_FRESH_ID = "I-003-FRESH"
_FRESH_FILE = "csv/writer.py"

# Поля замечания в манифесте §3.2 — ровно четыре. Ни `evidence`, ни
# `line_hint`, ни `suggestion`: манифест несёт то, что читает человек, а не
# сериализованный артефакт целиком.
_ISSUE_KEYS = frozenset({"claim", "file", "id", "severity"})

# Три поля честности — единственное, чем частичный манифест вправе
# отличаться от манифеста сошедшейся сессии на тех же артефактах раунда.
_HONESTY_KEYS = frozenset({"converged", "outcome", "stop_reason"})

# Имена, по которым модуль экспорта мог бы завести ветку «а тут частично».
# Ветки нет как формы: [DESIGN-018] требует, чтобы два исхода отличались
# содержимым манифеста, и перечень поведений обходится новой веткой, а
# отсутствующее имя в условии — нечем.
_BRANCH_NAMES = frozenset(
    {
        "converged",
        "escalated",
        "is_partial",
        "open_issues",
        "outcome",
        "partial",
    }
)

# То же для имён функций модуля: отдельной функции под частичный экспорт
# быть не должно вовсе.
_PARTIAL_FUNCTION_MARKERS = ("budget", "deadlock", "escalat", "partial")


def _exporting() -> ModuleType:
    """Модуль `runtime/exporting.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.exporting")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/exporting.py: {exc}") from exc


def _attr(module: ModuleType, name: str) -> Any:
    """Символ модуля; отсутствие — `AssertionError`, не `AttributeError`."""
    assert hasattr(module, name), f"{module.__name__} не определяет {name!r}"
    return getattr(module, name)


def _exporting_attr(name: str) -> Any:
    """Символ модуля экспорта."""
    return _attr(_exporting(), name)


def _steps_attr(name: str) -> Any:
    """Символ модуля шагов — `StepContext` и `decide_step` общие у цикла."""
    return _attr(import_module("disputatio.runtime.steps"), name)


def _source(module: ModuleType) -> str:
    """Исходник модуля с диска — для проверок «код такой формы отсутствует»."""
    path = module.__file__
    assert path is not None, f"у {module.__name__} нет файла на диске"
    return Path(path).read_text(encoding="utf-8")


def _functions_reachable_from(
    tree: ast.Module, root: str
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Функция `root` модуля и все функции модуля, вызываемые из неё.

    Замыкание транзитивное: ветка, вынесенная в хелпер, скан обязан видеть
    так же, как ветку в теле, — иначе запрет обходится одной лишней
    функцией.
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


def _mentioned_names(nodes: Sequence[ast.AST]) -> set[str]:
    """Имена идентификаторов и читаемых атрибутов внутри `nodes`."""
    names: set[str] = set()
    for scope in nodes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def _branch_condition_names() -> set[str]:
    """Имена, упомянутые в условиях ветвлений замыкания `export`.

    Именно в условиях, а не во всём теле: `not is_partial(outcome)` как
    ЗНАЧЕНИЕ поля манифеста — то самое, чего [DESIGN-018] требует, а то же
    имя в `if` означало бы второй кодовый путь.
    """
    tree = ast.parse(_source(_exporting()))
    names: set[str] = set()
    for function in _functions_reachable_from(tree, "export"):
        for node in ast.walk(function):
            if isinstance(node, ast.If | ast.IfExp):
                names |= _mentioned_names([node.test])
    return names


def _module_function_names() -> list[str]:
    """Имена функций, определённых в модуле экспорта."""
    tree = ast.parse(_source(_exporting()))
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


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
    """`AgentAdapter`-фейк: ни решение, ни экспорт с агентами не говорят."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Вызов означает, что шаг спросил агента вместо артефактов."""
        raise AssertionError("DECIDING/EXPORTING не вправе звать агентов")


@dataclass
class NoVerifier:
    """`Verifier`-фейк: гейты прогнаны раундом, повтора здесь нет."""

    def verify(self, round_no: int) -> VerificationReport:
        """Вызов означает, что шаг перепрогнал гейты раунда."""
        raise AssertionError(f"гейты раунда {round_no} уже прогнаны шагом VERIFYING")


@dataclass
class NoGit(GitOpsFakeBase):
    """`GitOps`-фейк: частичный исход не коммитится (§2.5), экспорт — тем более."""

    def diff_head(self) -> str:
        """Не вызывается: патч раунда снят шагом PROPOSING."""
        raise AssertionError("частичный исход не снимает дифф")

    def commit_round(self, round_no: int) -> None:
        """Не вызывается: эскалированную работу принимает только человек."""
        raise AssertionError(f"частичный исход не вправе коммитить раунд {round_no}")

    def reset_hard(self, rev: str) -> None:
        """Не вызывается: сброс дерева — прерогатива PROPOSING."""
        raise AssertionError(f"частичный исход не вправе сбрасывать дерево на {rev}")

    def clean(self) -> None:
        """Не вызывается: уборка дерева — прерогатива PROPOSING."""
        raise AssertionError("частичный исход не вправе убирать дерево")


@dataclass
class Harness:
    """Собранное окружение шагов: контекст, FSM и журналы фейков."""

    root: Path
    ctx: Any
    fsm: SessionFsm
    store: FakeStore
    sink: FakeSink


def _limits(*, max_total_tokens: int) -> Limits:
    """Лимиты сессии; переполняется ровно один — бюджет токенов."""
    return Limits(
        max_rounds=9,
        max_total_tokens=max_total_tokens,
        max_wall_seconds=600,
        schema_retries=1,
    )


def _state(*, limits: Limits, phase: SessionPhase) -> SessionState:
    """`SessionState` раунда `_ROUND` в фазе `phase`."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=phase,
        current_round=_ROUND,
        task=TaskSpec(
            prompt="ЗАДАЧА-ПОЛЬЗОВАТЕЛЯ: почини экспорт CSV",
            attachments=[],
            mode=Mode.DEVELOP,
        ),
        agents={
            Role.AUTHOR: AgentRef(
                adapter="claude_code", model="opus", session_ref="ref-author"
            ),
            Role.REVIEWER: AgentRef(
                adapter="claude_code", model="sonnet", session_ref="ref-reviewer"
            ),
        },
        limits=limits,
        budget_used=BudgetUsed(
            tokens=_TOKENS, wall_seconds=_WALL_SECONDS, cost_usd_est=0.25
        ),
    )


def _proposal_text(round_no: int) -> str:
    """`proposal.md` раунда `round_no`; номер раунда — в тексте."""
    marker = f"{round_no:03d}"
    return (
        "---\n"
        f"round: {round_no}\n"
        "summary: правка экспорта CSV\n"
        "---\n"
        f"# Предложение раунда {marker}\n"
        f"Текст предложения раунда {marker}: разделитель вынесен в конфиг.\n"
    )


def _patch_text(round_no: int) -> str:
    """`changes.patch` раунда `round_no`; изменённые строки помечены им же."""
    marker = f"{round_no:03d}"
    return (
        f"--- a/mod{marker}.py\n"
        f"+++ b/mod{marker}.py\n"
        "@@ -1 +1 @@\n"
        f"-СТАРАЯ-СТРОКА-РАУНДА-{marker}\n"
        f"+НОВАЯ-СТРОКА-РАУНДА-{marker}\n"
    )


def _open_issue(spec: tuple[str, str, Severity]) -> Issue:
    """Замечание, открытое с раунда 1: id, файл и claim не меняются.

    `evidence`, `line_hint` и `suggestion` заполнены нарочно: манифест несёт
    четыре поля §3.2, и «сериализовать замечание целиком» обязано быть
    отличимо от «перечислить то, что читает человек».
    """
    issue_id, file, severity = spec
    return Issue(
        id=issue_id,
        severity=severity,
        file=file,
        line_hint=42,
        claim=f"{file}: разделитель всё ещё захардкожен ({issue_id})",
        evidence=f"{file}:42 — свидетельство по {issue_id}",
        suggestion=f"вынести разделитель в конфиг ({issue_id})",
    )


def _fresh_issue() -> Issue:
    """Замечание раунда 3, которое решение раунда открытым НЕ объявило."""
    return Issue(
        id=_FRESH_ID,
        severity=Severity.NIT,
        file=_FRESH_FILE,
        claim=f"{_FRESH_FILE}: имя переменной стоит сократить",
        evidence=f"{_FRESH_FILE}:7 — свидетельство по {_FRESH_ID}",
    )


def _review(round_no: int) -> Review:
    """Ревью раунда `round_no`: те же открытые замечания в каждом раунде.

    Раунд 3 отличается порядком (B, свежее, A) и лишним замечанием: первый
    делает наблюдаемым порядок манифеста, второе — фильтрацию по решению.
    """
    marker = f"{round_no:03d}"
    if round_no == _ROUND:
        issues = [_open_issue(_OPEN_B), _fresh_issue(), _open_issue(_OPEN_A)]
    else:
        issues = [_open_issue(_OPEN_A), _open_issue(_OPEN_B)]
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.9,
        issues=issues,
        checked=[f"mod{marker}.py"],
        summary=f"свод раунда {marker}",
    )


def _verification(round_no: int) -> VerificationReport:
    """Отчёт проверок раунда `round_no` — красный: сходимость исключена §5.1."""
    marker = f"{round_no:03d}"
    return VerificationReport(
        round=round_no,
        gates=[
            GateResult(
                name=f"gate-{marker}",
                cmd="uv run pytest -q",
                status=GateStatus.FAIL,
                exit_code=1,
                duration_s=1.5,
                tail=f"вывод гейта раунда {marker}",
            )
        ],
        overall=OverallStatus.FAIL,
        diff_stats=DiffStats(files=1, insertions=4, deletions=2),
    )


def _prior_decision(round_no: int) -> Decision:
    """Решение раунда `round_no < _ROUND`: оба замечания оставлены открытыми.

    Порядок id — порядок ревью раунда 2 (A, B), и он нарочно отличается от
    порядка ревью раунда 3 (B, A): манифест обязан идти вторым.
    """
    marker = f"{round_no:03d}"
    return Decision(
        round=round_no,
        outcome=Outcome.CONTINUE,
        reason=f"нужны правки раунда {marker}",
        open_issues_carried=[_OPEN_A[0], _OPEN_B[0]],
        next_round_directive=f"директива раунда {marker}",
    )


def _write(
    root: Path,
    round_no: int,
    name: str,
    artifact: Review | Decision | VerificationReport,
) -> None:
    """Сериализует артефакт в раунд `round_no` как это делает оркестратор."""
    write_round_artifact(root, round_no, name, artifact.model_dump_json(by_alias=True))


def _seed(root: Path) -> None:
    """Кладёт на диск раунды `1…_ROUND` так, как их оставила бы сессия.

    Решения есть у раундов до третьего; решение раунда 3 пишет сам шаг
    DECIDING. Прошлые раунды финализированы (I3): к моменту эскалации они
    закрыты от правок.
    """
    bootstrap_session(root)
    for prior in range(1, _ROUND + 1):
        write_round_artifact(root, prior, "proposal.md", _proposal_text(prior))
        write_round_artifact(root, prior, "changes.patch", _patch_text(prior))
        _write(root, prior, "review.json", _review(prior))
        _write(root, prior, "verification.json", _verification(prior))
        if prior < _ROUND:
            _write(root, prior, "decision.json", _prior_decision(prior))
            finalize_round(root, prior)


def _make_harness(
    root: Path,
    *,
    max_total_tokens: int,
    phase: SessionPhase = SessionPhase.DECIDING,
) -> Harness:
    """Собирает `StepContext` над засеянной сессией раунда `_ROUND`."""
    _seed(root)
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(
        _state(limits=_limits(max_total_tokens=max_total_tokens), phase=phase),
        store=store,
        sink=sink,
        now=lambda: _FROZEN_NOW,
    )
    deps = RuntimeDeps(
        workspace_root=root,
        artifact_root=root,
        store=store,
        sink=sink,
        author=NoAgent(),
        reviewer=NoAgent(),
        verifier=NoVerifier(),
        git=NoGit(),
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    ctx = _steps_attr("StepContext")(deps=deps, fsm=fsm, base_commit="0" * 40)
    return Harness(root=root, ctx=ctx, fsm=fsm, store=store, sink=sink)


def _escalate(root: Path, *, max_total_tokens: int) -> Harness:
    """Раунд `_ROUND` → §5 → терминальная цепочка → `export`.

    Ни `decide`, ни `apply_decision` не подменяются: исход выносит ядро по
    настоящим входам, а цепочка §2 строится настоящим FSM. Тест на подменённом
    исходе доказывал бы только то, что шаг умеет писать переданный ему код.
    """
    harness = _make_harness(root, max_total_tokens=max_total_tokens)
    _steps_attr("decide_step")(harness.ctx)
    _exporting_attr("export")(harness.ctx)
    return harness


def _converged_twin(root: Path) -> Harness:
    """Тот же раунд, но с решением `CONVERGED` на диске: вход в `export`.

    Двойник нужен ровно для одного пина — что у частичной ветки нет своего
    кодового пути. Решение кладётся напрямую: сойтись на красном отчёте §5.1
    не даст, а сравнивать надо экспорт, а не критерий сходимости.
    """
    harness = _make_harness(
        root, max_total_tokens=100_000, phase=SessionPhase.EXPORTING
    )
    _write(
        root,
        _ROUND,
        "decision.json",
        Decision(
            round=_ROUND,
            outcome=Outcome.CONVERGED,
            reason="approve_with_gates_pass",
            open_issues_carried=[_OPEN_A[0], _OPEN_B[0]],
            next_round_directive=None,
        ),
    )
    _exporting_attr("export")(harness.ctx)
    return harness


def _result_path(root: Path, name: str) -> Path:
    """Путь файла результата `result/name`."""
    return session_dir(root) / "result" / name


def _manifest_on_disk(root: Path) -> dict[str, Any]:
    """`result/manifest.json`, прочитанный обратно."""
    raw = _result_path(root, _MANIFEST).read_text(encoding="utf-8")
    document = json.loads(raw)
    assert isinstance(document, dict), f"манифест обязан быть объектом: {raw!r}"
    return document


def _decision_on_disk(root: Path) -> Decision:
    """`rounds/003/decision.json` — решение раунда-источника."""
    path = session_dir(root) / "rounds" / f"{_ROUND:03d}" / "decision.json"
    return Decision.model_validate_json(path.read_text(encoding="utf-8"))


def _phases(harness: Harness) -> list[tuple[str, str]]:
    """Пары `(from, to)` всех переходов, попавших в журнал событий."""
    return [
        (event.payload["from"], event.payload["to"])
        for event in harness.sink.events
        if event.type is EventType.STATE_CHANGE
    ]


def _open_issues(root: Path) -> list[Any]:
    """`manifest["open_issues"]`; отсутствие ключа — `AssertionError`."""
    manifest = _manifest_on_disk(root)
    assert "open_issues" in manifest, (
        "manifest.json обязан нести ключ open_issues ([DESIGN-018]): без него "
        "частичный результат неотличим от полного"
    )
    issues = manifest["open_issues"]
    assert isinstance(issues, list), f"open_issues обязан быть списком: {issues!r}"
    return issues


_STOPS = [
    pytest.param(
        100_000,
        Outcome.DEADLOCK,
        f"{REASON_OSCILLATION_ISSUE}: {_OPEN_B[1]}/{_OPEN_B[0]}",
        id="deadlock",
    ),
    pytest.param(
        _TOKENS - 1, Outcome.BUDGET_HIT, REASON_BUDGET_TOKENS, id="budget_hit"
    ),
]


@pytest.mark.parametrize(("max_total_tokens", "outcome", "reason"), _STOPS)
def test_escalated_session_reaches_done_through_exporting(
    tmp_path: Path, max_total_tokens: int, outcome: Outcome, reason: str
) -> None:
    """`DEADLOCK`/`BUDGET_HIT → ESCALATED → EXPORTING → DONE` ([REQ-018]).

    Терминал — `DONE`, и это пинится явно: `FAILED` объявил бы штатную
    остановку протокола сбоем оркестратора, а пользователь по такой сессии
    не отличил бы деадлок от упавшего адаптера.
    """
    harness = _escalate(tmp_path, max_total_tokens=max_total_tokens)

    assert _decision_on_disk(tmp_path).outcome is outcome
    assert _phases(harness) == [
        ("DECIDING", outcome.name),
        (outcome.name, "ESCALATED"),
        ("ESCALATED", "EXPORTING"),
        ("EXPORTING", "DONE"),
    ]
    assert harness.fsm.state.state is SessionPhase.DONE
    assert harness.fsm.state.state is not SessionPhase.FAILED
    assert [saved.state for saved in harness.store.saved][-1] is SessionPhase.DONE
    assert _decision_on_disk(tmp_path).reason == reason


@pytest.mark.parametrize(("max_total_tokens", "outcome", "reason"), _STOPS)
def test_partial_result_is_exported_and_marked_as_not_converged(
    tmp_path: Path, max_total_tokens: int, outcome: Outcome, reason: str
) -> None:
    """`result/` создан, `converged is False`, исход и причина — из решения.

    `is False`, а не «ложно»: `converged: 0` или `converged: null` прошли бы
    проверку на истинность, а прочитавший манифест человек увидел бы не то,
    что сессия про себя знает.
    """
    _escalate(tmp_path, max_total_tokens=max_total_tokens)

    manifest = _manifest_on_disk(tmp_path)
    assert _result_path(tmp_path, _RESULT_MD).read_text(
        encoding="utf-8"
    ) == _proposal_text(_ROUND)
    assert _result_path(tmp_path, _RESULT_PATCH).read_text(
        encoding="utf-8"
    ) == _patch_text(_ROUND)
    assert manifest["converged"] is False
    assert manifest["outcome"] == outcome.value
    assert manifest["stop_reason"] == reason
    assert manifest["source_round"] == _ROUND


@pytest.mark.parametrize(("max_total_tokens", "outcome", "reason"), _STOPS)
def test_stop_reason_of_the_manifest_is_the_one_the_journal_recorded(
    tmp_path: Path, max_total_tokens: int, outcome: Outcome, reason: str
) -> None:
    """Причина остановки одна на манифест и на журнал ([REQ-018]).

    Манифест несёт machine-readable код `decision.reason` и переписывать его
    своими словами не вправе; журнал называет ту же остановку фазой, в
    которую сессия ушла из `DECIDING`. Разойдись они — пользователь получил
    бы два разных ответа на вопрос «почему сессия встала».
    """
    harness = _escalate(tmp_path, max_total_tokens=max_total_tokens)

    manifest = _manifest_on_disk(tmp_path)
    assert manifest["stop_reason"] == _decision_on_disk(tmp_path).reason
    assert manifest["stop_reason"] == reason
    assert ("DECIDING", outcome.name) in _phases(harness)
    assert manifest["outcome"] == outcome.name.casefold()


def test_manifest_lists_the_open_issues_in_full(tmp_path: Path) -> None:
    """`open_issues` — полные записи §3.2, а не список идентификаторов.

    Четыре поля, и ровно четыре: `id` без `claim` не скажет человеку, что
    осталось несделанным, а сериализованный артефакт целиком утащил бы в
    манифест `evidence` и `suggestion`, которых §3.2 там не ждёт.
    """
    _escalate(tmp_path, max_total_tokens=100_000)

    issues = _open_issues(tmp_path)
    assert issues == [
        {
            "id": _OPEN_B[0],
            "severity": _OPEN_B[2].value,
            "file": _OPEN_B[1],
            "claim": _open_issue(_OPEN_B).claim,
        },
        {
            "id": _OPEN_A[0],
            "severity": _OPEN_A[2].value,
            "file": _OPEN_A[1],
            "claim": _open_issue(_OPEN_A).claim,
        },
    ]
    assert [frozenset(entry) for entry in issues] == [_ISSUE_KEYS, _ISSUE_KEYS]


def test_open_issues_are_the_last_review_filtered_by_the_decision(
    tmp_path: Path,
) -> None:
    """Пересечение ревью раунда-источника с `open_issues_carried` его решения.

    Три наблюдаемых половины: свежее замечание ревью в манифест не попало
    (иначе это «взять весь список»), перенесённые попали ОБА (иначе это
    «взять первое»), а порядок остался порядком ревью — в решении id идут
    наоборот, и совпадение с ним означало бы, что манифест собран из id, а
    не из замечаний.
    """
    _escalate(tmp_path, max_total_tokens=100_000)

    listed = [entry["id"] for entry in _open_issues(tmp_path)]
    carried = list(_decision_on_disk(tmp_path).open_issues_carried)
    reviewed = [issue.id for issue in _review(_ROUND).issues]
    assert listed == [issue_id for issue_id in reviewed if issue_id in carried]
    assert listed == [_OPEN_B[0], _OPEN_A[0]]
    assert carried == [_OPEN_A[0], _OPEN_B[0]]
    assert _FRESH_ID in reviewed
    assert _FRESH_ID not in listed


def test_both_branches_are_exported_by_the_same_code(tmp_path: Path) -> None:
    """Частичная ветка отличается манифестом, а не кодовым путём ([DESIGN-018]).

    Двойники собраны из одних и тех же артефактов раунда и различаются лишь
    решением на диске: файлы результата обязаны совпасть побайтово, а
    манифесты — разойтись ровно в трёх полях честности. `open_issues` среди
    них нет: перечень открытых замечаний принадлежит манифесту как таковому,
    а не ветке эскалации.
    """
    escalated_root = tmp_path / "escalated"
    converged_root = tmp_path / "converged"
    escalated_root.mkdir()
    converged_root.mkdir()

    _escalate(escalated_root, max_total_tokens=100_000)
    _converged_twin(converged_root)

    for name in (_RESULT_MD, _RESULT_PATCH):
        assert (
            _result_path(escalated_root, name).read_bytes()
            == _result_path(converged_root, name).read_bytes()
        )
    escalated = _manifest_on_disk(escalated_root)
    converged = _manifest_on_disk(converged_root)
    assert set(escalated) == set(converged)
    differing = {key for key in escalated if escalated[key] != converged.get(key)}
    assert differing == set(_HONESTY_KEYS)
    assert _open_issues(escalated_root) == _open_issues(converged_root)


def test_export_has_no_separate_path_for_the_partial_outcome() -> None:
    """Ни своей функции, ни ветки по исходу — запрет формой, не поведением.

    Перечень поведений обходится новой веткой; отсутствующее в условии имя
    обойти нечем. Значение `not is_partial(outcome)` под запрет не попадает —
    оно и есть требуемое [DESIGN-018] содержимое манифеста.
    """
    leaked_functions = sorted(
        name
        for name in _module_function_names()
        if any(marker in name.casefold() for marker in _PARTIAL_FUNCTION_MARKERS)
    )
    leaked_branches = sorted(_branch_condition_names() & _BRANCH_NAMES)

    assert not leaked_functions, (
        f"runtime/exporting.py заводит функцию под частичный исход: {leaked_functions}"
    )
    assert not leaked_branches, (
        f"runtime/exporting.py ветвится по исходу сессии: {leaked_branches}"
    )
