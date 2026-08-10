"""`disp run` — новая сессия одной командой ([REQ-019], [DESIGN-019], [TASK-020]).

CLI проверяется как функция (`main(argv) -> int`, ADR-007), но сессия за ней
настоящая: реальный git-репозиторий, реальные `GitCli`, `FileStateStore`,
`JsonlEventSink`, `VerifierRunner` и реальный `ClaudeCodeAdapter`. Подменён
ровно один шов — `launcher` адаптера, то есть граница порождения процесса:
argv с `claude` собирает настоящий адаптер, но процесса за ним нет. Сверх
того на всё время каждого теста заминирован `asyncio.create_subprocess_exec`:
набор, зеленеющий на машине с установленным `claude`, доказывал бы совсем не
то, что обещает.

Четыре решения, без которых тесты пинили бы не то:

* **Порядок `preflight → bootstrap` пинится состоянием ФС.** На грязном
  дереве проверяется не «кто кого вызвал», а то, что `.disputatio/` в чужом
  репозитории не появился вовсе: spy на `bootstrap_session` прошёл бы и у
  реализации, которая каталог создаёт, а потом убирает за собой.
* **Профиль конфига заведомо враждебен.** Все четыре поля, которыми владеет
  запуск (`session.id`, `session.mode`, `session.base_commit`, `task.prompt`),
  лежат в файле негодными, и каждое наблюдаемо: `base_commit` из сорока нулей
  ни в один коммит не разрешается, поэтому его протечка не осталась бы
  незамеченной — она уронила бы раунд 1.
* **Шаг `EXPORTING` подменён заглушкой.** Настоящий `exporting.export` в
  `STEP_BY_PHASE` ещё не зарегистрирован — это интеграция [TASK-024]; предмет
  здесь — код возврата CLI, а не содержимое `result/`.
* **Профиль лежит вне репозитория** везде, кроме теста дефолтного пути: файл
  внутри рабочего дерева untracked, а `changes.patch` тянет untracked через
  intent-to-add — в analyze-режиме это сделало бы патч непустым и сорвало бы
  анти-сикофантию раунда 1 по причине, к CLI отношения не имеющей.

`disputatio.cli` до реализации не существует, поэтому доступ к нему идёт
через `_cli()`/`_attr`: отсутствие модуля обязано быть падением assertion'ом,
а не ошибкой коллекции.
"""

import asyncio
import json
import re
import subprocess
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest

from disputatio.adapters import ClaudeCodeAdapter
from disputatio.contracts import (
    EventSink,
    Issue,
    Mode,
    Review,
    Role,
    SessionPhase,
    Severity,
    Verdict,
)
from disputatio.events import FileStateStore
from disputatio.runtime import AgentConfig, LimitsConfig, RuntimeConfig
from disputatio.runtime.layout import session_dir
from disputatio.runtime.steps import StepContext
from disputatio.verifier import GateSpec

# Часы сессии инжектируются, и намеренно не в UTC: `session_id` обязан нести
# UTC-штамп, а реализация, забывшая перевод, отличается от правильной ровно
# на смещение зоны — с наивными часами эта разница невидима.
_CLOCK_ZONE = timezone(timedelta(hours=3))
_FROZEN_NOW = datetime(2026, 8, 10, 15, 34, 56, tzinfo=_CLOCK_ZONE)
_FROZEN_UTC_STAMP = "20260810-123456"

_SESSION_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")

# Текст задачи с кавычками и обратными слэшами: он едет в снапшот
# `config.toml` через ручной TOML-писатель, и сломанное экранирование
# потеряло бы сессию ещё до первого раунда.
_TASK_TEXT = 'Почини экспорт "CSV" и путь C:\\tmp\\выгрузка'

# Значения профиля, которыми запуск владеет сам. Каждое заведомо негодно:
# протечка любого из них наблюдаема, а не безобидна.
_PROFILE_ID = "ИДЕНТИФИКАТОР-ИЗ-ПРОФИЛЯ"
_PROFILE_PROMPT = "ЗАДАЧА ИЗ ПРОФИЛЯ"
_PROFILE_BASE_COMMIT = "0" * 40

