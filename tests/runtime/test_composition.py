"""Composition root — `build_runtime`/`RuntimeDeps` ([REQ-001], [DESIGN-001]).

[TASK-003]. Тест пинит не «объект собрался», а свойства, без которых
composition root перестаёт быть единственной точкой связывания портов
(INV-11):

* поля контейнера удовлетворяют `runtime_checkable` Protocol'ам
  `disputatio.contracts.ports` — цикл видит порты, а не классы;
* `EventSink` физически попадает В конструкторы адаптеров (иначе нативный
  поток CLI не превращается в события §8), а значит создаётся ДО них;
* роли различены (`Role.AUTHOR` / `Role.REVIEWER`) — иначе ревьюер получил
  бы права автора;
* `AdapterCapabilities` руками не конструируется и `reviewer_allowed_tools`
  не переопределяется (NFR-003): read-only §7 остаётся собственностью
  пакета адаптеров;
* неизвестное имя адаптера падает `UnknownAdapterError` ([DESIGN-020]), а не
  `KeyError` в лицо пользователю;
* импорты самого `composition.py` идут только через публичные `__init__`
  пакетов — проверяется AST-сканом исходника.

Символы `disputatio.runtime` берутся через `_attr`/`_composition`, а не
`from ... import` на уровне модуля: до реализации такой импорт уронил бы
коллекцию всего файла, и red-чекпоинт оказался бы ошибкой сборки, а не
падением assertion'ом.
"""

import ast
import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from disputatio.adapters import AdapterCapabilities, ClaudeCodeAdapter, CodexAdapter
from disputatio.contracts import (
    AgentAdapter,
    Event,
    EventSink,
    Mode,
    Role,
    SessionPhase,
    SessionState,
    StateStore,
    VerificationReport,
    Verifier,
)
from disputatio.verifier import GateSpec

_FROZEN_NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
_FROZEN_MONOTONIC = 1234.5

# Пакеты, чьи публичные `__init__` composition root'у импортировать можно.
# Подмодуль (`disputatio.events.paths`) в список не входит намеренно: импорт
# мимо публичного входа возвращает связывание с реализацией обратно в код.
_PUBLIC_PACKAGES = frozenset(
    {
        "disputatio.adapters",
        "disputatio.context",
        "disputatio.contracts",
        "disputatio.core",
        "disputatio.events",
        "disputatio.verifier",
    }
)

_OWN_PACKAGE = "disputatio.runtime"


def _attr(name: str) -> Any:
    """Публичный символ `disputatio.runtime`; отсутствие — AssertionError."""
    import disputatio.runtime as runtime_pkg

    assert hasattr(runtime_pkg, name), (
        f"disputatio.runtime не экспортирует {name!r}: composition root не собран"
    )
    return getattr(runtime_pkg, name)


def _composition() -> ModuleType:
    """Модуль `runtime/composition.py` — материал для скана самого себя."""
    from importlib import import_module

    try:
        return import_module("disputatio.runtime.composition")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/composition.py: {exc}") from exc


def _composition_source() -> str:
    """Исходник `composition.py` в виде текста."""
    path = _composition().__file__
    assert path is not None, "у composition.py нет файла на диске"
    return Path(path).read_text(encoding="utf-8")


@dataclass
class FakeStore:
    """`StateStore`-фейк: журналирует сохранения, ничего не пишет на диск."""

    saved: list[SessionState] = field(default_factory=list)

    def load(self, session_id: str) -> SessionState:
        """Сессии нет — `KeyError`, ровно как у файловой реализации."""
        raise KeyError(session_id)

    def save(self, state: SessionState) -> None:
        """Запоминает состояние вместо записи `session.json`."""
        self.saved.append(state)


@dataclass
class FakeSink:
    """`EventSink`-фейк: складывает события в список."""

    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        """Дописывает событие в память."""
        self.events.append(event)


@dataclass
class FakeVerifier:
    """`Verifier`-фейк: журналирует раунды, отчётов не отдаёт."""

    rounds: list[int] = field(default_factory=list)

    def verify(self, round_no: int) -> VerificationReport:
        """Журналирует раунд; в этом наборе результат не запрашивается."""
        self.rounds.append(round_no)
        raise AssertionError("FakeVerifier: отчёт в этом тесте не нужен")


@dataclass
class FakeGit:
    """`GitOps`-фейк: журналирует вызовы, ни одной git-команды не запускает."""

    calls: list[str] = field(default_factory=list)

    def diff_head(self) -> str:
        """Пустой дифф — валидный ответ (`analyze`-режим)."""
        self.calls.append("diff_head")
        return ""

    def commit_round(self, round_no: int) -> None:
        """Журналирует коммит принятого раунда."""
        self.calls.append(f"commit_round:{round_no}")

    def reset_hard(self, rev: str) -> None:
        """Журналирует сброс к целевому коммиту."""
        self.calls.append(f"reset_hard:{rev}")

    def clean(self) -> None:
        """Журналирует уборку untracked-файлов прерванной попытки."""
        self.calls.append("clean")


