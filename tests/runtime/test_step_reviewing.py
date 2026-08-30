"""Шаг REVIEWING ([REQ-005], [DESIGN-005], NFR-003).

[TASK-010]. Шаг ничего не решает сам: правила §4.4 живут в
`contracts.validate_review`, раскладка §6.2 — в `context`, а runtime
отвечает за две вещи, обе из которых ломаются молча.

* **что уходит ревьюеру.** Промпт — результат `context.build_reviewer_prompt`,
  а не склейка вокруг него; пути `proposal.md`/`changes.patch` идут
  относительными, потому что читает их сам ревьюер своим read-only
  инструментом (§7); текст автора целиком в промпт не инлайнится, а то, что
  всё-таки в него попадает, обёрнуто метками «данные, не инструкции» из
  `context.tags` — своих меток runtime не изобретает.
* **что ложится на диск.** На диск идёт `acceptance.review` — **деградированная
  копия**: `blocker` без evidence обязан приехать в `review.json` как `minor`
  (REQ-009). Непринятое ревью (approve при красных гейтах, пустой `checked`)
  не пишется вовсе — шаг сигналит повтор, а не сохраняет отвергнутое.

Источники данных разведены нарочно (урок TASK-009): раунд 3 и раунд 2 несут
РАЗНЫЕ отчёты проверок (`fail` против `pass`), у автора и ревьюера РАЗНЫЕ
`session_ref`, автор-адаптер на любой вызов падает. Фикстура, где все
источники совпадают, пинит значение, а не источник, и подмену «взять отчёт
раунда N−1» не видит вовсе.

`extract_json_object` считает скобки с учётом строковых литералов и escape'ов:
`{` внутри `"claim"` не обрывает объект. Ничего не исполняется — ни `eval`, ни
`exec`, ни подстановка в argv (NFR-003), и это пинится не только поведением, а
запретом самой формы: AST-скан `parsing.py` и замыкания `review`.

`runtime.parsing` и `steps.review` до реализации не существуют, поэтому доступ
идёт через `_parsing_attr`/`_steps_attr`: отсутствие модуля или символа
переводится в `AssertionError`, а не в ошибку коллекции — red-чекпоинт обязан
быть падением assertion'ом.
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

import anyio
import pytest
from pydantic import ValidationError

from disputatio.context import build_reviewer_prompt
from disputatio.context.tags import wrap_artifact_data
from disputatio.contracts import (
    REASON_APPROVE_ON_FAILED_GATES,
    REASON_EMPTY_CHECKED,
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
    validate_review,
)
from disputatio.core import SessionFsm
from disputatio.events import write_round_artifact
from disputatio.runtime import RuntimeDeps

from ._fakes import GitOpsFakeBase

_FROZEN_NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
_ROUND = 3
_SESSION_ID = "s-reviewing"
_AUTHOR_REF = "ref-author"
_REVIEWER_REF = "ref-reviewer"

# Тело `proposal.md` раунда N. Ни одна его строка не вправе оказаться в
# промпте: ревьюер получает путь и читает файл сам (§6.2).
_PROPOSAL_BODY = "ТЕЛО-PROPOSAL-РАУНДА-003"
_PATCH_BODY = "СОДЕРЖИМОЕ-PATCH-РАУНДА-003"

# Ожидаемая последовательность шага целиком. Список, а не множество: цена
# ошибки здесь — не «забыли вызвать», а «вызвали не в том порядке».
_EXPECTED_ORDER = [
    "build_reviewer_prompt",
    "reviewer.run",
    "write:review.json",
]

# Формы, которыми агентский текст превращается в исполняемое действие.
# Запрещены целиком: ни шагу, ни парсеру они не нужны ни для чего (NFR-003).
_EXECUTION_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "literal_eval",
        "system",
        "popen",
        "Popen",
        "check_output",
        "check_call",
        "getoutput",
        "getstatusoutput",
        "execv",
        "execvp",
        "spawnv",
    }
)

# Модули, из которых берётся исполнение чужого текста. `parsing.py` не
# импортирует их вовсе: импорт — первая половина исполнения.
_EXECUTION_MODULES = frozenset({"subprocess", "os", "pty", "shlex", "runpy"})


def _steps() -> ModuleType:
    """Модуль `runtime/steps.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.steps")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/steps.py: {exc}") from exc


def _parsing() -> ModuleType:
    """Модуль `runtime/parsing.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.parsing")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/parsing.py: {exc}") from exc


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


