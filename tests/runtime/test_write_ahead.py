"""Write-ahead-диспетчер `drive` ([REQ-008], [REQ-014], [DESIGN-008]).

[TASK-013]. Восстановимость сессии держится не на проверке, а на форме
цикла: тело шага не выполняется раньше перехода, поэтому `session.json`
всегда называет шаг, который НАЧИНАЛСЯ, а не тот, что завершился. Обрыв
процесса в любой точке оставляет сессию в фазе, которую resume обязан
переиграть целиком (ADR-001) — и переиграть безопасно, потому что уже
записанного результата у этой фазы нет.

Пинится это единственным способом, который не обманешь: адаптер (или
верификатор) падает при первом обращении, а `StateStore.save` с фазой
начинающегося шага обязан быть УЖЕ вызван. Порядок читается по ОДНОМУ
общему spy-логу — набор «вызвано/не вызвано» пропустил бы ровно ту ошибку,
которая здесь единственно возможна: перестановку двух строк.

Три вещи проверяются отдельно, потому что ломаются независимо:

* **порядок в памяти** — `save` раньше первого обращения к порту агента, и
  `state_change` строго после `save` (журнал не вправе обгонять состояние:
  подписчик увидел бы фазу, которой на диске нет).
* **байты на диске** — `session.json` читается обратно с диска, а не через
  spy: спай доказывает лишь то, что метод звали, а REQ-008 требует, чтобы
  после обрыва в файле лежала фаза начатого шага.
* **таблицы диспетчера** — `NEXT_PHASE` не содержит ключей `DECIDING` и
  `EXPORTING`. Отсутствие ключа здесь — часть контракта, а не пропуск:
  следующую фазу после `DECIDING` выбирает `apply_decision` по §5, и запись
  в таблице runtime означала бы второй источник правды о стоп-условиях.

`runtime/loop.py` до реализации не существует, поэтому доступ к модулю идёт
через `_loop_attr`: отсутствие символа переводится в `AssertionError`, а не
в ошибку коллекции — red-чекпоинт обязан быть падением assertion'ом.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
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
from disputatio.events import FileStateStore, bootstrap_session
from disputatio.runtime import RuntimeDeps
from disputatio.runtime.layout import session_dir
from disputatio.runtime.steps import StepContext

_FROZEN_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "s-write-ahead"
_BASE_COMMIT = "0" * 40

# Ревизия, которую отдаёт подменённый `base_rev`: настоящая история git
# здесь не при чём — диспетчер проверяется на порядке шагов, а не на
# разрешении коммитов ([DESIGN-012] пинится своей задачей).
_BASE_REV = "resolved-base-rev"

# Метки портов в общем spy-логе. Строки, а не enum: лог читается глазами в
# отчёте pytest, и `assert log == [...]` обязан показывать порядок целиком.
_AUTHOR_CALL = "author.run"
_REVIEWER_CALL = "reviewer.run"
_VERIFIER_CALL = "verifier.verify"

_AGENT_CALLS = frozenset({_AUTHOR_CALL, _REVIEWER_CALL, _VERIFIER_CALL})


class StepBoom(Exception):
    """Обрыв внутри тела шага — «процесс умер, не дойдя до результата».

    Не из `SCHEMA_INVALID_ERRORS`: schema-retry лечит вывод агента не той
    формы, а здесь моделируется отказ самого порта. Проглоти его повтор — и
    тест перестал бы наблюдать обрыв там, где он объявлен.
    """


def _loop() -> ModuleType:
    """Модуль `runtime/loop.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.loop")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/loop.py: {exc}") from exc


def _loop_attr(name: str) -> Any:
    """Символ модуля цикла; отсутствие — `AssertionError`, не `AttributeError`."""
    module = _loop()
    assert hasattr(module, name), f"runtime/loop.py не определяет {name!r}"
    return getattr(module, name)


@dataclass
class SpyStore:
    """`StateStore` поверх настоящего `FileStateStore` + общий лог порядка.

    Настоящий, а не фейковый: REQ-008 говорит про содержимое `session.json`
    на диске, и спай, не доводящий запись до файла, доказывал бы только
    факт вызова. Метка кладётся ПОСЛЕ записи — «состояние уже переживёт
    обрыв», иначе порядок в логе был бы слабее, чем на диске.
    """

    inner: FileStateStore
    log: list[str]
    saved: list[SessionState] = field(default_factory=list)

    def load(self, session_id: str) -> SessionState:
        """Читает состояние настоящим хранилищем."""
        return self.inner.load(session_id)

    def save(self, state: SessionState) -> None:
        """Пишет `session.json` и отмечает в логе фазу сохранённого шага."""
        self.inner.save(state)
        self.saved.append(state)
        self.log.append(f"save:{state.state.value}")


@dataclass
class SpySink:
    """`EventSink`-фейк: события в список и метка в общий лог."""

    log: list[str]
    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        """Запоминает событие вместо дописывания в `events.jsonl`."""
        self.events.append(event)
        self.log.append(_event_mark(event))


def _event_mark(event: Event) -> str:
    """Метка события для общего лога; переход несёт обе свои фазы."""
    if event.type is EventType.STATE_CHANGE:
        return f"emit:state_change:{event.payload['from']}->{event.payload['to']}"
    return f"emit:{event.type.value}"


@dataclass
class SpyAdapter:
    """`AgentAdapter`-фейк: журналирует вызов и падает на `fail_at`-м.

    Метка кладётся ДО падения: тест обязан видеть, что обращение к агенту
    состоялось, — иначе «save раньше адаптера» выполнялось бы вакуумно на
    цикле, который агента вообще не позвал.
    """

    name: str
    log: list[str]
    replies: list[str] = field(default_factory=list)
    fail_at: int | None = None
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Отмечает вызов, затем падает либо отдаёт следующий ответ очереди."""
        self.prompts.append(prompt)
        self.log.append(f"{self.name}.run")
        if self.fail_at is not None and len(self.prompts) >= self.fail_at:
            raise StepBoom(f"{self.name}: обрыв на вызове {len(self.prompts)}")
        assert self.replies, (
            f"SpyAdapter {self.name}: очередь ответов исчерпана, лишний вызов"
        )
        return AgentTurn(text=self.replies.pop(0), session_ref=session_ref)


@dataclass
class SpyVerifier:
    """`Verifier`-фейк: журналирует прогон и падает, если `fails`."""

    log: list[str]
    fails: bool = False
    rounds: list[int] = field(default_factory=list)

    def verify(self, round_no: int) -> VerificationReport:
        """Отмечает прогон гейтов раунда и отдаёт зелёный отчёт."""
        self.log.append(_VERIFIER_CALL)
        self.rounds.append(round_no)
        if self.fails:
            raise StepBoom(f"верификатор: обрыв на раунде {round_no}")
        return _verification(round_no)


@dataclass
class SpyGit:
    """`GitOps`-фейк: журналирует операции, рабочего дерева не трогает."""

    log: list[str]
    commits: list[int] = field(default_factory=list)
    resets: list[str] = field(default_factory=list)

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
        self.resets.append(rev)

    def clean(self) -> None:
        """Журналирует уборку untracked-файлов прерванной попытки."""
        self.log.append("git.clean")


@dataclass
class Harness:
    """Собранное окружение цикла: контекст, спаи и общий лог порядка."""

    root: Path
    ctx: StepContext
    fsm: SessionFsm
    store: SpyStore
    sink: SpySink
    author: SpyAdapter
    reviewer: SpyAdapter
    verifier: SpyVerifier
    git: SpyGit
    log: list[str]


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


def _review_reply(round_no: int) -> str:
    """Ответ ревьюера: `request_changes` с major-замечанием и evidence.

    Именно такой вердикт, а не `approve`: раунд 1 с `approve` уводит §5 в
    анти-сикофантию, и цикл всё равно продолжился бы — но уже по другой
    ветке ядра, и тест диспетчера читался бы как тест §5.
    """
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


def _state(phase: SessionPhase, round_no: int, *, max_rounds: int = 5) -> SessionState:
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
            max_rounds=max_rounds,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        budget_used=BudgetUsed(),
    )


def _fixed_base_rev(root: Path, round_no: int, *, base_commit: str) -> str:
    """Цель сброса без обращения к истории git — одна и та же для всех раундов."""
    return _BASE_REV


def _spy_base_rev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет `base_rev` в модуле шагов фиксированной ревизией.

    Цель сброса вычисляется из настоящей истории git ([DESIGN-012]) и имеет
    собственные тесты; здесь она была бы единственной причиной заводить
    репозиторий и коммитить принятые раунды по-настоящему.
    """
    steps = import_module("disputatio.runtime.steps")
    assert hasattr(steps, "base_rev"), (
        "runtime/steps.py обязан брать цель сброса у git.base_rev"
    )
    monkeypatch.setattr(steps, "base_rev", _fixed_base_rev)


def _make_harness(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    phase: SessionPhase = SessionPhase.IDLE,
    round_no: int = 0,
    max_rounds: int = 5,
    author_replies: tuple[str, ...] = (_proposal(1),),
    author_fail_at: int | None = None,
    verifier_fails: bool = False,
    reviewer_replies: tuple[str, ...] = (_review_reply(1),),
    reviewer_fail_at: int | None = None,
) -> Harness:
    """Собирает `StepContext` со спаями на всех наблюдаемых границах цикла."""
    bootstrap_session(root)
    _spy_base_rev(monkeypatch)
    log: list[str] = []
    store = SpyStore(inner=FileStateStore(root), log=log)
    sink = SpySink(log=log)
    author = SpyAdapter(
        name="author", log=log, replies=list(author_replies), fail_at=author_fail_at
    )
    reviewer = SpyAdapter(
        name="reviewer",
        log=log,
        replies=list(reviewer_replies),
        fail_at=reviewer_fail_at,
    )
    verifier = SpyVerifier(log=log, fails=verifier_fails)
    git = SpyGit(log=log)
    fsm = SessionFsm(
        _state(phase, round_no, max_rounds=max_rounds),
        store=store,
        sink=sink,
        now=lambda: _FROZEN_NOW,
    )
    deps = RuntimeDeps(
        root=root,
        store=store,
        sink=sink,
        author=author,
        reviewer=reviewer,
        verifier=verifier,
        git=git,
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    ctx = StepContext(deps=deps, fsm=fsm, base_commit=_BASE_COMMIT)
    return Harness(
        root=root,
        ctx=ctx,
        fsm=fsm,
        store=store,
        sink=sink,
        author=author,
        reviewer=reviewer,
        verifier=verifier,
        git=git,
        log=log,
    )


def _drive(harness: Harness) -> SessionState:
    """Крутит цикл до остановки — `drive` асинхронна, как и шаги в ней."""
    drive: Callable[[StepContext], Awaitable[SessionState]] = _loop_attr("drive")
    final: SessionState = anyio.run(drive, harness.ctx)
    return final


def _drive_until_boom(harness: Harness) -> StepBoom:
    """Крутит цикл до смоделированного обрыва и возвращает его исключение."""
    with pytest.raises(StepBoom) as caught:
        _drive(harness)
    return caught.value


def _on_disk(root: Path) -> dict[str, Any]:
    """`session.json` как он лежит на диске — байты, а не спай."""
    payload = (session_dir(root) / "session.json").read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(payload)
    return parsed


def _port_calls(harness: Harness) -> list[str]:
    """Только обращения к агентам и верификатору, в порядке лога."""
    return [mark for mark in harness.log if mark in _AGENT_CALLS]


def _saved_phases(harness: Harness) -> list[SessionPhase]:
    """Фазы всех сохранений `session.json`, в порядке вызовов."""
    return [state.state for state in harness.store.saved]


def _state_changes(harness: Harness) -> list[Event]:
    """События перехода, попавшие в журнал."""
    return [
        event for event in harness.sink.events if event.type is EventType.STATE_CHANGE
    ]


def test_idle_has_no_step_and_next_phase_moves_it_to_proposing() -> None:
    """`IDLE` — единственная фаза без шага: её работа есть переход.

    Шаг у `IDLE` означал бы работу до того, как `session.json` назвал фазу
    `PROPOSING`, то есть ровно то, что запрещает [REQ-008].
    """
    step_by_phase = _loop_attr("STEP_BY_PHASE")
    next_phase = _loop_attr("NEXT_PHASE")

    assert SessionPhase.IDLE not in step_by_phase
    assert next_phase[SessionPhase.IDLE] is SessionPhase.PROPOSING


def test_next_phase_has_no_deciding_and_no_exporting_keys() -> None:
    """`DECIDING`/`EXPORTING` в `NEXT_PHASE` нет — это часть контракта.

    Их следующую фазу выбирают `apply_decision` (§5) и `exporting.export`.
    Запись в таблице runtime продублировала бы порядок стоп-условий, и две
    копии разошлись бы ровно тогда, когда §5 поправят в одной из них.
    """
    next_phase = _loop_attr("NEXT_PHASE")

    assert SessionPhase.DECIDING not in next_phase
    assert SessionPhase.EXPORTING not in next_phase
    assert set(next_phase) == {
        SessionPhase.IDLE,
        SessionPhase.PROPOSING,
        SessionPhase.VERIFYING,
        SessionPhase.REVIEWING,
    }


def test_proposing_is_saved_before_the_author_is_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`save(PROPOSING)` предшествует первому обращению к автору ([REQ-008]).

    Первая метка лога — тоже `save`: до перехода цикл не вправе тронуть ни
    один порт, иначе обрыв оставил бы работу, о которой `session.json`
    ничего не знает.
    """
    harness = _make_harness(tmp_path, monkeypatch, author_fail_at=1)

    _drive_until_boom(harness)

    assert _AUTHOR_CALL in harness.log
    assert harness.log[0] == "save:PROPOSING"
    assert harness.log.index("save:PROPOSING") < harness.log.index(_AUTHOR_CALL)


def test_verifying_is_saved_before_the_verifier_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`save(VERIFYING)` предшествует прогону гейтов ([REQ-008])."""
    harness = _make_harness(tmp_path, monkeypatch, verifier_fails=True)

    _drive_until_boom(harness)

    assert _VERIFIER_CALL in harness.log
    assert harness.log.index("save:VERIFYING") < harness.log.index(_VERIFIER_CALL)
    assert harness.log.index(_AUTHOR_CALL) < harness.log.index("save:VERIFYING")


def test_reviewing_is_saved_before_the_reviewer_is_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`save(REVIEWING)` предшествует первому обращению к ревьюеру ([REQ-008])."""
    harness = _make_harness(tmp_path, monkeypatch, reviewer_fail_at=1)

    _drive_until_boom(harness)

    assert _REVIEWER_CALL in harness.log
    assert harness.log.index("save:REVIEWING") < harness.log.index(_REVIEWER_CALL)
    assert harness.log.index(_VERIFIER_CALL) < harness.log.index("save:REVIEWING")


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"author_fail_at": 1}, SessionPhase.PROPOSING),
        ({"verifier_fails": True}, SessionPhase.VERIFYING),
        ({"reviewer_fail_at": 1}, SessionPhase.REVIEWING),
    ],
    ids=["proposing", "verifying", "reviewing"],
)
def test_session_json_names_the_step_that_started_not_the_finished_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    expected: SessionPhase,
) -> None:
    """После обрыва на диске лежит фаза начатого шага ([REQ-008]).

    Проверяются байты `session.json`, а не спай: спай доказывает лишь то,
    что метод звали, а resume читает файл. Для `VERIFYING`/`REVIEWING` в
    файле обязана быть начатая фаза, а не предыдущая — завершённая.
    """
    harness = _make_harness(tmp_path, monkeypatch, **kwargs)

    _drive_until_boom(harness)

    payload = _on_disk(tmp_path)
    assert payload["state"] == expected.value
    assert payload["current_round"] == 1


def test_state_change_follows_save_and_agrees_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каждый `state_change` идёт сразу после своего `save` ([REQ-008]).

    «Согласовано» — это и порядок, и содержимое: фаза `to` события равна
    фазе сохранённого состояния, а `round` события — сохранённому раунду.
    Обгони журнал состояние, подписчик увидел бы фазу, которой на диске
    ещё нет.
    """
    harness = _make_harness(tmp_path, monkeypatch, reviewer_fail_at=1)

    _drive_until_boom(harness)

    changes = _state_changes(harness)
    assert len(changes) == len(harness.store.saved) == 3
    for index, mark in enumerate(harness.log):
        if not mark.startswith("emit:state_change:"):
            continue
        assert index > 0, "state_change оказался первой записью лога"
        assert harness.log[index - 1].startswith("save:"), (
            f"перед {mark} нет сохранения состояния: {harness.log}"
        )
        assert harness.log[index - 1] == f"save:{mark.rsplit('->', 1)[1]}"
    for event, saved in zip(changes, harness.store.saved, strict=True):
        assert event.payload["to"] == saved.state.value
        assert event.round == saved.current_round


def test_resume_runs_the_step_of_the_saved_phase_without_a_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Фаза из `session.json` — начало работы, а не место для перехода.

    [REQ-014]: сессия, поднятая в `VERIFYING` раунда 2, продолжает с прогона
    гейтов ЭТОГО раунда. Отдельной «логики восстановления» нет — есть цикл,
    который начинает с фазы сохранённого состояния, поэтому предшествующий
    шаг заново не выполняется и лишнего перехода перед ним не случается.
    """
    harness = _make_harness(
        tmp_path,
        monkeypatch,
        phase=SessionPhase.VERIFYING,
        round_no=2,
        reviewer_fail_at=1,
    )

    _drive_until_boom(harness)

    assert harness.log[0] == _VERIFIER_CALL
    assert harness.verifier.rounds == [2]
    assert harness.author.prompts == []
    assert _saved_phases(harness) == [SessionPhase.REVIEWING]


def test_revise_loop_dispatches_proposing_again_not_verifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После revise-петли цикл выполняет шаг новой фазы, а не следующей.

    `apply_decision` сам переводит `DECIDING → PROPOSING`, и раунд 2
    обязан начаться с предложения автора. Диспетчер, устроенный как
    «перейти, затем выполнить», проскочил бы `PROPOSING` раунда 2 и позвал
    верификатор по патчу раунда 1 — молча и с виду успешно.
    """
    harness = _make_harness(
        tmp_path, monkeypatch, author_replies=(_proposal(1),), author_fail_at=2
    )

    _drive_until_boom(harness)

    assert _port_calls(harness) == [
        _AUTHOR_CALL,
        _VERIFIER_CALL,
        _REVIEWER_CALL,
        _AUTHOR_CALL,
    ]
    assert _saved_phases(harness) == [
        SessionPhase.PROPOSING,
        SessionPhase.VERIFYING,
        SessionPhase.REVIEWING,
        SessionPhase.DECIDING,
        SessionPhase.PROPOSING,
    ]
    assert harness.git.commits == [1]
    assert _on_disk(tmp_path)["current_round"] == 2


@pytest.mark.parametrize(
    "phase", [SessionPhase.DONE, SessionPhase.FAILED], ids=["done", "failed"]
)
def test_drive_stops_on_terminal_phase_and_returns_final_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: SessionPhase
) -> None:
    """Терминальная фаза останавливает цикл и возвращает своё состояние.

    Обе терминальные фазы, а не одна: `DONE` — штатный конец, `FAILED` —
    исчерпание повторов, и остановка обязана быть общей. Ни одного порта
    цикл при этом не трогает: работать в терминальной фазе не над чем.
    """
    harness = _make_harness(tmp_path, monkeypatch, phase=phase, round_no=3)

    result = _drive(harness)

    assert result is harness.fsm.state
    assert result.state is phase
    assert result.current_round == 3
    assert harness.log == []


def test_phase_without_step_and_without_next_phase_does_not_spin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Фаза без шага и без перехода — дыра диспетчера, а не тихий простой.

    `EXPORTING` получит свой шаг вместе с `exporting.export` ([DESIGN-017]).
    Пока его нет, цикл обязан упасть с внятным текстом: молчаливый возврат
    выдал бы за успех сессию, которая не экспортировала результат, а
    повторная итерация крутилась бы вечно.
    """
    harness = _make_harness(
        tmp_path, monkeypatch, phase=SessionPhase.EXPORTING, round_no=2
    )

    with pytest.raises(AssertionError, match="EXPORTING"):
        _drive(harness)

    assert harness.log == []
