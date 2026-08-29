"""Тесты pipeline-портов: PipelineStateStore, RoundBoundaryPolicy, SessionLifecyclePolicy.

Импорты `disputatio.contracts.ports` выполняются внутри тестов: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы collection.
Red-селектор падает assertion'ом при отсутствии портов.
"""

from typing import TYPE_CHECKING

from disputatio.contracts.pipeline import PipelineState
from disputatio.contracts.review import Review
from disputatio.contracts.session import SessionState

if TYPE_CHECKING:
    from disputatio.contracts.ports import BoundaryVerdict


def make_pipeline_state() -> PipelineState:
    """Минимальный валидный PipelineState — груз для фейка PipelineStateStore."""
    # Используем model_validate с полным payload'ом, как в test_pipeline_state.py
    _SHA = "a" * 64

    payload = {
        "schema": "disputatio/pipeline/v1",
        "pipeline_id": "pipe-20260828-01",
        "created_at": "2026-08-28T12:00:00+00:00",
        "phase": "IDLE",
        "task": {"path": "task.md", "sha256": _SHA},
        "config": {"path": "config.toml", "sha256": _SHA},
        "checklists": {"path": "checklists.toml", "sha256": _SHA},
        "documents": {"spec_path": "spec/design.md", "plan_path": "spec/tasks.md"},
        "spec_sessions": [],
        "pair_sessions": [],
        "transitions": [],
        "budget_used": {"tokens": 0, "wall_seconds": 0.0, "cost_usd_est": 0.0},
        "operator_decisions": [],
        "anchor_id": "pipe-20260828-01",
        "next_action": None,
    }
    return PipelineState.model_validate(payload)


def make_review() -> Review:
    """Минимальный валидный Review — груз для фейка RoundBoundaryPolicy."""
    return Review.model_validate(
        {
            "schema": "disputatio/v1",
            "round": 1,
            "role": "reviewer",
            "verdict": "approve",
            "confidence": 0.9,
            "issues": [],
            "checked": ["code"],
            "summary": "одобрено",
        }
    )


def make_session_state() -> SessionState:
    """Минимальный валидный SessionState — груз для фейка SessionLifecyclePolicy."""
    from datetime import UTC, datetime

    from disputatio.contracts.base import Role
    from disputatio.contracts.session import (
        AgentRef,
        BudgetUsed,
        Limits,
        Mode,
        SessionPhase,
        SessionState,
        TaskSpec,
    )

    return SessionState(
        session_id="sess-20260828-01",
        created_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        state=SessionPhase.IDLE,
        current_round=0,
        task=TaskSpec(prompt="тестовая задача", mode=Mode.DEVELOP),
        agents={
            Role.AUTHOR: AgentRef(adapter="claude_code", model="claude-sonnet-5"),
            Role.REVIEWER: AgentRef(adapter="codex", model="gpt-5.4"),
        },
        limits=Limits(
            max_rounds=8,
            max_total_tokens=1_000_000,
            max_wall_seconds=3600,
            schema_retries=2,
        ),
        budget_used=BudgetUsed(),
    )


class FakePipelineStateStore:
    """Sync-фейк порта PipelineStateStore: хранит состояния в памяти."""

    def __init__(self) -> None:
        self._states: dict[str, PipelineState] = {}

    def load(self, pipeline_id: str) -> PipelineState:
        return self._states[pipeline_id]

    def save(self, state: PipelineState) -> None:
        self._states[state.pipeline_id] = state


class FakeRoundBoundaryPolicy:
    """Sync-фейк порта RoundBoundaryPolicy: always proceed."""

    def after_deciding(self, review: Review) -> "BoundaryVerdict":
        from disputatio.contracts.ports import BoundaryVerdict

        return BoundaryVerdict.PROCEED


class FakeSessionLifecyclePolicy:
    """Sync-фейк порта SessionLifecyclePolicy: no-op hooks."""

    def before_author_turn(self, state: SessionState) -> None:
        pass

    def after_author_turn(self, state: SessionState) -> None:
        pass


def test_fakes_pass_isinstance_checks() -> None:
    """Структурный фейк каждого pipeline-порта проходит isinstance-проверку."""
    try:
        from disputatio.contracts import ports
    except ImportError as exc:
        raise AssertionError("src/disputatio/contracts/ports.py ещё не создан") from exc

    assert isinstance(FakePipelineStateStore(), ports.PipelineStateStore)
    assert isinstance(FakeRoundBoundaryPolicy(), ports.RoundBoundaryPolicy)
    assert isinstance(FakeSessionLifecyclePolicy(), ports.SessionLifecyclePolicy)


def test_pipeline_state_store_requires_both_methods() -> None:
    """Фейк с load, но без save — не PipelineStateStore: нужны оба метода."""
    from disputatio.contracts import ports

    class LoadOnly:
        def load(self, pipeline_id: str) -> PipelineState:
            return make_pipeline_state()

    assert not isinstance(LoadOnly(), ports.PipelineStateStore)


def test_sync_pipeline_fakes_satisfy_protocol_annotations() -> None:
    """Sync-фейки присваиваются переменным с типом Protocol (ловит pyrefly)."""
    from disputatio.contracts.ports import (
        BoundaryVerdict,
        PipelineStateStore,
        RoundBoundaryPolicy,
        SessionLifecyclePolicy,
    )

    store: PipelineStateStore = FakePipelineStateStore()
    state = make_pipeline_state()
    store.save(state)
    assert store.load(state.pipeline_id) == state

    policy: RoundBoundaryPolicy = FakeRoundBoundaryPolicy()
    review = make_review()
    verdict = policy.after_deciding(review)
    assert verdict == BoundaryVerdict.PROCEED

    lifecycle: SessionLifecyclePolicy = FakeSessionLifecyclePolicy()
    session_state = make_session_state()
    lifecycle.before_author_turn(session_state)
    lifecycle.after_author_turn(session_state)


def test_boundary_verdict_enum_values() -> None:
    """BoundaryVerdict содержит ровно PROCEED и PARK."""
    from disputatio.contracts.ports import BoundaryVerdict

    assert BoundaryVerdict.PROCEED.value == "proceed"
    assert BoundaryVerdict.PARK.value == "park"
    assert len(BoundaryVerdict) == 2