def _parsing_attr(name: str) -> Any:
    """Символ модуля парсинга."""
    return _attr(_parsing(), name)


def _runtime_attr(name: str) -> Any:
    """Символ публичного `disputatio.runtime` — иерархия ошибок [DESIGN-020]."""
    return _attr(import_module("disputatio.runtime"), name)


def _source(module: ModuleType) -> str:
    """Исходник модуля с диска — для проверок «код такой формы отсутствует»."""
    path = module.__file__
    assert path is not None, f"у {module.__name__} нет файла на диске"
    return Path(path).read_text(encoding="utf-8")


def _functions_reachable_from(
    tree: ast.Module, root: str
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Функция `root` модуля и все функции модуля, вызываемые из неё.

    Замыкание транзитивное: форму, вынесенную в хелпер, скан обязан видеть
    так же, как форму в теле, — иначе запрет обходится одной лишней
    функцией. `AsyncFunctionDef` наравне с `FunctionDef`: шаг REVIEWING
    асинхронен, и скан по одному лишь `FunctionDef` не нашёл бы его вовсе.
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


def _called_names(nodes: Sequence[ast.AST]) -> set[str]:
    """Имена всех вызываемых в `nodes` функций — и голые, и через атрибут."""
    names: set[str] = set()
    for scope in nodes:
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def _shell_true_calls(nodes: Sequence[ast.AST]) -> list[ast.Call]:
    """Вызовы с `shell=True` в любом месте `nodes`."""
    found: list[ast.Call] = []
    for scope in nodes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Call) and any(
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                found.append(node)
    return found


def _imported_roots(tree: ast.Module) -> set[str]:
    """Корневые имена всех импортируемых модулем пакетов."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


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
class SpyReviewer:
    """`AgentAdapter`-фейк ревьюера: журнал вызовов и заготовленный ответ."""

    log: list[str]
    reply: str
    prompts: list[str] = field(default_factory=list)
    session_refs: list[str | None] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует вызов и отдаёт заготовленный текст ревью."""
        self.log.append("reviewer.run")
        self.prompts.append(prompt)
        self.session_refs.append(session_ref)
        return AgentTurn(text=self.reply, session_ref=session_ref)


@dataclass
class NoAgent:
    """`AgentAdapter`-фейк автора: шаг REVIEWING автора не зовёт вовсе."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Вызов означает, что шаг перепутал адаптеры ролей."""
        raise AssertionError("REVIEWING не вправе звать автора")


@dataclass
class NoVerifier:
    """`Verifier`-фейк: гейты прогнаны шагом VERIFYING, повтора не бывает."""

    def verify(self, round_no: int) -> VerificationReport:
        """Вызов означает, что шаг перепрогнал гейты раунда."""
        raise AssertionError(f"REVIEWING не вправе гонять гейты (раунд {round_no})")


@dataclass
class NoGit(GitOpsFakeBase):
    """`GitOps`-фейк: шаг REVIEWING git не трогает — любой вызов ошибка.

    Он же пин «вывод ревьюера не подставляется в argv»: единственный путь
    из шага во внешний процесс идёт через этот порт, и он закрыт наглухо.
    """

    def diff_head(self) -> str:
        """Не вызывается: патч раунда снят шагом PROPOSING."""
        raise AssertionError("REVIEWING не вправе трогать git")

    def commit_round(self, round_no: int) -> None:
        """Не вызывается: коммит раунда принимает DECIDING."""
        raise AssertionError(f"REVIEWING не вправе коммитить раунд {round_no}")

    def reset_hard(self, rev: str) -> None:
        """Не вызывается: сброс дерева — прерогатива PROPOSING."""
        raise AssertionError(f"REVIEWING не вправе сбрасывать дерево на {rev}")

    def clean(self) -> None:
        """Не вызывается: уборка дерева — прерогатива PROPOSING."""
        raise AssertionError("REVIEWING не вправе убирать дерево")


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
    reviewer: SpyReviewer
    log: list[str]
    prompt_calls: list[dict[str, Any]]
    writes: list[Written]


def _state(phase: SessionPhase = SessionPhase.REVIEWING) -> SessionState:
    """`SessionState` раунда `_ROUND` в фазе `phase`.

    `session_ref` у ролей разные: одинаковые ссылки сделали бы подмену
    «ревьюеру передали ссылку автора» ненаблюдаемой.
    """
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
                adapter="claude_code", model="opus", session_ref=_AUTHOR_REF
            ),
            Role.REVIEWER: AgentRef(
                adapter="claude_code", model="sonnet", session_ref=_REVIEWER_REF
            ),
        },
        limits=Limits(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        budget_used=BudgetUsed(),
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


def _prior_review(round_no: int) -> Review:
    """Ревью раунда `round_no` с помеченным раундом замечанием."""
    marker = f"{round_no:03d}"
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.9,
        issues=[
            Issue(
                id=f"I-{marker}",
                severity=Severity.MAJOR,
                file="feature.py",
                claim=f"замечание раунда {marker}",
                evidence=f"feature.py:1 — свидетельство раунда {marker}",
            )
        ],
        checked=["feature.py"],
        summary=f"свод раунда {marker}",
    )


