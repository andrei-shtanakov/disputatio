"""Порт git-операций ([DESIGN-011], [DESIGN-013], ADR-005).

`GitOps` — пятый порт оркестратора, объявленный в `runtime`, а не в
замороженных `contracts`: git-дисциплина §3 принадлежит циклу, а не схемам
артефактов. Здесь только интерфейс — конкретная `GitCli` и `preflight`
приходят с [DESIGN-010]…[DESIGN-012]; composition root связывает поле
`RuntimeDeps.git` уже сейчас, потому что без типа порта контейнер
зависимостей нельзя ни объявить, ни подменить фейком ([REQ-001]).
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class GitOps(Protocol):
    """Порт git-операций рабочего репозитория (SPEC-001 §3)."""

    def diff_head(self) -> str:
        """`git diff HEAD` — дифф рабочего дерева; пустая строка валидна."""
        ...

    def commit_round(self, round_no: int) -> None:
        """Коммитит принятый раунд сообщением `disputatio: round NNN`."""
        ...

    def reset_hard(self, rev: str) -> None:
        """`git reset --hard <rev>` — откат к коммиту прошлого раунда."""
        ...

    def clean(self) -> None:
        """Удаляет untracked-файлы прерванной попытки, сохраняя `.disputatio/`."""
        ...
