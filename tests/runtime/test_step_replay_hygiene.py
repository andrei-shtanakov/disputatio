"""Гигиена повтора для PROPOSING и DECIDING ([REQ-015], [DESIGN-015]).

Файл рядом с байт-locked `test_step_idempotency.py`. Тот пинит уборку
огрызков прерванной записи на шагах VERIFYING и REVIEWING — и только на них:
мутационная проба показала, что вырез той же уборки из `propose` и
`decide_step` переживает полный suite. Пин оказался вакуумен ровно там, где
цена мусора выше всего: `proposal.md` пишется в раунде первым, а
`decision.json` — последним, уже в раунде, который вот-вот закроется маркером
I3 и после этого чиститься будет некому.

Обрыв здесь не симулируется срывом `os.replace` (это делает соседний файл, и
второй раз доказывать форму имени временного файла незачем) — огрызок
кладётся на диск готовым. Проверяется другое: что шаг вообще берётся за
уборку своего раунда, и что уборка не задевает ни артефактов, ни маркера
финализации ([REQ-016]).
"""

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anyio

from disputatio.contracts import (
    AgentAdapter,
    AgentRef,
    AgentTurn,
    BudgetUsed,
    DiffStats,
    Event,
    GateResult,
    GateStatus,
    Issue,
    Limits,
    Mode,
    OverallStatus,
    Review,
    Role,
    SessionPhase,
    SessionState,
    Severity,
    TaskSpec,
    Verdict,
    VerificationReport,
)
from disputatio.core import SessionFsm
from disputatio.events import write_round_artifact
from disputatio.runtime import GitCli, GitOps, RuntimeDeps
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    DECISION_NAME,
    PROPOSAL_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    round_dir,
)
from disputatio.runtime.steps import StepContext, decide_step, propose

_FROZEN_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
_ROUND = 1
_SESSION_ID = "s-replay-hygiene"
_FINALIZED_MARKER = ".finalized"


@dataclass
class FakeStore:
    """`StateStore`-фейк: журналирует сохранения, на диск не пишет."""

    saved: list[SessionState] = field(default_factory=list)

    def load(self, session_id: str) -> SessionState:
        """Сессии нет — `KeyError`, как у файловой реализации."""
        raise KeyError(session_id)

    def save(self, state: SessionState) -> None:
        """Запоминает состояние вместо записи `session.json`."""
        self.saved.append(state)


@dataclass
class FakeSink:
    """`EventSink`-фейк: складывает события в список."""

    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        """Запоминает событие вместо дописывания в `events.jsonl`."""
        self.events.append(event)


@dataclass
class ReplyingAgent:
    """`AgentAdapter`-фейк: один заготовленный ответ и правка дерева."""

    reply: str
    root: Path
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует вызов, правит дерево и отдаёт заготовленный ответ."""
        self.prompts.append(prompt)
        (self.root / "feature.py").write_text("print('автор')\n", encoding="utf-8")
        return AgentTurn(text=self.reply, session_ref=session_ref)


@dataclass
class NoAgent:
    """`AgentAdapter`-фейк роли, которую шаг звать не вправе (§7)."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Вызов означает, что шаг перепутал адаптеры ролей."""
        raise AssertionError("шаг обратился к агенту чужой роли")


@dataclass
class NoVerifier:
    """`Verifier`-фейк: гейты прогоняет шаг VERIFYING, и только он."""

    def verify(self, round_no: int) -> VerificationReport:
        """Вызов означает, что шаг перепрогнал чужие гейты."""
        raise AssertionError(f"шаг не вправе гонять гейты (раунд {round_no})")


@dataclass
class CountingGit:
    """`GitOps`-фейк для DECIDING: коммит считается, дерево не трогается."""

    commits: list[int] = field(default_factory=list)

    def diff_head(self) -> str:
        """Не вызывается: патч раунда снимает PROPOSING."""
        raise AssertionError("DECIDING не вправе снимать дифф")

    def commit_round(self, round_no: int) -> None:
        """Журналирует фиксацию принятого раунда."""
        self.commits.append(round_no)

    def reset_hard(self, rev: str) -> None:
        """Не вызывается: сброс дерева — прерогатива PROPOSING."""
        raise AssertionError(f"DECIDING не вправе сбрасывать дерево на {rev}")

    def clean(self) -> None:
        """Не вызывается: уборка дерева — прерогатива PROPOSING."""
        raise AssertionError("DECIDING не вправе убирать дерево")


