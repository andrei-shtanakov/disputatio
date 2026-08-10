"""Диагностика `disp resume` на существующем `.disputatio/` ([REQ-020], NFR-003).

Файл отдельный не по вкусу, а вынужденно: два теста того же предмета в
`test_cli_resume.py` подготавливают сессию к `DONE` одобрением раунда 1 в
режиме `develop` с правками кода — то есть требуют схождения, которое
анти-сикофантия §5.1 запрещает по построению. Тот файл залочен побайтово
red-чекпоинтом задачи, и починка его подготовки — работа оператора (снять
claim, новый честный red-цикл), а не правка задним числом. Предмет при этом
без покрытия остаться не вправе, поэтому он проверяется здесь, на сессии,
которая до `DONE` действительно доходит: `analyze` без правок кода.

Проверяются обе половины NFR-003 сразу. «Пользователю traceback не показан» и
«traceback есть в журнале» поодиночке выполняются тривиально — первая
проглатыванием ошибки, вторая печатью traceback'а в stderr, — и только вместе
они означают то, что обещано.
"""

import asyncio
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
    Event,
    EventSink,
    EventSource,
    EventType,
    Mode,
    Review,
    Role,
    SessionPhase,
    Verdict,
)
from disputatio.runtime import AgentConfig, LimitsConfig, RuntimeConfig, SessionNotFound
from disputatio.runtime.layout import session_dir
from disputatio.runtime.steps import StepContext

_FROZEN_NOW = datetime(2026, 8, 10, 15, 34, 56, tzinfo=timezone(timedelta(hours=3)))

