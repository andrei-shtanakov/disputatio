"""Сцепка шагов с schema-retry: чей это поток и чья ошибка ([REQ-006]).

Ревью [TASK-011]. `test_schema_retry.py` пинит сам хелпер и считает события
`error` числом (`len(harness.errors) == 3`), а `source` события проверяет
только там, где шаг не участвует — в прямом вызове хелпера. Из-за этого две
подмены переживали весь suite:

* `steps.propose` объявляет источником `EventSource.REVIEWER` — неудачные
  попытки АВТОРА уезжают в журнал как поток ревьюера. UI §8 подписан на
  `events.jsonl` и других сведений о том, чей вызов сломался, не имеет:
  пользователь видит деградацию не того агента.
* `steps.review` объявляет источником `ORCHESTRATOR` — то же самое с другой
  стороны, плюс подмена смысла: «сломался оркестратор» вместо «ревьюер
  третий раз подряд отвечает прозой».

Третья подмена — `_exhausted` отдаёт `failures[0]` вместо `failures[-1]`.
Шаговые тесты локнутого файла кормят адаптеру ОДИН ответ на все попытки,
поэтому все ошибки там одинаковы, и первая от последней неотличима. Здесь
попытки ломаются по-разному: сначала проза (нет JSON), потом схемно валидный
объект, отвергнутый §4.4. Разные классы ошибок делают подмену видимой —
пользователь обязан услышать причину ПОСЛЕДНЕЙ попытки, а не первой.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
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
)
from disputatio.core import SessionFsm
from disputatio.events import write_round_artifact
from disputatio.runtime import RuntimeDeps
from disputatio.runtime.errors import ReviewNotAccepted, ReviewParseError
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    PROPOSAL_NAME,
    VERIFICATION_NAME,
)
from disputatio.runtime.steps import StepContext, propose, review

_FROZEN_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "s-retry-wiring"
_AUTHOR_REF = "ref-author"
_REVIEWER_REF = "ref-reviewer"

_INVALID_PROPOSAL = "просто текст без фронтматтера\n"
_PROSE_REVIEW = "Всё хорошо, замечаний нет."


def _review_with_empty_checked(round_no: int) -> str:
    """Схемно валидный `review.json`, который §4.4 не принимает.

    Пустой `checked` схемой разрешён (REQ-011 живёт в `validation.py`),
    поэтому попытка ломается НЕ там же, где проза: `ReviewNotAccepted`
    против `ReviewParseError`.
    """
    return (
        '{"schema": "disputatio/v1", '
        f'"round": {round_no}, '
        '"role": "reviewer", "verdict": "request_changes", "confidence": 0.8, '
        '"issues": [{"id": "I-A", "severity": "major", "file": "feature.py", '
        '"claim": "экспорт теряет заголовок", '
        '"evidence": "feature.py:12 — writer.writerow пропущен"}], '
        '"checked": [], "summary": "свод ревьюера"}'
    )


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

    Сценарий короче числа вызовов — повторяется последний ответ.
    """

    replies: list[str]
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует промпт и отдаёт очередной ответ сценария."""
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self.replies) - 1)
        return AgentTurn(text=self.replies[index], session_ref=session_ref)


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
    """`GitOps`-фейк: сброс и уборку журналирует, коммит запрещает."""

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
class Harness:
    """Собранное окружение шага: контекст, спаи и адаптер по сценарию."""

    ctx: StepContext
    fsm: SessionFsm
    adapter: ScriptedAdapter
    store: FakeStore
    sink: FakeSink

    @property
    def errors(self) -> list[Event]:
        """События `error` журнала — по одному на неудачную попытку."""
        return [e for e in self.sink.events if e.type is EventType.ERROR]


def _state(phase: SessionPhase, *, schema_retries: int) -> SessionState:
    """`SessionState` раунда 1 в фазе `phase` с лимитом повторов."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=phase,
        current_round=1,
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