def _config(
    *,
    author_adapter: str = "claude_code",
    reviewer_adapter: str = "claude_code",
) -> Any:
    """Конфиг сессии §4.1: два агента, лимиты, один gate."""
    agent_config = _attr("AgentConfig")
    limits_config = _attr("LimitsConfig")
    return _attr("RuntimeConfig")(
        session_id="20260809-120000-a1b2",
        mode=Mode.DEVELOP,
        base_commit="9f1c2ab",
        task_prompt="почини флаки-тест",
        author=agent_config(adapter=author_adapter, model="opus"),
        reviewer=agent_config(adapter=reviewer_adapter, model="sonnet"),
        limits=limits_config(
            max_rounds=5,
            max_total_tokens=400_000,
            max_wall_seconds=3600,
            schema_retries=2,
        ),
        gates=(GateSpec(name="pytest", cmd="uv run pytest -q"),),
    )


def _build(root: Path) -> Any:
    """Композиция на дефолтах: подменён только `git` (его реализация — TASK-004)."""
    return _attr("build_runtime")(_config(), root, git=FakeGit())


def test_runtime_deps_fields_satisfy_ports(tmp_path: Path) -> None:
    """Каждое поле контейнера проходит `isinstance` своего порта ([REQ-001])."""
    deps = _build(tmp_path)

    assert isinstance(deps.store, StateStore)
    assert isinstance(deps.sink, EventSink)
    assert isinstance(deps.author, AgentAdapter)
    assert isinstance(deps.reviewer, AgentAdapter)
    assert isinstance(deps.verifier, Verifier)


def test_runtime_deps_carries_root_and_injected_clocks(tmp_path: Path) -> None:
    """Корни и часы — часть контейнера: цикл не зовёт `datetime.now` сам.

    Корней два (SPEC-002 §4.1), и на дефолте они совпадают: без
    `artifact_root` журнал сессии остаётся в рабочем репозитории.
    """
    deps = _build(tmp_path)

    assert deps.workspace_root == tmp_path
    assert deps.artifact_root == tmp_path
    assert isinstance(deps.now(), datetime)
    assert deps.now().tzinfo is not None
    assert isinstance(deps.monotonic(), float)


def test_runtime_deps_is_frozen(tmp_path: Path) -> None:
    """Контейнер неизменяем: шаг не может подменить зависимость на ходу."""
    deps = _build(tmp_path)

    with pytest.raises(dataclasses.FrozenInstanceError):
        deps.store = FakeStore()


def test_default_sink_is_injected_into_both_adapters(tmp_path: Path) -> None:
    """Адаптеры держат ТОТ ЖЕ объект sink — значит sink создан до них."""
    deps = _build(tmp_path)

    assert isinstance(deps.author, ClaudeCodeAdapter)
    assert isinstance(deps.reviewer, ClaudeCodeAdapter)
    assert deps.author.event_sink is deps.sink
    assert deps.reviewer.event_sink is deps.sink


def test_overridden_sink_reaches_adapters(tmp_path: Path) -> None:
    """Подменённый sink доезжает до адаптеров, а не оседает в поле контейнера."""
    sink = FakeSink()

    deps = _attr("build_runtime")(_config(), tmp_path, sink=sink, git=FakeGit())

    assert deps.sink is sink
    assert deps.author.event_sink is sink
    assert deps.reviewer.event_sink is sink


def test_roles_are_distinguished(tmp_path: Path) -> None:
    """Author создаётся с `Role.AUTHOR`, reviewer — с `Role.REVIEWER`."""
    deps = _build(tmp_path)

    assert deps.author.role is Role.AUTHOR
    assert deps.reviewer.role is Role.REVIEWER


def test_adapters_get_session_id_and_root_as_session_dir(tmp_path: Path) -> None:
    """Оба адаптера работают в `root` и знают id сессии (для событий §8)."""
    config = _config()

    deps = _attr("build_runtime")(config, tmp_path, git=FakeGit())

    assert deps.author.session_dir == tmp_path
    assert deps.reviewer.session_dir == tmp_path
    assert deps.author.session == config.session_id
    assert deps.reviewer.session == config.session_id


def test_reviewer_capabilities_are_package_defaults(tmp_path: Path) -> None:
    """`reviewer_allowed_tools` — дефолт пакета адаптеров, не свой (NFR-003)."""
    package_default = AdapterCapabilities(
        supports_granular_permissions=True
    ).reviewer_allowed_tools

    deps = _build(tmp_path)

    assert deps.reviewer.capabilities.reviewer_allowed_tools == package_default


def test_composition_source_never_names_capabilities() -> None:
    """Исходник composition root'а не упоминает capabilities вовсе (NFR-003).

    Сравнения значений мало: runtime, сконструировавший `AdapterCapabilities`
    с тем же кортежем, прошёл бы его — и молча стал бы вторым владельцем
    прав §7.
    """
    source = _composition_source()

    assert "AdapterCapabilities" not in source
    assert "reviewer_allowed_tools" not in source


