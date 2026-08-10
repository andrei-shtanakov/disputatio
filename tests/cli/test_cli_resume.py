"""`disp resume` и диагностика без traceback ([REQ-020], [DESIGN-020], [TASK-021]).

Продолжение сессии проверяется на настоящем обрыве, а не на подложенном
`session.json`: сессия сначала запускается `disp run` и рвётся посреди
`PROPOSING` недоменной ошибкой из шва порождения процесса — ровно так, как её
оборвал бы kill. Всё, что resume обязан сделать, наблюдаемо на диске: коммиты
принятых раундов, артефакты `rounds/NNN/` и побайтовый префикс журнала.

Пять решений, без которых тесты пинили бы не то:

* **Обрыв изображает launcher, а не подмена состояния.** Записанный
  `session.json` — результат write-ahead цикла, и сессия, собранная тестом
  руками, доказывала бы совместимость resume с фантазией теста, а не с тем,
  что оркестратор действительно оставляет после обрыва ([REQ-014]).
* **Внешний профиль после обрыва портится нарочно.** Конфиг продолжения —
  снапшот `.disputatio/config.toml` и только он ([DESIGN-014]); реализация,
  читающая профиль окружения, отказала бы здесь `ConfigError`, а не молча
  продолжила сессию с чужими лимитами и гейтами.
* **Traceback ищется в двух местах сразу.** «Пользователю не показан» и «в
  журнале есть» — две половины NFR-003, и каждая по отдельности выполняется
  тривиально: первая проглатыванием ошибки, вторая печатью traceback'а в
  stderr.
* **Подкоманда берётся через `_attr`.** До реализации `cmd_resume` argparse
  отвечает на `resume` не assertion'ом, а `SystemExit(2)`: red-чекпоинт на
  таком падении был бы нечестным.
* **Шаг `EXPORTING` подменён заглушкой** — настоящий `exporting.export` в
  `STEP_BY_PHASE` ещё не зарегистрирован ([TASK-024]), а предмет здесь — код
  возврата и состояние на диске, а не содержимое `result/`.
"""

