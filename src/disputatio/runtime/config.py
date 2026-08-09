"""Конфиг сессии — `RuntimeConfig` ([DESIGN-014], [REQ-001], [REQ-014]).

Снапшот, из которого собирается вся сессия: адаптеры и модели агентов,
лимиты §5.2, список gates и `base_commit` — цель `git reset` первого раунда.
Frozen: resume обязан прочитать ровно то, что было записано на старте, и
никакой шаг не вправе подкрутить лимит на ходу.

Здесь только структура и переход в артефакт `session.json`
(`to_session_state`). Чтение и рендер `config.toml` (`from_toml`/
`render_toml`) приходят с [TASK-016]: round-trip — отдельное поведение с
собственным тестом, а не деталь конструктора.
"""

from dataclasses import dataclass
from datetime import datetime

from disputatio.contracts import (
    AgentRef,
    BudgetUsed,
    Limits,
    Mode,
    Role,
    SessionPhase,
    SessionState,
    TaskSpec,
)
from disputatio.verifier import GateSpec


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Один агент из `[agents.author]`/`[agents.reviewer]`: имя CLI и модель."""

    adapter: str
    model: str


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    """Лимиты сессии из `[limits]` (§5.2 SPEC-001)."""

    max_rounds: int
    max_total_tokens: int
    max_wall_seconds: int
    schema_retries: int


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Снапшот конфига сессии — вход composition root'а ([DESIGN-001])."""

    session_id: str
    mode: Mode
    base_commit: str
    task_prompt: str
    author: AgentConfig
    reviewer: AgentConfig
    limits: LimitsConfig
    gates: tuple[GateSpec, ...] = ()
    attachments: tuple[str, ...] = ()

    def to_session_state(self, *, created_at: datetime) -> SessionState:
        """Начальное состояние §4.1: `IDLE`, нулевой раунд, пустой бюджет.

        `created_at` передаётся, а не берётся у `datetime.now`: часы сессии
        инжектируются в `RuntimeDeps.now`, и второй источник времени сделал
        бы `session.json` недетерминированным в тестах ([REQ-001]).
        """
        return SessionState(
            session_id=self.session_id,
            created_at=created_at,
            state=SessionPhase.IDLE,
            current_round=0,
            task=TaskSpec(
                prompt=self.task_prompt,
                attachments=list(self.attachments),
                mode=self.mode,
            ),
            agents={
                Role.AUTHOR: AgentRef(
                    adapter=self.author.adapter, model=self.author.model
                ),
                Role.REVIEWER: AgentRef(
                    adapter=self.reviewer.adapter, model=self.reviewer.model
                ),
            },
            limits=Limits(
                max_rounds=self.limits.max_rounds,
                max_total_tokens=self.limits.max_total_tokens,
                max_wall_seconds=self.limits.max_wall_seconds,
                schema_retries=self.limits.schema_retries,
            ),
            budget_used=BudgetUsed(),
        )