def test_each_dependency_is_substitutable(tmp_path: Path) -> None:
    """Любая зависимость подменяется фейком — правок кода цикла не требуется."""
    sink = FakeSink()
    store = FakeStore()
    verifier = FakeVerifier()
    git = FakeGit()

    deps = _attr("build_runtime")(
        _config(),
        tmp_path,
        sink=sink,
        store=store,
        verifier=verifier,
        git=git,
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: _FROZEN_MONOTONIC,
    )

    assert deps.sink is sink
    assert deps.store is store
    assert deps.verifier is verifier
    assert deps.git is git
    assert deps.now() == _FROZEN_NOW
    assert deps.monotonic() == _FROZEN_MONOTONIC


def test_git_port_is_runtime_checkable() -> None:
    """`GitOps` — пятый порт (ADR-005), живущий в runtime: фейк его проходит."""
    assert isinstance(FakeGit(), _attr("GitOps"))


def test_adapter_factories_cover_both_clis() -> None:
    """Реестр адаптеров — плоский dict имён из конфига в фабрики."""
    factories = _attr("ADAPTER_FACTORIES")

    assert set(factories) == {"claude_code", "codex"}
    assert factories["claude_code"] is ClaudeCodeAdapter
    assert factories["codex"] is CodexAdapter


def test_unknown_author_adapter_raises_domain_error(tmp_path: Path) -> None:
    """Опечатка в имени адаптера автора → `UnknownAdapterError` с именем внутри."""
    config = _config(author_adapter="claude-code")

    with pytest.raises(_attr("UnknownAdapterError")) as exc_info:
        _attr("build_runtime")(config, tmp_path, git=FakeGit())

    assert "claude-code" in str(exc_info.value)


def test_unknown_reviewer_adapter_raises_domain_error(tmp_path: Path) -> None:
    """Ревьюер проверяется так же, как автор: своя ветка — свой тест."""
    config = _config(reviewer_adapter="gpt")

    with pytest.raises(_attr("UnknownAdapterError")) as exc_info:
        _attr("build_runtime")(config, tmp_path, git=FakeGit())

    assert "gpt" in str(exc_info.value)


def test_unknown_adapter_error_is_not_a_key_error() -> None:
    """`UnknownAdapterError` — доменная ошибка, а не переодетый `KeyError`.

    Без этого `pytest.raises(UnknownAdapterError)` выше зеленел бы и на
    подклассе `KeyError`, чей `str()` — это repr аргумента в кавычках.
    """
    assert issubclass(_attr("UnknownAdapterError"), _attr("DisputatioError"))
    assert not issubclass(_attr("UnknownAdapterError"), KeyError)


def test_domain_errors_share_one_base() -> None:
    """Иерархия [DESIGN-020]: CLI ловит одну базу и печатает `.args[0]`."""
    base = _attr("DisputatioError")
    names = (
        "DirtyWorkingTree",
        "NotAGitRepository",
        "SessionNotFound",
        "UnknownAdapterError",
        "ConfigError",
        "ReviewParseError",
    )

    for name in names:
        assert issubclass(_attr(name), base), name
    assert issubclass(base, Exception)


def _imported_disputatio_modules(source: str) -> list[str]:
    """Имена импортируемых `disputatio.*`-модулей, включая относительные."""
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, (
                f"относительный импорт в composition.py (строка {node.lineno}) "
                "прячет пакет-источник от скана"
            )
            if node.module is not None:
                names.append(node.module)
    return [name for name in names if name.split(".")[0] == "disputatio"]


def _is_own_package(name: str) -> bool:
    """Свой пакет — `disputatio.runtime` и его подмодули, но не `runtimeX`."""
    return name == _OWN_PACKAGE or name.startswith(f"{_OWN_PACKAGE}.")


def test_composition_imports_only_public_package_inits() -> None:
    """Реализации приходят только через публичные `__init__` пакетов."""
    imported = _imported_disputatio_modules(_composition_source())
    foreign = [name for name in imported if not _is_own_package(name)]

    assert foreign, (
        "скан вакуумен: composition root обязан импортировать реализации "
        "из чужих пакетов"
    )
    offenders = [name for name in foreign if name not in _PUBLIC_PACKAGES]
    assert offenders == []


def test_config_maps_to_initial_session_state() -> None:
    """`to_session_state` — единственный переход конфига в артефакт §4.1."""
    config = _config()

    state = config.to_session_state(created_at=_FROZEN_NOW)

    assert isinstance(state, SessionState)
    assert state.session_id == config.session_id
    assert state.created_at == _FROZEN_NOW
    assert state.state is SessionPhase.IDLE
    assert state.current_round == 0
    assert state.task.prompt == config.task_prompt
    assert state.task.mode is Mode.DEVELOP
    assert state.agents[Role.AUTHOR].adapter == "claude_code"
    assert state.agents[Role.AUTHOR].model == "opus"
    assert state.agents[Role.REVIEWER].model == "sonnet"
    assert state.limits.max_rounds == config.limits.max_rounds
    assert state.limits.schema_retries == config.limits.schema_retries
    assert state.budget_used.tokens == 0
    assert state.budget_used.wall_seconds == 0.0


def test_runtime_config_is_frozen() -> None:
    """Снапшот конфига неизменяем: resume обязан читать то же, что старт."""
    config = _config()

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.base_commit = "deadbeef"
