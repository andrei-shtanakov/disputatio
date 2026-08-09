"""`CodexAdapter` — зеркало `test_claude_code_adapter.py` — TASK-008.

[DESIGN-007], [REQ-004]: тот же внешний контракт `run() -> AgentTurn`, что
у `ClaudeCodeAdapter`, но собственный нативный протокол CLI. Тестовая
логика повторяет claude-модуль один-к-одному; расходятся только фикстуры
нативных строк и ожидаемое argv — ровно та «специфика CLI-протокола»,
которую REQ-004 разрешает не переиспользовать.

[REQ-005]/[REQ-006]: author не сужает инструменты, reviewer с
granular-permissions получает ограничивающий набор без write-инструментов.
[REQ-007]: reviewer без granular-permissions работает в read-only worktree.
[REQ-008]/[REQ-009]: каждая строка `stdout` доезжает до `EventSink` —
распознанная типизированной, любая другая как `agent_text_delta` с
`raw: true`. [REQ-011]: `tokens_used`/`session_ref` — `None`, а не `0`/`""`.

ADR-004: оба значения `supports_granular_permissions` проверяются явно,
поэтому смена дефолта в `codex.py` не оставит один из путей без теста.
[REQ-012]: ни один тест не трогает реальный бинарь `codex` — только
`make_fake_process` из `tests/adapters/conftest.py`.
"""

from pathlib import Path
from typing import Any

import anyio
import pytest

from disputatio.contracts.base import Role
from disputatio.contracts.events import Event, EventSource, EventType

# Фикстуры «нативного» codex-протокола. Реального вывода CLI под рукой
# нет (та же оговорка, что в `claude_code.py`), поэтому конверт выбран
# best-effort и намеренно НЕ совпадает с claude-конвертом: тест обязан
# ловить адаптер, который просто переиспользовал чужой парсер.
_DELTA_LINE = '{"msg": {"type": "agent_message_delta", "delta": "hello"}}'
_USAGE_LINE = (
    '{"msg": {"type": "task_complete", "session_id": "sess-abc",'
    ' "usage": {"output_tokens": 17}}}'
)
# Строка, распознаваемая claude-парсером, но не codex-парсером: у codex
# нет конверта `content_block_delta`, значит она обязана уйти в raw.
_CLAUDE_SHAPED_LINE = '{"type": "content_block_delta", "text": "hello"}'


