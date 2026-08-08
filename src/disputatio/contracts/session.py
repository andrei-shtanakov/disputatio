"""Модель `session.json` — SessionState и вложенные модели ([DESIGN-002], [REQ-002]).

Схема §4.1 SPEC-001. Сама state machine (переходы между состояниями) — вне
contracts; здесь только имена состояний (`SessionPhase`) и данные. Вложенные
модели наследуют `ArtifactChild` и не несут поле ``schema`` — версия одна на
артефакт-файл. Обновление счётчиков при frozen-моделях — через
``model_copy(update=...)`` на стороне оркестратора (write-ahead и так пишет
целый файл).
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from disputatio.contracts.base import ArtifactBase, ArtifactChild, Role


class SessionPhase(StrEnum):
    """Состояние сессии — все 12 состояний машины §2 SPEC-001."""

    IDLE = "IDLE"
    PROPOSING = "PROPOSING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    DECIDING = "DECIDING"
    CONVERGED = "CONVERGED"
    DEADLOCK = "DEADLOCK"
    BUDGET_HIT = "BUDGET_HIT"
    ESCALATED = "ESCALATED"
    EXPORTING = "EXPORTING"
    DONE = "DONE"
    FAILED = "FAILED"


class Mode(StrEnum):
    """Режим задачи: develop меняет код, analyze — только анализ."""

    DEVELOP = "develop"
    ANALYZE = "analyze"


class TaskSpec(ArtifactChild):
    """Пользовательская задача сессии (§4.1 `task`)."""

    prompt: str
    attachments: list[str] = Field(default_factory=list)
    mode: Mode


class AgentRef(ArtifactChild):
    """Ссылка на агента: CLI-адаптер, модель, опциональный session id."""

    adapter: str
    model: str
    session_ref: str | None = None


class Limits(ArtifactChild):
    """Лимиты сессии (§4.1 `limits`)."""

    max_rounds: int = Field(ge=1)
    max_total_tokens: int
    max_wall_seconds: int
    schema_retries: int


class BudgetUsed(ArtifactChild):
    """Израсходованный бюджет; все счётчики стартуют с нуля."""

    tokens: int = Field(default=0, ge=0)
    wall_seconds: float = 0.0
    cost_usd_est: float = 0.0


class SessionState(ArtifactBase):
    """Корневой артефакт `session.json` (§4.1 SPEC-001)."""

    session_id: str
    created_at: datetime
    state: SessionPhase
    current_round: int = Field(ge=0)
    task: TaskSpec
    agents: dict[Role, AgentRef]
    limits: Limits
    budget_used: BudgetUsed

    @model_validator(mode="after")
    def _require_both_agents(self) -> "SessionState":
        # Тип dict[Role, AgentRef] допускает неполный набор; debate loop
        # требует обоих агентов ([DESIGN-017], [REQ-019]).
        missing = [role.value for role in Role if role not in self.agents]
        if missing:
            raise ValueError(
                f"agents обязан содержать ключи author и reviewer; нет: {missing}"
            )
        return self