_ADAPTER_NAME = "claude_code"
_DEFAULT_CONFIG_NAME = "disputatio.toml"
_AUTHORED_FILE = "feature.py"

# Флаг read-only-прав ревьюера (§7): единственное, чем argv автора отличается
# от argv ревьюера, — и, значит, единственный честный способ различить их на
# границе launcher'а.
_REVIEWER_FLAG = "--allowedTools"


class _FakeStdout:
    """Однопроходный асинхронный итератор строк stdout фейкового процесса."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self) -> AsyncIterator[bytes]:
        """Сам себе итератор — адаптер читает поток ровно один раз."""
        return self

    async def __anext__(self) -> bytes:
        """Отдаёт следующую строку либо закрывает поток."""
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProcess:
    """Процесс, которого нет: stdout из готового текста, нулевой код возврата."""

    def __init__(self, text: str) -> None:
        self.stdout: object = _FakeStdout(
            [line.encode() for line in text.splitlines(keepends=True)]
        )
        self.stderr = b""

    async def wait(self) -> int:
        """Код возврата фейкового процесса — всегда успех."""
        return 0


@dataclass
class SpyLauncher:
    """Граница порождения процесса: argv настоящий, процесса нет.

    Роль различается по argv, а не по счётчику вызовов: очередь «автор,
    ревьюер, автор, ревьюер» совпала бы с порядком шагов и молча пережила бы
    перепутанные при сборке роли — ровно ту ошибку, ради которой [DESIGN-001]
    требует их различать.
    """

    author_replies: list[str]
    reviewer_replies: list[str]
    edits: bool
    argvs: list[tuple[str, ...]] = field(default_factory=list)

    async def __call__(self, *argv: str, cwd: str | None = None) -> _FakeProcess:
        """Отдаёт очередной ответ роли, попутно изображая работу автора."""
        self.argvs.append(argv)
        assert cwd is not None, "адаптер обязан назвать рабочую директорию"
        if _REVIEWER_FLAG in argv:
            return _FakeProcess(_next_reply(self.reviewer_replies, "ревьюер"))
        if self.edits:
            (Path(cwd) / _AUTHORED_FILE).write_text(
                f"# работа автора, вызов {len(self.argvs)}\n", encoding="utf-8"
            )
        return _FakeProcess(_next_reply(self.author_replies, "автор"))


@dataclass
class Bench:
    """Собранное окружение запуска: репозиторий, профиль и спай launcher'а."""

    root: Path
    profile: RuntimeConfig
    config_path: Path
    launcher: SpyLauncher
    built: list[tuple[str, str]]

    def argv(self, *extra: str, config: bool = True) -> list[str]:
        """`argv` подкоманды `run` с текстом задачи и путём к профилю."""
        flags = ["--root", str(self.root)]
        if config:
            flags += ["--config", str(self.config_path)]
        return ["run", *flags, *extra, _TASK_TEXT]


def _next_reply(queue: list[str], who: str) -> str:
    """Следующий ответ очереди; исчерпание — падение с внятным текстом."""
    assert queue, f"очередь ответов исчерпана: {who} вызван лишний раз"
    return queue.pop(0)


def _attr(owner: object, name: str, *, what: str) -> Any:
    """Символ `owner.name`; отсутствие — `AssertionError`, не `AttributeError`."""
    assert hasattr(owner, name), f"{what} не определяет {name!r}"
    return getattr(owner, name)


