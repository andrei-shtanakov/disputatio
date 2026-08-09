"""Role-based CLI argv builder — TASK-004.

[DESIGN-003], [REQ-005], [REQ-006]. Чистые функции без I/O: транслируют
`(Role, AdapterCapabilities)` в argv-фрагменты, без фейков/subprocess.
"""

from disputatio.contracts.base import Role


def test_author_argv_unrestricted() -> None:
    try:
        from disputatio.adapters.permissions import (
            AdapterCapabilities,
            build_role_argv,
        )
    except ImportError as exc:
        raise AssertionError("build_role_argv ещё не реализован из TASK-004") from exc

    capabilities = AdapterCapabilities(supports_granular_permissions=True)

    assert build_role_argv(Role.AUTHOR, capabilities) == []


def test_reviewer_argv_granular() -> None:
    try:
        from disputatio.adapters.permissions import (
            AdapterCapabilities,
            build_role_argv,
        )
    except ImportError as exc:
        raise AssertionError("build_role_argv ещё не реализован из TASK-004") from exc

    capabilities = AdapterCapabilities(supports_granular_permissions=True)

    argv = build_role_argv(Role.REVIEWER, capabilities)

    assert argv == [
        "--allowedTools",
        "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(pytest:*),Bash(ruff:*)",
    ]
    allowed_tools = argv[1]
    assert "Write" not in allowed_tools
    assert "Edit" not in allowed_tools


def test_reviewer_argv_no_granular_support() -> None:
    try:
        from disputatio.adapters.permissions import (
            AdapterCapabilities,
            build_role_argv,
        )
    except ImportError as exc:
        raise AssertionError("build_role_argv ещё не реализован из TASK-004") from exc

    capabilities = AdapterCapabilities(supports_granular_permissions=False)

    assert build_role_argv(Role.REVIEWER, capabilities) == []
