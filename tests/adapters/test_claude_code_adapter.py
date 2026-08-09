"""`ClaudeCodeAdapter` role→argv wiring — TASK-004, read-only fallback — TASK-005.

[DESIGN-006], [REQ-005], [REQ-006]. Проверяет, что `_build_argv` реально
подмешивает результат `build_role_argv` в argv, который видит `launcher`
(без прямого обращения к приватному методу — через шпион-launcher,
как в `test_process_seam.py`).

[DESIGN-004], [REQ-007]: Reviewer без granular-permissions работает в
read-only worktree — `launcher` получает его путь как `cwd`, а не
`session_dir`; во всех остальных сочетаниях роли и capabilities
`ReadOnlyWorkspace` не создаётся вовсе.
"""

from pathlib import Path

import anyio
import pytest

from disputatio.contracts.base import Role


def test_author_role_argv_has_no_allowed_tools_restriction(
    tmp_path: Path, make_fake_process
) -> None:
    try:
        from disputatio.adapters.claude_code import ClaudeCodeAdapter
        from disputatio.adapters.permissions import AdapterCapabilities
    except ImportError as exc:
        raise AssertionError(
            "role-based argv-wiring ещё не реализован из TASK-004"
        ) from exc

    captured_argv: list[str] = []

    def _spy_launcher(*argv: str, **kwargs: object):
        captured_argv.extend(argv)
        return make_fake_process(["ok"])

    try:
        adapter = ClaudeCodeAdapter(
            role=Role.AUTHOR,
            session_dir=tmp_path,
            capabilities=AdapterCapabilities(supports_granular_permissions=True),
            launcher=_spy_launcher,
        )
        anyio.run(adapter.run, "do the thing")
    except TypeError as exc:
        raise AssertionError(
            "ClaudeCodeAdapter ещё не принимает capabilities из TASK-004"
        ) from exc

    assert "--allowedTools" not in captured_argv


def test_reviewer_role_argv_is_granular_and_excludes_write_tools(
    tmp_path: Path, make_fake_process
) -> None:
    try:
        from disputatio.adapters.claude_code import ClaudeCodeAdapter
        from disputatio.adapters.permissions import AdapterCapabilities
    except ImportError as exc:
        raise AssertionError(
            "role-based argv-wiring ещё не реализован из TASK-004"
        ) from exc

    captured_argv: list[str] = []

    def _spy_launcher(*argv: str, **kwargs: object):
        captured_argv.extend(argv)
        return make_fake_process(["ok"])

    try:
        adapter = ClaudeCodeAdapter(
            role=Role.REVIEWER,
            session_dir=tmp_path,
            capabilities=AdapterCapabilities(supports_granular_permissions=True),
            launcher=_spy_launcher,
        )
        anyio.run(adapter.run, "review the thing")
    except TypeError as exc:
        raise AssertionError(
            "ClaudeCodeAdapter ещё не принимает capabilities из TASK-004"
        ) from exc

    assert "--allowedTools" in captured_argv
    allowed_tools = captured_argv[captured_argv.index("--allowedTools") + 1]
    assert "Write" not in allowed_tools
    assert "Edit" not in allowed_tools


def test_reviewer_without_granular_support_uses_fallback(
    tmp_path: Path, make_fake_process
) -> None:
    try:
        from disputatio.adapters.claude_code import ClaudeCodeAdapter
        from disputatio.adapters.permissions import AdapterCapabilities
    except ImportError as exc:
        raise AssertionError(
            "read-only fallback ещё не реализован из TASK-005"
        ) from exc

    session_dir = tmp_path / "session"
    worktree_path = tmp_path / "ro-worktree"
    create_calls: list[Path] = []
    remove_calls: list[Path] = []
    captured_cwd: list[object] = []

    async def _create(source: Path) -> Path:
        create_calls.append(source)
        return worktree_path

    async def _remove(path: Path) -> None:
        remove_calls.append(path)

    def _spy_launcher(*argv: str, **kwargs: object):
        captured_cwd.append(kwargs.get("cwd"))
        return make_fake_process(["ok"])

    try:
        adapter = ClaudeCodeAdapter(
            role=Role.REVIEWER,
            session_dir=session_dir,
            capabilities=AdapterCapabilities(supports_granular_permissions=False),
            launcher=_spy_launcher,
            worktree_create=_create,
            worktree_remove=_remove,
        )
    except TypeError as exc:
        raise AssertionError(
            "ClaudeCodeAdapter ещё не принимает worktree_create/worktree_remove "
            "из TASK-005"
        ) from exc

    anyio.run(adapter.run, "review the thing")

    assert create_calls == [session_dir]
    assert captured_cwd == [str(worktree_path)]
    assert str(session_dir) not in captured_cwd
    assert remove_calls == [worktree_path]


@pytest.mark.parametrize(
    ("role", "granular"),
    [
        (Role.AUTHOR, True),
        (Role.AUTHOR, False),
        (Role.REVIEWER, True),
    ],
)
def test_fallback_workspace_not_created_outside_reviewer_without_granular(
    role: Role, granular: bool, tmp_path: Path, make_fake_process
) -> None:
    try:
        from disputatio.adapters.claude_code import ClaudeCodeAdapter
        from disputatio.adapters.permissions import AdapterCapabilities
    except ImportError as exc:
        raise AssertionError(
            "read-only fallback ещё не реализован из TASK-005"
        ) from exc

    session_dir = tmp_path / "session"
    create_calls: list[Path] = []
    remove_calls: list[Path] = []
    captured_cwd: list[object] = []

    async def _create(source: Path) -> Path:
        create_calls.append(source)
        return tmp_path / "ro-worktree"

    async def _remove(path: Path) -> None:
        remove_calls.append(path)

    def _spy_launcher(*argv: str, **kwargs: object):
        captured_cwd.append(kwargs.get("cwd"))
        return make_fake_process(["ok"])

    try:
        adapter = ClaudeCodeAdapter(
            role=role,
            session_dir=session_dir,
            capabilities=AdapterCapabilities(supports_granular_permissions=granular),
            launcher=_spy_launcher,
            worktree_create=_create,
            worktree_remove=_remove,
        )
    except TypeError as exc:
        raise AssertionError(
            "ClaudeCodeAdapter ещё не принимает worktree_create/worktree_remove "
            "из TASK-005"
        ) from exc

    anyio.run(adapter.run, "do the thing")

    assert create_calls == []
    assert remove_calls == []
    assert captured_cwd == [str(session_dir)]
