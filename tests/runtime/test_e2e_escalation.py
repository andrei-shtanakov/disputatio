"""E2E: путь без сходимости — эскалация и частичный экспорт ([REQ-025]).

Второй сквозной сценарий workstream'а и зеркало [TASK-024]: там сессия
сходилась, здесь ревьюер не соглашается никогда. Проверяется он тем же
способом — настоящей сессией, а не сборкой шагов руками: `disp run`
вызывается функцией `cli.main`, за ней стоят реальные `GitCli`,
`FileStateStore`, `JsonlEventSink`, `VerifierRunner` и настоящий
`ClaudeCodeAdapter`. Подменена ровно одна граница — `launcher` адаптера, то
есть порождение процесса: argv с `claude` собирает настоящий адаптер, но
процесса за ним нет.

Четыре вещи, ради которых тест существует и которых сумма юнитов не даёт:

* **Терминал эскалации — `DONE` и код возврата `0`.** Остановка по деадлоку
  штатна для протокола (§2.5): пользователь, получивший `1`, искал бы
  поломку оркестратора вместо разбора двух позиций.
* **Манифест честен на сессии, которую никто не подделывал.** `converged`
  обязан быть `False`, а `outcome`/`stop_reason` — теми же, что записаны в
  `decision.json` раунда-источника. Конкретный код причины НЕ пинится
  строкой: выбор между `max_rounds` и осцилляцией принадлежит §5 и
  `core.decide`, и пин сделал бы E2E хрупким к легальным правкам ядра.
  Пинится терминальная ФАЗА (`DEADLOCK`) и честность документа.
* **Коммитится ровно принятая работа** ([REQ-011]). Раунд, закрытый
  `CONTINUE`, даёт ровно один коммит `disputatio: round NNN`; раунд,
  эскалированный пользователю, не даёт ни одного — принять такую работу
  вправе только человек, и его правки остаются в рабочем дереве. Оба
  утверждения проверяются на РАЗНЫХ сессиях: с `max_rounds = 2` принят один
  раунд, с `max_rounds = 3` — два, и второй сценарий делает наблюдаемым
  «по одному коммиту на раунд», которое на единственном коммите неотличимо
  от «один коммит на сессию».
* **Ни один агентский CLI не запущен.** Ловушка стоит на
  `asyncio.create_subprocess_exec` — единственном месте, куда приходит
  `_process.default_launcher`, и единственном, которое адаптер не проносит
  мимо себя значением аргумента по умолчанию. Собери тест по недосмотру
  адаптер без спая — на машине с установленным `claude` он бы зеленел,
  разговаривая с живой моделью.

Гейт сессии настоящий и зелёный (`git rev-parse --verify HEAD`): сессия
обязана эскалироваться из-за спора, а не из-за красных проверок — с
`overall == fail` §5.1 закрыла бы сходимость и без ревьюера, и тест
доказывал бы не то.
"""

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from disputatio.adapters import ClaudeCodeAdapter
from disputatio.contracts import (
    Decision,
    EventSink,
    Issue,
    Mode,
    Outcome,
    Review,
    Role,
    SessionPhase,
    Severity,
    Verdict,
)
from disputatio.events.paths import events_jsonl_path, result_dir
from disputatio.runtime import AgentConfig, LimitsConfig, RuntimeConfig
from disputatio.runtime.layout import (
    DECISION_NAME,
    PROPOSAL_NAME,
    REVIEW_NAME,
    round_artifact,
    round_dir,
    session_dir,
)
from disputatio.verifier import GateSpec

_FROZEN_NOW = datetime(2026, 8, 10, 11, 0, 0, tzinfo=UTC)

_TASK_PROMPT = "Почини экспорт CSV: разделитель берётся из локали."

# Имя адаптера настоящее: подмена собственного имени в реестре спрятала бы
# ошибку сборки, ради которой и стоит ловушка на запуск процесса.
_ADAPTER_NAME = "claude_code"

_AUTHORED_FILE = "csv_export.py"

# Единственное, чем argv ревьюера отличается от argv автора (§7), — и,
# значит, единственный честный способ различить роли на границе launcher'а.
_REVIEWER_FLAG = "--allowedTools"

# Гейт заведомо зелёный и заведомо offline: сессия обязана встать из-за
# спора, а не из-за красных проверок.
_GATE = GateSpec(name="head", cmd="git rev-parse --verify HEAD", enabled=True)

