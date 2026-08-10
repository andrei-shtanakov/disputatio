"""Что шаг PROPOSING кладёт на диск и что читает с него ([REQ-003], [REQ-012]).

[TASK-008], follow-up к `test_step_proposing.py`. Тот тест пинит порядок
вызовов и пути артефактов; здесь закрыты три дыры, которые порядок не
видит, потому что все три ломаются молча и с правильной последовательностью
вызовов:

* **содержимое `proposal.md`**. Порядковый лог фиксирует, что артефакт
  записан, но не то, ЧЕМ он записан: шаг, отдающий на диск тело без
  фронтматтера, оставляет файл, который `parse_proposal` следующего раунда
  уже не прочитает, — а `files_touched`/`self_declared_status` исчезают
  вместе с блоком. Артефакт обязан быть ответом автора байт-в-байт;
* **повреждённая история**. `history` обещает поднимать ошибку разбора, а
  не подставлять `None`: молчаливый `None` превращает битый `review.json`
  раунда N−1 в «замечаний не было», и автор получает промпт чистого первого
  раунда — ровно тот сценарий, против которого §6.1 и написан;
* **раунд 0 прошлого не читает вовсе**. Для холодного старта раунда 1
  прошлым служит раунд 0, и «нет прошлого» обязано быть решением кода, а не
  случайностью отсутствия каталога `rounds/000` на диске;
* **успешный шаг сбрасывает счётчик повторов I4** — иначе одна схемная
  ошибка раунда N делает раунд N+1 `FAILED` с первой же.
"""

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from disputatio.contracts import (
    AgentRef,
    AgentTurn,
    BudgetUsed,
    Event,
    Issue,
    Limits,
    Mode,
    Review,
    Role,
    SessionPhase,
    SessionState,
    Severity,
    TaskSpec,
    Verdict,
    VerificationReport,
)
from disputatio.core import RetryAction, SessionFsm
from disputatio.events import write_round_artifact
from disputatio.runtime import GitCli, RuntimeDeps
from disputatio.runtime.git import ROUND_COMMIT_TEMPLATE
from disputatio.runtime.history import load_prior_round
from disputatio.runtime.layout import round_artifact
from disputatio.runtime.steps import StepContext, propose

_FROZEN_NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


@dataclass
class RecordingStore:
    """`StateStore`-фейк: сохранения в память, `session.json` не трогается."""

    saved: list[SessionState] = field(default_factory=list)

    def load(self, session_id: str) -> SessionState:
        """Сессии нет — `KeyError`, как у файловой реализации."""
        raise KeyError(session_id)

    def save(self, state: SessionState) -> None:
        """Запоминает состояние вместо записи на диск."""
        self.saved.append(state)


@dataclass
class RecordingSink:
    """`EventSink`-фейк: события копятся в списке."""

    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        """Запоминает событие вместо дописывания в `events.jsonl`."""
        self.events.append(event)


@dataclass
class RefusingVerifier:
    """`Verifier`-фейк: PROPOSING гейтов не гоняет — вызов есть ошибка шага."""

    def verify(self, round_no: int) -> VerificationReport:
        """Вызовом шага PROPOSING быть не должна."""
        raise AssertionError(f"PROPOSING не вправе гонять гейты (раунд {round_no})")