def _prior_decision(round_no: int) -> Decision:
    """Решение раунда `round_no`; `open_issues_carried` пуст.

    Пуст намеренно: тогда замечание раунда N−1 попадает в промпт как
    «автор заявил решённым» (§6.2) — иначе секции нет и проверять обёртку
    «данные, не инструкции» было бы не на чем.
    """
    marker = f"{round_no:03d}"
    return Decision(
        round=round_no,
        outcome=Outcome.CONTINUE,
        reason=f"причина раунда {marker}",
        open_issues_carried=[],
        next_round_directive=f"директива раунда {marker}",
    )


def _seed_rounds(root: Path, *, overall: OverallStatus) -> None:
    """Кладёт на диск историю раундов 1–2 и артефакты раунда `_ROUND`.

    Раунды 1 и 2 различаются маркерами (`001`/`002`), а отчёт раунда 2
    всегда `pass` — при отчёте раунда `_ROUND` со статусом `fail` это
    делает наблюдаемой подмену «взять verification прошлого раунда».
    """
    for round_no in (1, 2):
        _write(root, round_no, "review.json", _prior_review(round_no))
        _write(root, round_no, "decision.json", _prior_decision(round_no))
        _write(
            root,
            round_no,
            "verification.json",
            _verification(round_no, overall=OverallStatus.PASS),
        )
    _write(root, _ROUND, "verification.json", _verification(_ROUND, overall=overall))
    write_round_artifact(root, _ROUND, "proposal.md", f"{_PROPOSAL_BODY}\n")
    write_round_artifact(root, _ROUND, "changes.patch", f"{_PATCH_BODY}\n")


def _write(
    root: Path,
    round_no: int,
    name: str,
    artifact: Review | Decision | VerificationReport,
) -> None:
    """Сериализует артефакт в раунд `round_no` как это делает оркестратор."""
    write_round_artifact(root, round_no, name, artifact.model_dump_json(by_alias=True))


def _review_payload(
    *,
    round_no: int = _ROUND,
    verdict: str = "request_changes",
    issues: list[dict[str, Any]] | None = None,
    checked: list[str] | None = None,
    summary: str = "свод ревьюера раунда 003",
) -> dict[str, Any]:
    """`review.json`-payload ревьюера; дефолт схемно и протокольно валиден."""
    return {
        "schema": "disputatio/v1",
        "round": round_no,
        "role": "reviewer",
        "verdict": verdict,
        "confidence": 0.8,
        "issues": (
            [
                {
                    "id": "I-A",
                    "severity": "major",
                    "file": "feature.py",
                    "claim": "экспорт теряет заголовок",
                    "evidence": "feature.py:12 — writer.writerow пропущен",
                }
            ]
            if issues is None
            else issues
        ),
        "checked": ["feature.py"] if checked is None else checked,
        "summary": summary,
    }


def _reply(payload: dict[str, Any], *, fenced: bool = False) -> str:
    """Ответ ревьюера: болтовня вокруг JSON, опционально в ```json-фенсе."""
    body = json.dumps(payload, ensure_ascii=False)
    if fenced:
        body = f"```json\n{body}\n```"
    return f"Посмотрел патч.\n{body}\nГотов ответить на вопросы."


