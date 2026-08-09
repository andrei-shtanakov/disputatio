"""CLI-agent adapters implementing contracts.ports.AgentAdapter (SPEC-001 §7/§8).

Оба адаптера реализованы полностью: `ClaudeCodeAdapter` (TASK-003…007) и
`CodexAdapter` (TASK-008) — заглушек уровня TASK-002 здесь больше нет.
Классы не связаны наследованием (ADR-003): общее у них — свободные функции
`permissions.py`/`fallback.py`/`events.py`/`_process.py`, а не базовый класс.
"""

from disputatio.adapters.claude_code import ClaudeCodeAdapter
from disputatio.adapters.codex import CodexAdapter
from disputatio.adapters.permissions import AdapterCapabilities

__all__ = ["ClaudeCodeAdapter", "CodexAdapter", "AdapterCapabilities"]  # noqa: RUF022