class _SpySink:
    """Шпион `contracts.ports.EventSink`: копит всё, что ему отдали."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


def _build(**kwargs: Any) -> Any:
    """Конструирует `CodexAdapter`, переводя «ещё не реализовано» в assert.

    На red-чекпоинте `codex.py` — заглушка TASK-002 с конструктором из
    двух аргументов: любой из seam-параметров даёт `TypeError`, который
    red-гейт не засчитал бы как честное падение теста.
    """
    from disputatio.adapters.codex import CodexAdapter

    try:
        return CodexAdapter(**kwargs)
    except TypeError as exc:
        raise AssertionError(
            "CodexAdapter ещё не принимает конструктор-shape ClaudeCodeAdapter "
            "(TASK-008)"
        ) from exc


def _run(adapter: Any, prompt: str = "do the thing") -> Any:
    """Гоняет `run()`, переводя заглушечный `NotImplementedError` в assert."""
    try:
        return anyio.run(adapter.run, prompt)
    except NotImplementedError as exc:
        raise AssertionError("CodexAdapter.run ещё не реализован (TASK-008)") from exc


def test_run_returns_agent_turn_from_fake_stdout(
    tmp_path: Path, make_fake_process
) -> None:
    """[REQ-004] базовый happy-path: тот же порт, свой нативный конверт."""
    from disputatio.contracts.ports import AgentAdapter

    captured_argv: list[str] = []

    def _spy_launcher(*argv: str, **kwargs: object):
        captured_argv.extend(argv)
        return make_fake_process([_DELTA_LINE])

    adapter = _build(
        role=Role.AUTHOR,
        session_dir=tmp_path,
        launcher=_spy_launcher,
    )

    assert isinstance(adapter, AgentAdapter)

    turn = _run(adapter)

    assert turn.text == "hello"
    assert captured_argv[0] == "codex"
    assert "do the thing" in captured_argv


def test_author_role_argv_has_no_allowed_tools_restriction(
    tmp_path: Path, make_fake_process
) -> None:
    """[REQ-005]: author не получает сужающего набора инструментов."""
    from disputatio.adapters.permissions import AdapterCapabilities

    captured_argv: list[str] = []

    def _spy_launcher(*argv: str, **kwargs: object):
        captured_argv.extend(argv)
        return make_fake_process(["ok"])

    adapter = _build(
        role=Role.AUTHOR,
        session_dir=tmp_path,
        capabilities=AdapterCapabilities(supports_granular_permissions=True),
        launcher=_spy_launcher,
    )
    _run(adapter)

    assert "--allowedTools" not in captured_argv


def test_reviewer_role_argv_is_granular_and_excludes_write_tools(
    tmp_path: Path, make_fake_process
) -> None:
    """[REQ-006]: reviewer с granular-permissions — без write-инструментов."""
    from disputatio.adapters.permissions import AdapterCapabilities

    captured_argv: list[str] = []

    def _spy_launcher(*argv: str, **kwargs: object):
        captured_argv.extend(argv)
        return make_fake_process(["ok"])

    adapter = _build(
        role=Role.REVIEWER,
        session_dir=tmp_path,
        capabilities=AdapterCapabilities(supports_granular_permissions=True),
        launcher=_spy_launcher,
    )
    _run(adapter, "review the thing")

    assert "--allowedTools" in captured_argv
    allowed_tools = captured_argv[captured_argv.index("--allowedTools") + 1]
    assert "Write" not in allowed_tools
    assert "Edit" not in allowed_tools


def test_reviewer_without_granular_support_uses_readonly_worktree(
    tmp_path: Path, make_fake_process
) -> None:
    """[REQ-007]: `supports_granular_permissions=False` задан явно, не дефолтом."""
    from disputatio.adapters.permissions import AdapterCapabilities

    session_dir = tmp_path / "session"
    worktree_path = tmp_path / "ro-worktree"
    create_calls: list[Path] = []
    remove_calls: list[Path] = []
    captured_cwd: list[object] = []
    captured_argv: list[str] = []

    async def _create(source: Path) -> Path:
        create_calls.append(source)
        return worktree_path

    async def _remove(path: Path) -> None:
        remove_calls.append(path)

    def _spy_launcher(*argv: str, **kwargs: object):
        captured_argv.extend(argv)
        captured_cwd.append(kwargs.get("cwd"))
        return make_fake_process(["ok"])

    adapter = _build(
        role=Role.REVIEWER,
        session_dir=session_dir,
        capabilities=AdapterCapabilities(supports_granular_permissions=False),
        launcher=_spy_launcher,
        worktree_create=_create,
        worktree_remove=_remove,
    )
    _run(adapter, "review the thing")

    assert create_calls == [session_dir]
    assert captured_cwd == [str(worktree_path)]
    assert str(session_dir) not in captured_cwd
    assert remove_calls == [worktree_path]
    # Read-only обеспечен ФС, а не флагами: сужающего argv здесь быть не должно.
    assert "--allowedTools" not in captured_argv


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
    from disputatio.adapters.permissions import AdapterCapabilities

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

    adapter = _build(
        role=role,
        session_dir=session_dir,
        capabilities=AdapterCapabilities(supports_granular_permissions=granular),
        launcher=_spy_launcher,
        worktree_create=_create,
        worktree_remove=_remove,
    )
    _run(adapter)

    assert create_calls == []
    assert remove_calls == []
    assert captured_cwd == [str(session_dir)]


@pytest.mark.parametrize("granular", [True, False])
def test_reviewer_path_covered_for_both_granular_capability_values(
    granular: bool, tmp_path: Path, make_fake_process
) -> None:
    """ADR-004: оба пути ревьюера покрыты независимо от дефолта `codex.py`.

    Дефолт — задокументированный выбор, который composition root может
    переопределить; поэтому тест не спрашивает у адаптера, какой путь он
    считает «своим», а прогоняет оба и требует ровно одного механизма
    read-only на каждый: флаги ИЛИ worktree, никогда не «ни того, ни
    другого».
    """
    from disputatio.adapters.permissions import AdapterCapabilities

    session_dir = tmp_path / "session"
    worktree_path = tmp_path / "ro-worktree"
    create_calls: list[Path] = []
    captured_argv: list[str] = []
    captured_cwd: list[object] = []

    async def _create(source: Path) -> Path:
        create_calls.append(source)
        return worktree_path

    async def _remove(path: Path) -> None:
        return None

    def _spy_launcher(*argv: str, **kwargs: object):
        captured_argv.extend(argv)
        captured_cwd.append(kwargs.get("cwd"))
        return make_fake_process(["ok"])

    adapter = _build(
        role=Role.REVIEWER,
        session_dir=session_dir,
        capabilities=AdapterCapabilities(supports_granular_permissions=granular),
        launcher=_spy_launcher,
        worktree_create=_create,
        worktree_remove=_remove,
    )
    _run(adapter, "review the thing")

    restricted_by_flags = "--allowedTools" in captured_argv
    restricted_by_worktree = create_calls == [session_dir]

    assert restricted_by_flags is granular
    assert restricted_by_worktree is not granular
    assert captured_cwd == [str(worktree_path if not granular else session_dir)]


def test_codex_default_capabilities_are_constructor_visible(tmp_path: Path) -> None:
    """ADR-004: дефолт — явное, читаемое снаружи поле, а не скрытая ветка."""
    adapter = _build(role=Role.AUTHOR, session_dir=tmp_path)

    assert isinstance(adapter.capabilities.supports_granular_permissions, bool)


def _adapter_with_sink(
    stdout_line: str, tmp_path: Path, make_fake_process
) -> tuple[Any, _SpySink]:
    """`CodexAdapter` (author) с одной строкой `stdout` и шпион-сником."""
    sink = _SpySink()
    adapter = _build(
        role=Role.AUTHOR,
        session_dir=tmp_path,
        launcher=lambda *a, **kw: make_fake_process([stdout_line]),
        event_sink=sink,
        session="s-42",
        round_no=2,
    )
    return adapter, sink


def test_recognized_native_line_emits_typed_event(
    tmp_path: Path, make_fake_process
) -> None:
    """[REQ-008]: распознанный codex-конверт → типизированный `Event`."""
    adapter, sink = _adapter_with_sink(_DELTA_LINE, tmp_path, make_fake_process)

    turn = _run(adapter)

    assert [event.type for event in sink.events] == [EventType.AGENT_TEXT_DELTA]
    assert sink.events[0].payload == {"text": "hello", "raw": False}
    assert sink.events[0].source is EventSource.AUTHOR
    assert sink.events[0].session == "s-42"
    assert sink.events[0].round == 2
    assert turn.text == "hello"


@pytest.mark.parametrize("line", ["не json вовсе", _CLAUDE_SHAPED_LINE])
def test_unrecognized_native_line_emits_raw_delta(
    line: str, tmp_path: Path, make_fake_process
) -> None:
    """[REQ-009]: чужой/битый конверт не теряется, а уходит в `raw: true`."""
    adapter, sink = _adapter_with_sink(line, tmp_path, make_fake_process)

    turn = _run(adapter)

    assert [event.type for event in sink.events] == [EventType.AGENT_TEXT_DELTA]
    assert sink.events[0].payload["raw"] is True
    assert sink.events[0].payload["text"] == f"{line}\n"
    assert turn.text == f"{line}\n"


def test_tokens_used_and_session_ref_default_to_none_when_absent(
    tmp_path: Path, make_fake_process
) -> None:
    """[REQ-011], [DESIGN-008]: `None` = «CLI не сообщил», не `0`/`""`.

    Проверка двусторонняя: половина «нет usage-строки → `is None`»
    проходит и на захардкоженных `None`, то есть сама по себе ничего не
    доказывает — дефолт существует только там, где есть недефолтная
    ветка. Поэтому тот же адаптер прогоняется по usage-несущей строке.
    """
    without_usage, _ = _adapter_with_sink(_DELTA_LINE, tmp_path, make_fake_process)

    silent = _run(without_usage)

    assert silent.tokens_used is None
    assert silent.session_ref is None

    with_usage, _ = _adapter_with_sink(_USAGE_LINE, tmp_path, make_fake_process)

    reported = _run(with_usage)

    assert reported.tokens_used == 17
    assert reported.session_ref == "sess-abc"


def test_usage_line_populates_agent_turn_without_polluting_text(
    tmp_path: Path, make_fake_process
) -> None:
    """[DESIGN-008]: usage-строка — метрика, а не текст ответа агента."""
    sink = _SpySink()
    adapter = _build(
        role=Role.AUTHOR,
        session_dir=tmp_path,
        launcher=lambda *a, **kw: make_fake_process([_DELTA_LINE, _USAGE_LINE]),
        event_sink=sink,
        session="s-42",
        round_no=2,
    )

    turn = _run(adapter)

    assert turn.text == "hello"
    assert turn.tokens_used == 17
    assert turn.session_ref == "sess-abc"
    assert len(sink.events) == 2
    assert sink.events[1].payload["usage"] == {
        "output_tokens": 17,
        "session_id": "sess-abc",
    }
    assert sink.events[1].payload["raw"] is False
