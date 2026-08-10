"""E2E: полный цикл, обрыв посреди раунда и resume ([REQ-024], [DESIGN-024]).

Один сквозной путь на фейковых агентах и ноль реальных CLI. Всё, что можно
оставить настоящим, оставлено настоящим: `.disputatio/` пишется на диск
`JsonlEventSink`/`FileStateStore`, раунды коммитятся `GitCli` во временный
репозиторий, экспорт собирает `result/` теми же байтами, что пойдут
пользователю. Подменены ровно три порта — автор, ревьюер и верификатор:
первые два разговаривают с сетью, третий запускает чужие процессы, и без
подмены тест перестал бы быть детерминированным, а не стал бы честнее.

Обрыв симулируется не убийством процесса (недетерминированно, а на Windows
и вовсе иначе), а `SimulatedCrash` из фейка ревьюера на втором его вызове.
Это точная модель обрыва в интересующей точке: write-ahead-переход в
`REVIEWING` уже случился и лежит на диске, тело шага — нет.

Три вещи, ради которых тест существует и которых сумма юнитов не даёт:

* **Стык шагов.** Раунд 2 сбрасывается на коммит раунда 1, читает его
  артефакты и решается по своему ревью — каждая из этих связок принадлежит
  не шагу, а границе между шагами.
* **Диск переживает обрыв.** `session.json` называет НАЧАТУЮ фазу,
  `verification.json` раунда 2 валиден целиком, огрызков записи в
  `rounds/002/` нет, а `events.jsonl` после resume имеет прежние байты
  ПРЕФИКСОМ — журнал append-only проверяется сравнением снимка, а не тем,
  что записей стало больше.
* **Ни один агентский CLI не запущен.** Утверждение не формальность:
  собери тест по недосмотру настоящий `ClaudeCodeAdapter`, и на машине с
  установленным `claude` он бы зеленел, разговаривая с живой моделью.
  Ловушка стоит на `asyncio.create_subprocess_exec` — единственном месте,
  куда приходит `_process.default_launcher`, и единственном, которое
  адаптер не проносит мимо себя значением аргумента по умолчанию.
"""

import asyncio
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any

import anyio
import pytest

from disputatio.contracts import (
    AgentTurn,
    DiffStats,
    Event,
    GateResult,
    GateStatus,
    Issue,
    Mode,
    OverallStatus,
    Review,
    Role,
    SessionPhase,
    SessionState,
    Severity,
    Verdict,
    VerificationReport,
)
from disputatio.core import SessionFsm
from disputatio.events import bootstrap_session, write_config_snapshot
from disputatio.events.paths import events_jsonl_path, result_dir
from disputatio.runtime import (
    AgentConfig,
    GitCli,
    LimitsConfig,
    RuntimeConfig,
    base_rev,
    build_runtime,
)
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    DECISION_NAME,
    PROPOSAL_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    round_artifact,
    round_dir,
    session_dir,
)
from disputatio.runtime.loop import drive, resume_session
from disputatio.runtime.steps import StepContext
from disputatio.verifier import GateSpec

_SESSION_ID = "20260810-090000-e2e0"
_CREATED_AT = datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)

# Имя фейкового адаптера в реестре композиции. Реальные `claude_code`/`codex`
# не перекрываются: подмена их имён спрятала бы ошибку сборки, ради которой
# и стоит ловушка на запуск процесса.
_ADAPTER_NAME = "e2e_fake"

_GATE = GateSpec(name="pytest", cmd="uv run pytest -q", enabled=True)

_TASK_PROMPT = "Почини экспорт CSV: разделитель берётся из локали."

_TOKENS_PER_TURN = 1000

# Шаг инжектированных часов. Сессия обязана уложиться в лимиты §5.2, иначе
# BUDGET_HIT сработал бы раньше сходимости и тест доказал бы другое.
_CLOCK_STEP = timedelta(seconds=1)
_MONOTONIC_STEP = 0.25

_ROUND_COMMITS = ("disputatio: round 001", "disputatio: round 002")

_ARTIFACT_NAMES = (
    PROPOSAL_NAME,
    CHANGES_PATCH_NAME,
    VERIFICATION_NAME,
    REVIEW_NAME,
    DECISION_NAME,
)

# Огрызки прерванной записи: `atomic_write` кладёт `.{имя}.XXXX.tmp` рядом с
# целью, редактор — `имя~`. Проверяются суффиксы, а не список шаблонов: тест
# спрашивает «что лежит в раунде», а не «нашёл ли я то, что искал».
_LEFTOVER_SUFFIXES = (".tmp", "~")


