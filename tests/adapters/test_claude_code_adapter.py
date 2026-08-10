"""`ClaudeCodeAdapter` role→argv wiring — TASK-004, read-only fallback — TASK-005.

[DESIGN-006], [REQ-005], [REQ-006]. Проверяет, что `_build_argv` реально
подмешивает результат `build_role_argv` в argv, который видит `launcher`
(без прямого обращения к приватному методу — через шпион-launcher,
как в `test_process_seam.py`).

[DESIGN-004], [REQ-007]: Reviewer без granular-permissions работает в
read-only worktree — `launcher` получает его путь как `cwd`, а не
`session_dir`; во всех остальных сочетаниях роли и capabilities
`ReadOnlyWorkspace` не создаётся вовсе.

[DESIGN-005], [REQ-008], [REQ-009], TASK-006: каждая строка `stdout`
доезжает до `EventSink` как `Event` — распознанная нативная строка
типизированной, любая другая (включая невалидный JSON) — `agent_text_delta`
с `raw: true` и неурезанным текстом.
"""

from pathlib import Path
from typing import Any

import anyio
import pytest

from disputatio.contracts.base import Role
from disputatio.contracts.events import Event, EventSource, EventType


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


class _SpySink:
    """Шпион `contracts.ports.EventSink`: копит всё, что ему отдали."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


def _adapter_with_sink(
    stdout_line: str, tmp_path: Path, make_fake_process
) -> tuple[Any, _SpySink]:
    """`ClaudeCodeAdapter` (author) с одной строкой `stdout` и шпион-сником."""
    from disputatio.adapters.claude_code import ClaudeCodeAdapter

    sink = _SpySink()
    try:
        adapter = ClaudeCodeAdapter(
            role=Role.AUTHOR,
            session_dir=tmp_path,
            launcher=lambda *a, **kw: make_fake_process([stdout_line]),
            event_sink=sink,
            session="s-42",
            round_no=2,
        )
    except TypeError as exc:
        raise AssertionError(
            "ClaudeCodeAdapter ещё не принимает event_sink/session/round_no из TASK-006"
        ) from exc
    return adapter, sink


def test_recognized_native_line_emits_typed_event(
    tmp_path: Path, make_fake_process
) -> None:
    adapter, sink = _adapter_with_sink(
        '{"type": "content_block_delta", "text": "hello"}', tmp_path, make_fake_process
    )

    turn = anyio.run(adapter.run, "do the thing")

    assert [event.type for event in sink.events] == [EventType.AGENT_TEXT_DELTA]
    assert sink.events[0].payload == {"text": "hello", "raw": False}
    assert sink.events[0].source is EventSource.AUTHOR
    assert sink.events[0].session == "s-42"
    assert sink.events[0].round == 2
    assert turn.text == "hello"


def test_unrecognized_native_line_emits_raw_delta(
    tmp_path: Path, make_fake_process
) -> None:
    adapter, sink = _adapter_with_sink("не json вовсе", tmp_path, make_fake_process)

    turn = anyio.run(adapter.run, "do the thing")

    assert [event.type for event in sink.events] == [EventType.AGENT_TEXT_DELTA]
    assert sink.events[0].payload["raw"] is True
    assert sink.events[0].payload["text"] == "не json вовсе\n"
    assert turn.text == "не json вовсе\n"


_USAGE_LINE = (
    '{"type": "result", "usage": {"output_tokens": 17, "session_id": "sess-abc"}}'
)


def test_tokens_used_and_session_ref_default_to_none_when_absent(
    tmp_path: Path, make_fake_process
) -> None:
    """[REQ-011], [DESIGN-008]: `None` = «CLI не сообщил», не `0`/`""`.

    Проверка идёт в обе стороны намеренно. Половина «без usage-строки →
    `is None`» выполняется и захардкоженным `session_ref=None,
    tokens_used=None` в `run()`, то есть сама по себе ничего не
    доказывает: «по умолчанию None» — утверждение о дефолте, а дефолт
    существует только там, где есть и недефолтная ветка. Поэтому тот же
    адаптер прогоняется по usage-несущей строке — значения обязаны
    доехать. `is None` (а не falsy-check) ловит регрессию к `0`/`""`.
    """
    without_usage, _ = _adapter_with_sink(
        '{"type": "content_block_delta", "text": "hello"}', tmp_path, make_fake_process
    )

    silent = anyio.run(without_usage.run, "do the thing")

    assert silent.tokens_used is None
    assert silent.session_ref is None

    with_usage, _ = _adapter_with_sink(_USAGE_LINE, tmp_path, make_fake_process)

    reported = anyio.run(with_usage.run, "do the thing")

    assert reported.tokens_used == 17
    assert reported.session_ref == "sess-abc"


def test_usage_line_populates_agent_turn_without_polluting_text(
    tmp_path: Path, make_fake_process
) -> None:
    """Регрессия к DESIGN-008: usage-строка — метрика, а не текст ответа."""
    from disputatio.adapters.claude_code import ClaudeCodeAdapter

    sink = _SpySink()
    adapter = ClaudeCodeAdapter(
        role=Role.AUTHOR,
        session_dir=tmp_path,
        launcher=lambda *a, **kw: make_fake_process(
            ['{"type": "content_block_delta", "text": "hello"}', _USAGE_LINE]
        ),
        event_sink=sink,
        session="s-42",
        round_no=2,
    )

    turn = anyio.run(adapter.run, "do the thing")

    assert turn.text == "hello"
    assert turn.tokens_used == 17
    assert turn.session_ref == "sess-abc"
    assert len(sink.events) == 2
    assert sink.events[1].payload["usage"] == {
        "output_tokens": 17,
        "session_id": "sess-abc",
    }
    assert sink.events[1].payload["raw"] is False
