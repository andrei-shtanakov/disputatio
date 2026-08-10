"""Исход `disp run` после пересадки FSM бюджетом ([DESIGN-009], [TASK-014]).

Файл отдельный не по вкусу, а по правилу: `test_cli_run.py` и
`test_cli_run_wiring.py` залочены red-чекпоинтами своих задач побайтово, а
дыра здесь появилась вместе с начислением бюджета — то есть уже после них.

Дыра ровно одна и наблюдаема одним сценарием. `cmd_run` собирает
`StepContext` до цикла и спрашивал исход у `context.fsm.state`. Пока бюджета
не было, этот FSM оставался единственным на всю сессию. С [TASK-014]
начисление пересаживает цикл на НОВЫЙ `SessionFsm` после каждого шага, и
собранный в `cmd_run` замирает в фазе первого шага: сессия, упавшая на
исчерпанных schema-повторах во ВТОРОМ шаге, записывает на диск `FAILED`, а
`context.fsm.state` продолжает называть `PROPOSING`. CLI отдал бы наружу
исключение вместо кода `1` — то есть traceback вместо внятного отказа
(NFR-003, [DESIGN-020]), и различить это можно только на сессии, которая
успела начислить бюджет ДО падения.

Цикл здесь подменён: предмет проверки — что `cmd_run` читает исход с диска,
а не то, как именно шаги доводят сессию до `FAILED` (это пинят свои наборы).
Подмена делает ровно то, что делает настоящий цикл: начисляет бюджет
`budget.charge_step` и переводит пересаженный FSM в `FAILED`.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from disputatio.contracts import (
    AgentTurn,
    Mode,
    ProposalParseError,
    SessionPhase,
    SessionState,
)
from disputatio.events import FileStateStore
from disputatio.runtime import AgentConfig, LimitsConfig, RuntimeConfig
from disputatio.runtime.budget import charge_step
from disputatio.runtime.steps import StepContext

_FROZEN_NOW = datetime(2026, 8, 10, 15, 34, 56, tzinfo=timezone(timedelta(hours=3)))

_TASK_TEXT = "Разбери экспорт CSV"
_ADAPTER_NAME = "claude_code"

# Расход первого шага: сообщённое число, а не ноль, — иначе начисление не
# изменило бы состояние и пересадки бы не случилось вовсе.
_TOKENS = 120
_ELAPSED = 1.5


def _cli() -> ModuleType:
    """Модуль `disputatio/cli.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.cli")
    except ImportError as exc:  # pragma: no cover - страховка на случай сноса
        raise AssertionError(f"нет модуля disputatio/cli.py: {exc}") from exc


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
            schema_retries=0,
        ),
        gates=(),
        attachments=(),
    )


def _argv(repo: Path) -> list[str]:
    """`argv` подкоманды `run` для репозитория `repo`."""
    return [
        "run",
        "--root",
        str(repo),
        "--config",
        str(repo.parent / "profile.toml"),
        _TASK_TEXT,
    ]


def _main(argv: Sequence[str]) -> int:
    """`main(argv, now=…)` на замороженных часах — CLI как функция."""
    main: Any = _cli().main
    code: int = main(list(argv), now=lambda: _FROZEN_NOW)
    return code


def _install_charging_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет цикл на «шаг начислил бюджет, следующий провалился».

    Начисление идёт настоящим `charge_step`: подмена обязана оставить после
    себя то же, что оставляет цикл, — новый `SessionFsm` и `FAILED` на диске
    при собранном в `cmd_run` контексте, застрявшем в прежней фазе.
    """

    async def charging_drive(ctx: StepContext) -> SessionState:
        """Начисляет расход первого шага и роняет сессию во втором."""
        ctx.fsm.transition(SessionPhase.PROPOSING)
        charged = charge_step(
            ctx,
            turn=AgentTurn(text="ответ автора", tokens_used=_TOKENS),
            elapsed_s=_ELAPSED,
        )
        assert charged.fsm is not ctx.fsm, (
            "начисление не пересадило FSM — сценарий дыры не воспроизведён"
        )
        charged.fsm.transition(SessionPhase.FAILED)
        raise ProposalParseError("proposal.md обязан начинаться с '---'")

    monkeypatch.setattr(_cli(), "drive", charging_drive)


def test_failed_session_after_a_budget_charge_still_exits_one(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Исход берётся с диска, а не у собранного до цикла `StepContext`.

    Сессия провалилась ПОСЛЕ того, как начислила бюджет, то есть после
    пересадки FSM ([DESIGN-009]). `FAILED` записан на диск ядром, значит
    исход сессии определён, и CLI обязан назвать его кодом `1`. Спроси он
    прежний `context.fsm`, тот назвал бы `PROPOSING` — и наружу вместо кода
    возврата ушёл бы traceback (NFR-003).

    Начисленный расход проверяется тем же чтением: состояние на диске
    обязано быть тем самым, которое пережило пересадку, а не снимком до неё.
    """
    (git_repo.parent / "profile.toml").write_text(
        _profile().render_toml(), encoding="utf-8"
    )
    _install_charging_drive(monkeypatch)

    code = _main(_argv(git_repo))

    session_id = capsys.readouterr().out.splitlines()[0]
    on_disk = FileStateStore(git_repo).load(session_id)
    assert code == 1
    assert on_disk.state is SessionPhase.FAILED
    assert on_disk.budget_used.tokens == _TOKENS
    assert on_disk.budget_used.wall_seconds == _ELAPSED
