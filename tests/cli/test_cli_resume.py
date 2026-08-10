"""`disp resume` и диагностика без traceback ([REQ-020], [DESIGN-020]).

Сессия за CLI настоящая — реальный git-репозиторий, реальные `GitCli`,
`FileStateStore`, `JsonlEventSink`, `VerifierRunner` и реальный
`ClaudeCodeAdapter`; подменён ровно один шов, граница порождения процесса.
Оттуда же изображается и обрыв: `launcher`, поднявший `_ProcessKilled`,
оставляет сессию ровно там, где её застал kill, и никакого другого способа
получить прерванную сессию, не подделывая `session.json` руками, нет.

Четыре решения, без которых набор пинил бы не то:

* **Подготовка двухраундовая, и иначе быть не может.** `approve` раунда 1 в
  `develop` с правками кода схождением не считается (§5.1, анти-сикофантия),
  поэтому очередь автора описывает всю жизнь сессии, включая её смерть:
  `[proposal(1), ОБРЫВ, proposal(2)]`. Первый процесс съедает два элемента,
  второй — третий, и «сессия дошла до `DONE`» получается тем же путём,
  которым его прошла бы настоящая пара агентов.
* **Прерванность пиньется состоянием на диске.** Между двумя вызовами `main`
  проверяется `session.json`: фаза `PROPOSING` раунда 2 — это и есть
  write-ahead-обещание [REQ-014], и resume обязан переиграть именно её.
* **Снапшот конфига пиньется враждебным профилем.** Внешний профиль после
  обрыва переписан негодным адаптером и продублирован в `<root>/`: сессия,
  продолженная не своим конфигом, отказала бы `UnknownAdapterError`, а не
  разошлась бы с записанными раундами молча ([REQ-014], [DESIGN-014]).
* **Шесть доменных классов пиньются подменой `preflight`.** Естественных
  поводов у них шесть разных, а обработчик — один, и предмет здесь именно
  он: код `2` и `.args[0]` одной строкой. Traceback в журнале при этом
  пиньется на НЕподменённой ошибке (`SessionNotFound`): событие `error`
  требует существующего `.disputatio/`, которого до первой записи нет.

`disputatio.cli` до реализации `cmd_resume` не знает, поэтому доступ к
подкоманде идёт через `_attr`: отсутствие обязано быть падением
assertion'ом, а не `SystemExit(2)` от argparse — код `2` этой задачи
означает ровно противоположное.
"""

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

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
from disputatio.runtime import (
    AgentConfig,
    ConfigError,
    DirtyWorkingTree,
    DisputatioError,
    LimitsConfig,
    NotAGitRepository,
    ReviewParseError,
    RuntimeConfig,
    SessionNotFound,
    UnknownAdapterError,
)
from disputatio.runtime.layout import session_dir
from disputatio.runtime.steps import StepContext
from disputatio.verifier import GateSpec

_CLOCK_ZONE = timezone(timedelta(hours=3))
_FROZEN_NOW = datetime(2026, 8, 10, 15, 34, 56, tzinfo=_CLOCK_ZONE)

_TASK_TEXT = "Почини экспорт CSV"

_PROFILE_ID = "ИДЕНТИФИКАТОР-ИЗ-ПРОФИЛЯ"
_PROFILE_PROMPT = "ЗАДАЧА ИЗ ПРОФИЛЯ"
_PROFILE_BASE_COMMIT = "0" * 40

_ADAPTER_NAME = "claude_code"
_UNKNOWN_ADAPTER = "клод-код"
_DEFAULT_CONFIG_NAME = "disputatio.toml"
_AUTHORED_FILE = "feature.py"

_REVIEWER_FLAG = "--allowedTools"

# Элемент очереди автора, на котором процесс агента умирает. Слот тратится
# как обычный ответ: после обрыва сессию поднимает второй процесс, и очередь
# обязана помнить, сколько вызовов уже случилось.
_KILL = "<обрыв процесса агента>"

_MISSING_SESSION_ID = "20260810-000000-dead"

_DOMAIN_ERRORS: tuple[type[DisputatioError], ...] = (
    DirtyWorkingTree,
    NotAGitRepository,
    SessionNotFound,
    UnknownAdapterError,
    ConfigError,
    ReviewParseError,
)

_TRACEBACK_MARKER = "Traceback (most recent call last)"