def _spy_prompt_builder(
    monkeypatch: pytest.MonkeyPatch, log: list[str], calls: list[dict[str, Any]]
) -> None:
    """Подменяет сборщик промпта в модуле шага, сохраняя его результат.

    Спай возвращает настоящий текст: тест сравнивает с ним промпт, ушедший
    адаптеру, и ловит любую склейку вокруг вызова.
    """
    steps = _steps()
    assert hasattr(steps, "build_reviewer_prompt"), (
        "runtime/steps.py обязан импортировать context.build_reviewer_prompt "
        "именем build_reviewer_prompt — промпт собирается вызовом, не склейкой"
    )

    def spy(**kwargs: Any) -> str:
        log.append("build_reviewer_prompt")
        calls.append(kwargs)
        return build_reviewer_prompt(**kwargs)

    monkeypatch.setattr(steps, "build_reviewer_prompt", spy)


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
    reply: str,
    overall: OverallStatus = OverallStatus.FAIL,
) -> Harness:
    """Собирает `StepContext` со спаями на всех наблюдаемых границах шага."""
    _seed_rounds(root, overall=overall)
    log: list[str] = []
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(_state(), store=store, sink=sink, now=lambda: _FROZEN_NOW)
    reviewer = SpyReviewer(log=log, reply=reply)
    deps = RuntimeDeps(
        workspace_root=root,
        artifact_root=root,
        store=store,
        sink=sink,
        author=NoAgent(),
        reviewer=reviewer,
        verifier=NoVerifier(),
        git=NoGit(),
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    prompt_calls: list[dict[str, Any]] = []
    writes: list[Written] = []
    _spy_prompt_builder(monkeypatch, log, prompt_calls)
    _spy_writer(monkeypatch, log, writes)
    ctx = _steps_attr("StepContext")(deps=deps, fsm=fsm, base_commit="0" * 40)
    return Harness(
        root=root,
        ctx=ctx,
        fsm=fsm,
        reviewer=reviewer,
        log=log,
        prompt_calls=prompt_calls,
        writes=writes,
    )


def _review(harness: Harness) -> None:
    """Выполняет шаг: единственный await шага крутится через `anyio.run`."""
    anyio.run(_steps_attr("review"), harness.ctx)


def _review_path(root: Path) -> Path:
    """Путь `rounds/NNN/review.json` по read-side зеркалу раскладки."""
    layout = _layout()
    return layout.round_artifact(root, _ROUND, layout.REVIEW_NAME)


def _data_blocks(prompt: str) -> list[str]:
    """Куски промпта внутри меток artifact-данных `context.tags`.

    Метки берутся у самой обёртки, а не переписываются литералом: тест,
    знающий текст меток наизусть, перестал бы падать при их смене — и
    именно это он обязан ловить.
    """
    sentinel = "__SENTINEL__"
    wrapped = wrap_artifact_data(sentinel)
    open_tag, _, tail = wrapped.partition(f"\n{sentinel}\n")
    close_tag = tail
    blocks: list[str] = []
    rest = prompt
    while open_tag in rest:
        _, _, after_open = rest.partition(open_tag)
        block, sep, rest = after_open.partition(close_tag)
        assert sep, "открытая метка artifact-данных не закрыта — блоки разъехались"
        blocks.append(block)
    return blocks


def _outside_blocks(prompt: str) -> str:
    """Промпт без содержимого artifact-блоков — «привилегированная» часть."""
    outside = prompt
    for block in _data_blocks(prompt):
        outside = outside.replace(block, "")
    return outside


def test_extract_json_object_keeps_braces_inside_string_literals() -> None:
    """`{` и `\\"` внутри строкового литерала не обрывают объект ([REQ-005]).

    Наивный счётчик скобок закрывает объект на первой `}` или обрывается
    на `{` внутри `claim`; вывод ревьюера, честно процитировавший код,
    ушёл бы в `ReviewParseError` вместо разбора.
    """
    extract = _parsing_attr("extract_json_object")
    text = (
        'Вот ревью.\n{"schema": "disputatio/v1", '
        '"claim": "a { b", '
        '"quote": "он написал \\"{\\" и ушёл", '
        '"nested": {"file": "}", "line": 12}}\nконец'
    )

    extracted = extract(text)

    assert extracted in text, "извлечение обязано быть подстрокой, а не пересборкой"
    payload = json.loads(extracted)
    assert payload["claim"] == "a { b"
    assert payload["quote"] == 'он написал "{" и ушёл'
    assert payload["nested"] == {"file": "}", "line": 12}


def test_extract_json_object_reads_bare_json_and_a_json_fence() -> None:
    """Голый JSON и ```json-фенс разбираются одинаково ([REQ-005])."""
    extract = _parsing_attr("extract_json_object")
    payload = _review_payload()

    bare = extract(_reply(payload))
    fenced = extract(_reply(payload, fenced=True))

    assert json.loads(bare) == payload
    assert json.loads(fenced) == payload
    assert "```" not in fenced, "фенс не является частью JSON-объекта"


def test_text_without_json_raises_review_parse_error() -> None:
    """Текст без объекта → `ReviewParseError`, а не `None` и не `""`."""
    parse_error = _runtime_attr("ReviewParseError")
    base = _runtime_attr("DisputatioError")
    extract = _parsing_attr("extract_json_object")

    with pytest.raises(parse_error):
        extract("Мне нечего добавить, всё хорошо. {этот объект не закрыт")

    assert issubclass(parse_error, base), (
        "ReviewParseError обязан быть в иерархии DisputatioError: CLI ловит "
        "одну базу и печатает .args[0], а не traceback"
    )


def test_parsing_never_evaluates_the_agent_text() -> None:
    """Парсер вывода агента ничего не исполняет и не импортирует (NFR-003)."""
    tree = ast.parse(_source(_parsing()))

    called = _called_names([tree])
    assert not (called & _EXECUTION_CALLS), (
        "runtime/parsing.py исполняет текст агента: "
        f"{sorted(called & _EXECUTION_CALLS)}"
    )
    assert not (_imported_roots(tree) & _EXECUTION_MODULES), (
        "runtime/parsing.py импортирует модуль исполнения — текст агента "
        "обязан оставаться данными"
    )
    assert not _shell_true_calls([tree]), "shell=True в парсере вывода ревьюера"


def test_step_order_is_prompt_then_reviewer_then_review_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порядок шага фиксирован общим spy-логом ([DESIGN-005])."""
    harness = _make_harness(tmp_path, monkeypatch, reply=_reply(_review_payload()))

    _review(harness)

    assert harness.log == _EXPECTED_ORDER


def test_prompt_is_the_builder_result_and_goes_to_the_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Адаптер получает ровно то, что вернул `build_reviewer_prompt` (§6.2).

    Заодно пин ролей: ссылка `--resume` — ревьюера, а не автора, и зовётся
    `deps.reviewer`, а не `deps.author` (тот на любой вызов падает).
    """
    harness = _make_harness(tmp_path, monkeypatch, reply=_reply(_review_payload()))

    _review(harness)

    assert len(harness.prompt_calls) == 1
    expected = build_reviewer_prompt(**harness.prompt_calls[0])
    assert harness.reviewer.prompts == [expected]
    assert harness.reviewer.session_refs == [_REVIEWER_REF]


def test_prompt_carries_relative_paths_and_no_inlined_author_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пути артефактов — относительные, содержимое не инлайнится (§6.2)."""
    harness = _make_harness(tmp_path, monkeypatch, reply=_reply(_review_payload()))

    _review(harness)

    layout = _layout()
    call = harness.prompt_calls[0]
    for param, name in (
        ("proposal_path", layout.PROPOSAL_NAME),
        ("patch_path", layout.CHANGES_PATCH_NAME),
    ):
        path = call[param]
        assert isinstance(path, str), f"{param} обязан быть строкой промпта"
        assert not Path(path).is_absolute(), (
            f"{param} абсолютен: путь машины оркестратора бесполезен ревьюеру "
            "и утекает раскладкой ФС в промпт"
        )
        assert "\\" not in path, f"{param} несёт разделитель Windows"
        assert (tmp_path / path) == layout.round_artifact(tmp_path, _ROUND, name)
        assert (tmp_path / path).is_file()
        assert path in harness.reviewer.prompts[0]

    prompt = harness.reviewer.prompts[0]
    assert _PROPOSAL_BODY not in prompt, "текст автора инлайнится вместо пути"
    assert _PATCH_BODY not in prompt, "патч инлайнится вместо пути"


def test_artifact_text_reaches_the_prompt_only_inside_context_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Барьер «данные, не инструкции» приходит из `context`, не из runtime.

    Пинится с двух сторон: артефактный текст обязан лежать внутри блоков,
    размеченных `context.tags.wrap_artifact_data`, и вне их не встречаться —
    а исходники runtime не вправе содержать текст меток вовсе, иначе он
    разъедется с `context` молча.
    """
    harness = _make_harness(tmp_path, monkeypatch, reply=_reply(_review_payload()))

    _review(harness)

    prompt = harness.reviewer.prompts[0]
    blocks = _data_blocks(prompt)
    outside = _outside_blocks(prompt)
    task_prompt = harness.fsm.state.task.prompt
    for text in (task_prompt, "замечание раунда 002"):
        assert any(text in block for block in blocks), (
            f"{text!r} попал в промпт вне меток artifact-данных"
        )
        assert text not in outside
    assert wrap_artifact_data(task_prompt) in prompt

    for module in (_steps(), _parsing()):
        source = _source(module)
        assert "ARTIFACT_DATA" not in source, (
            f"{module.__name__} содержит текст меток — runtime изобретает "
            "собственную обёртку вместо context.tags"
        )


def test_prompt_carries_round_n_verification_and_round_n_minus_one_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отчёт — раунда N, замечания — раунда N−1 ([REQ-005], §6.2)."""
    harness = _make_harness(tmp_path, monkeypatch, reply=_reply(_review_payload()))

    _review(harness)

    call = harness.prompt_calls[0]
    assert call["round"] == _ROUND
    assert call["task"] == harness.fsm.state.task
    assert call["verification"].round == _ROUND
    assert call["verification"].overall is OverallStatus.FAIL
    assert call["prior_review"].round == _ROUND - 1
    assert call["prior_decision"].round == _ROUND - 1

    prompt = harness.reviewer.prompts[0]
    assert "gate-003" in prompt
    assert "замечание раунда 002" in prompt
    assert "001" not in prompt, "в промпт просочился раунд N−2"


def test_review_json_is_written_by_alias_and_reads_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`review.json` лёг по пути раскладки и читается обратно моделью."""
    payload = _review_payload()
    harness = _make_harness(tmp_path, monkeypatch, reply=_reply(payload, fenced=True))

    _review(harness)

    assert [write.name for write in harness.writes] == ["review.json"]
    written = harness.writes[0]
    assert written.round_no == _ROUND
    assert written.path == _review_path(tmp_path)

    raw = written.path.read_text(encoding="utf-8")
    on_disk = json.loads(raw)
    assert on_disk["schema"] == "disputatio/v1"
    assert "schema_" not in on_disk
    assert Review.model_validate_json(raw) == Review.model_validate(payload)


def test_blocker_without_evidence_is_written_as_minor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-009: голословный блокер деградирует, а не отвергает ревью.

    На диск обязана лечь `acceptance.review` — деградированная копия;
    запись исходной модели оставила бы в истории `blocker`, которого
    §4.4 уже не признал, и следующий раунд считал бы его настоящим.
    """
    payload = _review_payload(
        issues=[
            {
                "id": "I-BARE",
                "severity": "blocker",
                "file": "feature.py",
                "claim": "тут всё сломано",
                "evidence": "",
            },
            {
                "id": "I-REAL",
                "severity": "major",
                "file": "feature.py",
                "claim": "экспорт теряет заголовок",
                "evidence": "feature.py:12 — writer.writerow пропущен",
            },
        ]
    )
    harness = _make_harness(tmp_path, monkeypatch, reply=_reply(payload))

    _review(harness)

    raw = _review_path(tmp_path).read_text(encoding="utf-8")
    severities = {issue["id"]: issue["severity"] for issue in json.loads(raw)["issues"]}
    assert severities == {"I-BARE": "minor", "I-REAL": "major"}

    original = Review.model_validate(payload)
    report = _verification(_ROUND, overall=OverallStatus.FAIL)
    expected = validate_review(original, report)
    assert expected.accepted is True
    assert Review.model_validate_json(raw) == expected.review
    assert Review.model_validate_json(raw) != original


def test_approve_with_failed_gates_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-010: `approve` при `overall == fail` на диск не попадает.

    Отчёт раунда N−1 в фикстуре зелёный, отчёт раунда N — красный: шаг,
    взявший правило от прошлого раунда, принял бы это ревью.
    """
    payload = _review_payload(verdict="approve", issues=[])
    harness = _make_harness(
        tmp_path, monkeypatch, reply=_reply(payload), overall=OverallStatus.FAIL
    )
    not_accepted = _runtime_attr("ReviewNotAccepted")

    with pytest.raises(not_accepted) as exc_info:
        _review(harness)

    assert exc_info.value.reasons == (REASON_APPROVE_ON_FAILED_GATES,)
    assert harness.writes == []
    assert not _review_path(tmp_path).exists()


def test_empty_checked_is_not_accepted_and_reports_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-011: пустой `checked` → ревью не принято → повтор.

    Причина отдаётся machine-readable кодом: её текстом схемный retry
    ([DESIGN-006]) наполняет следующий промпт, поэтому теряться ей нельзя.
    """
    payload = _review_payload(verdict="approve", issues=[], checked=[])
    harness = _make_harness(
        tmp_path, monkeypatch, reply=_reply(payload), overall=OverallStatus.PASS
    )
    not_accepted = _runtime_attr("ReviewNotAccepted")
    base = _runtime_attr("DisputatioError")

    with pytest.raises(not_accepted) as exc_info:
        _review(harness)

    assert exc_info.value.reasons == (REASON_EMPTY_CHECKED,)
    assert issubclass(not_accepted, base)
    assert harness.writes == []
    assert not _review_path(tmp_path).exists()


def test_unparsable_reviewer_text_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Текст без JSON роняет шаг `ReviewParseError` и не пишет артефакт."""
    harness = _make_harness(tmp_path, monkeypatch, reply="Всё отлично, вопросов нет.")
    parse_error = _runtime_attr("ReviewParseError")

    with pytest.raises(parse_error):
        _review(harness)

    assert harness.writes == []
    assert not _review_path(tmp_path).exists()


def test_schema_invalid_reviewer_json_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Схемно битый JSON не доезжает до диска: `review.json` не появляется."""
    payload = _review_payload()
    del payload["checked"]
    harness = _make_harness(tmp_path, monkeypatch, reply=_reply(payload))

    with pytest.raises(ValidationError):
        _review(harness)

    assert harness.writes == []
    assert not _review_path(tmp_path).exists()


def test_reviewer_output_is_data_and_is_never_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вывод ревьюера ни при каком содержимом не исполняется (NFR-003).

    Payload несёт всё, чем текст превращается в команду: подстановку
    `$(…)`, обратные кавычки, `;` и `&&`. Шаг обязан записать это дословно
    — данные остаются данными, — и не породить ни одного файла-улики.
    """
    canary = tmp_path / "canary.txt"
    payload = _review_payload(
        checked=[f"$(touch {canary}); feature.py"],
        summary=f"`touch {canary}` && rm -rf / ; echo pwned",
        issues=[
            {
                "id": "I-SH",
                "severity": "major",
                "file": f"feature.py; touch {canary}",
                "claim": "claim с ${IFS} и | pipe",
                "evidence": "feature.py:12 — evidence",
            }
        ],
    )
    harness = _make_harness(tmp_path, monkeypatch, reply=_reply(payload))

    _review(harness)

    assert not canary.exists(), "текст ревьюера исполнился как команда"
    on_disk = json.loads(_review_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["checked"] == payload["checked"]
    assert on_disk["summary"] == payload["summary"]
    assert on_disk["issues"][0]["file"] == payload["issues"][0]["file"]


def test_step_has_no_form_able_to_execute_the_agent_text() -> None:
    """Запрещена сама форма исполнения, а не перечень известных мутаций."""
    steps = _steps()
    tree = ast.parse(_source(steps))
    functions = _functions_reachable_from(tree, "review")

    called = _called_names(list(functions))
    assert not (called & _EXECUTION_CALLS), (
        f"шаг REVIEWING исполняет текст агента: {sorted(called & _EXECUTION_CALLS)}"
    )
    assert not _shell_true_calls([tree]), "shell=True в модуле шагов"
    assert not (_imported_roots(tree) & {"subprocess", "pty", "runpy"}), (
        "runtime/steps.py импортирует модуль запуска процессов: вывод "
        "ревьюера обязан не иметь пути в argv"
    )
