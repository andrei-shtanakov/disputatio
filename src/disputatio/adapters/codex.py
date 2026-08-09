"""`CodexAdapter` — заглушка ([DESIGN-001]; `run()` реализуется в TASK-003+)."""

from pathlib import Path

from disputatio.contracts.base import Role
from disputatio.contracts.ports import AgentTurn


class CodexAdapter:
    """AgentAdapter over the `codex` CLI (SPEC-001 §7/§8)."""

    def __init__(self, *, role: Role, session_dir: Path) -> None:
        self.role = role
        self.session_dir = session_dir

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        raise NotImplementedError
