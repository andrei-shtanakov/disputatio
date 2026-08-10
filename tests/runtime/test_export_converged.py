"""Экспорт результата сошедшейся сессии ([REQ-017], [DESIGN-017]).

[TASK-018]. Шаг `EXPORTING` ничего не решает и почти ничего не пишет: он
выбирает раунд-источник, собирает манифест из уже принятых решений и отдаёт
всё это одному вызову `events.write_result`. Ломается он ровно в четырёх
местах, и каждое проверяется здесь отдельно:

* **какой раунд объявлен источником.** `state.current_round` на входе в
  терминальную фазу — и он же в манифесте как `source_round`. Раунды
  фикстуры различимы во всём (текст `proposal.md`, текст `changes.patch`,
  исход и причина `decision.json`), поэтому подмена «взять соседний раунд»
  наблюдаема каждым из трёх артефактов по отдельности. Поиск «последнего
  раунда на диске» запрещён формой: у шага нет ни одного имени, которым
  каталог раундов обходят.
* **честен ли манифест.** `converged` берётся ОДНИМ вызовом
  `core.is_partial` — спай, отвечающий вопреки исходу, обязан быть исполнен
  буквально, а собственной таблицы частичных исходов у шага нет вовсе
  (пинится отсутствием имён, а не перечнем поведений).
* **описывает ли `files` то, что легло на диск.** Ключ вычисляет
  `write_result`, и runtime его не подставляет: в манифесте, который шаг
  передаёт, `files` отсутствует, а в манифесте на диске он равен sha256
  реально записанных байт. Отсюда же следует «манифест пишется последним»:
  писателя, кроме `write_result`, у шага нет — ни `atomic_write`, ни
  `write_text`, ни `open`.
* **когда сессия становится `DONE`.** Переход идёт ПОСЛЕ записи результата:
  фаза `DONE`, за которой не стоит `result/`, соврала бы пользователю о
  наличии артефакта.

`runtime/exporting.py` до реализации не существует, поэтому доступ идёт
через `_exporting_attr`: отсутствие модуля переводится в `AssertionError`, а
не в ошибку коллекции — red-чекпоинт обязан быть падением assertion'ом.
"""

import ast
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from disputatio.contracts import (
    SCHEMA_V1,
    AgentRef,
    AgentTurn,
    BudgetUsed,
    Decision,
    Event,
    EventType,
    Limits,
    Mode,
    Outcome,
    Role,
    SessionPhase,
    SessionState,
    TaskSpec,
    VerificationReport,
)
from disputatio.core import SessionFsm
from disputatio.events import (
    bootstrap_session,
    finalize_round,
    write_result,
    write_round_artifact,
)
from disputatio.runtime import RuntimeDeps
from disputatio.runtime.layout import session_dir

_FROZEN_NOW = datetime(2026, 8, 10, 11, 0, 0, tzinfo=UTC)
_SESSION_ID = "20260810-110000-e5f6"
_ROUND = 3
_TOKENS = 4242
_WALL_SECONDS = 7.5
_CONVERGED_REASON = "approve_with_gates_pass"

_RESULT_MD = "result.md"
_RESULT_PATCH = "result.patch"
_MANIFEST = "manifest.json"

# Имена, которыми шаг мог бы завести вторую таблицу частичных исходов §5.
# `converged` вычисляет `core.is_partial`, и второе мнение о том, какой
# исход частичен, разошлось бы с ядром при первой же правке §5.
_OUTCOME_TABLE_NAMES = frozenset(
    {
        "BUDGET_HIT",
        "CONVERGED",
        "DEADLOCK",
        "Outcome",
        "_PARTIAL_OUTCOMES",
        "is_converged",
    }
)

# Имена, которыми обходят каталог раундов. Раунд-источник назван состоянием
# сессии, а не найден на диске: «последний раунд в `rounds/`» совпадает с
# `current_round` ровно до первого обрыва и расходится молча.
_ROUND_SCAN_NAMES = frozenset(
    {
        "glob",
        "iterdir",
        "listdir",
        "rglob",
        "rounds_dir",
        "scandir",
        "walk",
    }
)