class _ProcessKilled(RuntimeError):
    """Процесс агента убит: сессия остаётся в фазе, которую начинала."""


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
    ревьюер, автор» совпала бы с порядком шагов и молча пережила бы
    перепутанные при сборке роли.
    """

    author_replies: list[str]
    reviewer_replies: list[str]
    argvs: list[tuple[str, ...]] = field(default_factory=list)

    async def __call__(self, *argv: str, cwd: str | None = None) -> _FakeProcess:
        """Отдаёт очередной ответ роли, попутно изображая работу автора."""
        self.argvs.append(argv)
        assert cwd is not None, "адаптер обязан назвать рабочую директорию"
        if _REVIEWER_FLAG in argv:
            return _FakeProcess(_next_reply(self.reviewer_replies, "ревьюер"))
        (Path(cwd) / _AUTHORED_FILE).write_text(
            f"# работа автора, вызов {len(self.argvs)}\n", encoding="utf-8"
        )
        reply = _next_reply(self.author_replies, "автор")
        if reply == _KILL:
            # Правка уже на диске: убитый автор оставляет за собой дерево,
            # которое resume обязан вернуть к базе раунда сам ([REQ-012]).
            raise _ProcessKilled("процесс агента убит между правкой и ответом")
        return _FakeProcess(reply)


@dataclass
class Bench:
    """Собранное окружение запуска: репозиторий, профиль и спай launcher'а."""

    root: Path
    profile: RuntimeConfig
    config_path: Path
    launcher: SpyLauncher

    def run_argv(self, *extra: str) -> list[str]:
        """`argv` подкоманды `run` с текстом задачи и путём к профилю."""
        return [
            "run",
            "--root",
            str(self.root),
            "--config",
            str(self.config_path),
            *extra,
            _TASK_TEXT,
        ]

    def resume_argv(self, session_id: str) -> list[str]:
        """`argv` подкоманды `resume`: репозиторий и имя сессии."""
        return ["resume", "--root", str(self.root), session_id]


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


def _main(argv: Sequence[str]) -> int:
    """`main(argv, now=…)` на замороженных часах — CLI как функция (ADR-007)."""
    main = _attr(_cli(), "main", what="disputatio/cli.py")
    code: int = main(list(argv), now=lambda: _FROZEN_NOW)
    return code


def _exit_code_of(argv: Sequence[str]) -> int:
    """Код возврата `main`, включая тот, которым argparse завершает процесс."""
    try:
        return _main(argv)
    except SystemExit as exc:
        code = exc.code
        assert isinstance(code, int), f"argparse завершился кодом {code!r}"
        return code


def _resume(bench: Bench, session_id: str) -> int:
    """`disp resume <id>`; отсутствие подкоманды — `AssertionError`."""
    _attr(_cli(), "cmd_resume", what="disputatio/cli.py")
    return _main(bench.resume_argv(session_id))


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


def _profile(*, adapter: str = _ADAPTER_NAME) -> RuntimeConfig:
    """Профиль запуска: поля, которыми владеет запуск, заведомо негодные."""
    return RuntimeConfig(
        session_id=_PROFILE_ID,
        mode=Mode.ANALYZE,
        base_commit=_PROFILE_BASE_COMMIT,
        task_prompt=_PROFILE_PROMPT,
        author=AgentConfig(adapter=adapter, model="opus"),
        reviewer=AgentConfig(adapter=adapter, model="sonnet"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=400_000,
            max_wall_seconds=3600,
            schema_retries=1,
        ),
        gates=(GateSpec(name="версия-git", cmd="git --version", enabled=True),),
        attachments=(),
    )


def _forbid_real_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Минирует порождение процессов: ни один агентский CLI не запускается."""

    async def boom(*argv: object, **kwargs: object) -> None:
        raise AssertionError(f"запущен реальный процесс агента: {argv}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)


def _install_export_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ставит временный шаг `EXPORTING`, доводящий сессию до `DONE`.

    Заглушка ровно потому, что регистрация настоящего `exporting.export` в
    диспетчере — предмет интеграции [TASK-024].
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
    monkeypatch: pytest.MonkeyPatch, *, launcher: SpyLauncher
) -> None:
    """Подменяет фабрику адаптера ровно на инъекции launcher'а."""
    composition = import_module("disputatio.runtime.composition")

    def factory(
        *, role: Role, session_dir: Path, event_sink: EventSink, session: str
    ) -> ClaudeCodeAdapter:
        """Собирает настоящий адаптер, подставляя спай на место launcher'а."""
        return ClaudeCodeAdapter(
            role=role,
            session_dir=session_dir,
            event_sink=event_sink,
            session=session,
            launcher=launcher,
        )

    monkeypatch.setitem(composition.ADAPTER_FACTORIES, _ADAPTER_NAME, factory)


