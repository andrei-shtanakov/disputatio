"""Общие фикстуры всего `tests/`: герметичный git и фейки портов.

[REQ-023], [DESIGN-023], [ADR-008]. Корневой conftest виден каждому набору,
включая принятые волны 0–1, которые настраивают герметичность сами, —
поэтому здесь **ни одной autouse-фикстуры** и ни одного изменения
глобального состояния на импорте. Всё, что делает модуль при импорте, —
объявляет фикстуры и дата-классы; окружение трогает только явно
запрошенный `git_env`, и только через `monkeypatch` (с откатом).

Импорт `disputatio.contracts` отложен внутрь `FakeAdapter.run` и ветку
`TYPE_CHECKING`: сбой импорта на уровне корневого conftest'а уронил бы
collection ВСЕГО `tests/`. Отсрочка заведена, когда рядом жил набор
`tests/harness/**`, не зависевший от продуктового пакета; набор удалён
вместе с локальным гейтом, а свойство «корневой conftest не тянет пакет на
импорте» осталось полезным само по себе.
"""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from disputatio.contracts import (
        AgentAdapter,
        AgentTurn,
        VerificationReport,
        Verifier,
    )

# Унаследованные переменные расположения репозитория перебивают `cwd`:
# при абсолютном `GIT_DIR` (обёртки git-хуков, экспорт в шелле
# разработчика) команды фикстуры отработают успешно, но `.git` в `tmp_path`
# не появится — `config` перепишет `user.*` ЧУЖОГО репозитория, а
# `add`/`commit` положат туда настоящий коммит. Поэтому `delenv` идёт до
# первой git-команды, а не рядом с ней.
_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")

# Переменные подписи перебивают локальный `user.*`: экспортированные
# `GIT_AUTHOR_NAME`/`GIT_COMMITTER_EMAIL` (шелл разработчика, CI, обёртки
# `git commit`) подпишут коммит фикстуры чужим именем — `config` отработает,
# а `%an`/`%cn` окажутся внешними. `GIT_*_DATE` делает коммит ещё и
# недетерминированным по времени.
_IDENTITY_VARS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
)

# `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n` — конфиг прямо из окружения, по
# приоритету он выше локального `.git/config` и не отключается ни
# `GIT_CONFIG_GLOBAL`, ни `GIT_CONFIG_NOSYSTEM`. Достаточно снять счётчик:
# без него git не читает пары.
_CONFIG_INJECTION_VAR = "GIT_CONFIG_COUNT"

_FIXTURE_USER_NAME = "disputatio-tests"
_FIXTURE_USER_EMAIL = "tests@disputatio.local"


@pytest.fixture
def git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отвязывает git от внешнего окружения и глобального конфига.

    Глобальный/системный gitconfig разработчика (`commit.gpgsign`,
    `core.hooksPath`, `commit.template`, `includeIf`) сорвал бы коммит
    фикстуры по причинам, не связанным с тестом; `GIT_CONFIG_NOSYSTEM`
    закрывает системный конфиг даже там, где `GIT_CONFIG_SYSTEM`
    игнорируется сборкой git. Снимаются три группы переменных: расположение
    репозитория, подпись коммита и конфиг из окружения — каждая перебивает
    то, что фикстура настраивает локально.
    """
    for var in (*_LOCATION_VARS, *_IDENTITY_VARS, _CONFIG_INJECTION_VAR):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(workdir: Path, *args: str) -> None:
    """Запускает `git *args` в `workdir`; ненулевой код возврата — ошибка.

    Окружение не подменяется: его уже привёл в порядок `git_env`, от
    которого зависит единственный вызывающий. Сбой пересобирается в
    `RuntimeError` со stderr — `CalledProcessError.__str__` печатает только
    код возврата, и причина падения фикстуры иначе не видна в отчёте.
    """
    try:
        subprocess.run(
            ["git", *args],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git {' '.join(args)} упал с кодом {exc.returncode}: "
            f"{(exc.stderr or exc.stdout or '').strip()}"
        ) from exc


@pytest.fixture
def git_repo(tmp_path: Path, git_env: None) -> Path:
    """Временный репозиторий: локальные `user.*` и ровно один коммит.

    Один коммит — это base_rev нулевого раунда: `PROPOSING` начинается с
    `git reset --hard` на последний принятый раунд, и без стартового
    коммита сбрасывать было бы не на что. `-b main` фиксирует имя ветки:
    без него оно зависит от `init.defaultBranch` — а его как раз и
    отключил `git_env`.
    """
    _git(tmp_path, "init", "--quiet", "-b", "main")
    _git(tmp_path, "config", "user.name", _FIXTURE_USER_NAME)
    _git(tmp_path, "config", "user.email", _FIXTURE_USER_EMAIL)
    (tmp_path / "README.md").write_text("disputatio test repo\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "--quiet", "-m", "initial")
    return tmp_path


@dataclass
class FakeAdapter:
    """`AgentAdapter`-фейк: очередь ответов + журнал промптов.

    `run` — именно `async def`: `runtime_checkable` проверяет только
    наличие метода, поэтому sync-подмена прошла бы `isinstance`, но
    сломалась бы у потребителя, который её ожидает awaitable.
    """

    replies: list[str]
    prompts: list[str] = field(default_factory=list)
    session_refs: list[str | None] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> "AgentTurn":
        """Журналирует вызов и отдаёт следующий ответ очереди."""
        from disputatio.contracts import AgentTurn

        self.prompts.append(prompt)
        self.session_refs.append(session_ref)
        assert self.replies, (
            f"FakeAdapter: очередь ответов исчерпана, лишний промпт {prompt!r}"
        )
        return AgentTurn(text=self.replies.pop(0), session_ref=session_ref)


@dataclass
class FakeVerifier:
    """`Verifier`-фейк: заранее заданные `VerificationReport` по раундам."""

    reports: dict[int, "VerificationReport"]
    rounds: list[int] = field(default_factory=list)

    def verify(self, round_no: int) -> "VerificationReport":
        """Журналирует раунд и отдаёт настроенный для него отчёт."""
        self.rounds.append(round_no)
        assert round_no in self.reports, (
            f"FakeVerifier: отчёт для раунда {round_no} не задан"
        )
        return self.reports[round_no]


if TYPE_CHECKING:
    # Structural-check фейков поручен pyrefly: `isinstance` с
    # `runtime_checkable` видит только имена методов, а расхождение
    # сигнатур (или sync вместо async) ловится лишь присваиванием
    # переменной типа порта.
    _ADAPTER_PORT: "AgentAdapter" = FakeAdapter(replies=[])
    _VERIFIER_PORT: "Verifier" = FakeVerifier(reports={})