# Имена, которыми шаг мог бы писать сам. Единственный писатель результата —
# `events.write_result`: только он считает sha256 по тем байтам, что кладёт
# на диск, и только он кладёт манифест последним.
_WRITER_NAMES = frozenset(
    {
        "atomic_write",
        "dump",
        "mkdir",
        "mkstemp",
        "rename",
        "write_bytes",
        "write_round_artifact",
        "write_text",
    }
)


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
    """Символ модуля шагов — `StepContext` общий у всех шагов цикла."""
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


def _mentioned_names(nodes: Sequence[ast.AST]) -> set[str]:
    """Имена идентификаторов и читаемых атрибутов внутри `nodes`.

    Голые имена и атрибуты вместе: `Outcome.DEADLOCK` прячет запрещённое имя
    в атрибуте, `from ... import DEADLOCK as X` — в присваивании, и скан,
    смотрящий только на одну из двух форм, обходится второй.
    """
    names: set[str] = set()
    for scope in nodes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def _export_names() -> set[str]:
    """Все имена, упомянутые в транзитивном замыкании `export`."""
    module = _exporting()
    functions = _functions_reachable_from(ast.parse(_source(module)), "export")
    return _mentioned_names(list(functions))


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
    """`AgentAdapter`-фейк: экспорт ни с кем не разговаривает (§7)."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Вызов означает, что шаг спросил агента вместо артефактов."""
        raise AssertionError("EXPORTING не вправе звать агентов")


@dataclass
class NoVerifier:
    """`Verifier`-фейк: гейты прогнаны раундом, повтора при экспорте нет."""

    def verify(self, round_no: int) -> VerificationReport:
        """Вызов означает, что шаг перепрогнал гейты раунда."""
        raise AssertionError(f"EXPORTING не вправе гонять гейты (раунд {round_no})")


@dataclass
class NoGit:
    """`GitOps`-фейк: экспорт читает артефакты, а не рабочее дерево."""

    def diff_head(self) -> str:
        """Не вызывается: патч раунда снят шагом PROPOSING."""
        raise AssertionError("EXPORTING не вправе снимать дифф")

    def commit_round(self, round_no: int) -> None:
        """Не вызывается: раунд зафиксирован шагом DECIDING."""
        raise AssertionError(f"EXPORTING не вправе коммитить раунд {round_no}")

    def reset_hard(self, rev: str) -> None:
        """Не вызывается: сброс дерева — прерогатива PROPOSING."""
        raise AssertionError(f"EXPORTING не вправе сбрасывать дерево на {rev}")

    def clean(self) -> None:
        """Не вызывается: уборка дерева — прерогатива PROPOSING."""
        raise AssertionError("EXPORTING не вправе убирать дерево")


@dataclass(frozen=True)
class WriteResultCall:
    """Один вызов `write_result`, пойманный спаем."""

    root: Path
    files: dict[str, str]
    manifest: dict[str, Any]


@dataclass
class Harness:
    """Собранное окружение шага: контекст, спаи и общий лог порядка."""

    root: Path
    ctx: Any
    fsm: SessionFsm
    store: FakeStore
    sink: FakeSink
    log: list[str]
    calls: list[WriteResultCall]


