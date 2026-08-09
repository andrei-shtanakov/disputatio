"""`ClaudeCodeAdapter` over the `claude` CLI ([DESIGN-002]/[DESIGN-006], TASK-003).

Роли/permissions подключены (TASK-004) через `permissions.build_role_argv`,
read-only fallback (TASK-005) — через `fallback.ReadOnlyWorkspace`, а
трансляция `stdout` в `Event` (TASK-006) — через `events.translate_line`:
каждая строка уходит в `event_sink` (если он задан) и попадает в текст
`AgentTurn`, распознана она или нет.

`tokens_used`/`session_ref` (TASK-006 → TASK-007, DESIGN-008) собираются
честно: только из терминальной `result`-строки с `usage`. Нет такой
строки — оба поля остаются `None`, потому что «неизвестно» и «ноль» для
бюджета §5.2 — разные ответы.
"""

import inspect
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from disputatio.adapters._process import ProcessLauncher, default_launcher
from disputatio.adapters.events import translate_line
from disputatio.adapters.fallback import (
    ReadOnlyWorkspace,
    WorktreeCreate,
    WorktreeRemove,
)
from disputatio.adapters.permissions import AdapterCapabilities, build_role_argv
from disputatio.contracts.base import Role
from disputatio.contracts.events import EventSource, EventType
from disputatio.contracts.ports import AgentTurn, EventSink

_DEFAULT_CAPABILITIES = AdapterCapabilities(supports_granular_permissions=True)

_EVENT_SOURCE_BY_ROLE = {
    Role.AUTHOR: EventSource.AUTHOR,
    Role.REVIEWER: EventSource.REVIEWER,
}

_TEXT_DELTA_TYPE = "content_block_delta"
_RESULT_TYPE = "result"

_MISSING_WORKTREE_OPS = (
    "Reviewer без granular-permissions требует worktree_create/"
    "worktree_remove: read-only (§7) иначе ничем не обеспечен"
)

_BAD_ROUND_NO = (
    "round_no должен быть >= 1 (Event.round: ge=1) либо None для событий вне раунда"
)


class ClaudeCodeAdapter:
    """AgentAdapter over the `claude` CLI (SPEC-001 §7/§8)."""

    def __init__(
        self,
        *,
        role: Role,
        session_dir: Path,
        capabilities: AdapterCapabilities = _DEFAULT_CAPABILITIES,
        event_sink: EventSink | None = None,
        session: str = "",
        round_no: int | None = None,
        launcher: ProcessLauncher = default_launcher,
        worktree_create: WorktreeCreate | None = None,
        worktree_remove: WorktreeRemove | None = None,
    ) -> None:
        self.role = role
        self.session_dir = session_dir
        self.capabilities = capabilities
        self.event_sink = event_sink
        self.session = session
        self.round_no = round_no
        self.launcher = launcher
        self.worktree_create = worktree_create
        self.worktree_remove = worktree_remove
        if self._needs_read_only_workspace() and (
            worktree_create is None or worktree_remove is None
        ):
            raise ValueError(_MISSING_WORKTREE_OPS)
        if round_no is not None and round_no < 1:
            raise ValueError(_BAD_ROUND_NO)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        workspace = self._make_workspace()
        if workspace is None:
            return await self._run_in(prompt, cwd=self.session_dir)
        async with workspace as worktree:
            return await self._run_in(prompt, cwd=worktree)

    async def _run_in(self, prompt: str, *, cwd: Path) -> AgentTurn:
        """Запускает CLI в `cwd`, транслируя `stdout` в `Event` построчно."""
        argv = self._build_argv(prompt)
        result = self.launcher(*argv, cwd=str(cwd))
        process = await result if inspect.isawaitable(result) else result

        buffer = ""
        # None ≠ 0: «CLI не сообщил расход» против «сообщил ноль» (REQ-011).
        # Локали стартуют как None и присваиваются только из positively
        # распознанной usage-строки — подстановки `0`/`""` здесь нет нигде.
        tokens_used: int | None = None
        session_ref_out: str | None = None
        stdout = cast(AsyncIterator[bytes], process.stdout)
        try:
            async for raw in stdout:
                # errors="replace": §8 требует, чтобы НИ ОДНА строка не
                # пропала. Строгий decode на одном битом байте уронил бы
                # весь turn вместе с уже накопленным текстом.
                event = translate_line(
                    raw.decode(errors="replace"),
                    parser=self._parse_native_line,
                    session=self.session,
                    source=_EVENT_SOURCE_BY_ROLE[self.role],
                    round_no=self.round_no,
                    ts=datetime.now(UTC),
                )
                if event.type is EventType.AGENT_TEXT_DELTA:
                    buffer += str(event.payload["text"])
                usage = event.payload.get("usage")
                if usage is not None:
                    tokens_used = usage.get("output_tokens", tokens_used)
                    session_ref_out = usage.get("session_id", session_ref_out)
                if self.event_sink is not None:
                    self.event_sink.emit(event)
        finally:
            await process.wait()

        return AgentTurn(
            text=buffer, session_ref=session_ref_out, tokens_used=tokens_used
        )

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

    @staticmethod
    def _parse_native_line(line: str) -> tuple[EventType, dict[str, object]] | None:
        """Распознаёт stream-json конверты `claude`; `None` — всё прочее.

        Best-effort отображение (DESIGN-005): реального вывода CLI под
        рукой нет, поэтому распознаются ровно два конверта — text-delta и
        терминальный `result` с `usage` (DESIGN-008). Любая другая форма,
        включая невалидный JSON, отдаётся общему raw-fallback'у
        `translate_line`, а не теряется.
        """
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(envelope, dict):
            return None
        if envelope.get("type") == _RESULT_TYPE:
            return ClaudeCodeAdapter._parse_usage(envelope)
        if envelope.get("type") != _TEXT_DELTA_TYPE:
            return None
        text = envelope.get("text")
        if not isinstance(text, str):
            return None
        return EventType.AGENT_TEXT_DELTA, {"text": text, "raw": False}

    @staticmethod
    def _parse_usage(
        envelope: dict[str, object],
    ) -> tuple[EventType, dict[str, object]] | None:
        """Терминальный `result` → delta с пустым текстом и `usage` в payload.

        Своего типа под «CLI отчитался о расходе» в словаре §8 нет, а
        расширять его адаптеру нельзя — UI знает ровно семь типов. Отсюда
        `agent_text_delta` с `text: ""`: событие доезжает до `EventSink`
        честно распознанным (`raw: false`), а буфер `AgentTurn.text` не
        засоряет. Поля `usage` кладутся только правильно типизированными —
        мусор из stdout не должен ломать `AgentTurn` при валидации.
        """
        raw_usage = envelope.get("usage")
        if not isinstance(raw_usage, dict):
            return None
        usage: dict[str, object] = {}
        output_tokens = raw_usage.get("output_tokens")
        # bool — подкласс int, но «True токенов» смысла не имеет.
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
            usage["output_tokens"] = output_tokens
        session_id = raw_usage.get("session_id")
        if isinstance(session_id, str):
            usage["session_id"] = session_id
        if not usage:
            return None
        return EventType.AGENT_TEXT_DELTA, {"text": "", "raw": False, "usage": usage}
