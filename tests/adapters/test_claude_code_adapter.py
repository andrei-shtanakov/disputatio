"""`ClaudeCodeAdapter` role→argv wiring — TASK-004.

[DESIGN-006], [REQ-005], [REQ-006]. Проверяет, что `_build_argv` реально
подмешивает результат `build_role_argv` в argv, который видит `launcher`
(без прямого обращения к приватному методу — через шпион-launcher,
как в `test_process_seam.py`).
"""

from pathlib import Path

import anyio

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