def _cli() -> ModuleType:
    """Модуль `disputatio/cli.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.cli")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля disputatio/cli.py: {exc}") from exc


def _main(argv: Sequence[str], *, now: Callable[[], datetime] | None = None) -> int:
    """`main(argv, now=…)` — CLI как обычная функция (ADR-007)."""
    main = _attr(_cli(), "main", what="disputatio/cli.py")
    clock = now if now is not None else (lambda: _FROZEN_NOW)
    code: int = main(list(argv), now=clock)
    return code


def _proposal(round_no: int) -> str:
    """Валидный `proposal.md` раунда `round_no` — ответ автора."""
    return (
        "---\n"
        "schema: disputatio/v1\n"
        f"round: {round_no}\n"
        "role: author\n"
        "responds_to: null\n"
        "files_touched:\n"
        f"  - {_AUTHORED_FILE}\n"
        "self_declared_status: complete\n"
        "---\n"
        f"Работа раунда {round_no:03d}.\n"
    )


def _request_changes(round_no: int) -> str:
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
                file=_AUTHORED_FILE,
                claim=f"замечание раунда {marker}",
                evidence=f"{_AUTHORED_FILE}:1 — свидетельство раунда {marker}",
            )
        ],
        checked=[_AUTHORED_FILE],
        summary=f"свод раунда {marker}",
    )
    return review.model_dump_json(by_alias=True)


def _approve(round_no: int) -> str:
    """Ответ ревьюера: `approve` — раунд `round_no` сходится (§5.1)."""
    review = Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.APPROVE,
        confidence=0.9,
        issues=[],
        checked=[_AUTHORED_FILE],
        summary=f"раунд {round_no:03d} принят",
    )
    return review.model_dump_json(by_alias=True)


def _profile(
    *,
    gates: tuple[GateSpec, ...] = (),
    schema_retries: int = 1,
    max_rounds: int = 5,
) -> RuntimeConfig:
    """Профиль запуска: агенты, лимиты и гейты — свои, остальное негодное."""
    return RuntimeConfig(
        session_id=_PROFILE_ID,
        mode=Mode.ANALYZE,
        base_commit=_PROFILE_BASE_COMMIT,
        task_prompt=_PROFILE_PROMPT,
        author=AgentConfig(adapter=_ADAPTER_NAME, model="opus"),
        reviewer=AgentConfig(adapter=_ADAPTER_NAME, model="sonnet"),
        limits=LimitsConfig(
            max_rounds=max_rounds,
            max_total_tokens=400_000,
            max_wall_seconds=3600,
            schema_retries=schema_retries,
        ),
        gates=gates,
        attachments=(),
    )


def _forbid_real_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Минирует порождение процессов: ни один агентский CLI не запускается.

    Ловушка ставится на `asyncio.create_subprocess_exec`, а не на
    `default_launcher`: дефолт зашит значением аргумента в конструкторе
    адаптера, и подмена имени в модуле его бы не тронула.
    """

    async def boom(*argv: object, **kwargs: object) -> None:
        raise AssertionError(f"запущен реальный процесс агента: {argv}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)


def _install_export_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ставит временный шаг `EXPORTING`, доводящий сессию до `DONE`.

    Заглушка ровно потому, что регистрация настоящего `exporting.export` в
    диспетчере — предмет интеграции [TASK-024]: CLI проверяется по коду
    возврата, а не по содержимому `result/`.
    """
    loop_module = import_module("disputatio.runtime.loop")

    def fake_export(ctx: StepContext) -> None:
        """Изображает экспорт: переводит сессию в терминальное `DONE`."""
        ctx.fsm.transition(SessionPhase.DONE)

    monkeypatch.setattr(
        loop_module,
        "STEP_BY_PHASE",
        {**loop_module.STEP_BY_PHASE, SessionPhase.EXPORTING: fake_export},
    )


def _register_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    launcher: SpyLauncher,
    built: list[tuple[str, str]],
) -> None:
    """Подменяет фабрику адаптера ровно на инъекции launcher'а.

    Класс остаётся настоящим: argv, права §7 и разбор stdout считает
    `ClaudeCodeAdapter`, а не тест. Composition root иначе до шва запуска не
    пускает — фабрике передаются только роль, каталог, sink и id сессии
    (NFR-003), и это правильно.
    """
    composition = import_module("disputatio.runtime.composition")

    def factory(
        *, role: Role, session_dir: Path, event_sink: EventSink, session: str
    ) -> ClaudeCodeAdapter:
        """Собирает настоящий адаптер, подставляя спай на место launcher'а."""
        built.append((role.value, session))
        return ClaudeCodeAdapter(
            role=role,
            session_dir=session_dir,
            event_sink=event_sink,
            session=session,
            launcher=launcher,
        )

    monkeypatch.setitem(composition.ADAPTER_FACTORIES, _ADAPTER_NAME, factory)


def _bench(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: RuntimeConfig | None = None,
    config_at: Path | None = None,
    author_replies: Sequence[str] | None = None,
    reviewer_replies: Sequence[str] | None = None,
    edits: bool = True,
) -> Bench:
    """Готовит профиль, спай launcher'а, заглушку экспорта и ловушку процессов."""
    _forbid_real_processes(monkeypatch)
    _install_export_step(monkeypatch)

    used_profile = profile if profile is not None else _profile()
    config_path = config_at if config_at is not None else repo.parent / "profile.toml"
    config_path.write_text(used_profile.render_toml(), encoding="utf-8")

    author = (
        author_replies if author_replies is not None else (_proposal(1), _proposal(2))
    )
    reviewer = (
        reviewer_replies
        if reviewer_replies is not None
        else (_request_changes(1), _approve(2))
    )
    launcher = SpyLauncher(
        author_replies=list(author), reviewer_replies=list(reviewer), edits=edits
    )
    built: list[tuple[str, str]] = []
    _register_adapter(monkeypatch, launcher=launcher, built=built)
    return Bench(
        root=repo,
        profile=used_profile,
        config_path=config_path,
        launcher=launcher,
        built=built,
    )


def _session_id(capsys: pytest.CaptureFixture[str]) -> str:
    """Первая строка stdout — `session_id` запущенной сессии ([REQ-019])."""
    lines = capsys.readouterr().out.splitlines()
    assert lines, "stdout пуст: session_id не напечатан"
    return lines[0]


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


def _round_subjects(repo: Path) -> list[str]:
    """Заголовки коммитов репозитория, от новых к старым."""
    completed = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.splitlines()


def test_run_on_a_clean_repo_creates_the_session_and_prints_its_id(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Чистый репозиторий: `.disputatio/` собран, id напечатан, код возврата 0.

    Совпадение напечатанного id с записанным проверяется чтением состояния
    ЭТИМ id: `FileStateStore.load` отвечает `KeyError` на чужой ключ, поэтому
    успешная загрузка и есть пин «в stdout ушёл id той самой сессии», а не
    сравнение строки с самой собой.
    """
    bench = _bench(git_repo, monkeypatch)

    code = _main(bench.argv())

    session_id = _session_id(capsys)
    assert code == 0
    assert _SESSION_ID_RE.match(session_id), f"напечатан id {session_id!r}"
    assert (session_dir(git_repo) / "session.json").is_file()
    assert (session_dir(git_repo) / "config.toml").is_file()

    state = FileStateStore(git_repo).load(session_id)
    assert state.state is SessionPhase.DONE
    assert state.current_round == 2
    assert state.task.prompt == _TASK_TEXT
    assert state.task.mode is Mode.DEVELOP
    assert _round_subjects(git_repo)[:2] == [
        "disputatio: round 002",
        "disputatio: round 001",
    ]


def test_session_id_is_the_utc_stamp_of_the_clock_plus_four_hex_digits() -> None:
    """Формат id — `{UTC:%Y%m%d-%H%M%S}-{4 hex}` ([DESIGN-019]).

    Часы отдают время с ненулевым смещением, поэтому «взял `strftime` как
    есть» и «перевёл в UTC» дают разные штампы; на наивных часах различить
    их было бы нечем. Случайность суффикса пиньется массовой генерацией:
    константа на его месте сделала бы два запуска в одну секунду одной
    сессией, а единственная пара значений совпала бы по случайности слишком
    часто, чтобы на неё опираться.
    """
    new_session_id = _attr(_cli(), "new_session_id", what="disputatio/cli.py")

    session_id: str = new_session_id(_FROZEN_NOW)
    generated = {new_session_id(_FROZEN_NOW) for _ in range(200)}

    assert _SESSION_ID_RE.match(session_id), f"собран id {session_id!r}"
    assert session_id.startswith(f"{_FROZEN_UTC_STAMP}-")
    assert len(generated) > 1


def test_dirty_working_tree_exits_two_and_leaves_no_session_dir(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Грязное дерево: код `2`, `.disputatio/` не создан ([REQ-010], [DESIGN-019]).

    Порядок «pre-flight раньше bootstrap» пиньется состоянием файловой
    системы, а не spy-логом: реализация, которая каталог создаёт и убирает
    за собой после отказа, spy прошла бы — а чужой репозиторий остался бы
    тронутым ровно тогда, когда обрыв случился между двумя вызовами.
    """
    bench = _bench(git_repo, monkeypatch)
    (git_repo / "README.md").write_text("правка пользователя\n", encoding="utf-8")

    code = _main(bench.argv())

    assert code == 2
    assert not session_dir(git_repo).exists()
    assert bench.launcher.argvs == []
    assert capsys.readouterr().out == ""


def test_config_snapshot_replaces_only_the_fields_owned_by_the_run(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Снапшот = профиль, где заменены ровно id, режим, база и текст задачи.

    Проверяется равенство целому ожидаемому конфигу, а не набор отдельных
    полей: «взял своё» и «не потерял чужое» — две половины одного требования,
    и половинчатая проверка пропустила бы реализацию, которая молча теряет
    гейты или лимиты профиля ([REQ-014]).
    """
    gates = (GateSpec(name="pytest", cmd="uv run pytest -q", enabled=True),)
    bench = _bench(
        git_repo,
        monkeypatch,
        profile=_profile(gates=gates, schema_retries=0),
        author_replies=["ЭТО НЕ ФРОНТМАТТЕР ПРЕДЛОЖЕНИЯ"],
        reviewer_replies=[],
    )
    head = _head_sha(git_repo)

    code = _main(bench.argv())

    session_id = _session_id(capsys)
    snapshot = RuntimeConfig.from_toml(
        (session_dir(git_repo) / "config.toml").read_text(encoding="utf-8")
    )
    assert code == 1
    assert snapshot == replace(
        bench.profile,
        session_id=session_id,
        mode=Mode.DEVELOP,
        base_commit=head,
        task_prompt=_TASK_TEXT,
    )


def test_failed_session_exits_one_and_says_so_on_disk(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Исчерпанные schema-повторы: `FAILED` на диске и код возврата `1`.

    Тело шага после исчерпания лимита поднимает ошибку последней попытки
    ([DESIGN-006]), и различить «сессия провалилась» и «CLI сломался» обязан
    сам CLI: `1` полагается только состоянию, которое уже записано в
    `session.json`.
    """
    bench = _bench(
        git_repo,
        monkeypatch,
        profile=_profile(schema_retries=0),
        author_replies=["ЭТО НЕ ФРОНТМАТТЕР ПРЕДЛОЖЕНИЯ"],
        reviewer_replies=[],
    )

    code = _main(bench.argv())

    session_id = _session_id(capsys)
    assert code == 1
    assert FileStateStore(git_repo).load(session_id).state is SessionPhase.FAILED


def test_analyze_mode_reaches_the_session_state(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--mode analyze` доезжает до `SessionState.mode` ([REQ-019]).

    Режим наблюдаем дважды: он записан в `session.json`, и он же пропустил
    approve раунда 1 — анти-сикофантия §5.1 засчитывает такой approve только
    `analyze` без правок кода, поэтому develop-сессия на тех же ответах
    агентов за один раунд до `DONE` не дошла бы.
    """
    bench = _bench(
        git_repo,
        monkeypatch,
        author_replies=[_proposal(1)],
        reviewer_replies=[_approve(1)],
        edits=False,
    )

    code = _main(bench.argv("--mode", "analyze"))

    session_id = _session_id(capsys)
    state = FileStateStore(git_repo).load(session_id)
    assert code == 0
    assert state.task.mode is Mode.ANALYZE
    assert state.state is SessionPhase.DONE
    assert state.current_round == 1


def test_default_config_path_is_disputatio_toml_under_root(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Без `--config` профиль берётся из `<root>/disputatio.toml`."""
    bench = _bench(git_repo, monkeypatch, config_at=git_repo / _DEFAULT_CONFIG_NAME)

    code = _main(bench.argv(config=False))

    session_id = _session_id(capsys)
    assert code == 0
    assert FileStateStore(git_repo).load(session_id).state is SessionPhase.DONE


def test_root_flag_selects_the_repository_not_the_current_directory(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Сессия открывается в `--root`, а не в текущем каталоге процесса.

    Текущий каталог — заведомо не репозиторий: реализация, читающая `cwd`,
    отказала бы pre-flight'ом и вернула бы `2`, а не завела сессию не там.
    """
    elsewhere = tmp_path_factory.mktemp("not-a-repo")
    bench = _bench(git_repo, monkeypatch)
    monkeypatch.chdir(elsewhere)

    code = _main(bench.argv())

    session_id = _session_id(capsys)
    assert code == 0
    assert not (elsewhere / ".disputatio").exists()
    assert FileStateStore(git_repo).load(session_id).state is SessionPhase.DONE


def test_the_loop_is_driven_by_anyio_run_not_asyncio_run(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Цикл крутится ровно одним `anyio.run`; `asyncio.run` заминирован.

    Правило проекта и требование порта `AgentAdapter.run`: `asyncio.run` завёл
    бы второй способ запускать асинхронный код, и обещание отменяемости шага
    перестало бы быть одним на всю сессию.
    """
    bench = _bench(git_repo, monkeypatch)
    real_run = anyio.run
    calls: list[Any] = []

    def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
        """Считает запуски цикла, отдавая работу настоящему `anyio.run`."""
        calls.append(func)
        return real_run(func, *args, **kwargs)

    def boom(*args: object, **kwargs: object) -> None:
        """Ловушка на `asyncio.run`: цикл обязан идти через anyio."""
        raise AssertionError("цикл запущен через asyncio.run, а не anyio.run")

    monkeypatch.setattr(anyio, "run", spy)
    monkeypatch.setattr(asyncio, "run", boom)

    code = _main(bench.argv())

    assert code == 0
    assert len(calls) == 1
    assert _SESSION_ID_RE.match(_session_id(capsys))


def test_no_agent_process_is_spawned_behind_the_real_adapter(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Оба агента собраны и вызваны, но ни одного процесса не порождено.

    Пиньется именно граница launcher'а: argv собран настоящим адаптером
    (`claude`, права ревьюера §7 отдельным флагом), а
    `asyncio.create_subprocess_exec` заминирован — сессия, дошедшая до
    `DONE`, доказывает, что реальный CLI не запускался ни разу.
    """
    bench = _bench(git_repo, monkeypatch)

    code = _main(bench.argv())

    session_id = _session_id(capsys)
    programs = {argv[0] for argv in bench.launcher.argvs}
    reviewer_calls = [argv for argv in bench.launcher.argvs if _REVIEWER_FLAG in argv]
    assert code == 0
    assert bench.built == [
        (Role.AUTHOR.value, session_id),
        (Role.REVIEWER.value, session_id),
    ]
    assert programs == {"claude"}
    assert len(bench.launcher.argvs) == 4
    assert len(reviewer_calls) == 2


def test_events_and_round_artifacts_land_in_the_session_directory(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Сессия из CLI пишет тот же `.disputatio/`, что и цикл, запущенный кодом.

    Без этого теста «код возврата 0» доказывал бы только выход из функции:
    журнал и артефакты раундов — единственное, что отличает состоявшуюся на
    диске сессию от корректно завершившегося вызова.
    """
    bench = _bench(git_repo, monkeypatch)

    code = _main(bench.argv())

    session_id = _session_id(capsys)
    events = (session_dir(git_repo) / "events.jsonl").read_text(encoding="utf-8")
    sessions = {json.loads(line)["session"] for line in events.splitlines()}
    round_two = session_dir(git_repo) / "rounds" / "002"
    assert code == 0
    assert sessions == {session_id}
    assert (round_two / "proposal.md").read_text(encoding="utf-8") == _proposal(2)
    assert (round_two / "changes.patch").read_text(encoding="utf-8") != ""
    assert (round_two / "verification.json").is_file()
    assert (round_two / "review.json").is_file()
    assert (round_two / "decision.json").is_file()