_TASK_TEXT = "Разбери экспорт CSV, ничего не правя"
_ADAPTER_NAME = "claude_code"
_AUTHORED_FILE = "export.py"
_REVIEWER_FLAG = "--allowedTools"
_MISSING_SESSION_ID = "20200101-000000-dead"


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
    ревьюер» совпала бы с порядком шагов и пережила бы перепутанные роли.
    Правок в рабочем дереве launcher не делает — `analyze`-сессия обязана
    сойтись на пустом патче, иначе анти-сикофантия §5.1 не пропустит
    одобрение раунда 1.
    """

    author_replies: list[str]
    reviewer_replies: list[str]
    argvs: list[tuple[str, ...]] = field(default_factory=list)

    async def __call__(self, *argv: str, cwd: str | None = None) -> _FakeProcess:
        """Отдаёт очередной ответ роли, определённой по argv."""
        self.argvs.append(argv)
        assert cwd is not None, "адаптер обязан назвать рабочую директорию"
        queue = self.reviewer_replies if _REVIEWER_FLAG in argv else self.author_replies
        assert queue, f"очередь ответов исчерпана на argv {argv}"
        return _FakeProcess(queue.pop(0))


def _attr(owner: object, name: str, *, what: str) -> Any:
    """Символ `owner.name`; отсутствие — `AssertionError`, не `AttributeError`."""
    assert hasattr(owner, name), f"{what} не определяет {name!r}"
    return getattr(owner, name)


def _cli() -> ModuleType:
    """Модуль `disputatio/cli.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.cli")
    except ImportError as exc:  # pragma: no cover - модуль реализован задачей
        raise AssertionError(f"нет модуля disputatio/cli.py: {exc}") from exc


def _main(argv: Sequence[str]) -> int:
    """`main(argv, now=…)` на замороженных часах — CLI как функция (ADR-007)."""
    main = _attr(_cli(), "main", what="disputatio/cli.py")
    code: int = main(list(argv), now=lambda: _FROZEN_NOW)
    return code


def _proposal() -> str:
    """Валидный `proposal.md` раунда 1: разбор без правок кода."""
    return (
        "---\n"
        "schema: disputatio/v1\n"
        "round: 1\n"
        "role: author\n"
        "responds_to: null\n"
        "files_touched:\n"
        f"  - {_AUTHORED_FILE}\n"
        "self_declared_status: complete\n"
        "---\n"
        "Разбор раунда 001: правок не требуется.\n"
    )


def _approve() -> str:
    """Ответ ревьюера: `approve` раунда 1 — законный для `analyze` (§5.1)."""
    review = Review(
        round=1,
        role=Role.REVIEWER,
        verdict=Verdict.APPROVE,
        confidence=0.9,
        issues=[],
        checked=[_AUTHORED_FILE],
        summary="раунд 001 принят",
    )
    return review.model_dump_json(by_alias=True)


def _profile() -> RuntimeConfig:
    """Профиль запуска; поля, которыми владеет запуск, заведомо негодные."""
    return RuntimeConfig(
        session_id="ИДЕНТИФИКАТОР-ИЗ-ПРОФИЛЯ",
        mode=Mode.DEVELOP,
        base_commit="0" * 40,
        task_prompt="ЗАДАЧА ИЗ ПРОФИЛЯ",
        author=AgentConfig(adapter=_ADAPTER_NAME, model="opus"),
        reviewer=AgentConfig(adapter=_ADAPTER_NAME, model="sonnet"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=400_000,
            max_wall_seconds=3600,
            schema_retries=1,
        ),
        gates=(),
        attachments=(),
    )


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


def _forbid_real_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Минирует порождение процессов: ни один агентский CLI не запускается."""

    async def boom(*argv: object, **kwargs: object) -> None:
        raise AssertionError(f"запущен реальный процесс агента: {argv}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)


def _finished_session(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    """Доводит `analyze`-сессию до `DONE` и отдаёт её id: журнал непуст.

    Режим и отсутствие правок здесь не декорация: одобрение раунда 1
    засчитывается §5.1 только этой парой условий, и сессия, запущенная
    иначе, ушла бы во второй раунд вместо `DONE`.
    """
    _forbid_real_processes(monkeypatch)
    _install_export_step(monkeypatch)
    config_path = repo.parent / "profile.toml"
    config_path.write_text(_profile().render_toml(), encoding="utf-8")
    _register_adapter(
        monkeypatch,
        launcher=SpyLauncher(
            author_replies=[_proposal()], reviewer_replies=[_approve()]
        ),
    )

    code = _main(
        [
            "run",
            "--root",
            str(repo),
            "--config",
            str(config_path),
            "--mode",
            Mode.ANALYZE.value,
            _TASK_TEXT,
        ]
    )

    lines = capsys.readouterr().out.splitlines()
    assert code == 0, "подготовка: сессия не дошла до DONE"
    assert lines, "подготовка: session_id не напечатан"
    return lines[0]


def _journal_bytes(repo: Path) -> bytes:
    """Сырые байты `events.jsonl` — предмет побайтового префикса ([REQ-016])."""
    return (session_dir(repo) / "events.jsonl").read_bytes()


def _events(repo: Path) -> list[Event]:
    """Журнал сессии, разобранный в схему `Event` ([REQ-016])."""
    text = _journal_bytes(repo).decode("utf-8")
    return [Event.model_validate_json(line) for line in text.splitlines()]


def _resume(root: Path, session_id: str) -> int:
    """`disp resume <id>`; отсутствие `cmd_resume` — `AssertionError`."""
    _attr(_cli(), "cmd_resume", what="disputatio/cli.py")
    return _main(["resume", "--root", str(root), session_id])


def test_an_unknown_session_id_exits_two_with_one_line_and_no_traceback(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Неизвестный `session_id`: одна строка stderr, код `2`, никакого traceback.

    Счёт строк — не придирка: `traceback.print_exc()` рядом с сообщением тоже
    «печатает ошибку», и проверка одного лишь кода возврата такую реализацию
    пропустила бы.
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

    Пиньется именно полный traceback, а не имя класса: сообщение ошибки уже
    ушло пользователю, и журнал нужен ровно затем, чтобы по нему
    восстанавливалось место отказа. Дописывание проверяется префиксом —
    диагностика не вправе усечь append-only журнал ([REQ-016]).
    """
    _finished_session(git_repo, monkeypatch, capsys)
    journal_before = _journal_bytes(git_repo)

    _resume(git_repo, _MISSING_SESSION_ID)

    errors = [event for event in _events(git_repo) if event.type is EventType.ERROR]
    assert len(errors) == 1, [event.type for event in _events(git_repo)]
    traceback_text = errors[0].payload["traceback"]
    assert errors[0].source is EventSource.ORCHESTRATOR
    assert errors[0].session == _MISSING_SESSION_ID
    assert "Traceback (most recent call last)" in traceback_text
    assert SessionNotFound.__name__ in traceback_text
    assert _journal_bytes(git_repo).startswith(journal_before)
