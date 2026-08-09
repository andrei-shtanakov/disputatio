"""Тестовая инфраструктура `tests/adapters`: фейки subprocess-шва и EventSink.

[DESIGN-010], NFR-002: реальные `claude`/`codex`/`git` в тестах не
запускаются никогда — все subprocess-вызовы идут через `FakeSpawn`
(записывает `(argv, cwd, stdin)` для пиновки, ADR-A2), возвращающий
`FakeProcess` с заготовленным stdout. `RecordingSink` структурно
удовлетворяет порту `EventSink`; `fixed_clock` даёт детерминированный
`ts`. Async — anyio (NFR-003).

Фикстуры нативных потоков — минимальные срезы форматов
`claude -p --output-format stream-json` и `codex exec --json`; каждая
содержит одну заведомо непарсибельную строку под правило `raw: true`
(DESIGN-004, [REQ-007]). Точный маппинг элементов пинуют тесты
трансляторов.
"""

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from disputatio.contracts.events import Event

FIXED_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeStdout:
    """Async-итератор байтовых строк — как `async for line in proc.stdout`."""

    def __init__(self, lines: Sequence[bytes]) -> None:
        self._lines: Iterator[bytes] = iter(lines)

    def __aiter__(self) -> "FakeStdout":
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._lines)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeProcess:
    """Фейковый CLI-процесс: stdout отдаёт заготовленные строки (NFR-002).

    Строки отдаются байтами с завершающим `\\n` — как их читает
    `asyncio.StreamReader`; `returncode` и `stderr` доступны сразу
    (у фейка процесс «уже завершён»).
    """

    def __init__(
        self,
        stdout_lines: Sequence[str] = (),
        *,
        returncode: int = 0,
        stderr: bytes = b"",
    ) -> None:
        self.stdout = FakeStdout([f"{line}\n".encode() for line in stdout_lines])
        self.returncode = returncode
        self.stderr = stderr

    async def wait(self) -> int:
        """Ожидание завершения: у фейка мгновенно, отдаёт `returncode`."""
        return self.returncode


@dataclass(frozen=True, slots=True)
class SpawnCall:
    """Записанный вызов spawn-шва: материал пиновки argv/cwd/stdin."""

    argv: tuple[str, ...]
    cwd: Path | None
    stdin: object | None


class FakeSpawn:
    """Записывающий фейк SpawnFn: копит `SpawnCall`, отдаёт `process`.

    Сигнатура `__call__` — по образу `asyncio.create_subprocess_exec`
    (позиционный argv, keyword-only прочее), ADR-A2: тесты передают фейк
    конструктору адаптера явно, monkeypatch не нужен.
    """

    def __init__(self, process: FakeProcess | None = None) -> None:
        self.process = process if process is not None else FakeProcess()
        self.calls: list[SpawnCall] = []

    async def __call__(
        self,
        *argv: str,
        cwd: Path | None = None,
        stdin: object | None = None,
        **kwargs: object,
    ) -> FakeProcess:
        """Записывает вызов и возвращает заготовленный `FakeProcess`."""
        self.calls.append(SpawnCall(argv=argv, cwd=cwd, stdin=stdin))
        return self.process


class RecordingSink:
    """Список принятых `Event`; структурно удовлетворяет `EventSink`."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        """Дописывает событие в конец списка (аналог append в events.jsonl)."""
        self.events.append(event)


@pytest.fixture
def fake_spawn() -> FakeSpawn:
    """Записывающий spawn-фейк с пустым процессом по умолчанию."""
    return FakeSpawn()


@pytest.fixture
def recording_sink() -> RecordingSink:
    """Свежий `RecordingSink` на тест."""
    return RecordingSink()


@pytest.fixture
def fixed_clock() -> Callable[[], datetime]:
    """Детерминированные часы: всегда возвращают `FIXED_TS`."""
    return lambda: FIXED_TS


@pytest.fixture
def claude_stream_lines() -> list[str]:
    """Минимальный срез потока `claude -p --output-format stream-json`."""
    return [
        json.dumps(
            {"type": "system", "subtype": "init", "session_id": "sess-claude-1"}
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Смотрю рабочий каталог."}]
                },
                "session_id": "sess-claude-1",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "Read",
                            "input": {"file_path": "README.md"},
                        }
                    ]
                },
                "session_id": "sess-claude-1",
            }
        ),
        "непарсибельная строка claude",
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "session_id": "sess-claude-1",
                "usage": {"input_tokens": 12, "output_tokens": 34},
            }
        ),
    ]


@pytest.fixture
def codex_stream_lines() -> list[str]:
    """Минимальный срез потока `codex exec --json` (JSONL)."""
    return [
        json.dumps({"type": "thread.started", "thread_id": "thread-codex-1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "agent_message",
                    "text": "Готово: патч применён.",
                },
            }
        ),
        "непарсибельная строка codex",
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 7, "output_tokens": 9},
            }
        ),
    ]
