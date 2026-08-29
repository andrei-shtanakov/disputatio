"""Разделение `workspace_root` / `artifact_root` (SPEC-002 §4.1, ADR-006 v2).

Действующий контракт хранения — «одна рабочая директория — одна сессия»:
`.disputatio/` жёстко висел на том же корне, который считается рабочим
git-репозиторием. Пайплайн §4.1 кладёт несколько сессий под ОДИН репозиторий
(`pipelines/<slug>/sessions/{spec-r1,pair-r1,…}`), и все ревизии конфликтовали
бы за один `session.json`. Здесь пинится обе половины нового контракта:

* **дефолт байт-в-байт**: без `artifact_root` раскладка та же, что была, —
  сравнением строк путей, а не «примерно тем же деревом»;
* **разделение полное**: врозь идут не только `session.json` и снапшот
  конфига, но и артефакты раунда, и чтение истории. Разойдись сессии по
  состоянию, но сложи раунды в один `rounds/` — сценарий пайплайна не
  заработал бы, а поломка была бы тихой.

Git при этом остаётся на `workspace_root`: `changes.patch` собирается по
рабочему дереву, общему у обеих сессий, — иначе разделение отняло бы у
ревьюера предмет ревью.
"""

import json
from collections.abc import Sequence
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
from disputatio.events import (
    FileStateStore,
    bootstrap_session,
    write_config_snapshot,
    write_round_artifact,
)
from disputatio.events.paths import (
    config_toml_path,
    events_jsonl_path,
    result_dir,
    round_dir,
    rounds_dir,
    session_dir,
    session_json_path,
)
from disputatio.runtime import (
    AgentConfig,
    ConfigError,
    GitCli,
    LimitsConfig,
    RuntimeConfig,
    base_rev,
    build_runtime,
)
from disputatio.runtime.history import load_prior_round
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    DECISION_NAME,
    PROPOSAL_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    round_artifact,
)
from disputatio.runtime.loop import drive, resume_session
from disputatio.runtime.steps import StepContext, propose
from disputatio.verifier import GateSpec

_CREATED_AT = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
_ADAPTER_NAME = "artifact_root_fake"
_GATE = GateSpec(name="pytest", cmd="uv run pytest -q", enabled=True)
_TOKENS_PER_TURN = 1000

_PIPELINE_SESSIONS = (".disputatio", "pipelines", "demo", "sessions")
"""Раскладка §4.1: сессии пайплайна лежат ВНУТРИ каталога рабочего репо."""

_ARTIFACT_NAMES = (
    PROPOSAL_NAME,
    CHANGES_PATCH_NAME,
    VERIFICATION_NAME,
    REVIEW_NAME,
    DECISION_NAME,
)

_EXPECTED_LAYOUT = (
    ".disputatio",
    ".disputatio/session.json",
    ".disputatio/config.toml",
    ".disputatio/events.jsonl",
    ".disputatio/rounds",
    ".disputatio/rounds/001",
    ".disputatio/rounds/001/proposal.md",
    ".disputatio/result",
)
"""Снимок раскладки сессии на дефолте — то, что было до разделения."""


