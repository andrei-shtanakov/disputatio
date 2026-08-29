"""Общий базис фейков порта `GitOps` для `tests/runtime/**`.

`GitOps` — `runtime_checkable`-протокол, и его расширение семью операциями
adoption/reconciliation (SPEC-002 §3.1, §7.3, §8.1) и предусловий `run`
(§3.1, задача 13) обязано было уронить каждый фейк порта разом: `isinstance`
смотрит на наличие атрибутов, а pyrefly — ещё и на совместимость сигнатур в
`RuntimeDeps(git=…)`. Дописывать семь заглушек в двадцать с лишним фейков
значило бы завести двадцать копий одного решения, расходящихся при
следующем расширении порта; поэтому заглушки живут здесь, а фейлы наследуют
их.

**Любая из семи операций — провал теста, а не тихий ответ.** Ни один шаг
цикла SPEC-001 их не вызывает: они принадлежат операторским решениям и
сверке worktree на resume. Дефолт, возвращающий правдоподобное значение,
скрыл бы обращение к git из шага, которому туда ходить не положено, — а
именно это и проверяет половина набора (`NoGit`-фейки). Тесту, которому эти
операции понадобятся по существу, полагается переопределить нужную и
объявить в докстринге, зачем.

Модуль назван с подчёркивания: pytest собирает `test_*.py`, а помощник
набором не является. Импорт — относительный (`from ._fakes import …`):
`tests/runtime/` — пакет (`__init__.py`), а `tests/` — нет, поэтому в
`sys.path` попадает `tests/`, и абсолютным именем модуля был бы
`runtime._fakes`, неотличимый на глаз от `disputatio.runtime`.
"""

from collections.abc import Sequence

from disputatio.runtime import StatusEntry


class GitOpsFakeBase:
    """Шесть операций SPEC-002 в порту `GitOps`: вызов — провал теста.

    Наследуется фейками цикла SPEC-001, которые реализуют свои четыре
    метода (`diff_head`, `commit_round`, `reset_hard`, `clean`) сами.
    Базис намеренно не даёт заглушек и для них: у каждого фейка своя
    договорённость на этот счёт — один журналирует вызов, другой его
    запрещает, — и общий дефолт подменил бы её молча.
    """

    def head_sha(self) -> str:
        """Не вызывается циклом SPEC-001 — обращение к git значит ошибку."""
        raise AssertionError(
            "GitOps.head_sha вызван фейком: шаги цикла SPEC-001 идентичность "
            "HEAD не спрашивают (SPEC-002 §8.1)"
        )

    def current_branch(self) -> str | None:
        """Не вызывается циклом SPEC-001 — обращение к git значит ошибку."""
        raise AssertionError(
            "GitOps.current_branch вызван фейком: имя ветки нужно только "
            "предусловию старта пайплайна (SPEC-002 §3.1)"
        )

    def status_entries(self) -> tuple[StatusEntry, ...]:
        """Не вызывается циклом SPEC-001 — обращение к git значит ошибку."""
        raise AssertionError(
            "GitOps.status_entries вызван фейком: статус дерева разбирают "
            "предусловие старта и scope adoption'а (SPEC-002 §3.1)"
        )

    def diff_readonly(self) -> str:
        """Не вызывается циклом SPEC-001 — обращение к git значит ошибку."""
        raise AssertionError(
            "GitOps.diff_readonly вызван фейком: немутирующий дифф — это "
            "сверка worktree на resume (SPEC-002 §8.1), а не шаг раунда"
        )

    def commit_paths(self, paths: Sequence[str], subject: str, *, trailer: str) -> str:
        """Не вызывается циклом SPEC-001 — обращение к git значит ошибку."""
        raise AssertionError(
            f"GitOps.commit_paths вызван фейком (пути {list(paths)}, заголовок "
            f"{subject!r}, операция {trailer!r}): операторский чекпоинт пишет "
            "adoption, а не раунд (SPEC-002 §3.1)"
        )

    def find_commit_by_trailer(self, trailer: str) -> str | None:
        """Не вызывается циклом SPEC-001 — обращение к git значит ошибку."""
        raise AssertionError(
            f"GitOps.find_commit_by_trailer вызван фейком (операция "
            f"{trailer!r}): чекпоинт по трейлеру ищет повтор adoption'а "
            "(SPEC-002 §3.1)"
        )

    def toplevel_prefix(self) -> str:
        """Не вызывается циклом SPEC-001 — обращение к git значит ошибку."""
        raise AssertionError(
            "GitOps.toplevel_prefix вызван фейком: нормализация базы путей "
            "нужна только предусловию старта `run` и scope adoption'а "
            "(SPEC-002 §3.1)"
        )
