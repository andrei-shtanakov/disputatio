"""CLI-agent adapters implementing contracts.ports.AgentAdapter (SPEC-001 §7/§8)."""

from disputatio.adapters.claude_code import ClaudeCodeAdapter
from disputatio.adapters.codex import CodexAdapter
from disputatio.adapters.permissions import AdapterCapabilities

__all__ = ["ClaudeCodeAdapter", "CodexAdapter", "AdapterCapabilities"]  # noqa: RUF022