def _state(round_no: int) -> SessionState:
    """`SessionState` раунда `round_no` в фазе `EXPORTING`."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=SessionPhase.EXPORTING,
        current_round=round_no,
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
        limits=Limits(
            max_rounds=9,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        budget_used=BudgetUsed(
            tokens=_TOKENS, wall_seconds=_WALL_SECONDS, cost_usd_est=0.25
        ),
    )


def _proposal_text(round_no: int) -> str:
    """`proposal.md` раунда `round_no`; номер раунда — в каждой строке текста.

    Помечен нарочно: результат собирается из ОДНОГО раунда, и «взяли
    соседний» обязано быть видно по содержимому файла, а не выводиться из
    совпадения длин.
    """
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


def _decision(round_no: int, *, source: bool) -> Decision:
    """Решение раунда: исход раунда-источника отличается от прочих.

    Прошлые раунды закрыты `continue` со своей причиной — при подмене
    источника это видно и в `outcome`, и в `stop_reason` манифеста.
    """
    marker = f"{round_no:03d}"
    if source:
        return Decision(
            round=round_no,
            outcome=Outcome.CONVERGED,
            reason=_CONVERGED_REASON,
            open_issues_carried=[],
            next_round_directive=None,
        )
    return Decision(
        round=round_no,
        outcome=Outcome.CONTINUE,
        reason=f"нужны правки раунда {marker}",
        open_issues_carried=[],
        next_round_directive=f"директива раунда {marker}",
    )


def _seed(root: Path, *, round_no: int, write_patch: bool = True) -> None:
    """Кладёт на диск раунды `1…round_no` так, как их оставила бы сессия.

    Все раунды финализированы (I3): к моменту `EXPORTING` принятые раунды
    закрыты от правок, и шаг обязан их читать, а не переписывать.
    """
    bootstrap_session(root)
    for prior in range(1, round_no + 1):
        source = prior == round_no
        write_round_artifact(root, prior, "proposal.md", _proposal_text(prior))
        if write_patch or not source:
            write_round_artifact(root, prior, "changes.patch", _patch_text(prior))
        write_round_artifact(
            root,
            prior,
            "decision.json",
            _decision(prior, source=source).model_dump_json(by_alias=True),
        )
        finalize_round(root, prior)


def _spy_write_result(
    monkeypatch: pytest.MonkeyPatch, log: list[str], calls: list[WriteResultCall]
) -> None:
    """Подменяет `write_result` в модуле шага, сохраняя настоящую запись."""
    exporting = _exporting()
    assert hasattr(exporting, "write_result"), (
        "runtime/exporting.py обязан писать результат через events.write_result — "
        "sha256 считает тот, кто кладёт байты на диск"
    )

    def spy(root: Path, files: Mapping[str, str], manifest: Mapping[str, Any]) -> None:
        log.append("write_result")
        calls.append(
            WriteResultCall(root=root, files=dict(files), manifest=dict(manifest))
        )
        write_result(root, files, manifest)

    monkeypatch.setattr(exporting, "write_result", spy)


def _spy_transition(
    monkeypatch: pytest.MonkeyPatch, fsm: SessionFsm, log: list[str]
) -> None:
    """Оборачивает `fsm.transition`, записывая целевую фазу в общий лог."""
    original = fsm.transition

    def spy(to: SessionPhase) -> SessionState:
        log.append(f"transition:{to.value}")
        return original(to)

    monkeypatch.setattr(fsm, "transition", spy)


def _make_harness(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    round_no: int = _ROUND,
    write_patch: bool = True,
    seed: bool = True,
) -> Harness:
    """Собирает `StepContext` со спаями на обеих наблюдаемых границах шага."""
    if seed:
        _seed(root, round_no=round_no, write_patch=write_patch)
    log: list[str] = []
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(_state(round_no), store=store, sink=sink, now=lambda: _FROZEN_NOW)
    deps = RuntimeDeps(
        root=root,
        store=store,
        sink=sink,
        author=NoAgent(),
        reviewer=NoAgent(),
        verifier=NoVerifier(),
        git=NoGit(),
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    calls: list[WriteResultCall] = []
    _spy_write_result(monkeypatch, log, calls)
    _spy_transition(monkeypatch, fsm, log)
    ctx = _steps_attr("StepContext")(deps=deps, fsm=fsm, base_commit="0" * 40)
    return Harness(
        root=root,
        ctx=ctx,
        fsm=fsm,
        store=store,
        sink=sink,
        log=log,
        calls=calls,
    )


def _export(harness: Harness) -> None:
    """Выполняет шаг EXPORTING — он синхронен, агентов не зовёт."""
    _exporting_attr("export")(harness.ctx)


def _result_path(root: Path, name: str) -> Path:
    """Путь файла результата `result/name`."""
    return session_dir(root) / "result" / name


def _manifest_on_disk(root: Path) -> dict[str, Any]:
    """`result/manifest.json`, прочитанный обратно."""
    raw = _result_path(root, _MANIFEST).read_text(encoding="utf-8")
    document = json.loads(raw)
    assert isinstance(document, dict), f"манифест обязан быть объектом: {raw!r}"
    return document


def _sha256_on_disk(root: Path, name: str) -> str:
    """sha256 байт, реально лежащих в `result/name`."""
    return hashlib.sha256(_result_path(root, name).read_bytes()).hexdigest()


def _state_changes(harness: Harness) -> list[tuple[str, str]]:
    """Пары `(from, to)` всех переходов, попавших в журнал событий."""
    return [
        (event.payload["from"], event.payload["to"])
        for event in harness.sink.events
        if event.type is EventType.STATE_CHANGE
    ]


@pytest.mark.parametrize("round_no", [1, _ROUND])
def test_result_files_are_the_artifacts_of_the_current_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, round_no: int
) -> None:
    """`result.md`/`result.patch` — артефакты раунда `state.current_round`.

    Два раунда, а не один: при единственном раунде «взять первый», «взять
    последний» и «взять текущий» неразличимы, и тест зеленел бы для любой
    из трёх реализаций.
    """
    harness = _make_harness(tmp_path, monkeypatch, round_no=round_no)

    _export(harness)

    assert _result_path(tmp_path, _RESULT_MD).read_text(
        encoding="utf-8"
    ) == _proposal_text(round_no)
    assert _result_path(tmp_path, _RESULT_PATCH).read_text(
        encoding="utf-8"
    ) == _patch_text(round_no)


def test_manifest_records_the_converged_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Манифест сошедшейся сессии — все поля §3.2, каждое из своего источника.

    `outcome`/`stop_reason` берутся у решения раунда-источника (у соседних
    раундов они другие), `budget_used` — у состояния сессии, `source_round`
    — у её текущего раунда.
    """
    harness = _make_harness(tmp_path, monkeypatch)

    _export(harness)

    manifest = _manifest_on_disk(tmp_path)
    assert manifest["schema"] == SCHEMA_V1
    assert manifest["session"] == _SESSION_ID
    assert manifest["converged"] is True
    assert manifest["outcome"] == Outcome.CONVERGED.value
    assert manifest["stop_reason"] == _CONVERGED_REASON
    assert manifest["source_round"] == _ROUND
    assert manifest["rounds_total"] == _ROUND
    assert manifest["budget_used"] == {
        "tokens": _TOKENS,
        "wall_seconds": _WALL_SECONDS,
    }


