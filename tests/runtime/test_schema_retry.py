"""Schema-retry I4: `K+1` вызовов и переход в FAILED ([REQ-006], [DESIGN-006]).

[TASK-011]. Хелпер `runtime/retry.py::run_with_schema_retry` — единственное
место, где схемно-невалидный вывод агента превращается в повтор, а исчерпание
повторов — в `FAILED`. Считает попытки не он: инвариант «ровно `K+1` вызовов»
целиком вытекает из `SessionFsm.handle_schema_invalid`, и собственного
счётчика runtime не заводит — два счётчика разошлись бы молча.

Что здесь пинится и почему именно так:

* **обе границы `K+1`.** Равенство, а не `>=`: «не дозвал» (K) и «перезвал»
  (K+2) — разные поломки, и обе проходят мимо проверки на неравенство.
  Отдельно `K == 0`: единственная попытка без единого повтора.
* **какая именно ошибка уезжает в повтор.** Ошибки попыток различимы
  литералами, поэтому промпт, несущий ошибку ПЕРВОЙ попытки вместо
  предыдущей, виден как чужая метка, а не как отсутствие секции.
* **ошибка — данные.** Текст ошибки приходит из вывода агента: попади он в
  промпт как обычный текст, агент, вернувший инструкцию в поле `summary`,
  получил бы её обратно в привилегированной позиции. Обёртка берётся у
  `context.tags` — своей runtime не изобретает.
* **событие на каждой неудаче.** Журнал обязан показывать деградацию, а не
  только смерть, поэтому событий ровно `K+1`, и `phase` в последнем из них —
  фаза шага, а не `failed`: событие уходит ДО перехода.
* **тело шага не выполняется.** После исчерпания попыток на диске нет ни
  одного артефакта раунда, а `session.json` сохранён в `FAILED`.

`runtime.retry` до реализации не существует, поэтому доступ идёт через
`_retry_attr`: отсутствие модуля или символа переводится в `AssertionError`,
а не в ошибку коллекции — red-чекпоинт обязан быть падением assertion'ом.
"""

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest

from disputatio.context.tags import wrap_artifact_data
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
    ProposalParseError,
    Role,
    SessionPhase,
    SessionState,
    TaskSpec,
    VerificationReport,
    parse_proposal,
)
from disputatio.core import SessionFsm
from disputatio.events import write_round_artifact
from disputatio.runtime import ReviewParseError, RuntimeDeps
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    PROPOSAL_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    round_artifact,
)
from disputatio.runtime.parsing import extract_json_object

_FROZEN_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "s-retry"
_AUTHOR_REF = "ref-author"
_REVIEWER_REF = "ref-reviewer"

_BASE_PROMPT = "БАЗОВЫЙ-ПРОМПТ-ШАГА"
_PARSED = "РАЗОБРАННЫЙ-РЕЗУЛЬТАТ"

# Тексты ошибок попыток. Ни один не является подстрокой другого: иначе
# «промпт несёт ошибку предыдущей попытки» проходило бы и при первой.
_DETAIL_ONE = "ОШИБКА-ПОПЫТКИ-ОДИН"
_DETAIL_TWO = "ОШИБКА-ПОПЫТКИ-ДВА"
_DETAIL_THREE = "ОШИБКА-ПОПЫТКИ-ТРИ"

_INVALID_PROPOSAL = "просто текст без фронтматтера\n"
_UNPARSABLE_REVIEW = "Всё хорошо, замечаний нет."


