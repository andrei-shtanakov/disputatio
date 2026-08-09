"""`ClaudeCodeAdapter` over the `claude` CLI ([DESIGN-002]/[DESIGN-006], TASK-003).

Роли/permissions подключены (TASK-004) через `permissions.build_role_argv`,
read-only fallback (TASK-005) — через `fallback.ReadOnlyWorkspace`. Без
`EventSink` (TASK-006) — `stdout` фейка/CLI накапливается построчно в текст.
"""

import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

from disputatio.adapters._process import ProcessLauncher, default_launcher
from disputatio.adapters.fallback import (
    ReadOnlyWorkspace,
    WorktreeCreate,
    WorktreeRemove,
)
from disputatio.adapters.permissions import AdapterCapabilities, build_role_argv
from disputatio.contracts.base import Role
from disputatio.contracts.ports import AgentTurn

_DEFAULT_CAPABILITIES = AdapterCapabilities(supports_granular_permissions=True)

_MISSING_WORKTREE_OPS = (
    "Reviewer без granular-permissions требует worktree_create/"
    "worktree_remove: read-only (§7) иначе ничем не обеспечен"
)


class ClaudeCodeAdapter:
    """AgentAdapter over the `claude` CLI (SPEC-001 §7/§8)."""

    def __init__(
        self,
        *,
        role: Role,
        session_dir: Path,
        capabilities: AdapterCapabilities = _DEFAULT_CAPABILITIES,
        launcher: ProcessLauncher = default_launcher,
        worktree_create: WorktreeCreate | None = None,
        worktree_remove: WorktreeRemove | None = None,
    ) -> None:
        self.role = role
        self.session_dir = session_dir
        self.capabilities = capabilities
        self.launcher = launcher
        self.worktree_create = worktree_create
        self.worktree_remove = worktree_remove
        if self._needs_read_only_workspace() and (
            worktree_create is None or worktree_remove is None
        ):
            raise ValueError(_MISSING_WORKTREE_OPS)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        workspace = self._make_workspace()
        if workspace is None:
            return await self._run_in(prompt, cwd=self.session_dir)
        async with workspace as worktree:
            return await self._run_in(prompt, cwd=worktree)

    async def _run_in(self, prompt: str, *, cwd: Path) -> AgentTurn:
        """Запускает CLI в `cwd` и собирает его `stdout` в один текст."""
        argv = self._build_argv(prompt)
        result = self.launcher(*argv, cwd=str(cwd))
        process = await result if inspect.isawaitable(result) else result

        buffer = ""
        stdout = cast(AsyncIterator[bytes], process.stdout)
        try:
            async for raw in stdout:
                buffer += raw.decode()
        finally:
            await process.wait()

        return AgentTurn(text=buffer, session_ref=None, tokens_used=None)

    def _needs_read_only_workspace(self) -> bool:
        """Read-only worktree нужен только ревьюеру без granular-permissions.

        Во всех остальных сочетаниях (`Role.AUTHOR`, либо ревьюер, чей CLI
        умеет `--allowedTools`) ограничение уже наложено `build_role_argv`,
        лишний worktree создавать нечего.
        """
        return (
            self.role is Role.REVIEWER
            and not self.capabilities.supports_granular_permissions
        )

    def _make_workspace(self) -> ReadOnlyWorkspace | None:
        """Строит workspace или `None`; конфигурацию проверил `__init__`."""
        if not self._needs_read_only_workspace():
            return None
        create, remove = self.worktree_create, self.worktree_remove
        if create is None or remove is None:  # атрибуты обнулили после __init__
            raise ValueError(_MISSING_WORKTREE_OPS)
        return ReadOnlyWorkspace(self.session_dir, create=create, remove=remove)

    def _build_argv(self, prompt: str) -> list[str]:
        return ["claude", "-p", prompt, *build_role_argv(self.role, self.capabilities)]