def test_converged_flag_is_taken_from_core_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`converged = not core.is_partial(outcome)` — и ничего больше.

    Спай отвечает вопреки исходу: решение раунда — `converged`, а ядро
    говорит «частичный». Любая собственная формула runtime («исход равен
    converged», «фаза была CONVERGED») отменила бы этот ответ.
    """
    harness = _make_harness(tmp_path, monkeypatch)
    exporting = _exporting()
    assert hasattr(exporting, "is_partial"), (
        "runtime/exporting.py обязан импортировать core.is_partial именем "
        "is_partial — частичность исхода определяет ядро, а не экспорт"
    )
    seen: list[Any] = []

    def spy(outcome: Any) -> bool:
        seen.append(outcome)
        return True

    monkeypatch.setattr(exporting, "is_partial", spy)

    _export(harness)

    assert seen == [Outcome.CONVERGED]
    assert _manifest_on_disk(tmp_path)["converged"] is False


def test_export_keeps_no_table_of_partial_outcomes() -> None:
    """§5 в модуле экспорта отсутствует как форма, а не как поведение.

    Запрещены сами имена исходов: перечень поведений обходится новой веткой,
    отсутствующее имя обойти нечем.
    """
    leaked = sorted(_export_names() & _OUTCOME_TABLE_NAMES)

    assert not leaked, f"runtime/exporting.py заводит свою таблицу исходов: {leaked}"


def test_export_does_not_search_the_rounds_directory() -> None:
    """Раунд-источник назван состоянием, а не найден на диске ([DESIGN-017]).

    «Последний раунд в `rounds/`» совпадает с `current_round` ровно до
    первого обрыва — и расходится с ним молча, уже в манифесте.
    """
    leaked = sorted(_export_names() & _ROUND_SCAN_NAMES)

    assert not leaked, f"runtime/exporting.py обходит каталог раундов: {leaked}"


def test_files_checksums_describe_the_bytes_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`files` заполняет `write_result`, а не runtime ([REQ-017]).

    Две половины одного правила: переданный шагом манифест ключа `files` не
    содержит вовсе, а манифест на диске несёт sha256 ровно тех байт, что
    лежат в `result/`. Подставь checksum сам шаг — он описывал бы то, что
    собирался записать, а не то, что записано.
    """
    harness = _make_harness(tmp_path, monkeypatch)

    _export(harness)

    assert len(harness.calls) == 1
    assert "files" not in harness.calls[0].manifest
    assert _manifest_on_disk(tmp_path)["files"] == {
        _RESULT_MD: {"sha256": _sha256_on_disk(tmp_path, _RESULT_MD)},
        _RESULT_PATCH: {"sha256": _sha256_on_disk(tmp_path, _RESULT_PATCH)},
    }