@dataclass
class ScriptedAgent:
    """`AgentAdapter`-фейк: очередь ответов + правка рабочего дерева.

    Автор правит дерево, потому что это делает настоящий: без правки
    `changes.patch` был бы пуст, и тест не отличил бы «патч собран по
    рабочему корню» от «патч не собран вовсе». Файл свой на каждый вызов —
    иначе раунд 2, сброшенный на коммит раунда 1, не оставил бы диффа вовсе.
    """

    role: Role
    workspace: Path
    replies: list[str]
    marker: str = ""
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует промпт, правит дерево и отдаёт следующий ответ."""
        self.prompts.append(prompt)
        assert self.replies, f"{self.role.value}: очередь ответов исчерпана"
        if self.role is Role.AUTHOR and self.marker:
            self.workspace.joinpath(
                work_file(self.marker, len(self.prompts))
            ).write_text(
                f"# работа сессии {self.marker}, вызов {len(self.prompts)}\n",
                encoding="utf-8",
            )
        return AgentTurn(
            text=self.replies.pop(0),
            session_ref=session_ref,
            tokens_used=_TOKENS_PER_TURN,
        )


@dataclass
class Clocks:
    """Инжектированные часы сессии: детерминированные и монотонные."""

    ticks: int = 0
    monotonic_ticks: int = 0

    def now(self) -> datetime:
        """Стенные часы: каждый вызов на секунду позже предыдущего."""
        self.ticks += 1
        return _CREATED_AT + timedelta(seconds=self.ticks)

    def monotonic(self) -> float:
        """Монотонные часы бюджета — их разностью считается `wall_seconds`."""
        self.monotonic_ticks += 1
        return self.monotonic_ticks * 0.25


def work_file(marker: str, attempt: int) -> str:
    """Имя файла, который автор кладёт в рабочее дерево на вызове `attempt`."""
    return f"{marker}_{attempt}.py"


@dataclass
class GreenVerifier:
    """`Verifier`-фейк: зелёный отчёт по любому раунду."""

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
                    duration_s=0.1,
                    tail="1 passed",
                )
            ],
            overall=OverallStatus.PASS,
            diff_stats=DiffStats(files=1, insertions=1, deletions=0),
        )


def test_default_artifact_root_equals_workspace(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без `artifact_root` раскладка та же, что до разделения (§4.1)."""
    root = git_repo
    config = _config("20260828-120000-aaaa", base_commit=_base(root))
    _register_agents(monkeypatch, workspace=root)

    deps = build_runtime(config, root, git=GitCli(root), verifier=GreenVerifier())

    assert deps.workspace_root == root
    assert deps.artifact_root == root
    assert _layout_snapshot(root) == list(_EXPECTED_LAYOUT)

    ctx = _context(deps, config)
    assert ctx.workspace_root == root
    assert ctx.artifact_root == root

    bootstrap_session(root)
    deps.store.save(config.to_session_state(created_at=_CREATED_AT))
    assert session_json_path(root).is_file()


