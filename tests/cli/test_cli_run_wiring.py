"""Проводка `disp run`: то, что пережило мутационную пробу [TASK-020].

Файл отдельный не по вкусу, а по правилу: `test_cli_run.py` залочен
red-чекпоинтом задачи побайтово, и дыры, найденные пробой уже после него,
закрываются рядом, а не правкой залоченного набора.

Три дыры, каждая со своим мутантом, пережившим основной набор:

* **`except Exception: return state`** — CLI, глушащий любое исключение,
  отчитался бы кодом `1` о сессии, которая не провалилась, а сломала
  оркестратор. Различие наблюдаемо только на недоменной ошибке из шага.
* **`store.save` начального состояния** ([DESIGN-019], шаг 4) — без него
  `session.json` появляется лишь первым переходом цикла, и окно между
  напечатанным id и первым `save` оставляет пользователя с именем сессии,
  которой на диске нет.
* **`StepContext.gates`** — весь основной набор гоняет сессии без гейтов,
  поэтому пустой кортеж на этом месте выживал: `gate_started` эмитит шаг из
  `ctx.gates`, а не верификатор ([DESIGN-004]), и потерянный список молча
  лишил бы UI половины §8.
"""

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
    Mode,
    Review,
    Role,
    SessionPhase,
    Verdict,
)
from disputatio.events import FileStateStore
from disputatio.runtime import AgentConfig, LimitsConfig, RuntimeConfig
from disputatio.runtime.layout import session_dir
from disputatio.runtime.steps import StepContext
from disputatio.verifier import GateSpec

_FROZEN_NOW = datetime(2026, 8, 10, 15, 34, 56, tzinfo=timezone(timedelta(hours=3)))

_TASK_TEXT = "Разбери экспорт CSV"
_ADAPTER_NAME = "claude_code"
_AUTHORED_FILE = "feature.py"
_REVIEWER_FLAG = "--allowedTools"

# Гейт, который есть на любой машине, где идёт этот набор: тесты и так
# требуют git. Дорогой команды здесь быть не должно — предмет проверки в
# том, что список гейтов доехал до шага, а не в том, что он делает.
_GATE_NAME = "версия-git"
_GATE_CMD = "git --version"


class _AgentBoom(RuntimeError):
    """Сбой порта, не имеющий отношения к форме вывода агента.

    Не из `SCHEMA_INVALID_ERRORS`: schema-retry лечит вывод не той формы, а
    здесь ломается сам запуск — то есть оркестратор, а не агент.
    """


class _LoopNotReached(RuntimeError):
    """Цикл до работы не допущен: проверяется состояние диска ДО него."""


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
    raises: bool = False
    argvs: list[tuple[str, ...]] = field(default_factory=list)

    async def __call__(self, *argv: str, cwd: str | None = None) -> _FakeProcess:
        """Отдаёт ответ по роли либо ломается недоменной ошибкой."""
        self.argvs.append(argv)
        if self.raises:
            raise _AgentBoom("запуск агента сорвался")
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
    """`main(argv, now=…)` на замороженных часах — CLI как функция."""
    main = _attr(_cli(), "main", what="disputatio/cli.py")
    code: int = main(list(argv), now=lambda: _FROZEN_NOW)
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
        f"Разбор раунда {round_no:03d}.\n"
    )


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


def _profile(*, gates: tuple[GateSpec, ...] = ()) -> RuntimeConfig:
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
        gates=gates,
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


def _setup(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gates: tuple[GateSpec, ...] = (),
    raises: bool = False,
) -> SpyLauncher:
    """Кладёт профиль рядом с репозиторием и собирает подмены запуска."""
    _install_export_step(monkeypatch)
    (repo.parent / "profile.toml").write_text(
        _profile(gates=gates).render_toml(), encoding="utf-8"
    )
    launcher = SpyLauncher(
        author_replies=[_proposal(1)], reviewer_replies=[_approve(1)], raises=raises
    )
    _register_adapter(monkeypatch, launcher=launcher)
    return launcher


def _argv(repo: Path, *extra: str) -> list[str]:
    """`argv` подкоманды `run` для репозитория `repo`."""
    return [
        "run",
        "--root",
        str(repo),
        "--config",
        str(repo.parent / "profile.toml"),
        *extra,
        _TASK_TEXT,
    ]


def _session_id(capsys: pytest.CaptureFixture[str]) -> str:
    """Первая строка stdout — `session_id` запущенной сессии."""
    lines = capsys.readouterr().out.splitlines()
    assert lines, "stdout пуст: session_id не напечатан"
    return lines[0]


def _events(repo: Path, event_type: str) -> list[dict[str, Any]]:
    """События журнала сессии типа `event_type`, в порядке записи."""
    text = (session_dir(repo) / "events.jsonl").read_text(encoding="utf-8")
    payloads: list[dict[str, Any]] = [json.loads(line) for line in text.splitlines()]
    return [event for event in payloads if event["type"] == event_type]


def test_a_broken_port_is_not_reported_as_a_failed_session(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Недоменная ошибка шага уходит наружу, а не превращается в код `1`.

    Код `1` обещает исход сессии, записанный в `session.json`. Отдай его CLI
    за любое исключение — и сломанный оркестратор отчитывался бы «агент не
    справился», а состояние на диске осталось бы в рабочей фазе, никакого
    `FAILED` не называя.
    """
    _setup(git_repo, monkeypatch, raises=True)

    with pytest.raises(_AgentBoom):
        _main(_argv(git_repo))

    session_id = _session_id(capsys)
    assert FileStateStore(git_repo).load(session_id).state is SessionPhase.PROPOSING


def test_initial_state_is_on_disk_before_the_loop_starts(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`session.json` записан до цикла ([DESIGN-019], шаг 4).

    Цикл до работы не допускается вовсе: первый же его переход сохранил бы
    состояние сам, и различить «сохранено до» и «сохранено циклом» после
    успешного прогона было бы нечем.
    """
    _setup(git_repo, monkeypatch)

    async def refuse(ctx: StepContext) -> None:
        """Стоп-заглушка цикла: до шагов сессия дойти не должна."""
        raise _LoopNotReached("цикл не запускается в этом тесте")

    monkeypatch.setattr(_cli(), "drive", refuse)

    with pytest.raises(_LoopNotReached):
        _main(_argv(git_repo))

    session_id = _session_id(capsys)
    state = FileStateStore(git_repo).load(session_id)
    assert state.state is SessionPhase.IDLE
    assert state.current_round == 0
    assert state.task.prompt == _TASK_TEXT


def test_gates_from_the_profile_reach_the_step_that_announces_them(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Гейты профиля доезжают и до событий §8, и до отчёта проверок.

    `gate_started` эмитит шаг из `ctx.gates`, а `gate_finished` — из
    результатов верификатора ([DESIGN-004]): потерянный по дороге список
    оставил бы UI без половины пары, а сессию — зелёной.
    """
    _setup(
        git_repo,
        monkeypatch,
        gates=(GateSpec(name=_GATE_NAME, cmd=_GATE_CMD, enabled=True),),
    )

    code = _main(_argv(git_repo, "--mode", "analyze"))

    session_id = _session_id(capsys)
    started = _events(git_repo, "gate_started")
    finished = _events(git_repo, "gate_finished")
    assert code == 0
    assert [event["payload"]["name"] for event in started] == [_GATE_NAME]
    assert [event["payload"]["cmd"] for event in started] == [_GATE_CMD]
    assert [event["payload"]["status"] for event in finished] == ["pass"]
    assert FileStateStore(git_repo).load(session_id).state is SessionPhase.DONE
