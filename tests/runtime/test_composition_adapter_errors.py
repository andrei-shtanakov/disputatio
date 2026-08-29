"""Отказ фабрики адаптера — доменная ошибка, а не `ValueError` ([DESIGN-020]).

Дыра, найденная ревью к [TASK-003]: `tests/runtime/test_composition.py` пинит
единственный способ провалить сборку адаптера — неизвестное имя
(`UnknownAdapterError`). Но `ADAPTER_FACTORIES` регистрирует `codex`, а
`CodexAdapter` по умолчанию не умеет granular-permissions (ADR-004): ревьюер
на нём требует worktree-операций, которых composition root не передаёт, и
конструктор падает голым `ValueError` из чужого пакета. Пользователь с
`adapter = "codex"` в `[agents.reviewer]` получал traceback вместо одной
строки — ровно то, что запрещает NFR-003.

Locked-файл задачи байт-неизменяем после red-чекпоинта, поэтому пробел
закрывается отдельным модулем рядом.
"""

from pathlib import Path
from typing import Any

import pytest

from disputatio.contracts import Mode
from disputatio.runtime import (
    AgentConfig,
    ConfigError,
    DisputatioError,
    LimitsConfig,
    RuntimeConfig,
    build_runtime,
)

from ._fakes import GitOpsFakeBase


class FakeGit(GitOpsFakeBase):
    """`GitOps`-фейк: сборке нужен объект, git-команд не запускает."""

    def diff_head(self) -> str:
        """Пустой дифф — валидный ответ."""
        return ""

    def commit_round(self, round_no: int) -> None:
        """Ничего не делает."""

    def reset_hard(self, rev: str) -> None:
        """Ничего не делает."""

    def clean(self) -> None:
        """Ничего не делает."""


def _config(*, author_adapter: str, reviewer_adapter: str) -> RuntimeConfig:
    """Конфиг сессии с заданными именами адаптеров ролей."""
    return RuntimeConfig(
        session_id="20260809-120000-a1b2",
        mode=Mode.DEVELOP,
        base_commit="9f1c2ab",
        task_prompt="почини флаки-тест",
        author=AgentConfig(adapter=author_adapter, model="opus"),
        reviewer=AgentConfig(adapter=reviewer_adapter, model="sonnet"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=400_000,
            max_wall_seconds=3600,
            schema_retries=2,
        ),
    )


def _build(config: RuntimeConfig, root: Path) -> Any:
    """Композиция с подменённым git: реализация `GitCli` — [DESIGN-010]."""
    return build_runtime(config, root, git=FakeGit())


def test_codex_reviewer_refusal_is_a_domain_error(tmp_path: Path) -> None:
    """`codex` ревьюером → `ConfigError`, а не `ValueError` из адаптеров.

    Имя адаптера зарегистрировано в `ADAPTER_FACTORIES`, то есть является
    легальным значением `config.toml`: отказ обязан приходить в той же
    иерархии, которую ловит CLI, иначе пользователь видит traceback.
    """
    config = _config(author_adapter="claude_code", reviewer_adapter="codex")

    with pytest.raises(ConfigError) as exc_info:
        _build(config, tmp_path)

    assert issubclass(ConfigError, DisputatioError)
    assert not isinstance(exc_info.value, ValueError)


def test_adapter_refusal_message_names_adapter_role_and_reason(
    tmp_path: Path,
) -> None:
    """Текст ошибки называет адаптер, роль и исходную причину отказа.

    Без причины сообщение остаётся нечитаемым «не собрался»: реальный ответ
    даёт пакет адаптеров, и его текст обязан доехать до пользователя.
    """
    config = _config(author_adapter="claude_code", reviewer_adapter="codex")

    with pytest.raises(ConfigError) as exc_info:
        _build(config, tmp_path)

    message = str(exc_info.value)
    assert "codex" in message
    assert "reviewer" in message
    cause = exc_info.value.__cause__
    assert isinstance(cause, ValueError)
    assert str(cause) in message


def test_codex_author_still_builds(tmp_path: Path) -> None:
    """Отказ адресный: автору на `codex` worktree не нужен, сборка проходит.

    Тест держит фикс от превращения в запрет `codex` целиком — иначе
    «починка» отняла бы у пользователя рабочую конфигурацию.
    """
    config = _config(author_adapter="codex", reviewer_adapter="claude_code")

    deps = _build(config, tmp_path)

    assert type(deps.author).__name__ == "CodexAdapter"
    assert deps.author.event_sink is deps.sink
