"""Resume и снапшот `config.toml` ([REQ-014], [DESIGN-014], [TASK-016]).

Восстановление сессии — это не отдельная логика, а два файла, переживших
перезапуск процесса: `session.json` называет фазу, с которой продолжать, а
`config.toml` — конфиг, которым сессия была запущена. Поэтому проверяются
ровно две вещи, и обе на наблюдаемых границах:

* **Диспетчеризация по фазе.** Сессия, поднятая в `VERIFYING` раунда 2,
  начинает с прогона гейтов ЭТОГО раунда; автора никто не зовёт заново.
  Пинится общим spy-логом портов, а не набором «вызвано/не вызвано»:
  «PROPOSING переигран» — это лишняя метка в начале лога, и увидеть её
  можно только по порядку.
* **Источник конфига.** В окружении лежит ЗАВЕДОМО другой конфиг: другие
  имена адаптеров, другой гейт, другой `base_commit`. Каждое из трёх полей
  наблюдаемо (какую фабрику дёрнула композиция, какие `gate_started` ушли в
  журнал, на что сброшено дерево), и если хоть одно придёт из окружения,
  тест это назовёт — а `base_commit` окружения ещё и уронил бы `base_rev`,
  потому что такого коммита в истории нет.

Round-trip TOML пинится побайтово. Писателя TOML в зависимостях нет
(`tomllib` только читает), сериализатор пишется руками (ADR-003) — а ручное
экранирование ломается ровно на том, чего в примере из спеки нет: кавычка,
обратный слэш, перевод строки внутри `task.prompt`. Поэтому round-trip
гоняется на враждебном конфиге, а не на образцовом, и разбирается ещё и
сторонним `tomllib`: пара «сломанный писатель + сочувствующий читатель»
прошла бы round-trip, оставив на диске текст, который больше никто не примет.

`render_toml`/`from_toml`/`resume_session` до реализации не существуют,
поэтому доступ к ним идёт через `_attr`: отсутствие символа переводится в
`AssertionError`, а не в ошибку коллекции — red-чекпоинт обязан быть
падением assertion'ом.
"""

import json
import subprocess
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest

from disputatio.contracts import (
    AgentRef,
    AgentTurn,
    BudgetUsed,
    DiffStats,
    Event,
    EventType,
    GateResult,
    GateStatus,
    Issue,
    Limits,
    Mode,
    OverallStatus,
    Review,
    Role,
    SessionPhase,
    SessionState,
    Severity,
    TaskSpec,
    Verdict,
    VerificationReport,
)
from disputatio.core import SessionFsm
from disputatio.events import (
    FileStateStore,
    bootstrap_session,
    write_config_snapshot,
)
from disputatio.runtime import (
    AgentConfig,
    ConfigError,
    LimitsConfig,
    RuntimeConfig,
    SessionNotFound,
    build_runtime,
)
from disputatio.runtime.layout import session_dir
from disputatio.runtime.steps import StepContext
from disputatio.verifier import GateSpec

_FROZEN_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "20260810-120000-a1b2"

# Имена адаптеров в реестре композиции. Оба легальны и оба зарегистрированы
# фейками; различает их только источник конфига — композиция берёт то имя,
# которое назвал доехавший до неё `RuntimeConfig`.
_SNAPSHOT_ADAPTER = "snapshot_cli"
_ENV_ADAPTER = "env_cli"

_SNAPSHOT_GATE = "снапшот-pytest"
_ENV_GATE = "окруженческий-ruff"

# `base_commit` конфига окружения: сорок нулей в истории тестового
# репозитория не разрешаются, поэтому его протечка в раунд 1 не осталась бы
# незамеченной даже без явного assert'а.
_ENV_BASE_COMMIT = "0" * 40

_AUTHOR_CALL = "author.run"
_REVIEWER_CALL = "reviewer.run"
_VERIFIER_CALL = "verifier.verify"
_AGENT_CALLS = frozenset({_AUTHOR_CALL, _REVIEWER_CALL, _VERIFIER_CALL})

# Ревизия подменённого `base_rev` — для тестов, где настоящая история git не
# при чём: [DESIGN-012] пинится своей задачей.
_FIXED_BASE_REV = "resolved-base-rev"

