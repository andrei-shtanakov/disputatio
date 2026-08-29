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

from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    PROPOSAL_NAME,
    REVIEW_NAME,
    round_artifact,
)
from disputatio.runtime.loop import resume_session
from disputatio.runtime.steps import StepContext, propose
from disputatio.verifier import GateSpec

_CREATED_AT = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
_ADAPTER_NAME = "artifact_root_fake"
_GATE = GateSpec(name="pytest", cmd="uv run pytest -q", enabled=True)

_PIPELINE_SESSIONS = (".disputatio", "pipelines", "demo", "sessions")
"""Раскладка §4.1: сессии пайплайна лежат ВНУТРИ каталога рабочего репо."""

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
    рабочему корню» от «патч не собран вовсе».
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
            (self.workspace / self.marker).write_text(
                f"# работа сессии {self.marker}\n", encoding="utf-8"
            )
        return AgentTurn(text=self.replies.pop(0), session_ref=session_ref)


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

    spec_proposal = _proposal(1, "spec.py")
    pair_proposal = _proposal(1, "pair.py")

    _run_first_round(
        root,
        spec_root,
        session_id="20260828-120000-spec",
        marker="spec.py",
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
    assert "spec.py" in spec_patch, "патч собран не по рабочему корню"
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
        marker="pair.py",
        reply=pair_proposal,
        monkeypatch=monkeypatch,
    )

    assert (
        round_artifact(pair_root, 1, PROPOSAL_NAME).read_text(encoding="utf-8")
        == pair_proposal
    )
    assert round_artifact(spec_root, 1, PROPOSAL_NAME).read_bytes() == spec_before
    assert "pair.py" in round_artifact(pair_root, 1, CHANGES_PATCH_NAME).read_text(
        encoding="utf-8"
    )


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
    _register_agents(monkeypatch, workspace=root, reply=reply, marker=marker)

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
    reply: str = "",
    marker: str = "",
) -> None:
    """Ставит фейк агента в реестр композиции — шов подмены без CLI."""
    composition = import_module("disputatio.runtime.composition")

    def factory(
        *, role: Role, session_dir: Path, event_sink: object, session: str
    ) -> ScriptedAgent:
        """Отдаёт фейк роли; `session_dir` обязан быть рабочим корнем."""
        assert session_dir == workspace, (
            f"адаптер {role.value} получил {session_dir}, а не рабочий корень: "
            "агентский CLI запускается из репозитория, а не из журнала сессии"
        )
        return ScriptedAgent(
            role=role,
            workspace=workspace,
            replies=[reply] if role is Role.AUTHOR else [],
            marker=marker,
        )

    monkeypatch.setitem(composition.ADAPTER_FACTORIES, _ADAPTER_NAME, factory)


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
