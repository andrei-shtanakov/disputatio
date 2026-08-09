"""`CodexAdapter` over the `codex` CLI ([DESIGN-007], TASK-008).

Зеркало `ClaudeCodeAdapter`: тот же пайплайн `argv → launcher → построчная
трансляция stdout → AgentTurn`, те же общие модули (`_process.py`,
`permissions.py`, `fallback.py`, `events.py`) — но собственные
`_build_argv`/`_parse_native_line`, потому что нативный протокол у codex
свой. Общего базового класса нет намеренно (ADR-003): при двух адаптерах
Template Method состоял бы почти целиком из хуков.

Дефолт `AdapterCapabilities` — `supports_granular_permissions=False`
(ADR-004): наличие у реального `codex` пофлаговой раздачи инструментов на
момент проектирования не подтверждено, а REQ-007 прямо называет «раннюю
версию codex» примером CLI без таких флагов. Выбор консервативный:
read-only ревьюера при `False` обеспечивает файловая система
(`ReadOnlyWorkspace`), и это работает даже если флаги у CLI есть; ошибка в
другую сторону молча сняла бы ограничение §7. Поле видно в сигнатуре
конструктора, поэтому composition root (w-runtime) переопределит его без
правок этого модуля, как только capability подтвердится.
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

_DEFAULT_CAPABILITIES = AdapterCapabilities(supports_granular_permissions=False)

_EVENT_SOURCE_BY_ROLE = {
    Role.AUTHOR: EventSource.AUTHOR,
    Role.REVIEWER: EventSource.REVIEWER,
}

_ENVELOPE_KEY = "msg"
_TEXT_DELTA_TYPE = "agent_message_delta"
_TASK_COMPLETE_TYPE = "task_complete"

_MISSING_WORKTREE_OPS = (
    "Reviewer без granular-permissions требует worktree_create/"
    "worktree_remove: read-only (§7) иначе ничем не обеспечен"
)

_BAD_ROUND_NO = (
    "round_no должен быть >= 1 (Event.round: ge=1) либо None для событий вне раунда"
)


class CodexAdapter:
    """AgentAdapter over the `codex` CLI (SPEC-001 §7/§8)."""

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
        tokens_used: int | None = None
        session_ref_out: str | None = None
        stdout = cast(AsyncIterator[bytes], process.stdout)
        try:
            async for raw in stdout:
                # errors="replace": §8 требует, чтобы НИ ОДНА строка не
                # пропала; строгий decode на одном битом байте уронил бы
                # весь turn вместе с уже накопленным текстом.
                event = translate_line(
                    raw.decode(errors="replace"),
                    parser=self._parse_native_line,
                    session=self.session,
                    source=_EVENT_SOURCE_BY_ROLE[self.role],
                    round_no=self.round_no,
                    ts=datetime.now(UTC),
                )
                # Копим ЛЮБУЮ text-delta, включая raw: нераспознанная
                # строка — тоже текст агента, и терять её §8 запрещает.
                # Usage-событие буфер не пачкает, потому что несёт `text: ""`.
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

        Для `CodexAdapter` это дефолтная ветка, а не экзотика (ADR-004), —
        но условие то же самое, что у claude-адаптера: author пишет всегда,
        а ревьюер с флагами уже ограничен `build_role_argv`.
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
        """`codex exec --json <prompt>` плюс role-фрагмент из permissions.py.

        Имя ограничивающего флага взято у `build_role_argv` без перевода в
        codex-специфичное: у реального `codex` пофлаговой раздачи
        инструментов не подтверждено вовсе (ADR-004), и выдумывать ей имя
        значило бы зафиксировать вымысел в argv. Единственным владельцем
        правила «author → пусто, reviewer → ограничивающий набор» остаётся
        `permissions.py` (ADR-003).

        TODO: как и в `claude_code.py`, `--resume`/`session_ref` до argv не
        доходит — DESIGN-006/007 его требуют, но ни один чеклист
        TASK-003…008 не добавил; отдельная задача с честным red-циклом.
        """
        return [
            "codex",
            "exec",
            "--json",
            prompt,
            *build_role_argv(self.role, self.capabilities),
        ]

    @staticmethod
    def _parse_native_line(line: str) -> tuple[EventType, dict[str, object]] | None:
        """Распознаёт JSONL-конверты `codex`; `None` — всё прочее.

        Best-effort, как и у claude-адаптера: реального вывода CLI под
        рукой нет (REQ-012 запрещает трогать бинарь), поэтому распознаются
        ровно два конверта — `agent_message_delta` и терминальный
        `task_complete`. Всё остальное, включая невалидный JSON и
        конверты чужого CLI, уходит в общий raw-fallback `translate_line`.
        """
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(envelope, dict):
            return None
        message = envelope.get(_ENVELOPE_KEY)
        if not isinstance(message, dict):
            return None
        if message.get("type") == _TASK_COMPLETE_TYPE:
            return CodexAdapter._parse_usage(message)
        if message.get("type") != _TEXT_DELTA_TYPE:
            return None
        delta = message.get("delta")
        if not isinstance(delta, str):
            return None
        return EventType.AGENT_TEXT_DELTA, {"text": delta, "raw": False}

    @staticmethod
    def _parse_usage(
        message: dict[str, object],
    ) -> tuple[EventType, dict[str, object]] | None:
        """Терминальный `task_complete` → delta с пустым текстом и `usage`.

        Своего типа под «CLI отчитался о расходе» в словаре §8 нет, а
        расширять его адаптеру нельзя — UI знает ровно семь типов. Отсюда
        `agent_text_delta` с `text: ""`: событие доезжает до `EventSink`
        честно распознанным (`raw: false`), а буфер `AgentTurn.text` не
        засоряет.

        Форма конверта codex-специфична: `session_id` лежит рядом с
        `usage`, а не внутри него. Нормализуем к одному payload-контракту
        (`usage.output_tokens` / `usage.session_id`), чтобы `run()` читал
        расход одинаково у обоих адаптеров (DESIGN-008). Поля кладутся
        только правильно типизированными — мусор из stdout не должен
        ломать `AgentTurn` при валидации.
        """
        usage: dict[str, object] = {}
        raw_usage = message.get("usage")
        if isinstance(raw_usage, dict):
            output_tokens = raw_usage.get("output_tokens")
            # bool — подкласс int, но «True токенов» смысла не имеет.
            if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
                usage["output_tokens"] = output_tokens
        session_id = message.get("session_id")
        if isinstance(session_id, str):
            usage["session_id"] = session_id
        if not usage:
            return None
        return EventType.AGENT_TEXT_DELTA, {"text": "", "raw": False, "usage": usage}
