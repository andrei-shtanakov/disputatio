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

Второе измерение — МОМЕНТ уборки. «Шаг убирает мусор» и «шаг убирает мусор
раньше своего тела» — разные обещания, и пинятся они по-разному: первое
видно на удачном прогоне, второе — только на прогоне, где тело до артефакта
не доходит. Мутационная проба показала, что уборка, перенесённая в конец
шага, переживает полный suite у ВСЕХ четырёх шагов; между тем именно
падающая попытка и копит огрызки — успешная перезаписывает свой артефакт
сама. Поэтому у каждого шага здесь есть тест с телом, которое падает:
упавший автор, упавший верификатор, ревьюер, чей ответ не разобрать, и
чужое решение в закрытом раунде.
"""

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

from disputatio.contracts import (
    AgentAdapter,
    AgentRef,
    AgentTurn,
    BudgetUsed,
    Decision,
    DiffStats,
    Event,
    GateResult,
    GateStatus,
    Issue,
    Limits,
    Mode,
    Outcome,
    OverallStatus,
    Review,
    Role,
    SessionPhase,
    SessionState,
    Severity,
    TaskSpec,
    Verdict,
    VerificationReport,
    Verifier,
)
from disputatio.core import SessionFsm
from disputatio.events import RoundImmutableError, finalize_round, write_round_artifact
from disputatio.runtime import GitCli, GitOps, ReviewParseError, RuntimeDeps
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    DECISION_NAME,
    PROPOSAL_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    round_artifact,
    round_dir,
)
from disputatio.runtime.steps import StepContext, decide_step, propose, review, verify

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


class PortFailure(Exception):
    """Отказ порта — не схемная ошибка агента, повтором она не лечится.

    Своё исключение, а не `RuntimeError`: `SCHEMA_INVALID_ERRORS` перечислен
    поимённо, и тест обязан падать шагом, а не быть проглоченным повтором.
    Отдельный тип делает это видимым в `pytest.raises`.
    """


@dataclass
class CrashingAgent:
    """`AgentAdapter`-фейк: разговор с агентом срывается ([REQ-006])."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Падает вместо ответа — шаг до своего артефакта не доходит."""
        raise PortFailure("адаптер агента упал посреди разговора")