_COMMIT_PREFIX = "disputatio: round "

# Поля открытого замечания в манифесте §3.2 — ровно четыре: манифест читает
# человек, и `evidence`/`suggestion` остаются в артефактах раунда.
_ISSUE_KEYS = frozenset({"claim", "file", "id", "severity"})

# Значения профиля, которыми владеет запуск: каждое заведомо негодное, чтобы
# протечка профиля в сессию была наблюдаемой, а не безобидной.
_PROFILE_ID = "ИДЕНТИФИКАТОР-ИЗ-ПРОФИЛЯ"
_PROFILE_PROMPT = "ЗАДАЧА ИЗ ПРОФИЛЯ"
_PROFILE_BASE_COMMIT = "0" * 40


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
    """Процесс, которого нет: stdout из готового текста, код возврата 0."""

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

    Автор вдобавок делает то, что делает настоящий, — правит рабочее дерево:
    без этого `changes.patch` был бы пуст, `commit_round` не создал бы
    коммита, а раунд N+1 сбрасывался бы не на работу раунда N. Сквозной
    тест, в котором никто не написал ни строки кода, проверял бы половину
    стыка.

    Роль различается по argv, а не по счётчику вызовов: очередь «автор,
    ревьюер, автор, ревьюер» совпала бы с порядком шагов и молча пережила бы
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
        turn = len(self.argvs)
        (Path(cwd) / _AUTHORED_FILE).write_text(
            f"# работа автора, вызов {turn}\nDELIMITER = {turn!r}\n",
            encoding="utf-8",
        )
        return _FakeProcess(_next_reply(self.author_replies, "автор"))


@dataclass
class Session:
    """Прогнанная сессия: репозиторий, код возврата и журналы спаев."""

    root: Path
    exit_code: int
    launcher: SpyLauncher
    launched: list[tuple[str, ...]]


def _next_reply(queue: list[str], who: str) -> str:
    """Следующий ответ очереди; исчерпание — падение с внятным текстом."""
    assert queue, f"очередь ответов исчерпана: {who} вызван лишний раз"
    return queue.pop(0)


def _proposal(round_no: int) -> str:
    """`proposal.md` раунда `round_no` — ответ автора с YAML-фронтматтером."""
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
        f"Работа раунда {round_no:03d}: разделитель вынесен в конфиг.\n"
    )


def _issue(round_no: int) -> Issue:
    """Замечание ревью раунда `round_no`: своё в каждом раунде.

    Идентификаторы и файлы разные нарочно: повтор одного замечания — это
    осцилляция §5.3, то есть ДРУГАЯ причина остановки. Сценарий обязан
    упираться в исчерпание раундов, а не в эвристику повтора, иначе
    `max_rounds` в конфиге ничего бы не значил.
    """
    marker = f"{round_no:03d}"
    return Issue(
        id=f"I-{marker}",
        severity=Severity.MAJOR,
        file=f"{marker}_{_AUTHORED_FILE}",
        claim=f"раунд {marker}: разделитель всё ещё читается из локали",
        evidence=f"{_AUTHORED_FILE}:2 — DELIMITER зависит от окружения",
        suggestion="взять разделитель из конфига сессии",
    )


def _request_changes(round_no: int) -> str:
    """Ответ ревьюера: `request_changes` с обоснованным major-замечанием."""
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.8,
        issues=[_issue(round_no)],
        checked=[_AUTHORED_FILE, f"rounds/{round_no:03d}/changes.patch"],
        summary=f"раунд {round_no:03d}: правка не там, где источник проблемы",
    ).model_dump_json(by_alias=True)


def _profile(*, max_rounds: int) -> RuntimeConfig:
    """Профиль запуска: оба агента — один адаптер, один зелёный гейт."""
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
            schema_retries=1,
        ),
        gates=(_GATE,),
        attachments=(),
    )