def _state(phase: SessionPhase) -> SessionState:
    """`SessionState` раунда `_ROUND` в фазе `phase`."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=phase,
        current_round=_ROUND,
        task=TaskSpec(
            prompt="ЗАДАЧА-ПОЛЬЗОВАТЕЛЯ: почини экспорт CSV",
            attachments=[],
            mode=Mode.DEVELOP,
        ),
        agents={
            Role.AUTHOR: AgentRef(adapter="claude_code", model="opus"),
            Role.REVIEWER: AgentRef(adapter="claude_code", model="sonnet"),
        },
        limits=Limits(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        budget_used=BudgetUsed(),
    )


def _context(
    root: Path,
    *,
    phase: SessionPhase,
    author: AgentAdapter,
    git: GitOps,
    base_commit: str = "0" * 40,
) -> StepContext:
    """`StepContext` поверх свежего состояния — то же, что даёт resume."""
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(_state(phase), store=store, sink=sink, now=lambda: _FROZEN_NOW)
    deps = RuntimeDeps(
        root=root,
        store=store,
        sink=sink,
        author=author,
        reviewer=NoAgent(),
        verifier=NoVerifier(),
        git=git,
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    return StepContext(deps=deps, fsm=fsm, base_commit=base_commit)


def _proposal() -> str:
    """Валидный `proposal.md` раунда `_ROUND`."""
    return (
        "---\n"
        "schema: disputatio/v1\n"
        f"round: {_ROUND}\n"
        "role: author\n"
        "responds_to: null\n"
        "files_touched:\n"
        "  - feature.py\n"
        "self_declared_status: complete\n"
        "---\n"
        "предложение автора\n"
    )


def _verification() -> VerificationReport:
    """Зелёный отчёт проверок раунда `_ROUND`."""
    return VerificationReport(
        round=_ROUND,
        gates=[
            GateResult(
                name="pytest",
                cmd="uv run pytest -q",
                status=GateStatus.PASS,
                exit_code=0,
                duration_s=1.5,
                tail="вывод гейта",
            )
        ],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=4, deletions=2),
    )


def _review() -> Review:
    """Ревью раунда `_ROUND`: правки требуются, раунд принимается и идёт дальше."""
    return Review(
        round=_ROUND,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.8,
        issues=[
            Issue(
                id="I-A",
                severity=Severity.MAJOR,
                file="feature.py",
                claim="экспорт теряет заголовок",
                evidence="feature.py:12 — writer.writerow пропущен",
            )
        ],
        checked=["feature.py"],
        summary="свод ревьюера",
    )


def _head(root: Path) -> str:
    """Полный SHA текущего `HEAD` — цель сброса раунда 1."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _leftovers(root: Path) -> list[str]:
    """Огрызки прерванной записи в каталоге раунда — прямой скан."""
    return sorted(
        entry.name
        for entry in round_dir(root, _ROUND).iterdir()
        if entry.name.endswith((".tmp", "~"))
    )


def _seed_leftovers(root: Path, name: str) -> None:
    """Кладёт в раунд огрызок записи артефакта `name` и копию редактора."""
    directory = round_dir(root, _ROUND)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f".{name}.deadbeef.tmp").write_text("полфайла", encoding="utf-8")
    (directory / f"{name}~").write_text("копия редактора", encoding="utf-8")


def test_proposing_replay_sweeps_an_interrupted_proposal_write(
    git_repo: Path,
) -> None:
    """Повтор PROPOSING убирает огрызки своего раунда ([REQ-015]).

    Раунд, начатый и оборванный посреди записи `proposal.md`, встречает
    повтор каталогом, в котором лежит полфайла. Не убери его шаг — и
    единственным, кто мог бы это сделать, оказался бы следующий шаг того же
    раунда, то есть уже после того, как ревьюер прочитал директорию.
    """
    _seed_leftovers(git_repo, PROPOSAL_NAME)
    assert _leftovers(git_repo), "фикстура обязана положить огрызки до шага"

    anyio.run(
        propose,
        _context(
            git_repo,
            phase=SessionPhase.PROPOSING,
            author=ReplyingAgent(reply=_proposal(), root=git_repo),
            git=GitCli(git_repo),
            base_commit=_head(git_repo),
        ),
    )

    assert _leftovers(git_repo) == []
    assert sorted(entry.name for entry in round_dir(git_repo, _ROUND).iterdir()) == [
        CHANGES_PATCH_NAME,
        PROPOSAL_NAME,
    ]


def test_deciding_replay_sweeps_leftovers_without_touching_the_marker(
    tmp_path: Path,
) -> None:
    """Повтор DECIDING убирает огрызки и оставляет I3 нетронутым ([REQ-016]).

    Два утверждения об одной уборке: мусор снят, а маркер финализации —
    такой же скрытый файл рядом — уцелел. Уборка, сметающая `.finalized`,
    открыла бы закрытый раунд на запись, и это было бы хуже мусора.
    """
    write_round_artifact(
        tmp_path, _ROUND, REVIEW_NAME, _review().model_dump_json(by_alias=True)
    )
    write_round_artifact(
        tmp_path,
        _ROUND,
        VERIFICATION_NAME,
        _verification().model_dump_json(by_alias=True),
    )
    write_round_artifact(tmp_path, _ROUND, CHANGES_PATCH_NAME, "--- a/feature.py\n")
    _seed_leftovers(tmp_path, DECISION_NAME)
    git = CountingGit()

    decide_step(
        _context(tmp_path, phase=SessionPhase.DECIDING, author=NoAgent(), git=git)
    )

    assert git.commits == [_ROUND]
    assert _leftovers(tmp_path) == []
    names = sorted(entry.name for entry in round_dir(tmp_path, _ROUND).iterdir())
    assert names == [
        _FINALIZED_MARKER,
        CHANGES_PATCH_NAME,
        DECISION_NAME,
        REVIEW_NAME,
        VERIFICATION_NAME,
    ]
