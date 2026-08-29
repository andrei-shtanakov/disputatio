"""Composition root — единственная точка связывания портов ([DESIGN-001]).

Здесь и только здесь имена конкретных реализаций встречаются вне их
собственных пакетов (INV-11). Результат — `RuntimeDeps`: frozen-контейнер,
все поля которого удовлетворяют `runtime_checkable` Protocol'ам
`disputatio.contracts.ports`. Цикл (`runtime/loop.py`, `runtime/steps.py`)
принимает контейнер и не знает, чем он наполнен — подмена любой зависимости
фейком не требует ни одной правки в коде шагов ([REQ-001]).

Три инварианта сборки, каждый со своим тестом:

1. **Sink создаётся раньше адаптеров** и инжектится в их конструкторы
   (`event_sink=`). Это единственный способ, которым нативный поток CLI
   превращается в события §8: порт `AgentAdapter` стриминга не описывает.
2. **Роли различены** — автор получает `Role.AUTHOR`, ревьюер
   `Role.REVIEWER`; из роли пакет адаптеров сам выводит права §7.
3. **Права ревьюера не переопределяются** (NFR-003): фабрике не передаётся
   ничего, кроме роли, каталога, sink'а и id сессии, поэтому read-only
   остаётся собственностью пакета адаптеров и меняется вместе с ним.

Импорты чужих пакетов идут только через их публичные `__init__` — это
пинится AST-сканом этого файла в `tests/runtime/test_composition.py`.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from disputatio.adapters import ClaudeCodeAdapter, CodexAdapter
from disputatio.contracts import AgentAdapter, EventSink, Role, StateStore, Verifier
from disputatio.events import FileStateStore, JsonlEventSink
from disputatio.runtime.config import RuntimeConfig
from disputatio.runtime.errors import ConfigError, UnknownAdapterError
from disputatio.runtime.git import GitOps
from disputatio.verifier import VerifierRunner

AdapterFactory = Callable[..., AgentAdapter]
"""Фабрика адаптера: вызывается только именованными аргументами сборки."""

ADAPTER_FACTORIES: Mapping[str, AdapterFactory] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}


@dataclass(frozen=True, slots=True)
class RuntimeDeps:
    """Связка портов с реализациями — результат единственной композиции.

    Корней два, и они названы порознь (SPEC-002 §4.1). `workspace_root` —
    рабочий git-репозиторий: из него запускаются агентские CLI, по нему
    считается `changes.patch` и по нему же прогоняются гейты. `artifact_root`
    — журнал сессии: `.disputatio/` со состоянием, событиями, раундами и
    экспортом. До разделения это был один параметр `root`, и он не
    различался; пайплайн кладёт несколько сессий под ОДИН репозиторий, и
    единственное поле свело бы их в общий `session.json`. Имени `root` здесь
    больше нет намеренно: молча перепутать два корня можно было только пока
    один из них назывался как оба.
    """

    workspace_root: Path
    artifact_root: Path
    store: StateStore
    sink: EventSink
    author: AgentAdapter
    reviewer: AgentAdapter
    verifier: Verifier
    git: GitOps
    now: Callable[[], datetime]
    monotonic: Callable[[], float]


def _utcnow() -> datetime:
    """Часы сессии по умолчанию: aware-UTC, как требует схема артефактов."""
    return datetime.now(UTC)


def build_runtime(
    config: RuntimeConfig,
    root: Path,
    *,
    artifact_root: Path | None = None,
    git: GitOps,
    sink: EventSink | None = None,
    store: StateStore | None = None,
    verifier: Verifier | None = None,
    now: Callable[[], datetime] = _utcnow,
    monotonic: Callable[[], float] = time.monotonic,
) -> RuntimeDeps:
    """Собирает `RuntimeDeps` из конфига сессии ([REQ-001]).

    Каждый порт имеет override-параметр: тест подменяет любую зависимость
    фейком, не трогая ни одной строки цикла. `git` пока обязателен —
    реализация `GitCli` приходит с [DESIGN-010]; дефолт по умолчанию,
    указывающий на несуществующий класс, превратился бы в отложенный
    `ImportError` в момент старта сессии.

    `root` — рабочий git-репозиторий; `artifact_root` — журнал сессии
    (SPEC-002 §4.1). `None` означает `artifact_root = root`, то есть
    раскладку до разделения байт-в-байт: `disp run`/`disp resume` второго
    корня не знают, и знать им его незачем — одна сессия на репозиторий
    остаётся законным случаем. Разводит корни только пайплайн, у которого
    сессий под одним репозиторием несколько.

    Кто какой корень получает — не деталь сборки, а само разделение:
    `JsonlEventSink` и `FileStateStore` пишут журнал, поэтому им уходит
    `artifact_root`; адаптеры (`session_dir` — их рабочая директория) и
    `VerifierRunner` (гейты идут по коду) работают с репозиторием, поэтому им
    уходит `root`. Перепутай эти две строки — сессия писала бы состояние
    туда, где его никто не ищет, а гейты гонялись бы по журналу.

    Порядок сборки значим: sink создаётся ПЕРЕД адаптерами, потому что
    попадает в их конструкторы. Соберись адаптеры первыми — они получили бы
    `event_sink=None`, и поток §8 молча исчез бы: адаптер без sink'а
    работает, просто ничего не транслирует.
    """
    journal_root = artifact_root if artifact_root is not None else root
    event_sink = sink if sink is not None else JsonlEventSink(journal_root)
    return RuntimeDeps(
        workspace_root=root,
        artifact_root=journal_root,
        store=store if store is not None else FileStateStore(journal_root),
        sink=event_sink,
        author=_build_adapter(
            config.author.adapter,
            role=Role.AUTHOR,
            root=root,
            sink=event_sink,
            session_id=config.session_id,
        ),
        reviewer=_build_adapter(
            config.reviewer.adapter,
            role=Role.REVIEWER,
            root=root,
            sink=event_sink,
            session_id=config.session_id,
        ),
        verifier=(
            verifier
            if verifier is not None
            else VerifierRunner(list(config.gates), root)
        ),
        git=git,
        now=now,
        monotonic=monotonic,
    )


def _build_adapter(
    name: str,
    *,
    role: Role,
    root: Path,
    sink: EventSink,
    session_id: str,
) -> AgentAdapter:
    """Создаёт адаптер `name` для роли `role` с инжектированным sink'ом.

    Фабрике передаются ровно четыре аргумента: всё остальное — дефолты
    пакета адаптеров. Любой пятый (права, флаги CLI) означал бы, что runtime
    завёл второе мнение о §7 (NFR-003).

    Фабрика вправе отказаться собирать пару «адаптер + роль»: `codex`
    ревьюером требует read-only worktree (ADR-004), которого на дефолтах нет.
    Имя зарегистрировано в реестре, то есть легально в `config.toml`, —
    значит отказ обязан прийти в иерархии [DESIGN-020], а не голым
    `ValueError` из чужого пакета. Причина не переписывается, а цитируется:
    почему именно роль не собирается, знает пакет адаптеров, не runtime.
    """
    factory = ADAPTER_FACTORIES.get(name)
    if factory is None:
        known = ", ".join(sorted(ADAPTER_FACTORIES))
        raise UnknownAdapterError(
            f"неизвестный адаптер {name!r} для роли {role.value}; известны: {known}"
        )
    try:
        return factory(
            role=role,
            session_dir=root,
            event_sink=sink,
            session=session_id,
        )
    except ValueError as exc:
        raise ConfigError(
            f"адаптер {name!r} не собирается для роли {role.value}: {exc}"
        ) from exc