@dataclass
class ProseAgent:
    """`AgentAdapter`-фейк: ответ без JSON-объекта, и так на каждой попытке."""

    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Отвечает прозой: `extract_json_object` разобрать её не сможет."""
        self.prompts.append(prompt)
        return AgentTurn(
            text="Патч посмотрел, возражений нет.", session_ref=session_ref
        )


@dataclass
class CrashingVerifier:
    """`Verifier`-фейк: прогон гейтов срывается на полпути."""

    def verify(self, round_no: int) -> VerificationReport:
        """Падает вместо отчёта — `verification.json` не появится."""
        raise PortFailure(f"прогон гейтов раунда {round_no} сорвался")


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
    git: GitOps,
    author: AgentAdapter | None = None,
    reviewer: AgentAdapter | None = None,
    verifier: Verifier | None = None,
    base_commit: str = "0" * 40,
) -> StepContext:
    """`StepContext` поверх свежего состояния — то же, что даёт resume.

    Порты, которые тест не назвал, подставляются фейками, отказывающимися
    работать: шаг, дотянувшийся до чужой роли, обязан упасть на этом, а не
    пройти на пустышке.
    """
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(_state(phase), store=store, sink=sink, now=lambda: _FROZEN_NOW)
    deps = RuntimeDeps(
        workspace_root=root,
        artifact_root=root,
        store=store,
        sink=sink,
        author=author or NoAgent(),
        reviewer=reviewer or NoAgent(),
        verifier=verifier or NoVerifier(),
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


def _seed_for_review(root: Path) -> None:
    """Кладёт то, из чего шаг REVIEWING собирает промпт: отчёт, текст, патч."""
    write_round_artifact(
        root,
        _ROUND,
        VERIFICATION_NAME,
        _verification().model_dump_json(by_alias=True),
    )
    write_round_artifact(root, _ROUND, PROPOSAL_NAME, _proposal())
    write_round_artifact(root, _ROUND, CHANGES_PATCH_NAME, "--- a/feature.py\n")


def _seed_for_decision(root: Path) -> None:
    """Кладёт то, из чего шаг DECIDING собирает снимок для ядра."""
    _seed_for_review(root)
    write_round_artifact(
        root, _ROUND, REVIEW_NAME, _review().model_dump_json(by_alias=True)
    )


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
    _seed_for_decision(tmp_path)
    _seed_leftovers(tmp_path, DECISION_NAME)
    git = CountingGit()

    decide_step(_context(tmp_path, phase=SessionPhase.DECIDING, git=git))

    assert git.commits == [_ROUND]
    assert _leftovers(tmp_path) == []
    names = sorted(entry.name for entry in round_dir(tmp_path, _ROUND).iterdir())
    assert names == [
        _FINALIZED_MARKER,
        CHANGES_PATCH_NAME,
        DECISION_NAME,
        PROPOSAL_NAME,
        REVIEW_NAME,
        VERIFICATION_NAME,
    ]


def test_proposing_sweeps_before_it_asks_the_author(git_repo: Path) -> None:
    """Уборка PROPOSING идёт до автора, а не после ([REQ-015]).

    Автор — первое, что в шаге способно упасть, и падение это законное:
    адаптер живёт в чужом процессе. Убирай шаг за собой в конце — попытка,
    не дошедшая до `proposal.md`, не убрала бы ничего, и каталог раунда
    копил бы по огрызку на каждый сорванный разговор. Пинится тем, что
    после падения каталог уже чист.
    """
    _seed_leftovers(git_repo, PROPOSAL_NAME)

    with pytest.raises(PortFailure):
        anyio.run(
            propose,
            _context(
                git_repo,
                phase=SessionPhase.PROPOSING,
                author=CrashingAgent(),
                git=GitCli(git_repo),
                base_commit=_head(git_repo),
            ),
        )

    assert _leftovers(git_repo) == []
    assert not round_artifact(git_repo, _ROUND, PROPOSAL_NAME).exists()


def test_verifying_sweeps_before_it_runs_the_gates(tmp_path: Path) -> None:
    """Уборка VERIFYING идёт до прогона гейтов ([REQ-015]).

    Гейт — внешний процесс, и сорваться прогон вправе. Отчёта в этом случае
    нет вовсе, а значит уборка, стоящая после записи отчёта, не случится ни
    разу за всю серию сорванных попыток.
    """
    _seed_leftovers(tmp_path, VERIFICATION_NAME)

    with pytest.raises(PortFailure):
        verify(
            _context(
                tmp_path,
                phase=SessionPhase.VERIFYING,
                verifier=CrashingVerifier(),
                git=CountingGit(),
            )
        )

    assert _leftovers(tmp_path) == []
    assert not round_artifact(tmp_path, _ROUND, VERIFICATION_NAME).exists()


def test_reviewing_sweeps_even_when_no_answer_ever_parses(tmp_path: Path) -> None:
    """Уборка REVIEWING переживает исчерпание повторов ([REQ-006], [REQ-015]).

    Ревьюер, отвечающий прозой, тратит все попытки и роняет шаг — на диск не
    попадает ничего. Это и есть та серия, ради которой уборка стоит впереди:
    исход «повторы кончились» повторяется при каждом resume, и мусор, не
    убранный на входе, не будет убран уже никогда.
    """
    _seed_for_review(tmp_path)
    _seed_leftovers(tmp_path, REVIEW_NAME)
    reviewer = ProseAgent()

    with pytest.raises(ReviewParseError):
        anyio.run(
            review,
            _context(
                tmp_path,
                phase=SessionPhase.REVIEWING,
                reviewer=reviewer,
                git=CountingGit(),
            ),
        )

    assert len(reviewer.prompts) > 1, (
        "тест обязан пройти через повтор, а не одну попытку"
    )
    assert _leftovers(tmp_path) == []
    assert not round_artifact(tmp_path, _ROUND, REVIEW_NAME).exists()


def test_deciding_sweeps_before_it_finds_the_round_closed(tmp_path: Path) -> None:
    """Уборка DECIDING идёт до записи решения ([REQ-015], [REQ-016]).

    Раунд закрыт маркером, а лежащее в нём решение шагу не принадлежит:
    переписать его шаг не вправе и падает. Уборка обязана случиться всё
    равно — она предшествует записи, а не следует за ней. Второй половиной
    пинится обратное: закрытый раунд остаётся закрытым, уборка его на запись
    не открывает.
    """
    _seed_for_decision(tmp_path)
    foreign = Decision(
        round=_ROUND,
        outcome=Outcome.CONVERGED,
        reason="чужое решение",
        open_issues_carried=[],
        next_round_directive=None,
    )
    write_round_artifact(
        tmp_path, _ROUND, DECISION_NAME, foreign.model_dump_json(by_alias=True)
    )
    finalize_round(tmp_path, _ROUND)
    _seed_leftovers(tmp_path, DECISION_NAME)
    git = CountingGit()

    with pytest.raises(RoundImmutableError):
        decide_step(_context(tmp_path, phase=SessionPhase.DECIDING, git=git))

    assert _leftovers(tmp_path) == []
    assert (round_dir(tmp_path, _ROUND) / _FINALIZED_MARKER).exists()
    assert git.commits == []
    on_disk = Decision.model_validate_json(
        round_artifact(tmp_path, _ROUND, DECISION_NAME).read_text(encoding="utf-8")
    )
    assert on_disk.reason == "чужое решение"


def test_a_directory_named_like_a_leftover_is_left_alone(git_repo: Path) -> None:
    """Уборка снимает файлы, а не всё, что подошло по имени ([REQ-015]).

    Каталог, чьё имя совпало с шаблоном огрызка, огрызком не является:
    `unlink` по нему не проходит вовсе, и шаг, взявшийся его снести, упал бы
    там, где обязан был всего лишь прибраться, — то есть уборка мусора
    остановила бы раунд. Отбор по `is_file()` — единственное, что отделяет
    одно от другого, и пинится он тем, что шаг доходит до конца, а каталог с
    его содержимым остаётся нетронутым.
    """
    stale = round_dir(git_repo, _ROUND) / "stale.tmp"
    stale.mkdir(parents=True)
    (stale / "внутри.txt").write_text("не огрызок", encoding="utf-8")
    _seed_leftovers(git_repo, PROPOSAL_NAME)

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

    directory = round_dir(git_repo, _ROUND)
    files_left = sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_file() and entry.name.endswith((".tmp", "~"))
    )
    assert files_left == []
    assert stale.is_dir()
    assert (stale / "внутри.txt").read_text(encoding="utf-8") == "не огрызок"
    assert (
        round_artifact(git_repo, _ROUND, PROPOSAL_NAME).read_text(encoding="utf-8")
        == _proposal()
    )