def _forbid_agent_processes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Ловушка на запуск агентского CLI; возвращает журнал запусков.

    Патчится `asyncio.create_subprocess_exec`, а не `default_launcher`:
    адаптеры принимают launcher значением аргумента по умолчанию, а оно
    связано в момент определения функции. Через `create_subprocess_exec`
    проходит сам `default_launcher`, и мимо него настоящий `claude` не
    стартует. `GitCli` и верификатор ловушку не задевают — они работают
    через `subprocess.run`/`Popen`.
    """
    launched: list[tuple[str, ...]] = []

    async def spy(*argv: object, **kwargs: object) -> object:
        command = tuple(str(item) for item in argv)
        launched.append(command)
        raise AssertionError("E2E запустил агентский CLI: " + " ".join(command))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return launched


def _register_adapter(
    monkeypatch: pytest.MonkeyPatch, *, launcher: SpyLauncher
) -> None:
    """Подменяет фабрику адаптера ровно на инъекции launcher'а.

    Класс остаётся настоящим: argv, права §7 и разбор stdout считает
    `ClaudeCodeAdapter`, а не тест. Composition root иначе до шва запуска не
    пускает — фабрике передаются только роль, каталог, sink и id сессии, и
    это правильно (NFR-003).
    """
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


def _run_session(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_rounds: int,
    rounds: int,
) -> Session:
    """Гоняет `disp run` до терминала: `rounds` раундов, ни одного approve.

    Профиль лежит ВНЕ репозитория: файл внутри рабочего дерева untracked, а
    `changes.patch` тянет untracked через intent-to-add — патч раунда стал
    бы описывать конфиг теста вместо работы автора.
    """
    launched = _forbid_agent_processes(monkeypatch)
    launcher = SpyLauncher(
        author_replies=[_proposal(n) for n in range(1, rounds + 1)],
        reviewer_replies=[_request_changes(n) for n in range(1, rounds + 1)],
    )
    _register_adapter(monkeypatch, launcher=launcher)

    config_path = repo.parent / "profile.toml"
    config_path.write_text(_profile(max_rounds=max_rounds).render_toml(), "utf-8")

    main: Callable[..., int] = import_module("disputatio.cli").main
    exit_code = main(
        [
            "run",
            "--root",
            str(repo),
            "--config",
            str(config_path),
            "--mode",
            Mode.DEVELOP.value,
            _TASK_PROMPT,
        ],
        now=lambda: _FROZEN_NOW,
    )
    return Session(root=repo, exit_code=exit_code, launcher=launcher, launched=launched)


def _session_json(root: Path) -> dict[str, Any]:
    """`session.json` как он лежит на диске — байты, а не объект в памяти."""
    payload = (session_dir(root) / "session.json").read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(payload)
    return parsed


def _manifest(root: Path) -> dict[str, Any]:
    """`result/manifest.json` — итоговый документ сессии (§3.2)."""
    payload = (result_dir(root) / "manifest.json").read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(payload)
    return parsed


def _decision(root: Path, round_no: int) -> Decision:
    """`rounds/NNN/decision.json` — решение раунда, прочитанное обратно."""
    path = round_artifact(root, round_no, DECISION_NAME)
    return Decision.model_validate_json(path.read_text(encoding="utf-8"))


def _review(root: Path, round_no: int) -> Review:
    """`rounds/NNN/review.json` — ревью раунда, прочитанное обратно."""
    path = round_artifact(root, round_no, REVIEW_NAME)
    return Review.model_validate_json(path.read_text(encoding="utf-8"))


def _phases(root: Path) -> list[tuple[str, str]]:
    """Пары `(from, to)` всех переходов, дошедших до `events.jsonl`."""
    events = [
        json.loads(line)
        for line in events_jsonl_path(root).read_text(encoding="utf-8").splitlines()
        if line
    ]
    return [
        (event["payload"]["from"], event["payload"]["to"])
        for event in events
        if event["type"] == "state_change"
    ]


def _round_commits(root: Path) -> list[str]:
    """Сообщения коммитов раундов в хронологическом порядке."""
    subjects = _git_output(root, "log", "--format=%s", "--reverse")
    return [line for line in subjects.splitlines() if line.startswith(_COMMIT_PREFIX)]


def _git_output(cwd: Path, *args: str) -> str:
    """`git *args` в `cwd`; ненулевой код возврата — ошибка теста."""
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout


def _sorted_names(paths: Sequence[Path]) -> list[str]:
    """Имена путей по алфавиту — содержимое каталога без порядка ФС."""
    return sorted(path.name for path in paths)


def test_session_without_convergence_escalates_and_exits_zero(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Раунд 1 → `CONTINUE`, раунд 2 → `DEADLOCK → ESCALATED → EXPORTING → DONE`.

    Код возврата `0` — часть утверждения, а не косметика ([REQ-018]):
    эскалация не сбой, и `1` отправил бы пользователя искать поломку
    оркестратора вместо разбора двух позиций.
    """
    session = _run_session(git_repo, monkeypatch, max_rounds=2, rounds=2)
    capsys.readouterr()

    assert session.exit_code == 0
    assert session.launched == []

    assert _decision(git_repo, 1).outcome is Outcome.CONTINUE
    assert _decision(git_repo, 2).outcome is Outcome.DEADLOCK

    assert _phases(git_repo)[-4:] == [
        (SessionPhase.DECIDING.name, SessionPhase.DEADLOCK.name),
        (SessionPhase.DEADLOCK.name, SessionPhase.ESCALATED.name),
        (SessionPhase.ESCALATED.name, SessionPhase.EXPORTING.name),
        (SessionPhase.EXPORTING.name, SessionPhase.DONE.name),
    ]
    state = _session_json(git_repo)
    assert state["state"] == SessionPhase.DONE.value
    assert state["state"] != SessionPhase.FAILED.value
    assert state["current_round"] == 2