def _harness(
    root: Path,
    *,
    phase: SessionPhase,
    schema_retries: int,
    replies: list[str],
    base_commit: str,
    git: Any,
) -> Harness:
    """Окружение шага фазы `phase` с агентом своей роли по сценарию."""
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(
        _state(phase, schema_retries=schema_retries),
        store=store,
        sink=sink,
        now=lambda: _FROZEN_NOW,
    )
    adapter = ScriptedAdapter(replies=replies)
    other: Any = NoAgent()
    deps = RuntimeDeps(
        root=root,
        store=store,
        sink=sink,
        author=adapter if phase is SessionPhase.PROPOSING else other,
        reviewer=other if phase is SessionPhase.PROPOSING else adapter,
        verifier=NoVerifier(),
        git=git,
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    ctx = StepContext(deps=deps, fsm=fsm, base_commit=base_commit)
    return Harness(ctx=ctx, fsm=fsm, adapter=adapter, store=store, sink=sink)


def _seed_round_one(root: Path) -> None:
    """Кладёт на диск артефакты, которые REVIEWING читает до вызова агента."""
    report = VerificationReport(
        round=1,
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
        root, 1, VERIFICATION_NAME, report.model_dump_json(by_alias=True)
    )
    write_round_artifact(
        root,
        1,
        PROPOSAL_NAME,
        (
            "---\nschema: disputatio/v1\nround: 1\nrole: author\n"
            "responds_to: null\nfiles_touched:\n  - feature.py\n"
            "self_declared_status: complete\n---\nтело\n"
        ),
    )
    write_round_artifact(root, 1, CHANGES_PATCH_NAME, "ДИФФ\n")


def _review_harness(root: Path, *, schema_retries: int, replies: list[str]) -> Harness:
    """Окружение шага REVIEWING раунда 1 с ревьюером по сценарию."""
    _seed_round_one(root)
    return _harness(
        root,
        phase=SessionPhase.REVIEWING,
        schema_retries=schema_retries,
        replies=replies,
        base_commit="0" * 40,
        git=SpyGit(),
    )


def test_propose_error_events_name_the_author_as_source(git_repo: Path) -> None:
    """Неудачные попытки АВТОРА уходят в журнал потоком автора ([REQ-006]).

    Число событий локнутый файл уже пинит; здесь пинится их авторство.
    `source` — единственное, из чего подписчик §8 узнаёт, чей вызов
    сломался: назови шаг ревьюера — и UI покажет деградацию не того агента,
    причём ровно в фазе PROPOSING, где ревьюера не звали вовсе.
    """
    harness = _harness(
        git_repo,
        phase=SessionPhase.PROPOSING,
        schema_retries=1,
        replies=[_INVALID_PROPOSAL],
        base_commit="HEAD",
        git=SpyGit(),
    )

    with pytest.raises(ProposalParseError):
        anyio.run(propose, harness.ctx)

    assert len(harness.errors) == 2
    assert [e.source for e in harness.errors] == [
        EventSource.AUTHOR,
        EventSource.AUTHOR,
    ], "неудачная попытка автора записана в журнал как чужой поток"


def test_review_error_events_name_the_reviewer_as_source(tmp_path: Path) -> None:
    """Неудачные попытки РЕВЬЮЕРА уходят в журнал потоком ревьюера.

    `ORCHESTRATOR` здесь особенно ядовит: он читается как «сломался
    оркестратор», хотя сломался ровно тот агент, чей вывод не разобрался.
    """
    harness = _review_harness(tmp_path, schema_retries=1, replies=[_PROSE_REVIEW])

    with pytest.raises(ReviewParseError):
        anyio.run(review, harness.ctx)

    assert len(harness.errors) == 2
    assert [e.source for e in harness.errors] == [
        EventSource.REVIEWER,
        EventSource.REVIEWER,
    ], "неудачная попытка ревьюера записана в журнал как чужой поток"


def test_step_raises_the_failure_of_the_last_attempt(tmp_path: Path) -> None:
    """Наружу уходит ошибка ПОСЛЕДНЕЙ попытки, а не первой ([REQ-006]).

    Попытки ломаются по-разному: проза не содержит JSON вовсе
    (`ReviewParseError`), а второй ответ схемно валиден и отвергнут §4.4 за
    пустой `checked` (`ReviewNotAccepted`). Шаг, отдающий первую ошибку,
    сообщил бы пользователю «ревьюер ответил прозой» про сессию, где
    последний ответ был разобран и отклонён совсем по другой причине.
    """
    harness = _review_harness(
        tmp_path,
        schema_retries=1,
        replies=[_PROSE_REVIEW, _review_with_empty_checked(1)],
    )

    with pytest.raises(ReviewNotAccepted) as excinfo:
        anyio.run(review, harness.ctx)

    assert len(harness.adapter.prompts) == 2
    assert excinfo.value.reasons, "причины §4.4 потеряны по дороге наружу"
    assert harness.fsm.state.state is SessionPhase.FAILED