def test_two_sessions_separate_artifact_roots_no_collision(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Две сессии под одним репо: два `session.json`, один рабочий корень."""
    root = git_repo
    spec_root = _session_root(root, "spec-r1")
    pair_root = _session_root(root, "pair-r1")

    spec = _config("20260828-120000-spec", base_commit=_base(root))
    pair = _config("20260828-120000-pair", base_commit=_base(root))
    _register_agents(monkeypatch, workspace=root)

    spec_deps = build_runtime(
        spec, root, artifact_root=spec_root, git=GitCli(root), verifier=GreenVerifier()
    )
    pair_deps = build_runtime(
        pair, root, artifact_root=pair_root, git=GitCli(root), verifier=GreenVerifier()
    )

    for deps, artifact_root in ((spec_deps, spec_root), (pair_deps, pair_root)):
        assert deps.workspace_root == root
        assert deps.artifact_root == artifact_root

    bootstrap_session(spec_root)
    bootstrap_session(pair_root)
    spec_deps.store.save(spec.to_session_state(created_at=_CREATED_AT))
    pair_deps.store.save(pair.to_session_state(created_at=_CREATED_AT))

    assert session_json_path(spec_root).is_file()
    assert session_json_path(pair_root).is_file()
    assert not session_json_path(root).exists()

    assert spec_deps.store.load(spec.session_id).session_id == spec.session_id
    assert pair_deps.store.load(pair.session_id).session_id == pair.session_id
    with pytest.raises(KeyError):
        spec_deps.store.load(pair.session_id)


def test_round_artifacts_go_to_artifact_root(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Раунды двух сессий не смешиваются, а патч собран по рабочему корню."""
    root = git_repo
    spec_root = _session_root(root, "spec-r1")
    pair_root = _session_root(root, "pair-r1")

    spec_proposal = _proposal(1, work_file("spec", 1))
    pair_proposal = _proposal(1, work_file("pair", 1))

    _run_first_round(
        root,
        spec_root,
        session_id="20260828-120000-spec",
        marker="spec",
        reply=spec_proposal,
        monkeypatch=monkeypatch,
    )

    assert (
        round_artifact(spec_root, 1, PROPOSAL_NAME).read_text(encoding="utf-8")
        == spec_proposal
    )
    assert not round_artifact(pair_root, 1, PROPOSAL_NAME).exists()
    assert not round_artifact(root, 1, PROPOSAL_NAME).exists()

    spec_patch = round_artifact(spec_root, 1, CHANGES_PATCH_NAME).read_text(
        encoding="utf-8"
    )
    assert work_file("spec", 1) in spec_patch, "патч собран не по рабочему корню"
    assert ".disputatio" not in spec_patch

    # История первой сессии не видна второй — читается она из своего корня.
    _write_review(spec_root, 1)
    assert load_prior_round(spec_root, 1).review is not None
    assert load_prior_round(pair_root, 1).review is None

    spec_before = round_artifact(spec_root, 1, PROPOSAL_NAME).read_bytes()
    _run_first_round(
        root,
        pair_root,
        session_id="20260828-120000-pair",
        marker="pair",
        reply=pair_proposal,
        monkeypatch=monkeypatch,
    )

    assert (
        round_artifact(pair_root, 1, PROPOSAL_NAME).read_text(encoding="utf-8")
        == pair_proposal
    )
    assert round_artifact(spec_root, 1, PROPOSAL_NAME).read_bytes() == spec_before
    assert work_file("pair", 1) in round_artifact(
        pair_root, 1, CHANGES_PATCH_NAME
    ).read_text(encoding="utf-8")


def test_two_split_sessions_run_end_to_end_without_mixing(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Две сессии от `IDLE` до `DONE` над одним репо: журналы врозь.

    Прогон целиком, а не по шагу, потому что дешёвые пробы разделение не
    ловят: `review`, `decide` и `export` пишут свои артефакты без единого
    вызова из тестов раунда 1, а чтение истории у `propose` включается
    только со второго раунда. Здесь через оба корня проходят все четыре
    шага и обе итерации revise-петли, поэтому возврат любого из них на
    рабочий корень валит тест.
    """
    root = git_repo

    spec = _run_session(
        root,
        "spec-r1",
        session_id="20260829-100000-spec",
        marker="spec",
        monkeypatch=monkeypatch,
    )
    spec_tree = _tree_snapshot(spec.artifact_root)

    pair = _run_session(
        root,
        "pair-r1",
        session_id="20260829-100000-pair",
        marker="pair",
        monkeypatch=monkeypatch,
    )

    for run in (spec, pair):
        assert run.final.state is SessionPhase.DONE
        assert run.final.current_round == 2

        # Журнал событий — свой у каждой сессии, и только её.
        journal = events_jsonl_path(run.artifact_root)
        assert journal.is_file()
        assert _journal_sessions(journal) == {run.session_id}

        # Артефакты обоих раундов и маркер I3 — в своём корне.
        for round_no in (1, 2):
            for name in _ARTIFACT_NAMES:
                assert round_artifact(run.artifact_root, round_no, name).is_file(), (
                    f"{run.session_id}: раунд {round_no}, нет {name}"
                )
            assert (
                round_dir(run.artifact_root, round_no).joinpath(".finalized").is_file()
            )

        # Экспорт — тоже в своём корне, и он про эту сессию.
        manifest = json.loads(
            (result_dir(run.artifact_root) / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["session"] == run.session_id
        assert manifest["converged"] is True
        assert manifest["source_round"] == 2
        assert set(manifest["files"]) == {"result.md", "result.patch"}

        # Рабочий корень не несёт НИЧЕГО из журнала сессии: `.disputatio/`
        # там существует, но только как родитель каталога пайплайна.
        assert not session_json_path(root).exists()
        assert not config_toml_path(root).exists()
        assert not events_jsonl_path(root).exists()
        assert not rounds_dir(root).exists()
        assert not result_dir(root).exists()

        # Стык корней: путь в промпте ревьюера считается от РАБОЧЕГО корня,
        # а указывает на артефакт под `artifact_root`.
        assert (
            f"proposal: {_prompt_path(run.revision, 1, PROPOSAL_NAME)}"
            in (run.reviewer.prompts[0])
        )
        assert (
            f"patch: {_prompt_path(run.revision, 1, CHANGES_PATCH_NAME)}"
            in (run.reviewer.prompts[0])
        )

        # История раунда 1 доехала до промпта раунда 2 — из своего корня.
        assert len(run.author.prompts) == 2
        assert _issue_claim(run.session_id) in run.author.prompts[1]

    # Вторая сессия не тронула ни байта первой.
    assert _tree_snapshot(spec.artifact_root) == spec_tree
    assert spec.artifact_root != pair.artifact_root


def test_artifact_root_outside_workspace_is_refused_at_build(git_repo: Path) -> None:
    """`artifact_root` вне рабочего корня отвергается на сборке, а не в раунде.

    Предусловие нужно шагу `review`: путь артефакта в промпте ревьюера
    считается от рабочего корня. Всплыви оно там — отказ пришёлся бы уже
    после `reset --hard`, работы автора и прогона гейтов, то есть стоил бы
    полного раунда. Проверка на сборке стоит одного сравнения путей.
    """
    root = git_repo
    outside = root.parent / "чужой-журнал"
    outside.mkdir()
    config = _config("20260829-100000-out", base_commit=_base(root))

    with pytest.raises(ValueError) as excinfo:
        build_runtime(config, root, artifact_root=outside, git=GitCli(root))

    message = str(excinfo.value)
    assert str(outside) in message
    assert str(root) in message


def test_resume_reads_from_artifact_root(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Снапшот конфига читается из `artifact_root`, а не из рабочего корня."""
    root = git_repo
    artifact_root = _session_root(root, "spec-r2")
    session_id = "20260828-120000-r2"
    config = _config(session_id, base_commit=_base(root))
    _register_agents(monkeypatch, workspace=root)

    bootstrap_session(artifact_root)
    write_config_snapshot(artifact_root, config.render_toml())
    state = config.to_session_state(created_at=_CREATED_AT).model_copy(
        update={"state": SessionPhase.DONE, "current_round": 1}
    )
    _store(artifact_root).save(state)

    assert not config_toml_path(root).exists()

    async def resumed() -> SessionState:
        """Продолжение сессии с журналом во вложенном каталоге."""
        return await resume_session(
            root,
            session_id,
            artifact_root=artifact_root,
            git=GitCli(root),
            verifier=GreenVerifier(),
        )

    final = anyio.run(resumed)
    assert final.session_id == session_id
    assert final.state is SessionPhase.DONE

    # Без `artifact_root` тот же resume ищет снапшот в рабочем корне и не
    # находит его — ровно то, обо что споткнулась бы вложенная сессия.
    async def resumed_default() -> SessionState:
        """Тот же resume на дефолте: снапшота в рабочем корне нет."""
        return await resume_session(root, session_id, git=GitCli(root))

    with pytest.raises(ConfigError):
        anyio.run(resumed_default)


@dataclass(frozen=True)
class SessionRun:
    """Итог одного сквозного прогона: корень, агенты и финальное состояние."""

    revision: str
    artifact_root: Path
    session_id: str
    author: ScriptedAgent
    reviewer: ScriptedAgent
    final: SessionState


def _run_session(
    root: Path,
    revision: str,
    *,
    session_id: str,
    marker: str,
    monkeypatch: pytest.MonkeyPatch,
) -> SessionRun:
    """Крутит сессию от `IDLE` до `DONE` в своём `artifact_root`.

    Два раунда, а не один: раунд 1 получает `request_changes`, раунд 2 —
    `approve` при зелёных гейтах. Меньше нельзя — §4.4 не принимает
    `approve` в первом раунде develop-сессии, а без второго раунда шаг
    `propose` не читал бы историю вовсе.
    """
    artifact_root = _session_root(root, revision)
    config = _config(session_id, base_commit=_base(root))
    author, reviewer = _register_agents(
        monkeypatch,
        workspace=root,
        author_replies=[
            _proposal(1, work_file(marker, 1)),
            _proposal(2, work_file(marker, 2)),
        ],
        reviewer_replies=[
            _request_changes(1, session_id, work_file(marker, 1)),
            _approve(2),
        ],
        marker=marker,
    )

    bootstrap_session(artifact_root)
    write_config_snapshot(artifact_root, config.render_toml())
    clocks = Clocks()
    deps = build_runtime(
        config,
        root,
        artifact_root=artifact_root,
        git=GitCli(root),
        verifier=GreenVerifier(),
        now=clocks.now,
        monotonic=clocks.monotonic,
    )
    state = config.to_session_state(created_at=_CREATED_AT)
    deps.store.save(state)
    ctx = StepContext(
        deps=deps,
        fsm=SessionFsm(state, store=deps.store, sink=deps.sink, now=deps.now),
        base_commit=config.base_commit,
        gates=config.gates,
    )
    final = anyio.run(drive, ctx)
    return SessionRun(
        revision=revision,
        artifact_root=artifact_root,
        session_id=session_id,
        author=author,
        reviewer=reviewer,
        final=final,
    )


def _prompt_path(revision: str, round_no: int, name: str) -> str:
    """Ожидаемый путь артефакта в промпте — собран из констант теста.

    Не через `layout.round_artifact`: путь, посчитанный тем же построителем,
    что и в реализации, совпал бы с ней и при подмене корня.
    """
    return "/".join(
        (
            *_PIPELINE_SESSIONS,
            revision,
            ".disputatio",
            "rounds",
            f"{round_no:03d}",
            name,
        )
    )


def _tree_snapshot(artifact_root: Path) -> dict[str, bytes]:
    """Побайтовый снимок журнала сессии — для сравнения «до/после»."""
    return {
        path.relative_to(artifact_root).as_posix(): path.read_bytes()
        for path in sorted(artifact_root.rglob("*"))
        if path.is_file()
    }


def _journal_sessions(journal: Path) -> set[str]:
    """Множество `session` во всех строках `events.jsonl`."""
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert lines, f"{journal}: журнал пуст"
    return {json.loads(line)["session"] for line in lines if line}


def _layout_snapshot(root: Path) -> list[str]:
    """Строки путей раскладки относительно `root` — снимок для сравнения."""
    paths = (
        session_dir(root),
        session_json_path(root),
        config_toml_path(root),
        events_jsonl_path(root),
        rounds_dir(root),
        round_dir(root, 1),
        round_artifact(root, 1, PROPOSAL_NAME),
        result_dir(root),
    )
    return [path.relative_to(root).as_posix() for path in paths]


def _session_root(root: Path, revision: str) -> Path:
    """`artifact_root` ревизии пайплайна: `.disputatio/pipelines/…/<rev>`."""
    return root.joinpath(*_PIPELINE_SESSIONS, revision)


def _base(root: Path) -> str:
    """`base_commit` сессии — `HEAD` рабочего репозитория на старте."""
    return base_rev(root, 1, base_commit="HEAD")


def _store(artifact_root: Path) -> FileStateStore:
    """`FileStateStore` над журналом сессии — читатель того же корня."""
    return FileStateStore(artifact_root)


def _run_first_round(
    root: Path,
    artifact_root: Path,
    *,
    session_id: str,
    marker: str,
    reply: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Прогоняет настоящий шаг PROPOSING раунда 1 в своём `artifact_root`."""
    config = _config(session_id, base_commit=_base(root))
    _register_agents(monkeypatch, workspace=root, author_replies=[reply], marker=marker)

    bootstrap_session(artifact_root)
    write_config_snapshot(artifact_root, config.render_toml())
    deps = build_runtime(
        config,
        root,
        artifact_root=artifact_root,
        git=GitCli(root),
        verifier=GreenVerifier(),
    )
    state = config.to_session_state(created_at=_CREATED_AT).model_copy(
        update={"state": SessionPhase.PROPOSING, "current_round": 1}
    )
    deps.store.save(state)
    ctx = StepContext(
        deps=deps,
        fsm=SessionFsm(state, store=deps.store, sink=deps.sink, now=deps.now),
        base_commit=config.base_commit,
        gates=config.gates,
    )
    anyio.run(propose, ctx)


def _context(deps: Any, config: RuntimeConfig) -> StepContext:
    """`StepContext` холодного старта — то же, что собирает `disp run`."""
    state = config.to_session_state(created_at=_CREATED_AT)
    return StepContext(
        deps=deps,
        fsm=SessionFsm(state, store=deps.store, sink=deps.sink, now=deps.now),
        base_commit=config.base_commit,
        gates=config.gates,
    )


def _register_agents(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace: Path,
    author_replies: Sequence[str] = (),
    reviewer_replies: Sequence[str] = (),
    marker: str = "",
) -> tuple[ScriptedAgent, ScriptedAgent]:
    """Ставит пару фейков в реестр композиции — шов подмены без CLI.

    Фейки создаются ЗДЕСЬ, а не в фабрике, и возвращаются наружу: их
    `prompts` — единственный способ увидеть, что именно ушло агенту, а
    промпт ревьюера несёт путь артефакта, то есть стык двух корней.
    """
    composition = import_module("disputatio.runtime.composition")
    author = ScriptedAgent(
        role=Role.AUTHOR,
        workspace=workspace,
        replies=list(author_replies),
        marker=marker,
    )
    reviewer = ScriptedAgent(
        role=Role.REVIEWER, workspace=workspace, replies=list(reviewer_replies)
    )
    agents = {Role.AUTHOR: author, Role.REVIEWER: reviewer}

    def factory(
        *, role: Role, session_dir: Path, event_sink: object, session: str
    ) -> ScriptedAgent:
        """Отдаёт фейк роли; `session_dir` обязан быть рабочим корнем."""
        assert session_dir == workspace, (
            f"адаптер {role.value} получил {session_dir}, а не рабочий корень: "
            "агентский CLI запускается из репозитория, а не из журнала сессии"
        )
        return agents[role]

    monkeypatch.setitem(composition.ADAPTER_FACTORIES, _ADAPTER_NAME, factory)
    return author, reviewer


def _config(session_id: str, *, base_commit: str) -> RuntimeConfig:
    """Конфиг сессии: оба агента — фейковый адаптер, один зелёный гейт."""
    return RuntimeConfig(
        session_id=session_id,
        mode=Mode.DEVELOP,
        base_commit=base_commit,
        task_prompt="Развести журнал сессии и рабочий репозиторий.",
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


def _proposal(round_no: int, touched: str) -> str:
    """`proposal.md` раунда — ответ автора с YAML-фронтматтером."""
    return (
        "---\n"
        "schema: disputatio/v1\n"
        f"round: {round_no}\n"
        "role: author\n"
        "responds_to: null\n"
        "files_touched:\n"
        f"  - {touched}\n"
        "self_declared_status: complete\n"
        "---\n"
        f"Работа раунда {round_no:03d} в файле {touched}.\n"
    )


def _issue_claim(session_id: str) -> str:
    """Текст замечания раунда 1 — метка, по которой видно, чья это история."""
    return f"сессия {session_id}: разделитель всё ещё читается из локали"


def _request_changes(round_no: int, session_id: str, touched: str) -> str:
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
                file=touched,
                claim=_issue_claim(session_id),
                evidence=f"{touched}:1 — значение зависит от окружения",
                suggestion="взять разделитель из конфига сессии",
            )
        ],
        checked=[touched],
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
        checked=[f"rounds/{round_no:03d}/changes.patch"],
        summary="замечание раунда 1 закрыто, гейты зелёные",
    ).model_dump_json(by_alias=True)


def _write_review(artifact_root: Path, round_no: int) -> None:
    """Кладёт `review.json` в раунд — история, которую чужая сессия не видит."""
    review = Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.7,
        issues=[
            Issue(
                id=f"I-{round_no:03d}-1",
                severity=Severity.MAJOR,
                file="spec.py",
                claim="журнал сессии всё ещё висит на рабочем корне",
                evidence="spec.py:1 — путь собран от workspace_root",
                suggestion="взять artifact_root",
            )
        ],
        checked=["spec.py"],
        summary="разделение не доведено до артефактов раунда",
    )
    write_round_artifact(
        artifact_root, round_no, REVIEW_NAME, review.model_dump_json(by_alias=True)
    )