_PATCH = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-СТАРО\n+НОВО\n"

# Враждебный конфиг round-trip'а: кавычки, обратные слэши, перевод строки,
# табуляция и возврат каретки — ровно то, на чём ломается ручное
# экранирование, и ровно то, чего нет в образце [DESIGN-014].
_HOSTILE_PROMPT = (
    'Почини экспорт: строка с "кавычками", путь C:\\tmp\\выгрузка и\n'
    "перевод строки внутри задачи.\n"
    "Табуляция:\tи возврат каретки:\r конец."
)

# Снапшот раскладки [DESIGN-014] — эталон рендера, а не просто валидный TOML.
# Разъедься раскладка с этим текстом, и `config.toml` перестал бы читаться
# глазами в отчёте об инциденте, ради чего он и текстовый.
_COMPLETE_TOML = """[session]
id = "s"
mode = "develop"
base_commit = "abc"

[task]
prompt = "p"
attachments = []

[limits]
max_rounds = 5
max_total_tokens = 100000
max_wall_seconds = 600
schema_retries = 1

[agents.author]
adapter = "claude_code"
model = "opus"

[agents.reviewer]
adapter = "claude_code"
model = "sonnet"

[[gates]]
name = "pytest"
cmd = "uv run pytest -q"
enabled = true
"""

_REVIEWER_BLOCK = '[agents.reviewer]\nadapter = "claude_code"\nmodel = "sonnet"\n'


class StepBoom(Exception):
    """Обрыв внутри тела шага — «процесс умер, не дойдя до результата».

    Не из `SCHEMA_INVALID_ERRORS`: schema-retry лечит вывод агента не той
    формы, а здесь моделируется отказ самого порта.
    """


def _attr(owner: object, name: str, *, what: str) -> Any:
    """Символ `owner.name`; отсутствие — `AssertionError`, не `AttributeError`."""
    assert hasattr(owner, name), f"{what} не определяет {name!r}"
    return getattr(owner, name)


def _loop() -> ModuleType:
    """Модуль `runtime/loop.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.loop")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/loop.py: {exc}") from exc


def _render(config: RuntimeConfig) -> str:
    """`config.render_toml()` — снапшот сессии как текст."""
    text: str = _attr(config, "render_toml", what="RuntimeConfig")()
    return text


def _from_toml(text: str) -> RuntimeConfig:
    """`RuntimeConfig.from_toml(text)` — разбор снапшота."""
    factory = _attr(RuntimeConfig, "from_toml", what="RuntimeConfig")
    parsed: RuntimeConfig = factory(text)
    return parsed


def _resume(root: Path, session_id: str, **overrides: Any) -> SessionState:
    """`resume_session(root, session_id, **overrides)` под `anyio.run`."""
    resume_session = _attr(_loop(), "resume_session", what="runtime/loop.py")

    async def call() -> SessionState:
        result: SessionState = await resume_session(root, session_id, **overrides)
        return result

    return anyio.run(call)


def _drive(ctx: StepContext) -> SessionState:
    """`drive(ctx)` под `anyio.run` — холодная половина сравнения с resume."""
    drive = _attr(_loop(), "drive", what="runtime/loop.py")

    async def call() -> SessionState:
        result: SessionState = await drive(ctx)
        return result

    return anyio.run(call)


@dataclass
class SpyStore:
    """`StateStore` поверх настоящего `FileStateStore` + общий лог порядка."""

    inner: FileStateStore
    log: list[str]
    saved: list[SessionState] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)

    def load(self, session_id: str) -> SessionState:
        """Читает состояние настоящим хранилищем, отмечая запрошенный id."""
        self.loaded.append(session_id)
        return self.inner.load(session_id)

    def save(self, state: SessionState) -> None:
        """Пишет `session.json` и отмечает в логе фазу сохранённого шага."""
        self.inner.save(state)
        self.saved.append(state)
        self.log.append(f"save:{state.state.value}")


@dataclass
class SpySink:
    """`EventSink`-фейк: события в список, метка типа — в общий лог."""

    log: list[str]
    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        """Запоминает событие вместо дописывания в `events.jsonl`."""
        self.events.append(event)
        self.log.append(f"emit:{event.type.value}")


@dataclass
class SpyAdapter:
    """`AgentAdapter`-фейк: журналирует вызов и падает на `fail_at`-м."""

    name: str
    log: list[str]
    replies: list[str] = field(default_factory=list)
    fail_at: int | None = None
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Отмечает вызов, затем падает либо отдаёт следующий ответ очереди."""
        self.prompts.append(prompt)
        self.log.append(f"{self.name}.run")
        if self.fail_at is not None and len(self.prompts) >= self.fail_at:
            raise StepBoom(f"{self.name}: обрыв на вызове {len(self.prompts)}")
        assert self.replies, f"SpyAdapter {self.name}: очередь ответов исчерпана"
        return AgentTurn(text=self.replies.pop(0), session_ref=session_ref)