class SimulatedCrash(Exception):
    """Обрыв процесса оркестратора посреди шага ([DESIGN-024]).

    Не из `SCHEMA_INVALID_ERRORS`: schema-retry лечит вывод агента не той
    формы, а здесь моделируется смерть процесса — повтора не будет, и
    единственное, что остаётся от раунда, это диск.
    """


@dataclass
class ScriptedAgent:
    """`AgentAdapter`-фейк: очередь ответов, журнал промптов, точка обрыва.

    Автор вдобавок делает то, что делает настоящий, — правит рабочее дерево:
    без этого `changes.patch` был бы пуст, `commit_round` не создал бы
    коммита, а раунд 2 сбрасывался бы не на работу раунда 1. Сквозной тест,
    в котором никто не написал ни строки кода, проверял бы половину стыка.
    """

    role: Role
    root: Path
    replies: list[str]
    crash_at: int | None = None
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует промпт, затем обрывается либо отдаёт следующий ответ."""
        self.prompts.append(prompt)
        attempt = len(self.prompts)
        if self.crash_at is not None and attempt >= self.crash_at:
            raise SimulatedCrash(
                f"{self.role.value}: процесс оркестратора умер на вызове {attempt}"
            )
        assert self.replies, (
            f"ScriptedAgent {self.role.value}: очередь ответов исчерпана, "
            f"лишний промпт на вызове {attempt}"
        )
        if self.role is Role.AUTHOR:
            self._write_work(attempt)
        return AgentTurn(
            text=self.replies.pop(0),
            session_ref=session_ref,
            tokens_used=_TOKENS_PER_TURN,
        )

    def _write_work(self, attempt: int) -> None:
        """Кладёт в рабочее дерево файл раунда — работа автора, а не артефакт."""
        (self.root / f"feature_{attempt}.py").write_text(
            f"# работа автора, вызов {attempt}\nVALUE = {attempt}\n",
            encoding="utf-8",
        )


@dataclass
class GreenVerifier:
    """`Verifier`-фейк: зелёный отчёт по каждому запрошенному раунду."""

    rounds: list[int] = field(default_factory=list)

    def verify(self, round_no: int) -> VerificationReport:
        """Журналирует раунд и отдаёт `overall == pass`."""
        self.rounds.append(round_no)
        return VerificationReport(
            round=round_no,
            gates=[
                GateResult(
                    name=_GATE.name,
                    cmd=_GATE.cmd,
                    status=GateStatus.PASS,
                    exit_code=0,
                    duration_s=0.5,
                    tail=f"1 passed (раунд {round_no:03d})",
                )
            ],
            overall=OverallStatus.PASS,
            diff_stats=DiffStats(files=1, insertions=2, deletions=0),
        )


@dataclass
class Clocks:
    """Инжектированные часы сессии: детерминированные и монотонные."""

    ticks: int = 0
    monotonic_ticks: int = 0

    def now(self) -> datetime:
        """Стенные часы: каждый вызов на шаг позже предыдущего."""
        self.ticks += 1
        return _CREATED_AT + _CLOCK_STEP * self.ticks

    def monotonic(self) -> float:
        """Монотонные часы бюджета — их же разностью считается `wall_seconds`."""
        self.monotonic_ticks += 1
        return self.monotonic_ticks * _MONOTONIC_STEP


def test_converged_session_survives_a_crash_and_resumes_to_done(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Раунд 1 → обрыв в раунде 2 → resume → `CONVERGED → EXPORTING → DONE`."""
    root = git_repo
    launched = _forbid_agent_processes(monkeypatch)
    project_before = _project_repo_state()

    config = _config(base_commit=base_rev(root, 1, base_commit="HEAD"))
    bootstrap_session(root)
    write_config_snapshot(root, config.render_toml())

    author = ScriptedAgent(
        role=Role.AUTHOR, root=root, replies=[_proposal(1), _proposal(2)]
    )
    crashing_reviewer = ScriptedAgent(
        role=Role.REVIEWER, root=root, replies=[_request_changes(1)], crash_at=2
    )
    _register_agents(monkeypatch, author=author, reviewer=crashing_reviewer)

    with pytest.raises(SimulatedCrash):
        _run(drive, _cold_context(root, config))

    # --- раунд 1 доехал целиком, раунд 2 оборван ровно в REVIEWING ---------
    assert [path.name for path in _round_files(root, 1)] == sorted(
        (*_ARTIFACT_NAMES, ".finalized")
    )
    assert author.prompts and len(author.prompts) == 2
    assert len(crashing_reviewer.prompts) == 2

    crashed = _session_json(root)
    assert crashed["state"] == SessionPhase.REVIEWING.value
    assert crashed["current_round"] == 2

    report = VerificationReport.model_validate_json(
        round_artifact(root, 2, VERIFICATION_NAME).read_text(encoding="utf-8")
    )
    assert report.round == 2
    assert report.overall is OverallStatus.PASS
    assert not round_artifact(root, 2, REVIEW_NAME).exists()
    assert _leftovers(root, 2) == []

    journal_before = events_jsonl_path(root).read_bytes()
    assert journal_before

    # --- resume теми же файлами, но фейками без обрыва ---------------------
    silent_author = ScriptedAgent(role=Role.AUTHOR, root=root, replies=[])
    approving_reviewer = ScriptedAgent(
        role=Role.REVIEWER, root=root, replies=[_approve(2)]
    )
    _register_agents(monkeypatch, author=silent_author, reviewer=approving_reviewer)
    clocks = Clocks()
    verifier = GreenVerifier()

    async def resumed() -> SessionState:
        """Продолжение сессии тем же `drive`, что крутил холодный старт."""
        return await resume_session(
            root,
            _SESSION_ID,
            git=GitCli(root),
            verifier=verifier,
            now=clocks.now,
            monotonic=clocks.monotonic,
        )

    final = anyio.run(resumed)

    # --- терминал, журнал, манифест ----------------------------------------
    assert final.state is SessionPhase.DONE
    assert final.current_round == 2
    assert _session_json(root)["state"] == SessionPhase.DONE.value

    assert silent_author.prompts == []
    assert len(approving_reviewer.prompts) == 1
    assert verifier.rounds == []

    journal_after = events_jsonl_path(root).read_bytes()
    assert journal_after[: len(journal_before)] == journal_before
    assert len(journal_after) > len(journal_before)
    events = [
        Event.model_validate_json(line)
        for line in journal_after.decode("utf-8").splitlines()
        if line
    ]
    assert events
    assert {event.session for event in events} == {_SESSION_ID}

    manifest = _manifest(root)
    assert manifest["converged"] is True
    assert manifest["source_round"] == 2
    assert manifest["open_issues"] == []
    assert set(manifest["files"]) == {"result.md", "result.patch"}
    assert (result_dir(root) / "result.md").read_text(encoding="utf-8") == _proposal(2)

    # --- границы: чужих процессов нет, чужого репозитория тоже -------------
    assert launched == []
    assert _round_commits(root) == list(_ROUND_COMMITS)
    assert _project_repo_state() == project_before