@dataclass
class ReplyingAdapter:
    """`AgentAdapter`-фейк: отдаёт заготовленный ответ и журналирует промпты."""

    reply: str = ""
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует промпт и возвращает заготовленный ответ автора."""
        self.prompts.append(prompt)
        return AgentTurn(text=self.reply, session_ref=session_ref)


def _git(workdir: Path, *args: str) -> str:
    """`git *args` в `workdir`; ненулевой код — `RuntimeError` со stderr."""
    result = subprocess.run(
        ["git", *args], cwd=workdir, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} упал с кодом {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def _head(root: Path) -> str:
    """Полный SHA текущего `HEAD`."""
    return _git(root, "rev-parse", "HEAD")


def _proposal(round_no: int, *, body: str) -> str:
    """Валидный `proposal.md` раунда `round_no` с телом `body`."""
    return (
        "---\n"
        "schema: disputatio/v1\n"
        f"round: {round_no}\n"
        "role: author\n"
        "responds_to: null\n"
        "files_touched:\n"
        "  - feature.py\n"
        "self_declared_status: complete\n"
        "---\n"
        f"{body}\n"
    )


def _state(round_no: int) -> SessionState:
    """`SessionState` раунда `round_no` в фазе `PROPOSING`."""
    return SessionState(
        session_id="s-fidelity",
        created_at=_FROZEN_NOW,
        state=SessionPhase.PROPOSING,
        current_round=round_no,
        task=TaskSpec(prompt="Почини экспорт CSV", attachments=[], mode=Mode.DEVELOP),
        agents={
            Role.AUTHOR: AgentRef(
                adapter="claude_code", model="opus", session_ref="ref-author"
            ),
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
    root: Path, *, round_no: int, author: ReplyingAdapter
) -> tuple[StepContext, SessionFsm]:
    """`StepContext` на настоящем `GitCli` и фейках остальных портов."""
    store = RecordingStore()
    sink = RecordingSink()
    fsm = SessionFsm(_state(round_no), store=store, sink=sink, now=lambda: _FROZEN_NOW)
    deps = RuntimeDeps(
        root=root,
        store=store,
        sink=sink,
        author=author,
        reviewer=ReplyingAdapter(),
        verifier=RefusingVerifier(),
        git=GitCli(root),
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    return StepContext(deps=deps, fsm=fsm, base_commit=_head(root)), fsm


def test_proposal_md_on_disk_is_the_author_reply_byte_for_byte(
    git_repo: Path,
) -> None:
    """`proposal.md` — весь ответ автора, вместе с фронтматтером ([REQ-003])."""
    reply = _proposal(1, body="тело раунда один")
    author = ReplyingAdapter(reply=reply)
    ctx, _ = _context(git_repo, round_no=1, author=author)

    anyio.run(propose, ctx)

    written = round_artifact(git_repo, 1, "proposal.md").read_text(encoding="utf-8")
    assert written == reply, (
        "proposal.md на диске не равен ответу автора: артефакт без "
        "фронтматтера не читается parse_proposal следующего раунда"
    )


def test_corrupt_prior_review_is_not_silently_read_as_no_remarks(
    git_repo: Path,
) -> None:
    """Битый `review.json` раунда N−1 обрывает шаг, а не обнуляется (§6.1)."""
    (git_repo / "r1.txt").write_text("1\n", encoding="utf-8")
    _git(git_repo, "add", "r1.txt")
    _git(git_repo, "commit", "--quiet", "-m", ROUND_COMMIT_TEMPLATE.format(round=1))
    write_round_artifact(
        git_repo, 1, "review.json", '{"schema": "disputatio/v1", "round": 1}'
    )

    author = ReplyingAdapter(reply=_proposal(2, body="тело раунда два"))
    ctx, _ = _context(git_repo, round_no=2, author=author)

    with pytest.raises(ValidationError):
        anyio.run(propose, ctx)

    assert author.prompts == [], (
        "автор получил промпт при повреждённом review.json раунда 1 — "
        "битая история подана как «замечаний не было»"
    )


def test_round_zero_reads_no_prior_artifacts_from_disk(git_repo: Path) -> None:
    """Прошлое раунда 1 пусто по решению кода, а не по отсутствию каталога."""
    review = Review(
        round=1,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.9,
        issues=[
            Issue(
                id="I-000",
                severity=Severity.MAJOR,
                file="feature.py",
                claim="замечание каталога rounds/000",
                evidence="feature.py:1 — свидетельство",
            )
        ],
        checked=["feature.py"],
        summary="свод каталога rounds/000",
    )
    write_round_artifact(
        git_repo, 0, "review.json", review.model_dump_json(by_alias=True)
    )

    prior = load_prior_round(git_repo, 0)

    assert prior.review is None
    assert prior.verification is None
    assert prior.decision is None


def test_successful_step_resets_the_schema_retry_counter(git_repo: Path) -> None:
    """Успех шага обнуляет счётчик повторов I4 ([DESIGN-008])."""
    author = ReplyingAdapter(reply=_proposal(1, body="тело раунда один"))
    ctx, fsm = _context(git_repo, round_no=1, author=author)
    assert fsm.handle_schema_invalid("первая схемная ошибка") is RetryAction.RETRY

    anyio.run(propose, ctx)

    assert fsm.handle_schema_invalid("схемная ошибка нового шага") is RetryAction.RETRY
    assert fsm.state.state is SessionPhase.PROPOSING
