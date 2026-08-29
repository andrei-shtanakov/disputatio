"""Path write deny capability — TASK-010.

[DESIGN-003], [REQ-006]. Адаптер может объявить, что умеет запрещать
запись по путям. Слой необязательный: якорь доверия P9 — файловая граница
`integrity_anchor`, не адаптер.
"""

from disputatio.contracts.base import Role


def test_author_argv_unchanged_by_default() -> None:
    """Регресс: дефолтный дефолт автора — `[]`."""
    try:
        from disputatio.adapters.permissions import (
            AdapterCapabilities,
            build_role_argv,
        )
    except ImportError as exc:
        raise AssertionError("build_role_argv ещё не реализован") from exc

    capabilities = AdapterCapabilities(supports_granular_permissions=True)
    assert build_role_argv(Role.AUTHOR, capabilities) == []


def test_author_with_path_write_deny_capability() -> None:
    """claude_code с path_write_deny=True собирает deny-аргументы."""
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

    # Проверяем, что argv содержит deny-правило
    assert len(argv) >= 2
    assert "--denyPaths" in argv
    denyPaths_idx = argv.index("--denyPaths")
    # Пути должны быть либо в одном аргументе, либо в следующих
    assert denyPaths_idx + 1 < len(argv)
    assert ".disputatio/**" in argv[denyPaths_idx + 1]


def test_author_without_path_write_deny_capability() -> None:
    """Адаптер без path_write_deny допускается; argv остаётся пуст."""
    try:
        from disputatio.adapters.permissions import (
            AdapterCapabilities,
            build_role_argv,
        )
    except ImportError as exc:
        raise AssertionError("build_role_argv ещё не реализован") from exc

    # Адаптер без path_write_deny с тем же deny_write_paths
    capabilities = AdapterCapabilities(
        supports_granular_permissions=True,
        path_write_deny=False,
        deny_write_paths=(".disputatio/**",),
    )

    argv = build_role_argv(Role.AUTHOR, capabilities)
    assert argv == []


def test_author_with_empty_deny_write_paths() -> None:
    """При path_write_deny=True но пусто deny_write_paths — argv пуст."""
    try:
        from disputatio.adapters.permissions import (
            AdapterCapabilities,
            build_role_argv,
        )
    except ImportError as exc:
        raise AssertionError("build_role_argv ещё не реализован") from exc

    capabilities = AdapterCapabilities(
        supports_granular_permissions=True,
        path_write_deny=True,
        deny_write_paths=(),
    )

    argv = build_role_argv(Role.AUTHOR, capabilities)
    assert argv == []
