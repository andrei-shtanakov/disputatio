"""Path write deny capability — TASK-010.

[DESIGN-003], [REQ-006]. Адаптер может объявить, что умеет запрещать
запись по путям. Слой необязательный: якорь доверия P9 — файловая граница
`integrity_anchor`, не адаптер.

Мэппинг в argv отложен до верификации механизма claude CLI. Тесты проверяют,
что данные хранятся в AdapterCapabilities и не ломают существующее поведение.
"""

from disputatio.contracts.base import Role


def test_author_argv_unchanged_by_default() -> None:
    """Регресс: дефолтный argv автора — `[]`."""
    try:
        from disputatio.adapters.permissions import (
            AdapterCapabilities,
            build_role_argv,
        )
    except ImportError as exc:
        raise AssertionError("build_role_argv ещё не реализован") from exc

    capabilities = AdapterCapabilities(supports_granular_permissions=True)
    assert build_role_argv(Role.AUTHOR, capabilities) == []


def test_author_argv_with_path_write_deny_capability_still_empty() -> None:
    """path_write_deny и deny_write_paths не влияют на argv (мэппинг отложен)."""
    try:
        from disputatio.adapters.permissions import (
            AdapterCapabilities,
            build_role_argv,
        )
    except ImportError as exc:
        raise AssertionError("path_write_deny ещё не реализован") from exc

    capabilities = AdapterCapabilities(
        supports_granular_permissions=True,
        path_write_deny=True,
        deny_write_paths=(".disputatio/**",),
    )

    argv = build_role_argv(Role.AUTHOR, capabilities)
    assert argv == []


def test_author_without_path_write_deny_capability_argv_empty() -> None:
    """Адаптер без path_write_deny — argv остаётся пуст, исключения нет."""
    try:
        from disputatio.adapters.permissions import (
            AdapterCapabilities,
            build_role_argv,
        )
    except ImportError as exc:
        raise AssertionError("build_role_argv ещё не реализован") from exc

    capabilities = AdapterCapabilities(
        supports_granular_permissions=True,
        path_write_deny=False,
        deny_write_paths=(".disputatio/**",),
    )

    argv = build_role_argv(Role.AUTHOR, capabilities)
    assert argv == []


def test_adapter_capabilities_accepts_path_write_deny_fields() -> None:
    """AdapterCapabilities принимает path_write_deny и deny_write_paths."""
    try:
        from disputatio.adapters.permissions import AdapterCapabilities
    except ImportError as exc:
        raise AssertionError("AdapterCapabilities ещё не реализован") from exc

    capabilities = AdapterCapabilities(
        supports_granular_permissions=True,
        path_write_deny=True,
        deny_write_paths=(".disputatio/**", ".git/**"),
    )

    assert capabilities.path_write_deny is True
    assert capabilities.deny_write_paths == (".disputatio/**", ".git/**")
