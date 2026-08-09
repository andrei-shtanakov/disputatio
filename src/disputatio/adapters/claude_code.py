"""`ClaudeCodeAdapter` over the `claude` CLI ([DESIGN-002]/[DESIGN-006], TASK-003).

Минимальная рабочая версия `run()`: без ролей/permissions (TASK-004), без
read-only fallback (TASK-005) и без `EventSink` (TASK-006) — argv простой,
`stdout` фейка/CLI накапливается построчно в текст.
"""

import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

from disputatio.adapters._process import ProcessLauncher, default_launcher
from disputatio.contracts.base import Role
from disputatio.contracts.ports import AgentTurn


class ClaudeCodeAdapter:
    """AgentAdapter over the `claude` CLI (SPEC-001 §7/§8)."""

    def __init__(
        self,
        *,
        role: Role,
        session_dir: Path,
        launcher: ProcessLauncher = default_launcher,
    ) -> None:
        self.role = role
        self.session_dir = session_dir
        self.launcher = launcher

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        argv = self._build_argv(prompt)
        result = self.launcher(*argv, cwd=str(self.session_dir))
        process = await result if inspect.isawaitable(result) else result

        buffer = ""
        stdout = cast(AsyncIterator[bytes], process.stdout)
        try:
            async for raw in stdout:
                buffer += raw.decode()
        finally:
            await process.wait()

        return AgentTurn(text=buffer, session_ref=None, tokens_used=None)

    def _build_argv(self, prompt: str) -> list[str]:
        return ["claude", "-p", prompt]