@dataclass
class SpyVerifier:
    """`Verifier`-фейк: журналирует прогон и падает, если `fails`."""

    log: list[str]
    fails: bool = False
    rounds: list[int] = field(default_factory=list)

    def verify(self, round_no: int) -> VerificationReport:
        """Отмечает прогон гейтов раунда и отдаёт зелёный отчёт."""
        self.log.append(_VERIFIER_CALL)
        self.rounds.append(round_no)
        if self.fails:
            raise StepBoom(f"верификатор: обрыв на раунде {round_no}")
        return _verification(round_no)


@dataclass
class SpyGit:
    """`GitOps`-фейк: журналирует операции, рабочего дерева не трогает."""

    log: list[str]
    commits: list[int] = field(default_factory=list)
    resets: list[str] = field(default_factory=list)

    def diff_head(self) -> str:
        """Отдаёт патч раунда — содержимое здесь роли не играет."""
        self.log.append("git.diff_head")
        return _PATCH

    def commit_round(self, round_no: int) -> None:
        """Журналирует фиксацию принятого раунда."""
        self.log.append("git.commit_round")
        self.commits.append(round_no)

    def reset_hard(self, rev: str) -> None:
        """Журналирует сброс дерева на цель раунда."""
        self.log.append("git.reset_hard")
        self.resets.append(rev)

    def clean(self) -> None:
        """Журналирует уборку untracked-файлов прерванной попытки."""
        self.log.append("git.clean")


@dataclass
class Bench:
    """Собранное окружение resume: спаи, общий лог и журнал сборки адаптеров."""

    root: Path
    log: list[str]
    store: SpyStore
    sink: SpySink
    verifier: SpyVerifier
    git: SpyGit
    built: list[tuple[str, str]]

    def overrides(self) -> dict[str, Any]:
        """Именованные подмены портов для `resume_session`/`build_runtime`."""
        return {
            "store": self.store,
            "sink": self.sink,
            "verifier": self.verifier,
            "git": self.git,
            "now": lambda: _FROZEN_NOW,
            "monotonic": lambda: 0.0,
        }


def _proposal(round_no: int) -> str:
    """Валидный `proposal.md` раунда `round_no` — ответ автора."""
    return (
        "---\n"
        "schema: disputatio/v1\n"
        f"round: {round_no}\n"
        "role: author\n"
        "responds_to: null\n"
        "files_touched:\n"
        "  - feature.py\n"
        "self_declared_status: complete\n"
        "---\n"
        f"Работа раунда {round_no:03d}.\n"
    )


def _review_reply(round_no: int) -> str:
    """Ответ ревьюера: `request_changes` с major-замечанием и evidence."""
    marker = f"{round_no:03d}"
    review = Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.8,
        issues=[
            Issue(
                id=f"I-{marker}",
                severity=Severity.MAJOR,
                file=f"feature{marker}.py",
                claim=f"замечание раунда {marker}",
                evidence=f"feature{marker}.py:1 — свидетельство раунда {marker}",
            )
        ],
        checked=[f"feature{marker}.py"],
        summary=f"свод раунда {marker}",
    )
    return review.model_dump_json(by_alias=True)