def _retry() -> ModuleType:
    """Модуль `runtime/retry.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.retry")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/retry.py: {exc}") from exc


def _steps() -> ModuleType:
    """Модуль `runtime/steps.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.steps")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/steps.py: {exc}") from exc


def _attr(module: ModuleType, name: str) -> Any:
    """Символ модуля; отсутствие — `AssertionError`, не `AttributeError`."""
    assert hasattr(module, name), f"{module.__name__} не определяет {name!r}"
    return getattr(module, name)


def _retry_attr(name: str) -> Any:
    """Символ модуля schema-retry."""
    return _attr(_retry(), name)


def _steps_attr(name: str) -> Any:
    """Символ модуля шагов."""
    return _attr(_steps(), name)


def _source(module: ModuleType) -> str:
    """Исходник модуля с диска — для проверок «текста такой формы нет»."""
    path = module.__file__
    assert path is not None, f"у {module.__name__} нет файла на диске"
    return Path(path).read_text(encoding="utf-8")


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
class ScriptedAdapter:
    """`AgentAdapter`-фейк: отдаёт ответы по сценарию, считая вызовы.

    Сценарий короче числа вызовов — повторяется последний ответ: «агент,
    всегда возвращающий невалидный вывод» это один ответ, а не K+1 копий.
    Лишний вызов при этом остаётся наблюдаемым — его видно по `calls`.
    """

    replies: list[str]
    prompts: list[str] = field(default_factory=list)
    session_refs: list[str | None] = field(default_factory=list)

    @property
    def calls(self) -> int:
        """Сколько раз шаг сходил к агенту."""
        return len(self.prompts)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует промпт и отдаёт очередной ответ сценария."""
        self.prompts.append(prompt)
        self.session_refs.append(session_ref)
        reply = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
        return AgentTurn(text=reply, session_ref=session_ref)


@dataclass
class ScriptedParse:
    """`parse`-фейк: на каждый вызов свой исход по сценарию.

    Исход-исключение имитирует схемно-невалидный вывод, исход-строка —
    разобранный результат. Сценарий короче числа вызовов — повторяется
    последний исход.
    """

    outcomes: list[str | Exception]
    texts: list[str] = field(default_factory=list)

    def __call__(self, text: str) -> str:
        """Разбирает текст попытки по сценарию, запоминая вход."""
        self.texts.append(text)
        outcome = self.outcomes[min(len(self.texts) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class NoAgent:
    """`AgentAdapter`-фейк чужой роли: её адаптер шагом не зовётся."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Вызов означает, что шаг перепутал адаптеры ролей."""
        raise AssertionError("шаг позвал адаптер чужой роли")


@dataclass
class NoVerifier:
    """`Verifier`-фейк: гейты гоняет только шаг VERIFYING."""

    def verify(self, round_no: int) -> VerificationReport:
        """Вызов означает, что шаг перепрогнал гейты раунда."""
        raise AssertionError(f"шаг не вправе гонять гейты (раунд {round_no})")


@dataclass
class SpyGit:
    """`GitOps`-фейк шага PROPOSING: журналирует вызовы, git не трогает."""

    log: list[str] = field(default_factory=list)

    def diff_head(self) -> str:
        """Патч раунда — константа: содержимое дерева здесь не проверяется."""
        self.log.append("diff_head")
        return "ДИФФ-РАУНДА\n"

    def commit_round(self, round_no: int) -> None:
        """Не вызывается: коммит раунда принимает DECIDING."""
        raise AssertionError(f"шаг не вправе коммитить раунд {round_no}")

    def reset_hard(self, rev: str) -> None:
        """Журналирует сброс дерева на цель раунда."""
        self.log.append(f"reset_hard:{rev}")

    def clean(self) -> None:
        """Журналирует уборку дерева."""
        self.log.append("clean")


@dataclass
class NoGit:
    """`GitOps`-фейк шага REVIEWING: git ему не нужен вовсе."""

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


@dataclass
class Harness:
    """Собранное окружение попытки: контекст, спаи и порты."""

    root: Path
    ctx: Any
    fsm: SessionFsm
    adapter: ScriptedAdapter
    store: FakeStore
    sink: FakeSink

    @property
    def errors(self) -> list[Event]:
        """События `error` журнала — по одному на неудачную попытку."""
        return [e for e in self.sink.events if e.type is EventType.ERROR]


def _state(phase: SessionPhase, *, schema_retries: int, round_no: int) -> SessionState:
    """`SessionState` раунда `round_no` в фазе `phase` с лимитом повторов."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=phase,
        current_round=round_no,
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
            schema_retries=schema_retries,
        ),
        budget_used=BudgetUsed(),
    )


def _deps(
    root: Path,
    *,
    store: FakeStore,
    sink: FakeSink,
    author: Any,
    reviewer: Any,
    git: Any,
) -> RuntimeDeps:
    """`RuntimeDeps` с замороженными часами: журнал детерминирован."""
    return RuntimeDeps(
        workspace_root=root,
        artifact_root=root,
        store=store,
        sink=sink,
        author=author,
        reviewer=reviewer,
        verifier=NoVerifier(),
        git=git,
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )


def _harness(
    root: Path,
    *,
    schema_retries: int,
    replies: list[str],
    phase: SessionPhase = SessionPhase.REVIEWING,
    round_no: int = 1,
) -> Harness:
    """Окружение для прямого вызова хелпера: адаптер и FSM без шага."""
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(
        _state(phase, schema_retries=schema_retries, round_no=round_no),
        store=store,
        sink=sink,
        now=lambda: _FROZEN_NOW,
    )
    adapter = ScriptedAdapter(replies=replies)
    deps = _deps(
        root,
        store=store,
        sink=sink,
        author=adapter,
        reviewer=adapter,
        git=NoGit(),
    )
    ctx = _steps_attr("StepContext")(deps=deps, fsm=fsm, base_commit="0" * 40)
    return Harness(root=root, ctx=ctx, fsm=fsm, adapter=adapter, store=store, sink=sink)


def _run(
    harness: Harness,
    *,
    parse: ScriptedParse,
    source: EventSource = EventSource.REVIEWER,
    on_invalid: Any = None,
) -> Any:
    """Крутит `run_with_schema_retry` до исхода; промпт шага — константа."""
    run_with_schema_retry = _retry_attr("run_with_schema_retry")

    async def call() -> Any:
        return await run_with_schema_retry(
            harness.ctx,
            adapter=harness.adapter,
            build_prompt=lambda: _BASE_PROMPT,
            parse=parse,
            source=source,
            session_ref=_REVIEWER_REF,
            on_invalid=on_invalid,
        )

    return anyio.run(call)


def _head(root: Path) -> str:
    """SHA `HEAD` репозитория — цель сброса раунда 1 ([DESIGN-012])."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _proposal(round_no: int, *, body: str) -> str:
    """Валидный `proposal.md` раунда `round_no` с телом `body`."""
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
        f"{body}\n"
    )


def _proposal_detail(text: str) -> str:
    """Текст ошибки, которую `parse_proposal` вернёт на `text`."""
    try:
        parse_proposal(text)
    except ProposalParseError as exc:
        return str(exc)
    raise AssertionError("текст неожиданно разобрался как proposal.md")


def _review_detail(text: str) -> str:
    """Текст ошибки, которую `extract_json_object` вернёт на `text`."""
    try:
        extract_json_object(text)
    except ReviewParseError as exc:
        return str(exc)
    raise AssertionError("текст неожиданно разобрался как JSON-объект")


def _review_reply(round_no: int) -> str:
    """Ответ ревьюера, проходящий и схему, и правила §4.4."""
    return (
        '{"schema": "disputatio/v1", '
        f'"round": {round_no}, '
        '"role": "reviewer", "verdict": "request_changes", "confidence": 0.8, '
        '"issues": [{"id": "I-A", "severity": "major", "file": "feature.py", '
        '"claim": "экспорт теряет заголовок", '
        '"evidence": "feature.py:12 — writer.writerow пропущен"}], '
        '"checked": ["feature.py"], "summary": "свод ревьюера"}'
    )


def _seed_verification(root: Path, round_no: int) -> None:
    """Кладёт зелёный `verification.json` раунда `round_no` на диск."""
    report = VerificationReport(
        round=round_no,
        gates=[
            GateResult(
                name="pytest",
                cmd="uv run pytest -q",
                status=GateStatus.PASS,
                exit_code=0,
                duration_s=1.5,
                tail="всё зелено",
            )
        ],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=4, deletions=2),
    )
    write_round_artifact(
        root, round_no, VERIFICATION_NAME, report.model_dump_json(by_alias=True)
    )


def _propose_harness(root: Path, *, schema_retries: int, replies: list[str]) -> Harness:
    """Окружение шага PROPOSING раунда 1 с автором по сценарию."""
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(
        _state(SessionPhase.PROPOSING, schema_retries=schema_retries, round_no=1),
        store=store,
        sink=sink,
        now=lambda: _FROZEN_NOW,
    )
    author = ScriptedAdapter(replies=replies)
    deps = _deps(
        root,
        store=store,
        sink=sink,
        author=author,
        reviewer=NoAgent(),
        git=SpyGit(),
    )
    ctx = _steps_attr("StepContext")(deps=deps, fsm=fsm, base_commit=_head(root))
    return Harness(root=root, ctx=ctx, fsm=fsm, adapter=author, store=store, sink=sink)


def _review_harness(root: Path, *, schema_retries: int, replies: list[str]) -> Harness:
    """Окружение шага REVIEWING раунда 1 с ревьюером по сценарию."""
    _seed_verification(root, 1)
    write_round_artifact(root, 1, PROPOSAL_NAME, _proposal(1, body="тело"))
    write_round_artifact(root, 1, CHANGES_PATCH_NAME, "ДИФФ\n")
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(
        _state(SessionPhase.REVIEWING, schema_retries=schema_retries, round_no=1),
        store=store,
        sink=sink,
        now=lambda: _FROZEN_NOW,
    )
    reviewer = ScriptedAdapter(replies=replies)
    deps = _deps(
        root,
        store=store,
        sink=sink,
        author=NoAgent(),
        reviewer=reviewer,
        git=NoGit(),
    )
    ctx = _steps_attr("StepContext")(deps=deps, fsm=fsm, base_commit="0" * 40)
    return Harness(
        root=root, ctx=ctx, fsm=fsm, adapter=reviewer, store=store, sink=sink
    )


@pytest.mark.parametrize("retries", [0, 1, 2, 5])
def test_always_invalid_agent_is_called_exactly_k_plus_one_times(
    tmp_path: Path, retries: int
) -> None:
    """`schema_retries = K` → ровно `K+1` вызовов адаптера ([REQ-006]).

    Равенство пинит обе границы: недобор (K) означает, что последняя
    разрешённая попытка не сделана, перебор (K+2) — что лимит считается на
    единицу щедрее, чем сказано в `session.json`.
    """
    harness = _harness(tmp_path, schema_retries=retries, replies=["мусор"])
    parse = ScriptedParse(outcomes=[ProposalParseError(_DETAIL_ONE)])

    outcome = _run(harness, parse=parse)

    assert outcome is None
    assert harness.adapter.calls == retries + 1, (
        f"при schema_retries={retries} адаптер вызван "
        f"{harness.adapter.calls} раз вместо {retries + 1}"
    )
    assert len(parse.texts) == retries + 1


def test_zero_retries_calls_the_adapter_once(tmp_path: Path) -> None:
    """`K == 0` — одна попытка и сразу `FAILED`, без единого повтора."""
    harness = _harness(tmp_path, schema_retries=0, replies=["мусор"])
    parse = ScriptedParse(outcomes=[ProposalParseError(_DETAIL_ONE)])

    outcome = _run(harness, parse=parse)

    assert outcome is None
    assert harness.adapter.calls == 1
    assert harness.adapter.prompts == [_BASE_PROMPT], (
        "единственная попытка получила промпт с секцией повтора: "
        "ошибки предыдущей попытки ещё не существует"
    )
    assert harness.fsm.state.state is SessionPhase.FAILED


def test_retry_prompt_carries_the_error_of_the_previous_attempt(
    tmp_path: Path,
) -> None:
    """Повтор несёт ошибку ПРЕДЫДУЩЕЙ попытки, а не первой ([REQ-006]).

    Разница видна только при `K >= 2`: реализация, запомнившая первую
    ошибку и подставляющая её во все повторы, на `K == 1` неотличима от
    правильной.
    """
    harness = _harness(tmp_path, schema_retries=2, replies=["м1", "м2", "м3"])
    parse = ScriptedParse(
        outcomes=[
            ProposalParseError(_DETAIL_ONE),
            ProposalParseError(_DETAIL_TWO),
            ProposalParseError(_DETAIL_THREE),
        ]
    )

    _run(harness, parse=parse)

    prompts = harness.adapter.prompts
    assert len(prompts) == 3
    assert prompts[0] == _BASE_PROMPT, "первая попытка — промпт шага без добавок"
    for prompt in prompts[1:]:
        assert prompt.startswith(_BASE_PROMPT), (
            "повтор пересобрал промпт вместо того, чтобы дополнить базовый"
        )
    assert _DETAIL_ONE in prompts[1]
    assert _DETAIL_TWO in prompts[2]
    assert _DETAIL_ONE not in prompts[2], (
        "второй повтор несёт ошибку первой попытки: агент чинит не то, "
        "что сломал в прошлый раз"
    )
    assert _DETAIL_THREE not in "".join(prompts), (
        "ошибка последней попытки уехала в промпт, которого уже не было"
    )


def test_error_reaches_the_agent_only_as_artifact_data(tmp_path: Path) -> None:
    """Текст ошибки уходит агенту внутри меток `context.tags` ([REQ-006]).

    Ошибка собрана из вывода агента: вне меток агент, вернувший инструкцию
    в поле `summary`, получил бы её обратно в привилегированной позиции.
    Обёртка обязана прийти из `context`, а не быть переписана литералом —
    поэтому в исходнике `retry.py` текста меток нет вовсе.
    """
    harness = _harness(tmp_path, schema_retries=1, replies=["мусор"])
    parse = ScriptedParse(outcomes=[ProposalParseError(_DETAIL_ONE)])

    _run(harness, parse=parse)

    retry_prompt = harness.adapter.prompts[1]
    assert wrap_artifact_data(_DETAIL_ONE) in retry_prompt, (
        "текст ошибки попал в промпт вне обёртки «данные, не инструкции»"
    )
    assert "ARTIFACT_DATA" not in _source(_retry()), (
        "runtime/retry.py содержит текст меток — обёртка изобретена заново "
        "и разъедется с context.tags молча"
    )


def test_valid_reply_before_the_limit_stops_the_loop(tmp_path: Path) -> None:
    """Валидный ответ на попытке `i < K` завершает петлю без лишних вызовов."""
    harness = _harness(
        tmp_path, schema_retries=3, replies=["м1", "м2", "ХОРОШИЙ-ОТВЕТ", "лишний"]
    )
    parse = ScriptedParse(
        outcomes=[
            ProposalParseError(_DETAIL_ONE),
            ProposalParseError(_DETAIL_TWO),
            _PARSED,
        ]
    )

    outcome = _run(harness, parse=parse)

    assert outcome is not None
    parsed, turn = outcome
    assert parsed == _PARSED
    assert isinstance(turn, AgentTurn)
    assert turn.text == "ХОРОШИЙ-ОТВЕТ", "вернулась не та попытка, которая прошла"
    assert harness.adapter.calls == 3, "петля не остановилась на валидном ответе"
    assert harness.fsm.state.state is SessionPhase.REVIEWING
    assert harness.store.saved == [], "успешная попытка тронула session.json"
    assert len(harness.errors) == 2


def test_exhausted_retries_save_failed_state_and_return_none(
    tmp_path: Path,
) -> None:
    """Исчерпание попыток: `FAILED` через `StateStore.save`, хелпер → `None`."""
    harness = _harness(tmp_path, schema_retries=1, replies=["мусор"])
    parse = ScriptedParse(outcomes=[ProposalParseError(_DETAIL_ONE)])

    outcome = _run(harness, parse=parse)

    assert outcome is None
    assert [state.state for state in harness.store.saved] == [SessionPhase.FAILED], (
        "переход в FAILED не записан write-ahead через StateStore.save"
    )
    assert harness.fsm.state.state is SessionPhase.FAILED
    changes = [e for e in harness.sink.events if e.type is EventType.STATE_CHANGE]
    assert [e.payload["to"] for e in changes] == [SessionPhase.FAILED.value]


def test_error_event_is_emitted_on_every_failed_attempt(tmp_path: Path) -> None:
    """Событие `error` — на каждой неудаче, с `{attempt, detail, phase}`.

    `phase` последнего события — фаза шага, а не `failed`: событие уходит
    ДО перехода, иначе журнал показывает смерть вместо деградации.
    """
    harness = _harness(tmp_path, schema_retries=2, replies=["м1", "м2", "м3"])
    parse = ScriptedParse(
        outcomes=[
            ProposalParseError(_DETAIL_ONE),
            ProposalParseError(_DETAIL_TWO),
            ProposalParseError(_DETAIL_THREE),
        ]
    )

    _run(harness, parse=parse, source=EventSource.REVIEWER)

    assert [e.payload for e in harness.errors] == [
        {"attempt": 1, "detail": _DETAIL_ONE, "phase": SessionPhase.REVIEWING.value},
        {"attempt": 2, "detail": _DETAIL_TWO, "phase": SessionPhase.REVIEWING.value},
        {"attempt": 3, "detail": _DETAIL_THREE, "phase": SessionPhase.REVIEWING.value},
    ]
    assert {e.source for e in harness.errors} == {EventSource.REVIEWER}
    assert {e.round for e in harness.errors} == {1}
    assert {e.ts for e in harness.errors} == {_FROZEN_NOW}


def test_on_invalid_receives_every_failure_in_order(tmp_path: Path) -> None:
    """Шаг получает сами ошибки попыток, а не только их текст.

    Из последней собирается исключение шага: после `FAILED` пользователь
    обязан услышать причину, а разбирать обратно собственный журнал
    оркестратор не должен.
    """
    failures: list[Exception] = []
    first = ProposalParseError(_DETAIL_ONE)
    second = ProposalParseError(_DETAIL_TWO)
    harness = _harness(tmp_path, schema_retries=1, replies=["м1", "м2"])
    parse = ScriptedParse(outcomes=[first, second])

    _run(harness, parse=parse, on_invalid=failures.append)

    assert failures == [first, second]


def test_error_outside_the_schema_contract_is_not_retried(tmp_path: Path) -> None:
    """Не-схемная ошибка не тратит лимит и не становится `FAILED`.

    Повтор лечит вывод агента; упавший порт повтором не лечится, и молча
    съеденный `RuntimeError` увёл бы сессию в `FAILED` с причиной «агент
    вернул невалидный вывод», которой не было.
    """
    harness = _harness(tmp_path, schema_retries=2, replies=["мусор"])
    parse = ScriptedParse(outcomes=[RuntimeError("порт лёг")])

    with pytest.raises(RuntimeError):
        _run(harness, parse=parse)

    assert harness.adapter.calls == 1
    assert harness.store.saved == []
    assert harness.errors == []
    assert harness.fsm.state.state is SessionPhase.REVIEWING


def test_propose_retries_the_author_and_fails_the_session(git_repo: Path) -> None:
    """PROPOSING переведён на хелпер: `K+1` вызовов, `FAILED`, пустой раунд."""
    harness = _propose_harness(git_repo, schema_retries=2, replies=[_INVALID_PROPOSAL])
    detail = _proposal_detail(_INVALID_PROPOSAL)

    with pytest.raises(ProposalParseError):
        anyio.run(_steps_attr("propose"), harness.ctx)

    assert harness.adapter.calls == 3
    assert wrap_artifact_data(detail) not in harness.adapter.prompts[0]
    for prompt in harness.adapter.prompts[1:]:
        assert wrap_artifact_data(detail) in prompt, (
            "повтор автора не несёт ошибку разбора предыдущей попытки"
        )
    assert harness.fsm.state.state is SessionPhase.FAILED
    assert [state.state for state in harness.store.saved] == [SessionPhase.FAILED]
    assert len(harness.errors) == 3
    assert not round_artifact(git_repo, 1, PROPOSAL_NAME).exists(), (
        "тело шага выполнилось после исчерпания попыток"
    )
    assert not round_artifact(git_repo, 1, CHANGES_PATCH_NAME).exists()


def test_propose_writes_the_attempt_that_finally_parsed(git_repo: Path) -> None:
    """Артефакты раунда собираются из УДАЧНОЙ попытки, а не из первой."""
    good = _proposal(1, body="исправился")
    harness = _propose_harness(
        git_repo, schema_retries=2, replies=[_INVALID_PROPOSAL, good]
    )

    anyio.run(_steps_attr("propose"), harness.ctx)

    assert harness.adapter.calls == 2
    written = round_artifact(git_repo, 1, PROPOSAL_NAME).read_text(encoding="utf-8")
    assert written == good, "на диск лёг ответ не той попытки"
    assert harness.fsm.state.state is SessionPhase.PROPOSING
    assert harness.store.saved == []
    assert len(harness.errors) == 1


def test_review_retries_the_reviewer_and_fails_the_session(tmp_path: Path) -> None:
    """REVIEWING переведён на хелпер: `K+1` вызовов, `FAILED`, нет review.json."""
    harness = _review_harness(tmp_path, schema_retries=1, replies=[_UNPARSABLE_REVIEW])
    detail = _review_detail(_UNPARSABLE_REVIEW)

    with pytest.raises(ReviewParseError):
        anyio.run(_steps_attr("review"), harness.ctx)

    assert harness.adapter.calls == 2
    assert wrap_artifact_data(detail) in harness.adapter.prompts[1]
    assert harness.fsm.state.state is SessionPhase.FAILED
    assert [state.state for state in harness.store.saved] == [SessionPhase.FAILED]
    assert len(harness.errors) == 2
    assert not round_artifact(tmp_path, 1, REVIEW_NAME).exists(), (
        "непринятое ревью легло на диск после исчерпания попыток"
    )


def test_review_writes_the_attempt_that_finally_validated(tmp_path: Path) -> None:
    """Удачный повтор ревьюера пишет `review.json` и не трогает `FAILED`."""
    harness = _review_harness(
        tmp_path, schema_retries=2, replies=[_UNPARSABLE_REVIEW, _review_reply(1)]
    )

    anyio.run(_steps_attr("review"), harness.ctx)

    assert harness.adapter.calls == 2
    assert round_artifact(tmp_path, 1, REVIEW_NAME).is_file()
    assert harness.fsm.state.state is SessionPhase.REVIEWING
    assert harness.store.saved == []
    assert len(harness.errors) == 1
