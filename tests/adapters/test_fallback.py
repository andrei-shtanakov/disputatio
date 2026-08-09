"""Read-only fallback workspace (`fallback.py`) — TASK-005, [DESIGN-004], ADR-002.

Тесты с фейковыми `create`/`remove` (никакого реального git/subprocess) —
REQ-007's acceptance shape: вход в контекст вызывает `create(session_dir)`
ровно один раз и возвращает его результат, выход — `remove(worktree_path)`
ровно один раз, включая случай исключения внутри `with`.
"""

from pathlib import Path

import anyio


def test_enter_calls_create_once_and_returns_result(tmp_path: Path) -> None:
    try:
        from disputatio.adapters.fallback import ReadOnlyWorkspace
    except ImportError as exc:
        raise AssertionError("ReadOnlyWorkspace ещё не реализован из TASK-005") from exc

    create_calls: list[Path] = []
    worktree_path = tmp_path / "worktree"

    async def _create(session_dir: Path) -> Path:
        create_calls.append(session_dir)
        return worktree_path

    async def _remove(path: Path) -> None:
        pass

    async def _scenario() -> Path:
        async with ReadOnlyWorkspace(
            tmp_path, create=_create, remove=_remove
        ) as yielded:
            return yielded

    yielded = anyio.run(_scenario)

    assert create_calls == [tmp_path]
    assert yielded == worktree_path


def test_exit_calls_remove_once_even_when_body_raises(tmp_path: Path) -> None:
    try:
        from disputatio.adapters.fallback import ReadOnlyWorkspace
    except ImportError as exc:
        raise AssertionError("ReadOnlyWorkspace ещё не реализован из TASK-005") from exc

    remove_calls: list[Path] = []
    worktree_path = tmp_path / "worktree"

    async def _create(session_dir: Path) -> Path:
        return worktree_path

    async def _remove(path: Path) -> None:
        remove_calls.append(path)

    class _Boom(Exception):
        pass

    async def _scenario() -> None:
        async with ReadOnlyWorkspace(tmp_path, create=_create, remove=_remove):
            raise _Boom("boom")

    try:
        anyio.run(_scenario)
    except _Boom:
        pass
    else:
        raise AssertionError("исключение из тела `with` должно распространиться")

    assert remove_calls == [worktree_path]
