"""Protocol-порты оркестратора и модель AgentTurn ([DESIGN-009], [REQ-012]).

Четыре интерфейса `typing.Protocol`, все `@runtime_checkable`: structural
check фейков через `isinstance` (проверка наличия методов; сигнатуры
покрывает pyrefly-присваивание фейка переменной типа Protocol, ADR-004).
Сигнатуры оперируют только моделями contracts и стандартными типами;
реализации портов живут вне contracts (INV-11).

`AgentAdapter.run` — единственный async-метод (адаптеры — subprocess/stream);
`StateStore`/`EventSink`/`Verifier` — sync в v1. Схемная валидация вывода
агента — обязанность оркестратора поверх `AgentTurn.text`, не адаптера;
права ревьюера (read-only, §7) — конфигурация реализации адаптера, в порт
не входят.
"""

from typing import Protocol, runtime_checkable

from disputatio.contracts.base import ArtifactChild
from disputatio.contracts.events import Event
from disputatio.contracts.session import SessionState
from disputatio.contracts.verification import VerificationReport


@runtime_checkable
class StateStore(Protocol):
    """Порт хранилища `session.json`: load/save состояния сессии."""

    def load(self, session_id: str) -> SessionState:
        """Читает состояние сессии `session_id` с диска."""
        ...

    def save(self, state: SessionState) -> None:
        """Пишет состояние атомарно (write-ahead: до следующего шага)."""
        ...


@runtime_checkable
class EventSink(Protocol):
    """Порт журнала событий: append в `events.jsonl`."""

    def emit(self, event: Event) -> None:
        """Дописывает событие в конец журнала."""
        ...


class AgentTurn(ArtifactChild):
    """Сырой результат одного вызова агента до схемной валидации."""

    text: str
    session_ref: str | None = None
    tokens_used: int = 0


@runtime_checkable
class AgentAdapter(Protocol):
    """Порт CLI-агента: один вызов промпта → сырой `AgentTurn`."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Выполняет промпт; `session_ref` — оптимизация `--resume` адаптера."""
        ...


@runtime_checkable
class Verifier(Protocol):
    """Порт детерминированных гейтов: прогон verification раунда."""

    def verify(self, round_no: int) -> VerificationReport:
        """Запускает гейты раунда `round_no` и возвращает отчёт."""
        ...