def test_result_is_written_by_a_single_write_result_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Единственный писатель результата — `write_result` ([DESIGN-017]).

    Отсюда следует «манифест пишется последним»: порядок «сначала файлы,
    потом манифест» принадлежит `write_result` и пинится его собственным
    тестом, а шаг лишён формы, которой мог бы записать что-то мимо него.
    """
    harness = _make_harness(tmp_path, monkeypatch)

    _export(harness)

    assert len(harness.calls) == 1
    call = harness.calls[0]
    assert call.root == tmp_path
    assert set(call.files) == {_RESULT_MD, _RESULT_PATCH}

    leaked = sorted(_export_names() & _WRITER_NAMES)
    assert not leaked, f"runtime/exporting.py пишет мимо write_result: {leaked}"


def test_export_transitions_to_done_after_the_result_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Фаза `DONE` наступает ПОСЛЕ записи результата ([REQ-017]).

    Порядок — само поведение шага: `DONE`, за которой не стоит `result/`,
    объявила бы законченной сессию без артефакта, а resume переигрывать её
    уже не стал бы.
    """
    harness = _make_harness(tmp_path, monkeypatch)

    _export(harness)

    assert harness.log == ["write_result", "transition:DONE"]
    assert harness.fsm.state.state is SessionPhase.DONE
    assert harness.fsm.state.current_round == _ROUND
    assert _state_changes(harness) == [("EXPORTING", "DONE")]
    assert [saved.state for saved in harness.store.saved] == [SessionPhase.DONE]


def test_round_without_changes_exports_an_empty_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нет `changes.patch` — `result.patch` пуст, а не отсутствует ([REQ-013]).

    Файл обязан быть: «автор ничего не менял» и «экспорт до патча не дошёл»
    — разные факты, и различает их только наличие файла с его checksum.
    """
    harness = _make_harness(tmp_path, monkeypatch, write_patch=False)

    _export(harness)

    assert _result_path(tmp_path, _RESULT_PATCH).read_bytes() == b""
    assert _manifest_on_disk(tmp_path)["files"][_RESULT_PATCH] == {
        "sha256": hashlib.sha256(b"").hexdigest()
    }


def test_missing_decision_of_the_source_round_is_an_ordering_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нет `decision.json` раунда-источника → `AssertionError`, не пустой манифест.

    Сюда приводит только сломанная диспетчеризация цикла: в `EXPORTING`
    сессию заводит `apply_decision`, то есть решение уже лежит на диске.
    Манифест без исхода был бы честнее пустого, но `result/` при этом уже
    объявлял бы результат экспортированным.
    """
    harness = _make_harness(tmp_path, monkeypatch)
    (session_dir(tmp_path) / "rounds" / f"{_ROUND:03d}" / "decision.json").unlink()

    with pytest.raises(AssertionError):
        _export(harness)

    assert harness.calls == []
    assert not _result_path(tmp_path, _MANIFEST).exists()
    assert harness.fsm.state.state is SessionPhase.EXPORTING


def test_missing_proposal_of_the_source_round_is_an_ordering_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нет `proposal.md` раунда-источника → `AssertionError`, не пустой файл.

    Пустой `result.md` с честным sha256 выглядел бы законным экспортом
    сессии, у которой автор ничего не написал.
    """
    harness = _make_harness(tmp_path, monkeypatch)
    (session_dir(tmp_path) / "rounds" / f"{_ROUND:03d}" / "proposal.md").unlink()

    with pytest.raises(AssertionError):
        _export(harness)

    assert harness.calls == []
    assert not _result_path(tmp_path, _MANIFEST).exists()
    assert harness.fsm.state.state is SessionPhase.EXPORTING
