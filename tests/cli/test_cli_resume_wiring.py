"""Проводка `disp resume`: то, что пережило мутационную пробу [TASK-021].

Файл отдельный не по вкусу, а по правилу: `test_cli_resume.py` залочен
red-чекпоинтом задачи побайтово, и дыры, найденные пробой уже после него,
закрываются рядом, а не правкой залоченного набора.

Две дыры, обе — на шве, который есть только у `resume`:

* **Исход провалившейся сессии.** У `run` состояние лежит в собранном до
  цикла `StepContext`, у `resume` — внутри `resume_session`, и спросить его
  можно только с диска. Отвечай CLI «исхода нет» — сессия, исчерпавшая
  schema-повторы, уходила бы пользователю голым traceback'ом вместо кода
  `1`, хотя `FAILED` записан в `session.json` (NFR-003, [REQ-019]).
* **Часы продолженной сессии.** `now` инжектируется ровно затем, чтобы часы
  были одни на весь запуск; потерянный по дороге в `resume_session`
  аргумент незаметен на кодах возврата и виден только в `ts` событий,
  дописанных вторым процессом.
"""

import asyncio
import json
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
from disputatio.runtime import AgentConfig, LimitsConfig, RuntimeConfig
from disputatio.runtime.layout import session_dir
from disputatio.runtime.steps import StepContext

_FROZEN_NOW = datetime(2026, 8, 10, 15, 34, 56, tzinfo=timezone(timedelta(hours=3)))

_TASK_TEXT = "Почини экспорт CSV"
_ADAPTER_NAME = "claude_code"
_AUTHORED_FILE = "feature.py"
_REVIEWER_FLAG = "--allowedTools"

_KILL = "<обрыв процесса агента>"
_NOT_A_PROPOSAL = "ЭТО НЕ ФРОНТМАТТЕР ПРЕДЛОЖЕНИЯ"
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
    """Граница порождения процесса: argv настоящий, процесса нет."""

    author_replies: list[str]
    reviewer_replies: list[str]
    argvs: list[tuple[str, ...]] = field(default_factory=list)

    async def __call__(self, *argv: str, cwd: str | None = None) -> _FakeProcess:
        """Отдаёт очередной ответ роли, попутно изображая работу автора."""
        self.argvs.append(argv)
        assert cwd is not None, "адаптер обязан назвать рабочую директорию"
        if _REVIEWER_FLAG in argv:
            queue, who = self.reviewer_replies, "ревьюер"
        else:
            (Path(cwd) / _AUTHORED_FILE).write_text(
                f"# работа автора, вызов {len(self.argvs)}\n", encoding="utf-8"
            )
            queue, who = self.author_replies, "автор"
        assert queue, f"очередь ответов исчерпана: {who} вызван лишний раз"
        reply = queue.pop(0)
        if reply == _KILL:
            raise _ProcessKilled("процесс агента убит между правкой и ответом")
        return _FakeProcess(reply)


def _cli() -> ModuleType:
    """Модуль `disputatio/cli.py`."""
    return import_module("disputatio.cli")


def _main(argv: Sequence[str]) -> int:
    """`main(argv, now=…)` на замороженных часах — CLI как функция (ADR-007)."""
    code: int = _cli().main(list(argv), now=lambda: _FROZEN_NOW)
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


def _profile(*, schema_retries: int) -> RuntimeConfig:
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


def _install_fakes(monkeypatch: pytest.MonkeyPatch, *, launcher: SpyLauncher) -> None:
    """Минирует порождение процессов, ставит экспорт-заглушку и адаптер."""

    async def boom(*argv: object, **kwargs: object) -> None:
        raise AssertionError(f"запущен реальный процесс агента: {argv}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    loop_module = import_module("disputatio.runtime.loop")

    def fake_export(ctx: StepContext) -> None:
        """Изображает экспорт [TASK-024]: переводит сессию в `DONE`."""
        ctx.fsm.transition(SessionPhase.DONE)

    monkeypatch.setattr(
        loop_module,
        "STEP_BY_PHASE",
        {**loop_module.STEP_BY_PHASE, SessionPhase.EXPORTING: fake_export},
    )

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


def _interrupted_session(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    author_tail: str,
    schema_retries: int = 1,
) -> str:
    """Сессия, убитая в PROPOSING раунда 2; `author_tail` ждёт второй процесс.

    Подготовка двухраундовая по §5.1: `approve` раунда 1 в `develop` с
    правками кода схождением не является, поэтому раунд 1 закрывает
    `request_changes`, а очередь автора помнит съеденный обрывом слот.
    """
    config_path = repo.parent / "profile.toml"
    config_path.write_text(
        _profile(schema_retries=schema_retries).render_toml(), encoding="utf-8"
    )
    launcher = SpyLauncher(
        author_replies=[_proposal(1), _KILL, author_tail],
        reviewer_replies=[_request_changes(1), _approve(2)],
    )
    _install_fakes(monkeypatch, launcher=launcher)

    with pytest.raises(_ProcessKilled):
        _main(["run", "--root", str(repo), "--config", str(config_path), _TASK_TEXT])

    session_id = capsys.readouterr().out.splitlines()[0]
    assert FileStateStore(repo).load(session_id).state is SessionPhase.PROPOSING
    return session_id


def _events(repo: Path) -> list[dict[str, Any]]:
    """Строки журнала сессии, разобранные в словари, в порядке записи."""
    text = (session_dir(repo) / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_a_resumed_session_that_fails_exits_one_instead_of_a_traceback(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Провал продолженной сессии — код `1`, а не traceback (NFR-003).

    Шаг, исчерпавший schema-повторы, поднимает ошибку последней попытки
    ([DESIGN-006]), но `FAILED` к этому моменту уже записан ядром. У `resume`
    состояние живёт внутри `resume_session`, и спросить его CLI может только
    с диска: не спроси — и определённый исход сессии ушёл бы пользователю
    голым traceback'ом.
    """
    session_id = _interrupted_session(
        git_repo, monkeypatch, capsys, author_tail=_NOT_A_PROPOSAL, schema_retries=0
    )

    code = _main(["resume", "--root", str(git_repo), session_id])

    captured = capsys.readouterr()
    assert code == 1
    assert FileStateStore(git_repo).load(session_id).state is SessionPhase.FAILED
    assert _TRACEBACK_MARKER not in captured.out + captured.err


def test_the_injected_clock_reaches_the_resumed_session(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Часы `main` доезжают до продолженной сессии ([DESIGN-019]).

    Часы сессии обязаны быть одни на весь запуск, а инжекция — единственное,
    что делает тест детерминированным. Потерянный по дороге в
    `resume_session` аргумент на кодах возврата не виден вовсе: он виден
    только в `ts` переходов, дописанных вторым процессом.
    """
    session_id = _interrupted_session(
        git_repo, monkeypatch, capsys, author_tail=_proposal(2)
    )
    before = len(_events(git_repo))

    code = _main(["resume", "--root", str(git_repo), session_id])

    appended = _events(git_repo)[before:]
    stamps = {event["ts"] for event in appended if event["type"] == "state_change"}
    assert code == 0
    assert stamps, "продолженная сессия не записала ни одного перехода"
    assert {datetime.fromisoformat(stamp) for stamp in stamps} == {_FROZEN_NOW}
