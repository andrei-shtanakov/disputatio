"""Subprocess launch seam ([DESIGN-002], [REQ-012]).

Единственное место в пакете, которому разрешено ссылаться на
`asyncio.create_subprocess_exec`. Адаптеры принимают `launcher` через
конструктор вместо прямого вызова этой функции, чтобы
`tests/adapters/conftest.py`'s `make_fake_process` подставлялся без патчей
глобального состояния (REQ-012).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, cast, runtime_checkable


@runtime_checkable
class SubprocessLike(Protocol):
    """Структурная форма, которую в тестах занимает `FakeProcess`."""

    stdout: object
    stderr: bytes

    async def wait(self) -> int: ...


ProcessLauncher = Callable[..., Awaitable[SubprocessLike] | SubprocessLike]


STDOUT_LINE_LIMIT = 1 << 20
"""Потолок длины строки `stdout`.

Дефолтные 64 KiB `StreamReader` роняют `async for` c `ValueError` на
длинном stream-json конверте (tool result), а вместе с ним — и весь turn:
прямое нарушение §8 «ни одна строка не теряется».
"""


async def default_launcher(*argv: str, cwd: str | None = None) -> SubprocessLike:
    """Тонкая обёртка над `asyncio.create_subprocess_exec` (продовый дефолт)."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STDOUT_LINE_LIMIT,
    )
    return cast(SubprocessLike, process)