import asyncio
import subprocess
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from disputatio.adapters import ClaudeCodeAdapter
from disputatio.contracts import (
    Event,
    EventSink,
    EventSource,
    EventType,
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
from disputatio.runtime.layout import round_dir, session_dir
from disputatio.runtime.steps import StepContext

_CLOCK_ZONE = timezone(timedelta(hours=3))
_FROZEN_NOW = datetime(2026, 8, 10, 15, 34, 56, tzinfo=_CLOCK_ZONE)

_TASK_TEXT = "Почини экспорт CSV"
_ADAPTER_NAME = "claude_code"
_AUTHORED_FILE = "feature.py"

# Единственное, чем argv ревьюера отличается от argv автора (§7).
_REVIEWER_FLAG = "--allowedTools"

# Ответ-«обрыв»: очередь отдаёт его вместо текста, и launcher падает
# недоменной ошибкой — процесс, убитый посреди шага, выглядит для цикла
# именно так.
_KILL = "<<обрыв процесса>>"

_GARBAGE_PROFILE = "ЭТО НЕ TOML И НИКОГДА ИМ НЕ БЫЛО"
_UNPARSABLE_PROPOSAL = "ЭТО НЕ ФРОНТМАТТЕР ПРЕДЛОЖЕНИЯ"
_MISSING_SESSION_ID = "20200101-000000-dead"

_ROUND_ARTIFACTS = frozenset(
    {
        "proposal.md",
        "changes.patch",
        "verification.json",
        "review.json",
        "decision.json",
    }
)

# Наследники [DESIGN-020], которые CLI обязан перевести в одну строку и код
# `2`. Список именно перечислен, а не собран из `__subclasses__`: требование
# называет эти шесть, и автоматический обход зеленел бы и на пустом наборе.
_DOMAIN_ERRORS = (
    DirtyWorkingTree,
    NotAGitRepository,
    SessionNotFound,
    UnknownAdapterError,
    ConfigError,
    ReviewParseError,
)


class _Interrupted(RuntimeError):
    """Обрыв процесса посреди шага: не доменная ошибка и не форма вывода."""


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

    Один экземпляр обслуживает и запуск, и продолжение: очередь ответов
    сквозная, поэтому «сколько раз позвали автора после обрыва» видно прямо
    в ней, а не выводится из двух независимых журналов. Роль различается по
    argv, а не по счётчику вызовов ([DESIGN-001]).
    """

    author_replies: list[str]
    reviewer_replies: list[str]
    edits: bool = True
    argvs: list[tuple[str, ...]] = field(default_factory=list)

    async def __call__(self, *argv: str, cwd: str | None = None) -> _FakeProcess:
        """Отдаёт очередной ответ роли, попутно изображая работу автора."""
        self.argvs.append(argv)
        assert cwd is not None, "адаптер обязан назвать рабочую директорию"
        if _REVIEWER_FLAG in argv:
            return _FakeProcess(_next_reply(self.reviewer_replies, "ревьюер"))
        reply = _next_reply(self.author_replies, "автор")
        if reply == _KILL:
            raise _Interrupted("процесс убит посреди работы автора")
        if self.edits:
            (Path(cwd) / _AUTHORED_FILE).write_text(
                f"# работа автора, вызов {len(self.argvs)}\n", encoding="utf-8"
            )
        return _FakeProcess(reply)


@dataclass
class Bench:
    """Собранное окружение запуска: репозиторий, путь профиля и спай."""

    root: Path
    config_path: Path
    launcher: SpyLauncher

    def run_argv(self) -> list[str]:
        """`argv` подкоманды `run` с профилем вне рабочего дерева."""
        return [
            "run",
            "--root",
            str(self.root),
            "--config",
            str(self.config_path),
            _TASK_TEXT,
        ]

    def spoil_profile(self) -> None:
        """Портит внешний профиль: продолжение обязано читать снапшот."""
        self.config_path.write_text(_GARBAGE_PROFILE, encoding="utf-8")


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


def _resume(root: Path, session_id: str) -> int:
    """`disp resume <id>`; отсутствие `cmd_resume` — `AssertionError`.

    Проверка символа стоит до вызова сознательно: без подкоманды argparse
    отвечает `SystemExit(2)`, а красный чекпоинт обязан падать assertion'ом.
    """
    _attr(_cli(), "cmd_resume", what="disputatio/cli.py")
    return _main(["resume", "--root", str(root), session_id])


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


def _profile(*, schema_retries: int = 1) -> RuntimeConfig:
    """Профиль запуска; поля, которыми владеет запуск, заведомо негодные."""
    return RuntimeConfig(
        session_id="ИДЕНТИФИКАТОР-ИЗ-ПРОФИЛЯ",
        mode=Mode.ANALYZE,
        base_commit="0" * 40,
        task_prompt="ЗАДАЧА ИЗ ПРОФИЛЯ",
        author=AgentConfig(adapter=_ADAPTER_NAME, model="opus"),
        reviewer=AgentConfig(adapter=_ADAPTER_NAME, model="sonnet"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=400_000,
            max_wall_seconds=3600,
            schema_retries=schema_retries,
        ),
        gates=(),
        attachments=(),
    )


def _forbid_real_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Минирует порождение процессов: ни один агентский CLI не запускается."""

    async def boom(*argv: object, **kwargs: object) -> None:
        raise AssertionError(f"запущен реальный процесс агента: {argv}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)


def _install_export_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ставит временный шаг `EXPORTING`, доводящий сессию до `DONE`."""
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


def _bench(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    author_replies: Sequence[str],
    reviewer_replies: Sequence[str],
    schema_retries: int = 1,
) -> Bench:
    """Кладёт профиль ВНЕ репозитория и собирает подмены запуска.

    Профиль снаружи не по вкусу: untracked-файл в рабочем дереве уезжает в
    `changes.patch` через intent-to-add и портит раунды по причине, к CLI
    отношения не имеющей.
    """
    _forbid_real_processes(monkeypatch)
    _install_export_step(monkeypatch)

    config_path = repo.parent / "profile.toml"
    config_path.write_text(
        _profile(schema_retries=schema_retries).render_toml(), encoding="utf-8"
    )
    launcher = SpyLauncher(
        author_replies=list(author_replies), reviewer_replies=list(reviewer_replies)
    )
    _register_adapter(monkeypatch, launcher=launcher)
    return Bench(root=repo, config_path=config_path, launcher=launcher)


def _session_id(capsys: pytest.CaptureFixture[str]) -> str:
    """Первая строка stdout — `session_id` запущенной сессии ([REQ-019])."""
    lines = capsys.readouterr().out.splitlines()
    assert lines, "stdout пуст: session_id не напечатан"
    return lines[0]


def _finished_session(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    """Доводит сессию до `DONE` и отдаёт её id: журнал сессии заполнен."""
    bench = _bench(
        repo,
        monkeypatch,
        author_replies=[_proposal(1)],
        reviewer_replies=[_approve(1)],
    )
    assert _main(bench.run_argv()) == 0, "подготовка: сессия не дошла до DONE"
    return _session_id(capsys)


def _journal_bytes(repo: Path) -> bytes:
    """Сырые байты `events.jsonl` — предмет побайтового префикса ([REQ-016])."""
    return (session_dir(repo) / "events.jsonl").read_bytes()


def _events(repo: Path) -> list[Event]:
    """Журнал сессии, разобранный в схему `Event` ([REQ-016])."""
    text = _journal_bytes(repo).decode("utf-8")
    return [Event.model_validate_json(line) for line in text.splitlines()]


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


def test_resume_drives_the_interrupted_session_to_a_terminal_state(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Оборванная сессия продолжается с сохранённой фазы ([REQ-014]…[REQ-016]).

    Сессия рвётся уже во втором раунде: обрыв на первом не отличал бы «продолжил
    с сохранённой фазы» от «начал сначала». Поэтому продолжение пиньется тремя
    независимыми следами — коммит раунда 001 остался ровно один (раунд не
    переигран), автор после обрыва вызван ровно столько раз, сколько ответов
    осталось в очереди, и журнал дописан, а не переписан.

    Конфиг продолжения — снапшот сессии: внешний профиль перед resume испорчен,
    и реализация, читающая окружение, отказала бы `ConfigError` вместо `DONE`.
    """
    bench = _bench(
        git_repo,
        monkeypatch,
        author_replies=[_proposal(1), _KILL, _proposal(2)],
        reviewer_replies=[_request_changes(1), _approve(2)],
    )

    with pytest.raises(_Interrupted):
        _main(bench.run_argv())

    session_id = _session_id(capsys)
    interrupted = FileStateStore(git_repo).load(session_id)
    journal_before = _journal_bytes(git_repo)
    assert interrupted.state is SessionPhase.PROPOSING
    assert interrupted.current_round == 2
    bench.spoil_profile()

    code = _resume(git_repo, session_id)

    state = FileStateStore(git_repo).load(session_id)
    second = round_dir(git_repo, 2)
    assert code == 0
    assert state.state is SessionPhase.DONE
    assert state.current_round == 2
    assert state.task.prompt == _TASK_TEXT
    assert bench.launcher.author_replies == []
    assert bench.launcher.reviewer_replies == []
    assert _round_subjects(git_repo)[:2] == [
        "disputatio: round 002",
        "disputatio: round 001",
    ]
    # [REQ-015]: прерванный шаг переигран целиком — артефакт раунда на месте,
    # валиден по схеме, и ни одного temp-файла `atomic_write` рядом не осталось.
    names = {path.name for path in second.iterdir()}
    review = Review.model_validate_json(
        (second / "review.json").read_text(encoding="utf-8")
    )
    assert _ROUND_ARTIFACTS <= names
    assert [name for name in names if name.endswith(".tmp")] == []
    assert (second / "proposal.md").read_text(encoding="utf-8") == _proposal(2)
    assert review.verdict is Verdict.APPROVE
    # [REQ-016]: журнал до обрыва — побайтовый префикс журнала после resume.
    assert _journal_bytes(git_repo).startswith(journal_before)
    assert len(_events(git_repo)) > len(journal_before.splitlines())
    assert {event.session for event in _events(git_repo)} == {session_id}


def test_resume_of_a_session_that_runs_out_of_retries_exits_one(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Продолженная сессия, записавшая `FAILED`, уходит кодом `1`, не `2`.

    `FAILED` — исход сессии, а не отказ старта, и различает их только то, что
    уже записано в `session.json`. Реализация, отдавшая ошибку последней
    попытки наружу как доменную, вернула бы `2` и объявила бы сорвавшуюся
    сессию несостоявшейся.
    """
    bench = _bench(
        git_repo,
        monkeypatch,
        author_replies=[_KILL, _UNPARSABLE_PROPOSAL],
        reviewer_replies=[],
        schema_retries=0,
    )

    with pytest.raises(_Interrupted):
        _main(bench.run_argv())

    session_id = _session_id(capsys)

    code = _resume(git_repo, session_id)

    captured = capsys.readouterr()
    assert code == 1
    assert FileStateStore(git_repo).load(session_id).state is SessionPhase.FAILED
    assert "Traceback" not in captured.err


def test_an_unknown_session_id_exits_two_with_one_line_and_no_traceback(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Неизвестный `session_id`: одна строка stderr, код `2`, никакого traceback.

    Пиньется и то, что строка ровно одна: `traceback.print_exc()` рядом с
    сообщением тоже «печатает ошибку», и без счёта строк такая реализация
    прошла бы проверку на код возврата.
    """
    _finished_session(git_repo, monkeypatch, capsys)

    code = _resume(git_repo, _MISSING_SESSION_ID)

    captured = capsys.readouterr()
    assert code == 2
    assert len(captured.err.splitlines()) == 1, captured.err
    assert _MISSING_SESSION_ID in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_the_full_traceback_goes_to_the_event_journal_not_to_the_user(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Traceback не проглочен: он уходит событием `error` в журнал (NFR-003).

    Половина требования «пользователю не показывать» выполняется молчанием, и
    без этой второй половины отказ старта не восстанавливался бы вообще
    ничем. Проверяется именно полный traceback, а не имя класса: сообщение
    ошибки уже есть в stderr, и дублировать его в журнал смысла нет.
    """
    _finished_session(git_repo, monkeypatch, capsys)
    journal_before = _journal_bytes(git_repo)

    _resume(git_repo, _MISSING_SESSION_ID)

    errors = [event for event in _events(git_repo) if event.type is EventType.ERROR]
    assert len(errors) == 1, [event.type for event in _events(git_repo)]
    traceback_text = errors[0].payload["traceback"]
    assert errors[0].source is EventSource.ORCHESTRATOR
    assert "Traceback (most recent call last)" in traceback_text
    assert SessionNotFound.__name__ in traceback_text
    assert _journal_bytes(git_repo).startswith(journal_before)


@pytest.mark.parametrize("error_type", _DOMAIN_ERRORS, ids=lambda cls: cls.__name__)
def test_every_domain_error_exits_two_and_prints_its_own_message(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[DisputatioError],
) -> None:
    """Любой наследник `DisputatioError` → код `2` и ровно его `args[0]`.

    Ловушка ставится на `preflight`, то есть на первое же действие `cmd_run`:
    так проверяется обработчик `main`, общий для всей иерархии, а не путь
    конкретной ошибки. Печатается именно `args[0]` — `str(exc)` совпал бы с
    ним у одноаргументных исключений, но `repr` и «класс: текст» разошлись бы.
    """
    message = f"диагностика {error_type.__name__} без единого traceback'а"

    def boom(root: Path) -> None:
        """Отказ старта вместо pre-flight-проверки."""
        raise error_type(message)

    monkeypatch.setattr(_cli(), "preflight", boom)

    code = _main(["run", "--root", str(git_repo), _TASK_TEXT])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == f"{message}\n"
    assert captured.out == ""
    assert not session_dir(git_repo).exists()


def test_an_unknown_subcommand_prints_usage_and_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Неизвестная подкоманда — usage в stderr и код `2` ([DESIGN-020])."""
    with pytest.raises(SystemExit) as raised:
        _main(["продолжить-как-нибудь"])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert "usage:" in captured.err


def test_no_subcommand_at_all_prints_usage_and_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Подкоманда обязательна: пустой argv даёт usage и код `2`.

    `required=True` на subparsers — единственное, что отделяет этот случай от
    молчаливого `Namespace(command=None)` и падения `AttributeError` где-то
    дальше по коду.
    """
    with pytest.raises(SystemExit) as raised:
        _main([])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert "usage:" in captured.err
