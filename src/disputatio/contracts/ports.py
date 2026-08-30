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

Стриминг §8 (`agent_text_delta`, `agent_tool_use`, …) живёт в реализациях
адаптеров: EventSink инжектится в конструктор конкретного адаптера
composition root'ом (w-runtime) и транслирует нативный поток CLI в события
§8. В порт `AgentAdapter` он не входит: порт описывает только запрос-ответ
(`prompt → AgentTurn`), чтобы фейки в тестах и адаптеры без стриминга
оставались тривиальными ([REQ-016]).
"""

from enum import StrEnum
from typing import Protocol, runtime_checkable

from disputatio.contracts.base import ArtifactChild
from disputatio.contracts.events import Event
from disputatio.contracts.pipeline import PipelineState
from disputatio.contracts.review import Review
from disputatio.contracts.session import SessionState
from disputatio.contracts.verification import VerificationReport


@runtime_checkable
class StateStore(Protocol):
    """Порт хранилища `session.json`: load/save состояния сессии."""

    def load(self, session_id: str) -> SessionState:
        """Читает состояние сессии `session_id`.

        Отсутствующая сессия — `KeyError(session_id)`: стандартный тип,
        не привязанный к носителю (файловая система, БД — деталь
        реализации). Повреждённый/схемно-невалидный payload —
        `ValidationError` pydantic.
        """
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
    """Сырой результат одного вызова агента до схемной валидации.

    `tokens_used is None` означает «адаптер не сообщил расход» — оркестратор
    обязан отличать неизвестность от нуля, иначе учёт бюджета BUDGET_HIT
    (§5.2) слепнет на адаптерах без token-метрик. `0` — валидное явное
    значение («сообщил: ноль»).
    """

    text: str
    session_ref: str | None = None
    tokens_used: int | None = None


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


@runtime_checkable
class PipelineStateStore(Protocol):
    """Порт хранилища `pipeline.json`: load/save состояния пайплайна."""

    def load(self, pipeline_id: str) -> PipelineState:
        """Читает состояние пайплайна `pipeline_id`.

        Отсутствующий пайплайн — `KeyError(pipeline_id)`: стандартный тип,
        не привязанный к носителю. Повреждённый/схемно-невалидный payload —
        `ValidationError` pydantic.
        """
        ...

    def save(self, state: PipelineState) -> None:
        """Пишет состояние атомарно (write-ahead: до следующего шага)."""
        ...


class BoundaryVerdict(StrEnum):
    """Вердикт граничной политики после раунда deciding."""

    PROCEED = "proceed"
    PARK = "park"


@runtime_checkable
class RoundBoundaryPolicy(Protocol):
    """Порт политики границы раунда: решение после deciding."""

    def after_deciding(self, review: Review) -> BoundaryVerdict:
        """Принимает вердикт ревьюера и выбирает действие.

        Возвращает `PROCEED` (продолжить цикл ревизии) или `PARK`
        (припаркировать раунд для ручного разбора).
        """
        ...


@runtime_checkable
class SessionLifecyclePolicy(Protocol):
    """Порт политики жизненного цикла сессии: хуки вокруг хода автора."""

    def before_author_turn(self, state: SessionState) -> None:
        """Вызывается перед запуском автора.

        Может проверять инварианты, логировать состояние,
        подготавливать контекст автора.
        """
        ...

    def after_author_turn(self, state: SessionState) -> None:
        """Вызывается после завершения хода автора.

        Может анализировать результаты, обновлять метрики,
        принимать решения о продолжении.
        """
        ...