def _verification(round_no: int) -> VerificationReport:
    """Зелёный отчёт проверок раунда `round_no`."""
    return VerificationReport(
        round=round_no,
        gates=[
            GateResult(
                name=_SNAPSHOT_GATE,
                cmd="uv run pytest -q",
                status=GateStatus.PASS,
                exit_code=0,
                duration_s=1.25,
                tail=f"вывод гейта раунда {round_no:03d}",
            )
        ],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=1, deletions=1),
    )


def _config(
    *,
    base_commit: str,
    adapter: str = _SNAPSHOT_ADAPTER,
    gate_name: str = _SNAPSHOT_GATE,
    prompt: str = "Почини экспорт CSV",
) -> RuntimeConfig:
    """`RuntimeConfig` сессии; всё различимое вынесено в аргументы."""
    return RuntimeConfig(
        session_id=_SESSION_ID,
        mode=Mode.DEVELOP,
        base_commit=base_commit,
        task_prompt=prompt,
        author=AgentConfig(adapter=adapter, model="opus"),
        reviewer=AgentConfig(adapter=adapter, model="sonnet"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        gates=(GateSpec(name=gate_name, cmd="uv run pytest -q", enabled=True),),
        attachments=(),
    )


def _layout_config() -> RuntimeConfig:
    """Конфиг, чей рендер обязан совпасть с `_COMPLETE_TOML` побайтово."""
    return RuntimeConfig(
        session_id="s",
        mode=Mode.DEVELOP,
        base_commit="abc",
        task_prompt="p",
        author=AgentConfig(adapter="claude_code", model="opus"),
        reviewer=AgentConfig(adapter="claude_code", model="sonnet"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        gates=(GateSpec(name="pytest", cmd="uv run pytest -q", enabled=True),),
        attachments=(),
    )


def _hostile_config() -> RuntimeConfig:
    """Конфиг с кавычками, слэшами и переводами строк во всех строковых полях."""
    return RuntimeConfig(
        session_id='ses"sion\\01',
        mode=Mode.ANALYZE,
        base_commit="9f1c2ab",
        task_prompt=_HOSTILE_PROMPT,
        author=AgentConfig(adapter="claude_code", model='opus"4\\5'),
        reviewer=AgentConfig(adapter="codex", model="sonnet"),
        limits=LimitsConfig(
            max_rounds=7,
            max_total_tokens=400_000,
            max_wall_seconds=3600,
            schema_retries=2,
        ),
        gates=(
            GateSpec(name="pytest", cmd='uv run pytest -q -k "не тот"', enabled=True),
            GateSpec(
                name="ruff", cmd="ruff check . --config C:\\r.toml", enabled=False
            ),
        ),
        attachments=('док"1.md', "каталог\\файл.md"),
    )


def _state(phase: SessionPhase, round_no: int) -> SessionState:
    """`SessionState` в фазе `phase` раунда `round_no`."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=phase,
        current_round=round_no,
        task=TaskSpec(prompt="Почини экспорт CSV", attachments=[], mode=Mode.DEVELOP),
        agents={
            Role.AUTHOR: AgentRef(adapter=_SNAPSHOT_ADAPTER, model="opus"),
            Role.REVIEWER: AgentRef(adapter=_SNAPSHOT_ADAPTER, model="sonnet"),
        },
        limits=Limits(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        budget_used=BudgetUsed(),
    )


def _fixed_base_rev(root: Path, round_no: int, *, base_commit: str) -> str:
    """Цель сброса без обращения к истории git — одна на все раунды."""
    return _FIXED_BASE_REV


def _pin_base_rev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет `base_rev` в модуле шагов фиксированной ревизией."""
    steps = import_module("disputatio.runtime.steps")
    assert hasattr(steps, "base_rev"), (
        "runtime/steps.py обязан брать цель сброса у git.base_rev"
    )
    monkeypatch.setattr(steps, "base_rev", _fixed_base_rev)


def _register_adapters(
    monkeypatch: pytest.MonkeyPatch,
    *,
    log: list[str],
    built: list[tuple[str, str]],
    author_replies: Iterable[str],
    author_fail_at: int | None,
    reviewer_replies: Iterable[str],
    reviewer_fail_at: int | None,
) -> None:
    """Регистрирует фейковые фабрики ОБОИХ имён адаптера в реестре композиции.

    Оба имени легальны, поэтому «неизвестный адаптер» отпадает как способ
    различить конфиги: фабрика записывает пару «имя адаптера, роль», и по
    ней видно, чей именно конфиг доехал до сборки.
    """
    composition = import_module("disputatio.runtime.composition")
    settings: Mapping[Role, tuple[list[str], int | None]] = {
        Role.AUTHOR: (list(author_replies), author_fail_at),
        Role.REVIEWER: (list(reviewer_replies), reviewer_fail_at),
    }

    def make_factory(name: str) -> Callable[..., SpyAdapter]:
        def factory(
            *,
            role: Role,
            session_dir: Path,
            event_sink: object,
            session: str,
        ) -> SpyAdapter:
            built.append((name, role.value))
            replies, fail_at = settings[role]
            return SpyAdapter(
                name=role.value, log=log, replies=replies, fail_at=fail_at
            )

        return factory

    for adapter_name in (_SNAPSHOT_ADAPTER, _ENV_ADAPTER):
        monkeypatch.setitem(
            composition.ADAPTER_FACTORIES, adapter_name, make_factory(adapter_name)
        )


def _bench(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    author_replies: Iterable[str] = (_proposal(1),),
    author_fail_at: int | None = None,
    reviewer_replies: Iterable[str] = (_review_reply(1),),
    reviewer_fail_at: int | None = None,
    verifier_fails: bool = False,
) -> Bench:
    """Готовит `.disputatio/`, спаи и реестр адаптеров под `root`."""
    bootstrap_session(root)
    log: list[str] = []
    built: list[tuple[str, str]] = []
    _register_adapters(
        monkeypatch,
        log=log,
        built=built,
        author_replies=author_replies,
        author_fail_at=author_fail_at,
        reviewer_replies=reviewer_replies,
        reviewer_fail_at=reviewer_fail_at,
    )
    return Bench(
        root=root,
        log=log,
        store=SpyStore(inner=FileStateStore(root), log=log),
        sink=SpySink(log=log),
        verifier=SpyVerifier(log=log, fails=verifier_fails),
        git=SpyGit(log=log),
        built=built,
    )


def _write_env_config(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Кладёт в окружение ЗАВЕДОМО другой конфиг и указывает на него трижды.

    Файл рядом с рабочей директорией, переменная окружения и `cwd`: какой бы
    из способов реализация ни попробовала, она получит другие имена
    адаптеров, другой гейт и `base_commit`, которого в истории нет.
    """
    text = _render(
        _config(
            base_commit=_ENV_BASE_COMMIT,
            adapter=_ENV_ADAPTER,
            gate_name=_ENV_GATE,
            prompt="ЗАДАЧА ИЗ ОКРУЖЕНИЯ",
        )
    )
    for name in ("disputatio.toml", "config.toml"):
        (root / name).write_text(text, encoding="utf-8")
    monkeypatch.setenv("DISPUTATIO_CONFIG", str(root / "disputatio.toml"))
    monkeypatch.chdir(root)


def _seed_session(root: Path, config: RuntimeConfig, state: SessionState) -> None:
    """Кладёт на диск снапшот конфига и состояние — вход resume."""
    write_config_snapshot(root, _render(config))
    FileStateStore(root).save(state)


def _on_disk(root: Path) -> dict[str, Any]:
    """`session.json` как он лежит на диске — байты, а не спай."""
    payload = (session_dir(root) / "session.json").read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(payload)
    return parsed


def _port_calls(log: list[str]) -> list[str]:
    """Только обращения к агентам и верификатору, в порядке лога."""
    return [mark for mark in log if mark in _AGENT_CALLS]


def _gate_started_names(bench: Bench) -> list[str]:
    """Имена гейтов, ушедшие в журнал событием `gate_started`."""
    return [
        str(event.payload["name"])
        for event in bench.sink.events
        if event.type is EventType.GATE_STARTED
    ]


def _head_sha(repo: Path) -> str:
    """Полный SHA `HEAD` тестового репозитория."""
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def test_render_toml_lays_out_the_sections_of_design_014() -> None:
    """Рендер — ровно раскладка [DESIGN-014], побайтово.

    Пинится текст, а не только разбираемость: `config.toml` читается
    человеком в разборе инцидента, и «валидный TOML» с перетасованными
    таблицами эту его работу отменяет. Заодно фиксируется, что
    `_COMPLETE_TOML` ниже — не выдумка теста, а то, что пишет писатель.
    """
    assert _render(_layout_config()) == _COMPLETE_TOML


def test_render_toml_round_trip_is_byte_stable() -> None:
    """`render → from_toml → render` побайтово стабилен ([DESIGN-014]).

    Проверяется и текст, и структура: равные байты при разъехавшемся
    разборе означали бы, что `from_toml` теряет поле, а `render_toml`
    печатает на его месте дефолт — round-trip был бы стабилен и неверен.
    """
    config = _hostile_config()

    once = _render(config)
    parsed = _from_toml(once)
    twice = _render(parsed)

    assert twice == once
    assert parsed == config


def test_render_toml_escaping_survives_quotes_backslashes_and_newlines() -> None:
    """Кавычки, слэши и переводы строк доезжают до `tomllib` без потерь.

    Разбор идёт СТОРОННИМ парсером, а не собственным `from_toml`: своя пара
    писатель-читатель вправе договориться о чём угодно, а на диске обязан
    лежать TOML, а не диалект.
    """
    config = _hostile_config()

    parsed = tomllib.loads(_render(config))

    assert parsed["task"]["prompt"] == _HOSTILE_PROMPT
    assert parsed["session"]["id"] == config.session_id
    assert parsed["session"]["mode"] == config.mode.value
    assert parsed["agents"]["author"]["model"] == config.author.model
    assert parsed["agents"]["reviewer"]["adapter"] == config.reviewer.adapter
    assert [gate["cmd"] for gate in parsed["gates"]] == [
        config.gates[0].cmd,
        config.gates[1].cmd,
    ]
    assert parsed["gates"][1]["enabled"] is False
    assert parsed["task"]["attachments"] == list(config.attachments)


def test_render_toml_writes_limits_as_integers_not_strings() -> None:
    """`[limits]` — целые, а `[[gates]]` — массив таблиц ([DESIGN-014]).

    TOML принял бы и `max_rounds = "5"`, и `gates = "pytest"`, а `bool` в
    Python — подкласс `int`, поэтому `True` прошёл бы проверку «это целое»
    молча. Проверяется тип разобранного значения, не его печать.
    """
    parsed = tomllib.loads(_render(_hostile_config()))

    assert isinstance(parsed["gates"], list)
    assert [gate["name"] for gate in parsed["gates"]] == ["pytest", "ruff"]
    for key in ("max_rounds", "max_total_tokens", "max_wall_seconds", "schema_retries"):
        value = parsed["limits"][key]
        assert isinstance(value, int) and not isinstance(value, bool), key


def test_from_toml_reads_the_design_014_snapshot() -> None:
    """Образец раскладки читается — иначе негативные случаи ниже вакуумны."""
    config = _from_toml(_COMPLETE_TOML)

    assert config == _layout_config()


def test_from_toml_translates_broken_toml_into_config_error() -> None:
    """Битый TOML — `ConfigError`, а не `TOMLDecodeError` наружу ([DESIGN-020]).

    Диагноз при этом не теряется: исходная ошибка остаётся причиной, иначе
    пользователь услышал бы «конфиг битый» без указания, где именно.
    """
    with pytest.raises(ConfigError) as caught:
        _from_toml('[session\nid = "x"\n')

    assert isinstance(caught.value.__cause__, tomllib.TOMLDecodeError)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('base_commit = "abc"\n', ""),
        (_REVIEWER_BLOCK, ""),
        ('prompt = "p"\n', ""),
        ('mode = "develop"', 'mode = "хз"'),
        ("max_rounds = 5", 'max_rounds = "5"'),
        ("schema_retries = 1\n", ""),
    ],
    ids=[
        "no-base-commit",
        "no-reviewer",
        "no-prompt",
        "unknown-mode",
        "string-limit",
        "no-schema-retries",
    ],
)
def test_from_toml_translates_incomplete_config_into_config_error(
    old: str, new: str
) -> None:
    """Неполный снапшот — `ConfigError`, а не `KeyError`/`ValueError` наружу.

    Синтаксически валидный TOML без обязательного поля (или с полем не того
    типа) — та же «конфиг сессии не читается» с точки зрения пользователя, и
    техническое исключение из stdlib на этом месте не назвало бы, чего
    именно не хватает.
    """
    broken = _COMPLETE_TOML.replace(old, new, 1)
    assert broken != _COMPLETE_TOML, "мутация конфига ничего не изменила"

    with pytest.raises(ConfigError):
        _from_toml(broken)


def test_resume_runs_the_saved_phase_and_does_not_rerun_proposing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сессия в `VERIFYING` раунда 2 продолжает с гейтов раунда 2 ([REQ-014]).

    Первая метка лога — прогон гейтов, а не вызов автора: `PROPOSING`
    раунда 2 уже состоялся, и его повтор переписал бы работу автора, выдав
    за раунд 2 пустое дерево. Ревьюер обрывается сразу после перехода —
    дальше цикла тест не ведёт, ему хватает начала.
    """
    bench = _bench(tmp_path, monkeypatch, reviewer_fail_at=1)
    _seed_session(
        tmp_path,
        _config(base_commit=_FIXED_BASE_REV),
        _state(SessionPhase.VERIFYING, 2),
    )

    with pytest.raises(StepBoom):
        _resume(tmp_path, _SESSION_ID, **bench.overrides())

    assert _port_calls(bench.log) == [_VERIFIER_CALL, _REVIEWER_CALL]
    assert bench.log[0] == _VERIFIER_CALL
    assert bench.verifier.rounds == [2]
    assert bench.store.loaded == [_SESSION_ID]
    assert _on_disk(tmp_path)["state"] == SessionPhase.REVIEWING.value
    assert _on_disk(tmp_path)["current_round"] == 2


def test_resume_takes_the_config_from_the_snapshot_not_from_the_environment(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Конфиг сессии — снапшот `.disputatio/config.toml` ([REQ-014]).

    В окружении лежит другой конфиг, и отличается он всем, что наблюдаемо:
    именем адаптера (какую фабрику дёрнет композиция), именем гейта (что
    уйдёт в `gate_started`) и `base_commit` (на что сбросится раунд 1).
    Протечка любого из трёх видна отдельным assert'ом.
    """
    head = _head_sha(git_repo)
    bench = _bench(git_repo, monkeypatch, reviewer_fail_at=1)
    _seed_session(git_repo, _config(base_commit=head), _state(SessionPhase.IDLE, 0))
    _write_env_config(git_repo, monkeypatch)

    with pytest.raises(StepBoom):
        _resume(git_repo, _SESSION_ID, **bench.overrides())

    assert bench.built == [
        (_SNAPSHOT_ADAPTER, Role.AUTHOR.value),
        (_SNAPSHOT_ADAPTER, Role.REVIEWER.value),
    ]
    assert _gate_started_names(bench) == [_SNAPSHOT_GATE]
    assert bench.git.resets == [head]


def test_base_commit_snapshot_names_head_at_start_and_targets_round_one_reset(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[session].base_commit` — `HEAD` старта, и он же цель reset раунда 1.

    [DESIGN-012] вычисляет цель сброса из двух источников на диске: истории
    git и снапшота конфига. Пропади `base_commit` из `config.toml` — цель
    раунда 1 стала бы невосстановимой после перезапуска процесса, а `reset`
    поехал бы на `HEAD` прерванной попытки, то есть сохранил бы ровно то,
    от чего избавляет.
    """
    head = _head_sha(git_repo)
    bench = _bench(git_repo, monkeypatch, verifier_fails=True)
    _seed_session(git_repo, _config(base_commit=head), _state(SessionPhase.IDLE, 0))

    snapshot = (session_dir(git_repo) / "config.toml").read_text(encoding="utf-8")
    assert tomllib.loads(snapshot)["session"]["base_commit"] == head

    with pytest.raises(StepBoom):
        _resume(git_repo, _SESSION_ID, **bench.overrides())

    assert bench.git.resets == [head]
    assert bench.log.index("git.reset_hard") < bench.log.index(_AUTHOR_CALL)


def test_resume_of_unknown_session_id_raises_session_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Несуществующая сессия — `SessionNotFound`, а не `KeyError` ([DESIGN-020]).

    Ошибка пользователя обязана прийти в доменной иерархии: CLI печатает
    `.args[0]` и возвращает ненулевой код, а `repr` ключа в кавычках не
    сказал бы ему ничего. Причина сохраняется — `KeyError` от `store.load`.
    """
    bench = _bench(tmp_path, monkeypatch)
    _seed_session(
        tmp_path,
        _config(base_commit=_FIXED_BASE_REV),
        _state(SessionPhase.VERIFYING, 2),
    )

    with pytest.raises(SessionNotFound) as caught:
        _resume(tmp_path, "нет-такой-сессии", **bench.overrides())

    assert isinstance(caught.value.__cause__, KeyError)
    assert "нет-такой-сессии" in str(caught.value)
    assert _port_calls(bench.log) == []


@pytest.mark.parametrize(
    "snapshot",
    ["[session\nid = 1\n", '[session]\nid = "s"\n', None],
    ids=["broken-toml", "incomplete", "missing-file"],
)
def test_resume_with_unusable_config_snapshot_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot: str | None
) -> None:
    """Нечитаемый снапшот — `ConfigError`, и ни один порт не тронут.

    Отсутствие файла здесь наравне с битым содержимым: `FileNotFoundError`
    от `read_text` — то же самое «конфига сессии нет», и техническое
    исключение на этом месте увело бы CLI мимо ветки [DESIGN-020].
    """
    bench = _bench(tmp_path, monkeypatch)
    FileStateStore(tmp_path).save(_state(SessionPhase.VERIFYING, 2))
    if snapshot is not None:
        write_config_snapshot(tmp_path, snapshot)

    with pytest.raises(ConfigError):
        _resume(tmp_path, _SESSION_ID, **bench.overrides())

    assert bench.log == []


def test_resume_and_cold_start_differ_only_in_the_source_of_the_initial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отдельной «логики восстановления» нет ([REQ-014], [DESIGN-014]).

    Один и тот же `SessionState` подаётся в цикл двумя путями: холодный
    старт берёт его у `config.to_session_state`, resume — у `store.load`.
    Дальше обе половины обязаны совпасть целиком: порядок обращений к
    портам, последовательность сохранённых фаз и общая точка остановки.
    Заведись у resume хоть один собственный шаг — «пропустить фазу»,
    «переиграть раунд», «поправить конфиг» — логи разошлись бы.
    """
    _pin_base_rev(monkeypatch)
    config = _config(base_commit=_FIXED_BASE_REV)
    initial = config.to_session_state(created_at=_FROZEN_NOW)

    cold_root = tmp_path / "cold"
    cold_root.mkdir()
    cold = _bench(cold_root, monkeypatch, reviewer_fail_at=1)
    cold_deps = build_runtime(config, cold_root, **cold.overrides())
    cold_fsm = SessionFsm(initial, store=cold.store, sink=cold.sink, now=cold_deps.now)
    with pytest.raises(StepBoom):
        _drive(
            StepContext(
                deps=cold_deps,
                fsm=cold_fsm,
                base_commit=config.base_commit,
                gates=config.gates,
            )
        )

    warm_root = tmp_path / "warm"
    warm_root.mkdir()
    warm = _bench(warm_root, monkeypatch, reviewer_fail_at=1)
    _seed_session(warm_root, config, initial)
    with pytest.raises(StepBoom):
        _resume(warm_root, _SESSION_ID, **warm.overrides())

    assert warm.log == cold.log
    assert [state.state for state in warm.store.saved] == [
        state.state for state in cold.store.saved
    ]
    assert _on_disk(warm_root)["state"] == _on_disk(cold_root)["state"]
    assert _on_disk(warm_root)["current_round"] == _on_disk(cold_root)["current_round"]
    assert warm.built == cold.built