def _bench(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    """Профиль вне репозитория, спай launcher'а, заглушка экспорта, ловушка."""
    _forbid_real_processes(monkeypatch)
    _install_export_step(monkeypatch)

    profile = _profile()
    config_path = repo.parent / "profile.toml"
    config_path.write_text(profile.render_toml(), encoding="utf-8")

    launcher = SpyLauncher(
        author_replies=[_proposal(1), _KILL, _proposal(2)],
        reviewer_replies=[_request_changes(1), _approve(2)],
    )
    _register_adapter(monkeypatch, launcher=launcher)
    return Bench(root=repo, profile=profile, config_path=config_path, launcher=launcher)


def _session_id(capsys: pytest.CaptureFixture[str]) -> str:
    """Первая строка stdout — `session_id` запущенной сессии ([REQ-019])."""
    lines = capsys.readouterr().out.splitlines()
    assert lines, "stdout пуст: session_id не напечатан"
    return lines[0]


def _interrupted_session(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[Bench, str]:
    """Сессия, убитая в PROPOSING раунда 2, — вход любого resume-теста.

    Раунд 1 к этому моменту прожит целиком: предложение, гейты,
    `request_changes` и решение «править дальше» (§5.1 иного и не позволяет
    — approve раунда 1 в `develop` с правками кода схождением не является).
    """
    bench = _bench(repo, monkeypatch)

    with pytest.raises(_ProcessKilled):
        _main(bench.run_argv())

    session_id = _session_id(capsys)
    state = FileStateStore(repo).load(session_id)
    assert state.state is SessionPhase.PROPOSING, "сессия убита не в PROPOSING"
    assert state.current_round == 2, "обрыв ожидался в раунде 2"
    return bench, session_id


def _round_bytes(repo: Path, round_no: int) -> dict[str, bytes]:
    """Артефакты раунда `round_no` побайтово — снимок для сверки после resume."""
    directory = session_dir(repo) / "rounds" / f"{round_no:03d}"
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


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


def _events(repo: Path) -> list[dict[str, Any]]:
    """Строки журнала сессии, разобранные в словари, в порядке записи."""
    text = (session_dir(repo) / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_resume_drives_the_interrupted_session_to_its_terminal_state(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Прерванная сессия доезжает до `DONE` вторым процессом ([REQ-014]).

    Фаза, в которой сессию застал kill, переигрывается целиком: очередь
    автора отдаёт `proposal(2)` ровно потому, что убитый вызов свой слот уже
    потратил. Раунд не выдумывается заново — `current_round` остаётся вторым,
    и коммит принятого раунда ложится ровно один на раунд ([REQ-015]).
    """
    bench, session_id = _interrupted_session(git_repo, monkeypatch, capsys)

    code = _resume(bench, session_id)

    state = FileStateStore(git_repo).load(session_id)
    assert code == 0
    assert state.state is SessionPhase.DONE
    assert state.current_round == 2
    assert state.task.prompt == _TASK_TEXT
    assert _round_subjects(git_repo)[:2] == [
        "disputatio: round 002",
        "disputatio: round 001",
    ]
    assert bench.launcher.author_replies == []
    assert bench.launcher.reviewer_replies == []


def test_resume_takes_the_config_from_the_session_snapshot_not_the_profile(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Продолжение идёт снапшотом сессии, а не внешним профилем ([REQ-014]).

    Оба места, куда `disp run` заглядывает за профилем, после обрыва заняты
    конфигом с адаптером, которого нет в реестре: сессия, продолженная не
    своим конфигом, отказала бы `UnknownAdapterError` — то есть кодом `2`, а
    не разошлась бы с уже записанными раундами молча ([DESIGN-014]).
    """
    bench, session_id = _interrupted_session(git_repo, monkeypatch, capsys)
    hostile = _profile(adapter=_UNKNOWN_ADAPTER).render_toml()
    bench.config_path.write_text(hostile, encoding="utf-8")
    (git_repo / _DEFAULT_CONFIG_NAME).write_text(hostile, encoding="utf-8")

    code = _resume(bench, session_id)

    assert code == 0
    assert FileStateStore(git_repo).load(session_id).state is SessionPhase.DONE


def test_resume_appends_to_the_journal_and_leaves_the_finished_round_intact(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Журнал дописывается, финализированный раунд не трогается ([REQ-016]).

    Снимок `events.jsonl` до обрыва обязан остаться **побайтовым префиксом**
    журнала после resume: append-only — это про байты, а не про то, что
    строки «в целом те же». Артефакты раунда 1 сверяются тем же способом и по
    той же причине.
    """
    bench, session_id = _interrupted_session(git_repo, monkeypatch, capsys)
    journal = session_dir(git_repo) / "events.jsonl"
    before = journal.read_bytes()
    round_one = _round_bytes(git_repo, 1)

    code = _resume(bench, session_id)

    after = journal.read_bytes()
    assert code == 0
    assert after.startswith(before)
    assert len(after) > len(before)
    assert _round_bytes(git_repo, 1) == round_one
    assert {event["session"] for event in _events(git_repo)} == {session_id}


def test_an_unknown_session_id_exits_two_with_one_line_and_no_traceback(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Чужой `session_id` — одна строка в stderr и код `2` ([DESIGN-020]).

    Каталог сессии на месте, и не той, которую назвали: `SessionNotFound`
    отличает «сессии нет» от «`.disputatio/` нет», а пользователь обязан
    услышать имя, которое он ввёл, а не repr ключа в кавычках.
    """
    bench, _ = _interrupted_session(git_repo, monkeypatch, capsys)

    code = _resume(bench, _MISSING_SESSION_ID)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1, captured.err
    assert _MISSING_SESSION_ID in captured.err
    assert _TRACEBACK_MARKER not in captured.out + captured.err


def test_the_traceback_of_a_domain_error_goes_to_the_journal_as_an_error_event(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Traceback не проглочен — он уходит событием `error` (NFR-003).

    Пиньется на неподменённой ошибке: событие требует существующего
    `.disputatio/`, поэтому «одна строка пользователю» и «полный traceback в
    журнале» — не альтернативы, а две половины одного требования. Строка
    stderr сверяется с payload'ом события: разойдись они, и по журналу
    восстанавливалась бы не та ошибка, которую увидел пользователь.
    """
    bench, _ = _interrupted_session(git_repo, monkeypatch, capsys)

    code = _resume(bench, _MISSING_SESSION_ID)

    message = capsys.readouterr().err.strip()
    errors = [event for event in _events(git_repo) if event["type"] == "error"]
    assert code == 2
    assert len(errors) == 1, errors
    recorded = errors[-1]
    assert recorded["source"] == "orchestrator"
    assert recorded["session"] == _MISSING_SESSION_ID
    assert recorded["payload"]["message"] == message
    assert _TRACEBACK_MARKER in recorded["payload"]["traceback"]
    assert SessionNotFound.__name__ in recorded["payload"]["traceback"]


@pytest.mark.parametrize("error_type", _DOMAIN_ERRORS, ids=lambda cls: cls.__name__)
def test_every_domain_error_is_one_stderr_line_and_exit_two(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[DisputatioError],
) -> None:
    """Любой наследник `DisputatioError` → код `2` и печать `.args[0]`.

    Подменяется `preflight` — самый ранний шов старта: естественных поводов
    у шести классов шесть разных, а обработчик у них один, и предмет здесь
    именно он. Ранний шов заодно пиньет тишину журнала: `.disputatio/` ещё
    нет, и попытка записать в него событие `error` создала бы каталог сессии
    в чужом репозитории ([REQ-010]).
    """
    bench = _bench(git_repo, monkeypatch)
    message = f"диагностика {error_type.__name__}: одна строка для пользователя"

    def boom(root: Path) -> None:
        """Отказ старта, каким его видит CLI."""
        raise error_type(message)

    monkeypatch.setattr(_cli(), "preflight", boom)

    code = _main(bench.run_argv())

    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == f"{message}\n"
    assert captured.out == ""
    assert not session_dir(git_repo).exists()


def test_an_unknown_subcommand_prints_usage_and_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Неизвестная подкоманда: usage в stderr и код `2` ([DESIGN-020])."""
    code = _exit_code_of(["нет-такой-подкоманды"])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "usage:" in captured.err


def test_a_missing_subcommand_prints_usage_and_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Подкоманды нет вовсе: `required=True` даёт usage и код `2`.

    Отдельный тест от неизвестного имени: `add_subparsers` без `required`
    молча разбирает пустой argv, и `main` ушёл бы не в usage, а в
    `AttributeError` на несуществующем поле разбора.
    """
    code = _exit_code_of([])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "usage:" in captured.err
