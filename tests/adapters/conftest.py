"""Фейковый subprocess для тестов адаптеров — TASK-001, [DESIGN-008], [REQ-015].

Единственный интерфейс, который тесты `tests/adapters/**` подсовывают вместо
`asyncio.create_subprocess_exec`: `stdout` (асинхронный итератор строк
`bytes + b"\\n"`), `wait()` (awaitable → exit_code) и атрибут `stderr`
(bytes). Ни один фейк здесь не импортирует и не запускает реальный
`claude`/`codex` бинарник.
"""

from collections.abc import AsyncIterator

import pytest


class _FakeStdout:
    """Однопроходный асинхронный итератор строк stdout фейкового процесса."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class FakeProcess:
    """Минимальный фейк `asyncio.subprocess.Process` для тестов адаптеров."""

    def __init__(
        self, stdout_lines: list[str], stderr: bytes = b"", exit_code: int = 0
    ) -> None:
        self.stdout = _FakeStdout([f"{line}\n".encode() for line in stdout_lines])
        self.stderr = stderr
        self._exit_code = exit_code

    async def wait(self) -> int:
        return self._exit_code


@pytest.fixture
def make_fake_process():
    """Фикстура-фабрика `make_fake_process(stdout_lines, stderr, exit_code)`."""

    def _make(
        stdout_lines: list[str], stderr: bytes = b"", exit_code: int = 0
    ) -> FakeProcess:
        return FakeProcess(stdout_lines, stderr=stderr, exit_code=exit_code)

    return _make
