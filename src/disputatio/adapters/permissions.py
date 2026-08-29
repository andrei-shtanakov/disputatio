"""Role-based CLI argument builder ([DESIGN-003]).

Чистые функции без I/O: транслируют `(Role, AdapterCapabilities)` в
argv-фрагменты. `Role` — существующий `disputatio.contracts.base.Role`, этот
модуль не вводит конкурирующий тип роли.
"""

from dataclasses import dataclass

from disputatio.contracts.base import Role

REVIEWER_DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Grep",
    "Glob",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(pytest:*)",
    "Bash(ruff:*)",
)


@dataclass(frozen=True)
class AdapterCapabilities:
    """Per-CLI capability flags read by permissions.py/fallback.py.

    `path_write_deny` и `deny_write_paths` объявляют, что CLI умеет
    запрещать запись по путям. Мэппинг в argv отложен до верификации
    механизма claude CLI (какой флаг, какой синтаксис спецификаторов путей).
    """

    supports_granular_permissions: bool
    reviewer_allowed_tools: tuple[str, ...] = REVIEWER_DEFAULT_ALLOWED_TOOLS
    path_write_deny: bool = False
    deny_write_paths: tuple[str, ...] = ()


def build_role_argv(role: Role, capabilities: AdapterCapabilities) -> list[str]:
    """Возвращает доп. argv-фрагменты для `role`; `[]` для Author (без ограничений).

    `path_write_deny`/`deny_write_paths` объявляют capability, но мэппинг
    в argv отложен до верификации механизма claude CLI.
    """
    if role is Role.AUTHOR:
        return []
    if not capabilities.supports_granular_permissions:
        return []  # ограничение обеспечивает fallback.py через файловую систему
    allowed = ",".join(capabilities.reviewer_allowed_tools)
    return ["--allowedTools", allowed]
