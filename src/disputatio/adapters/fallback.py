"""Read-only fallback workspace ([DESIGN-004], ADR-002, [REQ-007]).

Когда CLI не умеет granular-permissions (`AdapterCapabilities.
supports_granular_permissions is False`), read-only ревьюера (SPEC-001 §7)
обеспечивает файловая система, а не флаги: адаптер запускает CLI в
детач-worktree, а не в рабочем каталоге сессии.

Создание/удаление worktree инжектируются как `create`/`remove` — тем же
приёмом, что `launcher` в `_process.py`: тесты подставляют шпионов и не
трогают ни git, ни диск. Реальные реализации (`git worktree add --detach`,
`chmod 0o555` по дереву, `git worktree remove --force`) — забота
composition root'а (w-runtime, INV-11), не этого пакета.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType

WorktreeCreate = Callable[[Path], Awaitable[Path]]
"""`session_dir -> worktree_path`: создаёт read-only worktree."""

WorktreeRemove = Callable[[Path], Awaitable[None]]
"""`worktree_path -> None`: снимает worktree, созданный `WorktreeCreate`."""


class ReadOnlyWorkspace:
    """Async-контекст: детач read-only worktree для fallback-ревьюера."""

    def __init__(
        self,
        session_dir: Path,
        *,
        create: WorktreeCreate,
        remove: WorktreeRemove,
    ) -> None:
        self.session_dir = session_dir
        self._create = create
        self._remove = remove
        self._worktree: Path | None = None

    async def __aenter__(self) -> Path:
        """Создаёт worktree и отдаёт его путь под `cwd` подпроцесса."""
        worktree = await self._create(self.session_dir)
        self._worktree = worktree
        return worktree

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Снимает worktree ровно один раз, в т.ч. когда тело `with` упало.

        Путь обнуляется ДО `remove`, поэтому повторный выход (или выход без
        успешного входа) не приводит ко второму удалению. Исключение из тела
        не подавляется: возвращаем `None`.

        Падение самого `remove` не вытесняет исключение тела — иначе вызывающий
        (оркестратор) поймал бы ошибку уборки вместо настоящей ошибки агента.
        Уборочное исключение при этом не теряется: оно оседает заметкой на
        исходном (`add_note`) и видно в traceback.
        """
        worktree = self._worktree
        if worktree is None:
            return
        self._worktree = None
        try:
            await self._remove(worktree)
        except Exception as cleanup_exc:
            if exc is None:
                raise
            exc.add_note(f"cleanup: worktree {worktree} не снят: {cleanup_exc!r}")