def test_manifest_of_the_escalated_session_is_honest(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`converged is False` и перечень открытых замечаний в полном виде.

    `is False`, а не «ложно»: `converged: 0` или `converged: null` прошли бы
    проверку на истинность, а прочитавший манифест человек увидел бы не то,
    что сессия про себя знает. Причина остановки не пинится строкой — она
    сверяется с `decision.json` раунда-источника: вторая её формулировка
    разошлась бы с историей сессии, а буквальный код принадлежит §5.
    """
    _run_session(git_repo, monkeypatch, max_rounds=2, rounds=2)
    capsys.readouterr()

    manifest = _manifest(git_repo)
    decision = _decision(git_repo, 2)

    assert manifest["converged"] is False
    assert manifest["source_round"] == 2
    assert manifest["outcome"] == decision.outcome.value
    assert manifest["stop_reason"] == decision.reason
    assert set(manifest["files"]) == {"result.md", "result.patch"}
    assert (result_dir(git_repo) / "result.md").read_text(
        encoding="utf-8"
    ) == round_artifact(git_repo, 2, PROPOSAL_NAME).read_text(encoding="utf-8")

    open_issues = manifest["open_issues"]
    assert open_issues, (
        "manifest.json обязан перечислить открытые замечания ([REQ-025]): "
        "сессия встала на ревью, у которого замечания были"
    )
    assert [frozenset(entry) for entry in open_issues] == [
        _ISSUE_KEYS for _ in open_issues
    ]
    assert [entry["id"] for entry in open_issues] == [
        issue.id for issue in _review(git_repo, 2).issues
    ]


def test_the_escalated_round_is_not_committed(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Коммит получает принятый раунд, эскалированный — нет (§2.5, [REQ-011]).

    Работа эскалированного раунда остаётся в рабочем дереве незакоммиченной:
    принять её вправе только человек, а `disputatio: round 002` объявил бы
    принятой работу, которую сессия как раз и вынесла на его суд.
    """
    _run_session(git_repo, monkeypatch, max_rounds=2, rounds=2)
    capsys.readouterr()

    assert _round_commits(git_repo) == [f"{_COMMIT_PREFIX}001"]
    assert ".finalized" in _sorted_names(list(round_dir(git_repo, 1).iterdir()))
    assert ".finalized" not in _sorted_names(list(round_dir(git_repo, 2).iterdir()))
    assert _git_output(git_repo, "status", "--porcelain").strip()


def test_every_accepted_round_gets_exactly_one_commit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Два принятых раунда — два коммита, `001` и `002`, по одному на раунд.

    Отдельная сессия с `max_rounds = 3`: на единственном принятом раунде
    «по коммиту на раунд» неотличимо от «один коммит на сессию», и REQ-011
    проверяется только там, где принятых раундов больше одного. Терминал у
    сессии тот же — эскалация на раунде 3.
    """
    session = _run_session(git_repo, monkeypatch, max_rounds=3, rounds=3)
    capsys.readouterr()

    assert session.exit_code == 0
    assert session.launched == []
    assert _round_commits(git_repo) == [
        f"{_COMMIT_PREFIX}001",
        f"{_COMMIT_PREFIX}002",
    ]
    assert _session_json(git_repo)["state"] == SessionPhase.DONE.value
    assert _manifest(git_repo)["converged"] is False
    assert _manifest(git_repo)["source_round"] == 3