def _cold_context(root: Path, config: RuntimeConfig) -> StepContext:
    """Контекст холодного старта — то же, что собирает `disp run` до цикла."""
    clocks = Clocks()
    deps = build_runtime(
        config,
        root,
        git=GitCli(root),
        verifier=GreenVerifier(),
        now=clocks.now,
        monotonic=clocks.monotonic,
    )
    state = config.to_session_state(created_at=_CREATED_AT)
    deps.store.save(state)
    return StepContext(
        deps=deps,
        fsm=SessionFsm(state, store=deps.store, sink=deps.sink, now=deps.now),
        base_commit=config.base_commit,
        gates=config.gates,
    )


def _run(body: Callable[[StepContext], Any], ctx: StepContext) -> SessionState:
    """Крутит корутину цикла под `anyio`, как это делает CLI."""

    async def call() -> SessionState:
        result: SessionState = await body(ctx)
        return result

    return anyio.run(call)


def _register_agents(
    monkeypatch: pytest.MonkeyPatch,
    *,
    author: ScriptedAgent,
    reviewer: ScriptedAgent,
) -> None:
    """Ставит фейки в реестр композиции — единственный шов подмены агента.

    Подменяется реестр, а не собранный `RuntimeDeps`: `resume_session`
    собирает порты сам, из снапшота конфига, и подсунуть ему готовый адаптер
    негде. Заодно это доказывает, что resume поднимает агентов по тем именам,
    которые записаны в `config.toml`, а не по тем, что помнит процесс.
    """
    composition = import_module("disputatio.runtime.composition")
    agents = {Role.AUTHOR: author, Role.REVIEWER: reviewer}

    def factory(
        *, role: Role, session_dir: Path, event_sink: object, session: str
    ) -> ScriptedAgent:
        """Отдаёт заранее заготовленный фейк для роли."""
        return agents[role]

    monkeypatch.setitem(composition.ADAPTER_FACTORIES, _ADAPTER_NAME, factory)


