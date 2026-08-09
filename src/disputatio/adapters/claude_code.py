"""`ClaudeCodeAdapter` over the `claude` CLI ([DESIGN-002]/[DESIGN-006], TASK-003).

Роли/permissions подключены (TASK-004) через `permissions.build_role_argv`.
Без read-only fallback (TASK-005) и без `EventSink` (TASK-006) — `stdout`
фейка/CLI накапливается построчно в текст.
"""

import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

from disputatio.adapters._process import ProcessLauncher, default_launcher
from disputatio.adapters.permissions import AdapterCapabilities, build_role_argv
from disputatio.contracts.base import Role
from disputatio.contracts.ports import AgentTurn

_DEFAULT_CAPABILITIES = AdapterCapabilities(supports_granular_permissions=True)


class ClaudeCodeAdapter:
    """AgentAdapter over the `claude` CLI (SPEC-001 §7/§8)."""

    def __init__(
        self,
        *,
        role: Role,
        session_dir: Path,
        capabilities: AdapterCapabilities = _DEFAULT_CAPABILITIES,
        launcher: ProcessLauncher = default_launcher,
    ) -> None:
        self.role = role
        self.session_dir = session_dir
        self.capabilities = capabilities
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
        return ["claude", "-p", prompt, *build_role_argv(self.role, self.capabilities)]