def _forbid_agent_processes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Ловушка на запуск агентского CLI; возвращает журнал запусков.

    Патчится `asyncio.create_subprocess_exec`, а не
    `_process.default_launcher`: адаптеры принимают launcher значением
    аргумента по умолчанию, а оно связано в момент определения функции —
    подмена имени в модуле такому адаптеру уже не видна. Через
    `create_subprocess_exec` же проходит сам `default_launcher`, и мимо него
    настоящий `claude` не стартует.

    Ловушка ещё и падает, а не только считает: тест, случайно собравший
    реальный адаптер, обязан упасть там, где он это сделал, а не в конце —
    иначе диагноз пришлось бы искать по argv в самом последнем assert'е.
    """
    launched: list[tuple[str, ...]] = []

    async def spy(*argv: object, **kwargs: object) -> object:
        command = tuple(str(item) for item in argv)
        launched.append(command)
        raise AssertionError(
            "E2E запустил настоящий агентский CLI: " + " ".join(command)
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    monkeypatch.setattr(
        import_module("disputatio.adapters._process"), "default_launcher", spy
    )
    return launched


def _config(*, base_commit: str) -> RuntimeConfig:
    """Конфиг сессии: оба агента — фейковый адаптер, один зелёный гейт."""
    return RuntimeConfig(
        session_id=_SESSION_ID,
        mode=Mode.DEVELOP,
        base_commit=base_commit,
        task_prompt=_TASK_PROMPT,
        author=AgentConfig(adapter=_ADAPTER_NAME, model="opus"),
        reviewer=AgentConfig(adapter=_ADAPTER_NAME, model="sonnet"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        gates=(_GATE,),
        attachments=(),
    )


def _proposal(round_no: int) -> str:
    """`proposal.md` раунда `round_no` — ответ автора с YAML-фронтматтером."""
    return (
        "---\n"
        "schema: disputatio/v1\n"
        f"round: {round_no}\n"
        "role: author\n"
        "responds_to: null\n"
        "files_touched:\n"
        f"  - feature_{round_no}.py\n"
        "self_declared_status: complete\n"
        "---\n"
        f"Работа раунда {round_no:03d}: разделитель вынесен в конфиг.\n"
    )


def _request_changes(round_no: int) -> str:
    """Ответ ревьюера раунда 1: `request_changes` с major-замечанием."""
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.7,
        issues=[
            Issue(
                id=f"I-{round_no:03d}-1",
                severity=Severity.MAJOR,
                file=f"feature_{round_no}.py",
                claim="разделитель всё ещё читается из локали процесса",
                evidence=f"feature_{round_no}.py:2 — VALUE зависит от окружения",
                suggestion="взять разделитель из конфига сессии",
            )
        ],
        checked=[f"feature_{round_no}.py", "rounds/001/changes.patch"],
        summary="правка в нужном месте, но источник разделителя не изменился",
    ).model_dump_json(by_alias=True)


def _approve(round_no: int) -> str:
    """Ответ ревьюера раунда 2: `approve` при зелёных гейтах."""
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.APPROVE,
        confidence=0.9,
        issues=[],
        checked=[f"feature_{round_no}.py", f"rounds/{round_no:03d}/changes.patch"],
        summary="замечание раунда 1 закрыто, гейты зелёные",
    ).model_dump_json(by_alias=True)


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


def _round_files(root: Path, round_no: int) -> list[Path]:
    """Содержимое `rounds/NNN/` по именам, отсортированное."""
    return sorted(round_dir(root, round_no).iterdir(), key=lambda path: path.name)


def _leftovers(root: Path, round_no: int) -> list[str]:
    """Огрызки прерванной записи в `rounds/NNN/`; пусто — раунд чист."""
    return sorted(
        path.name
        for path in round_dir(root, round_no).iterdir()
        if path.name.endswith(_LEFTOVER_SUFFIXES)
    )


def _round_commits(root: Path) -> list[str]:
    """Сообщения коммитов раундов в хронологическом порядке."""
    subjects = _git_output(root, "log", "--format=%s", "--reverse")
    return [line for line in subjects.splitlines() if line.startswith("disputatio: ")]


def _project_repo_state() -> tuple[str, str]:
    """`HEAD` и `git status --porcelain` репозитория, где лежит сам тест.

    Сессия обязана жить целиком в `tmp_path`: `GitCli` работает командами
    без указания репозитория, и ошибка в `cwd` увела бы `reset --hard` в
    рабочее дерево разработчика. Проверяется парой «ревизия + статус» — по
    одной ревизии не видно ни снесённых правок, ни новых файлов.
    """
    project = Path(__file__).resolve().parents[2]
    return (
        _git_output(project, "rev-parse", "HEAD"),
        _git_output(project, "status", "--porcelain"),
    )


def _git_output(cwd: Path, *args: str) -> str:
    """`git *args` в `cwd`; ненулевой код возврата — ошибка теста."""
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout
