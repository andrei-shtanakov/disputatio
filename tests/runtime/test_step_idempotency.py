"""Идемпотентность прерванного шага ([REQ-015], [DESIGN-015], [ADR-001]).

[TASK-015]. Оркестратор вправе умереть в любой точке, поэтому прерванный шаг
переигрывается ЦЕЛИКОМ, а не «дописывается»: частично записанный артефакт в
историю не попадает. Проверяется это не рассуждением о коде, а поведением
всех четырёх шагов при повторе подряд — состояние на диске обязано выйти
эквивалентным.

Каждый шаг ломается здесь по-своему, и каждая поломка тихая:

* **PROPOSING** — единственный шаг, у которого есть внешнее состояние помимо
  артефактов: рабочее дерево. Не сбрось его перед повтором — и правки
  прерванной попытки уйдут ревьюеру как работа этого раунда; не убери
  untracked — и они переживут `reset --hard`, который их не видит.
* **VERIFYING** — отчёт собирается заново, а не дополняется: гейт, дважды
  попавший в `verification.json`, сделал бы §4.4 и §5 арифметикой по
  задвоенным входам.
* **REVIEWING** — `review.json` заменяется целиком (temp + rename). Правка на
  месте оставила бы окно, в котором на диске лежит полфайла; пинится это
  сменой inode, а не наличием файла: усечение-и-запись прошло бы любую
  проверку содержимого.
* **DECIDING** — вход ядра пересобирается с диска, поэтому повтор обязан
  подать `core.decide` РОВНО тот же снимок, а `commit_round` при пустом
  диффе — не создать второго коммита: лишний коммит сдвинул бы цель сброса
  раунда N+1 на состояние, работы автора не содержащее.

Отдельная половина требования — мусор. `atomic_write` пишет через temp-файл в
той же директории и при обрыве ПОСРЕДИ записи оставляет его рядом с целью
(так и задокументировано в `disputatio.events`). Значит «временных файлов не
остаётся в `rounds/NNN/`» — обязательство переигрывающей стороны, и проверять
его надо прямым сканом каталога, а не доверием к писателю. Обрыв здесь
симулируется настоящим срывом `os.replace` посреди записи, а не выдуманным
именем файла: иначе тест пинил бы шаблон, который сам же и придумал.

Осознанное отступление [DESIGN-015]/[ADR-004] зафиксировано тестом
`test_replayed_step_asks_the_agent_again_and_keeps_no_ledger`: переигранный
шаг разговаривает с агентом заново, и его токены считаются повторно. Реестра
попыток runtime не заводит намеренно — оверкаунт консервативен (`BUDGET_HIT`
сработает не позже, чем следует), а реестр был бы ещё одним состоянием,
которое само переживает обрыв неполным.
"""

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest

from disputatio.contracts import (
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
    parse_proposal,
)
from disputatio.core import DecidingInputs, DecisionDraft, SessionFsm, decide
from disputatio.events import write_round_artifact
from disputatio.runtime import ROUND_COMMIT_PATTERN, GitCli, RuntimeDeps, steps
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    DECISION_NAME,
    PROPOSAL_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    round_artifact,
    round_dir,
)
from disputatio.verifier import GateSpec

from ._fakes import GitOpsFakeBase

_FROZEN_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "s-idempotency"

# Раунд 1: цель сброса — `base_commit`, прошлого раунда на диске нет вовсе.
# Так тест PROPOSING не зависит от коммита `disputatio: round NNN`, который
# кладёт совсем другой шаг, — идемпотентность проверяется своими средствами.
_ROUND = 1

# Маркер финализации раунда — файл, а не запись реестра ([DESIGN-005]). Он
# скрытый и обязан пережить любую уборку мусора: скан «лишних файлов» знает
# о нём явно, иначе сметание временных файлов заодно снесло бы I3.
_FINALIZED_MARKER = ".finalized"

# Всё, чему позволено лежать в `rounds/NNN/` после переигранного шага.
_ALLOWED_NAMES = frozenset(
    {
        PROPOSAL_NAME,
        CHANGES_PATCH_NAME,
        VERIFICATION_NAME,
        REVIEW_NAME,
        DECISION_NAME,
        _FINALIZED_MARKER,
    }
)

# Хвосты имён временных файлов: `.tmp` — то, что оставляет `atomic_write`,
# `~` — резервная копия редактора. Скрытые огрызки ловятся отдельной веткой
# скана: временному файлу `atomic_write` даёт и точку в начале, и `.tmp` в
# конце, и полагаться на один признак из двух незачем.
_TEMP_SUFFIXES = (".tmp", "~")

_README_TEXT = "disputatio test repo\n"


class InterruptedWrite(OSError):
    """Обрыв ПОСРЕДИ `atomic_write`: временный файл создан, rename не дошёл."""


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
class QueueAgent:
    """`AgentAdapter`-фейк: очередь ответов, журнал промптов, правка дерева.

    Очередь, а не один ответ: попытки обязаны быть различимы — повтор,
    подсунувший результат первой попытки, иначе выглядел бы как честный.
    `on_run` — «работа агента» в дереве, сделанная ровно там, где её делает
    настоящий адаптер.
    """

    replies: list[str]
    on_run: Callable[[], None] | None = None
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует вызов, правит дерево и отдаёт следующий ответ очереди."""
        self.prompts.append(prompt)
        assert self.replies, "очередь ответов исчерпана: шаг сходил к агенту лишний раз"
        if self.on_run is not None:
            self.on_run()
        return AgentTurn(text=self.replies.pop(0), session_ref=session_ref)


@dataclass
class NoAgent:
    """`AgentAdapter`-фейк роли, которую шаг звать не вправе (§7)."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Вызов означает, что шаг перепутал адаптеры ролей."""
        raise AssertionError("шаг обратился к агенту чужой роли")


@dataclass
class QueueVerifier:
    """`Verifier`-фейк: свой отчёт на каждый прогон гейтов.

    Отдельный объект отчёта на попытку, а не один общий: `VerifierRunner`
    состояния между вызовами не держит, и фейк, возвращающий тот же
    экземпляр, скрыл бы накопление результатов внутри шага.
    """

    reports: list[VerificationReport]
    rounds: list[int] = field(default_factory=list)

    def verify(self, round_no: int) -> VerificationReport:
        """Журналирует раунд и отдаёт очередной отчёт."""
        self.rounds.append(round_no)
        assert self.reports, "очередь отчётов исчерпана: гейты прогнаны лишний раз"
        return self.reports.pop(0)


@dataclass
class NoVerifier:
    """`Verifier`-фейк для шагов, которым гейты гонять не положено."""

    def verify(self, round_no: int) -> VerificationReport:
        """Вызов означает, что шаг перепрогнал гейты чужого шага."""
        raise AssertionError(f"шаг не вправе гонять гейты (раунд {round_no})")


@dataclass
class RealGit(GitOpsFakeBase):
    """`GitOps` поверх настоящего `GitCli` — с журналом вызовов.

    Делегирование, а не подмена: «повтор сбросил дерево» и «второго коммита
    нет» — утверждения о настоящем репозитории, и фейк, обещающий их сам,
    проверял бы собственное обещание.
    """

    root: Path
    commits: list[int] = field(default_factory=list)
    resets: list[str] = field(default_factory=list)

    @property
    def _cli(self) -> GitCli:
        return GitCli(self.root)

    def diff_head(self) -> str:
        """`git diff HEAD` рабочего дерева."""
        return self._cli.diff_head()

    def commit_round(self, round_no: int) -> None:
        """Фиксирует принятый раунд; журналирует попытку, а не результат."""
        self.commits.append(round_no)
        self._cli.commit_round(round_no)

    def reset_hard(self, rev: str) -> None:
        """Сброс дерева на `rev`; цель запоминается для проверки."""
        self.resets.append(rev)
        self._cli.reset_hard(rev)

    def clean(self) -> None:
        """Уборка untracked-файлов прерванной попытки."""
        self._cli.clean()


@dataclass
class NoGit(GitOpsFakeBase):
    """`GitOps`-фейк для шагов, которым трогать репозиторий не положено."""

    def diff_head(self) -> str:
        """Не вызывается: патч раунда снимает PROPOSING."""
        raise AssertionError("шаг не вправе снимать дифф")

    def commit_round(self, round_no: int) -> None:
        """Не вызывается: коммит принятого раунда делает DECIDING."""
        raise AssertionError(f"шаг не вправе коммитить раунд {round_no}")

    def reset_hard(self, rev: str) -> None:
        """Не вызывается: сброс дерева — прерогатива PROPOSING."""
        raise AssertionError(f"шаг не вправе сбрасывать дерево на {rev}")

    def clean(self) -> None:
        """Не вызывается: уборка дерева — прерогатива PROPOSING."""
        raise AssertionError("шаг не вправе убирать дерево")


@dataclass
class SpyDecide:
    """Спай на `core.decide`: журналирует поданный снимок и зовёт ядро."""

    calls: list[DecidingInputs] = field(default_factory=list)

    def __call__(self, inputs: DecidingInputs) -> DecisionDraft:
        """Записывает вход и возвращает решение настоящего ядра."""
        self.calls.append(inputs)
        return decide(inputs)


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


def _round_commit_subjects(root: Path) -> list[str]:
    """Сообщения коммитов раундов в истории `HEAD`, от старых к новым."""
    log = _git(root, "log", "--format=%s", "--reverse")
    return [line for line in log.splitlines() if re.match(ROUND_COMMIT_PATTERN, line)]


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
            Role.AUTHOR: AgentRef(
                adapter="claude_code", model="opus", session_ref="ref-author"
            ),
            Role.REVIEWER: AgentRef(
                adapter="claude_code", model="sonnet", session_ref="ref-reviewer"
            ),
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
    author: Any = None,
    reviewer: Any = None,
    verifier: Any = None,
    git: Any = None,
    base_commit: str = "0" * 40,
    gates: tuple[GateSpec, ...] = (),
) -> steps.StepContext:
    """`StepContext` поверх СВЕЖЕГО состояния — это и есть resume.

    Новая `SessionFsm` на каждую попытку не оптимизация теста, а условие
    задачи: после обрыва состояние поднимается из `session.json`, и ничего
    из памяти прошлой попытки шаг унаследовать не вправе.
    """
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(_state(phase), store=store, sink=sink, now=lambda: _FROZEN_NOW)
    deps = RuntimeDeps(
        workspace_root=root,
        artifact_root=root,
        store=store,
        sink=sink,
        author=NoAgent() if author is None else author,
        reviewer=NoAgent() if reviewer is None else reviewer,
        verifier=NoVerifier() if verifier is None else verifier,
        git=NoGit() if git is None else git,
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    return steps.StepContext(deps=deps, fsm=fsm, base_commit=base_commit, gates=gates)


def _round_names(root: Path) -> list[str]:
    """Прямой скан `rounds/NNN/`: имена всего, что там лежит."""
    return sorted(entry.name for entry in round_dir(root, _ROUND).iterdir())


def _temp_leftovers(root: Path) -> list[str]:
    """Временные файлы раунда: `*.tmp`, `*~` и скрытые огрызки записи.

    Скан прямой, а не «через писателя»: обещание `disputatio.events` —
    атомарность одной записи, а не уборка после обрыва посреди неё, и
    отвечает за отсутствие мусора именно переигрывающая сторона.
    """
    return sorted(
        name
        for name in _round_names(root)
        if name.endswith(_TEMP_SUFFIXES)
        or (name.startswith(".") and name != _FINALIZED_MARKER)
    )


def _unexpected(root: Path) -> list[str]:
    """Всё, чему в раунде быть не положено, — по белому списку имён.

    Белый список, а не набор шаблонов мусора: переигранный раунд обязан
    состоять ровно из артефактов, и любой файл сверх них — след попытки,
    независимо от того, угадан ли его шаблон.
    """
    return sorted(name for name in _round_names(root) if name not in _ALLOWED_NAMES)


def _artifact_text(root: Path, name: str) -> str:
    """Текст артефакта раунда с диска."""
    return round_artifact(root, _ROUND, name).read_text(encoding="utf-8")


def _proposal(body: str) -> str:
    """Валидный `proposal.md` раунда `_ROUND` с телом `body`."""
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
        f"{body}\n"
    )


def _gates() -> tuple[GateSpec, ...]:
    """Две спеки гейтов — на одной задвоение результата ненаблюдаемо."""
    return (
        GateSpec(name="pytest", cmd="uv run pytest -q"),
        GateSpec(name="ruff", cmd="ruff check ."),
    )


def _verification(*, overall: OverallStatus = OverallStatus.PASS) -> VerificationReport:
    """Отчёт проверок раунда `_ROUND` — по одному результату на спеку."""
    failing = overall is OverallStatus.FAIL
    return VerificationReport(
        round=_ROUND,
        gates=[
            GateResult(
                name=spec.name,
                cmd=spec.cmd,
                status=GateStatus.FAIL if failing else GateStatus.PASS,
                exit_code=1 if failing else 0,
                duration_s=1.5,
                tail=f"вывод гейта {spec.name}",
            )
            for spec in _gates()
        ],
        overall=overall,
        diff_stats=DiffStats(files=1, insertions=4, deletions=2),
    )


def _review_payload(summary: str) -> dict[str, Any]:
    """`review.json`-payload ревьюера; схемно и протокольно валиден (§4.4)."""
    return {
        "schema": "disputatio/v1",
        "round": _ROUND,
        "role": "reviewer",
        "verdict": "request_changes",
        "confidence": 0.8,
        "issues": [
            {
                "id": "I-A",
                "severity": "major",
                "file": "feature.py",
                "claim": "экспорт теряет заголовок",
                "evidence": "feature.py:12 — writer.writerow пропущен",
            }
        ],
        "checked": ["feature.py"],
        "summary": summary,
    }


def _reply(summary: str) -> str:
    """Ответ ревьюера: болтовня вокруг JSON — как у настоящего адаптера."""
    body = json.dumps(_review_payload(summary), ensure_ascii=False)
    return f"Посмотрел патч.\n{body}\nГотов ответить на вопросы."


def _review_model(summary: str) -> Review:
    """Модель ревью раунда `_ROUND` — для посева `review.json`."""
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
        summary=summary,
    )


def _seed_for_review(root: Path) -> None:
    """Кладёт то, из чего шаг REVIEWING собирает промпт: отчёт, текст, патч."""
    write_round_artifact(
        root,
        _ROUND,
        VERIFICATION_NAME,
        _verification().model_dump_json(by_alias=True),
    )
    write_round_artifact(root, _ROUND, PROPOSAL_NAME, _proposal("предложение автора"))
    write_round_artifact(root, _ROUND, CHANGES_PATCH_NAME, "--- a/feature.py\n")


def _seed_for_decision(root: Path) -> None:
    """Кладёт то, из чего шаг DECIDING собирает снимок для ядра."""
    _seed_for_review(root)
    write_round_artifact(
        root,
        _ROUND,
        REVIEW_NAME,
        _review_model("свод ревьюера").model_dump_json(by_alias=True),
    )


def _break_replace_once(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Срывает `os.replace` ровно на первой записи артефакта `name`.

    Это и есть «обрыв посреди записи»: `atomic_write` к этому моменту создал
    временный файл, записал и сбросил его на диск, а переименования не
    случилось. Точка отказа выбрана по имени цели, чтобы срыв не задел
    посторонние записи процесса, и однократна — повтор обязан пройти запись
    до конца.
    """
    real_replace = os.replace
    tripped = False

    def flaky(src: Any, dst: Any, **kwargs: Any) -> None:
        nonlocal tripped
        if not tripped and Path(dst).name == name:
            tripped = True
            raise InterruptedWrite(f"обрыв посреди записи {name}")
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)


def test_proposing_twice_leaves_no_trace_of_the_first_attempt(git_repo: Path) -> None:
    """Повтор PROPOSING начинает с чистого дерева ([REQ-012], [REQ-015]).

    Первая попытка оставляет два разных следа: untracked-файл, который
    `reset --hard` не видит, и правку tracked-файла, которую не видит
    `clean`. Переживи любой из них повтор — `changes.patch` предъявил бы
    ревьюеру чужую работу как работу автора этого раунда.
    """
    base = _head(git_repo)
    readme = git_repo / "README.md"

    def first_attempt() -> None:
        (git_repo / "attempt_one.py").write_text("print('одна')\n", encoding="utf-8")
        readme.write_text("испорчено первой попыткой\n", encoding="utf-8")

    def second_attempt() -> None:
        (git_repo / "attempt_two.py").write_text("print('две')\n", encoding="utf-8")

    for body, effect in (
        ("первая попытка", first_attempt),
        ("вторая попытка", second_attempt),
    ):
        anyio.run(
            steps.propose,
            _context(
                git_repo,
                phase=SessionPhase.PROPOSING,
                author=QueueAgent(replies=[_proposal(body)], on_run=effect),
                git=RealGit(git_repo),
                base_commit=base,
            ),
        )

    assert not (git_repo / "attempt_one.py").exists()
    assert readme.read_text(encoding="utf-8") == _README_TEXT

    assert _artifact_text(git_repo, PROPOSAL_NAME) == _proposal("вторая попытка")
    patch = _artifact_text(git_repo, CHANGES_PATCH_NAME)
    assert "attempt_two.py" in patch
    assert "attempt_one.py" not in patch
    assert "испорчено первой попыткой" not in patch

    assert _round_names(git_repo) == sorted({PROPOSAL_NAME, CHANGES_PATCH_NAME})


def test_verifying_twice_writes_an_equivalent_report_without_duplicate_gates(
    tmp_path: Path,
) -> None:
    """Повтор VERIFYING пересобирает отчёт, а не дополняет ([REQ-015]).

    Гейтов ровно столько, сколько спек: задвоенный результат прошёл бы
    сравнение «отчёт непуст», но §4.4 и §5 считали бы по нему дважды.
    """
    verifier = QueueVerifier(reports=[_verification(), _verification()])

    def run_step() -> None:
        steps.verify(
            _context(
                tmp_path,
                phase=SessionPhase.VERIFYING,
                verifier=verifier,
                gates=_gates(),
            )
        )

    run_step()
    after_first = _artifact_text(tmp_path, VERIFICATION_NAME)
    run_step()
    after_second = _artifact_text(tmp_path, VERIFICATION_NAME)

    assert verifier.rounds == [_ROUND, _ROUND]
    assert after_second == after_first
    report = VerificationReport.model_validate_json(after_second)
    assert [gate.name for gate in report.gates] == [spec.name for spec in _gates()]
    assert _temp_leftovers(tmp_path) == []


def test_reviewing_twice_replaces_review_json_atomically(tmp_path: Path) -> None:
    """Повтор REVIEWING заменяет `review.json` целиком ([DESIGN-015]).

    Смена inode — прямой признак temp + rename: правка на месте (усечение и
    запись) оставила бы номер прежним, а вместе с ним и окно, в котором на
    диске лежит полфайла. Содержимое пинится вторым ответом ревьюера:
    повтор, подсунувший результат первой попытки, иначе прошёл бы.
    """
    _seed_for_review(tmp_path)
    reviewer = QueueAgent(replies=[_reply("свод первой"), _reply("свод второй")])
    path = round_artifact(tmp_path, _ROUND, REVIEW_NAME)

    def run_step() -> None:
        anyio.run(
            steps.review,
            _context(tmp_path, phase=SessionPhase.REVIEWING, reviewer=reviewer),
        )

    run_step()
    first_text = path.read_text(encoding="utf-8")
    first_inode = path.stat().st_ino
    run_step()
    second_text = path.read_text(encoding="utf-8")

    assert Review.model_validate_json(first_text).summary == "свод первой"
    assert Review.model_validate_json(second_text).summary == "свод второй"
    assert path.stat().st_ino != first_inode
    assert _temp_leftovers(tmp_path) == []


def test_deciding_twice_feeds_the_core_the_same_inputs_and_commits_once(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повтор DECIDING подаёт ядру тот же снимок и не двоит коммит.

    Оба утверждения — про одно свойство с разных сторон: вход ядра собирается
    с диска, поэтому решение повтора совпадает с записанным, а `commit_round`
    при пустом диффе — no-op ([DESIGN-011]). Лишний коммит сдвинул бы цель
    сброса раунда N+1 на состояние, работы автора не содержащее
    ([DESIGN-012]).
    """
    _seed_for_decision(git_repo)
    (git_repo / "feature.py").write_text("print('работа автора')\n", encoding="utf-8")
    spy = SpyDecide()
    monkeypatch.setattr(steps, "decide", spy)
    git = RealGit(git_repo)

    steps.decide_step(_context(git_repo, phase=SessionPhase.DECIDING, git=git))
    after_first = _artifact_text(git_repo, DECISION_NAME)
    steps.decide_step(_context(git_repo, phase=SessionPhase.DECIDING, git=git))

    assert len(spy.calls) == 2
    assert spy.calls[0] == spy.calls[1]
    assert _artifact_text(git_repo, DECISION_NAME) == after_first
    assert git.commits == [_ROUND, _ROUND]
    assert _round_commit_subjects(git_repo) == ["disputatio: round 001"]
    assert _temp_leftovers(git_repo) == []


def test_replay_of_an_interrupted_write_leaves_no_temp_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обрыв посреди записи не оставляет мусора в `rounds/NNN/` ([REQ-015]).

    Обрыв здесь настоящий: `os.replace` срывается ровно на записи
    `review.json`, и временный файл `atomic_write` остаётся на диске — так и
    задокументировано в `disputatio.events`. Убрать его обязан тот, кто
    переигрывает шаг; иначе каталог раунда копит по огрызку на каждый обрыв,
    а экспорт и ревьюер читают директорию, в которой лежит полфайла.
    """
    _seed_for_review(tmp_path)
    reviewer = QueueAgent(replies=[_reply("свод первой"), _reply("свод второй")])
    _break_replace_once(monkeypatch, REVIEW_NAME)

    def run_step() -> None:
        anyio.run(
            steps.review,
            _context(tmp_path, phase=SessionPhase.REVIEWING, reviewer=reviewer),
        )

    with pytest.raises(InterruptedWrite):
        run_step()

    leftovers = _temp_leftovers(tmp_path)
    assert len(leftovers) == 1, (
        "обрыв посреди atomic_write обязан оставить ровно один временный файл — "
        f"иначе симуляция не воспроизводит обрыв: {leftovers}"
    )
    assert leftovers[0].startswith(".")
    assert leftovers[0].endswith(".tmp")
    assert not round_artifact(tmp_path, _ROUND, REVIEW_NAME).exists()

    run_step()

    assert _temp_leftovers(tmp_path) == []
    assert _unexpected(tmp_path) == []
    written = Review.model_validate_json(_artifact_text(tmp_path, REVIEW_NAME))
    assert written.summary == "свод второй"


def test_replay_sweeps_stale_temp_and_backup_leftovers(tmp_path: Path) -> None:
    """Огрызки прошлых обрывов не переживают повтор шага ([REQ-015]).

    Три формы мусора разом: скрытый `.tmp` от `atomic_write`, резервная копия
    редактора `*~` и нескрытый `.tmp`. Маркер `.finalized` рядом с ними обязан
    уцелеть — уборка мусора не вправе отменять I3 ([REQ-016]).
    """
    _seed_for_review(tmp_path)
    directory = round_dir(tmp_path, _ROUND)
    (directory / f".{REVIEW_NAME}.deadbeef.tmp").write_text("огр", encoding="utf-8")
    (directory / f"{PROPOSAL_NAME}~").write_text("огр", encoding="utf-8")
    (directory / f"{VERIFICATION_NAME}.tmp").write_text("огр", encoding="utf-8")

    steps.verify(
        _context(
            tmp_path,
            phase=SessionPhase.VERIFYING,
            verifier=QueueVerifier(reports=[_verification()]),
            gates=_gates(),
        )
    )

    assert _temp_leftovers(tmp_path) == []
    assert _unexpected(tmp_path) == []


def test_every_artifact_of_a_replayed_round_is_valid_against_its_schema(
    git_repo: Path,
) -> None:
    """Раунд, каждый шаг которого переигран, валиден целиком ([REQ-015]).

    Проверка сквозная нарочно: идемпотентность отдельного шага ничего не
    стоит, если пара переигранных шагов оставляет раунд, который следующий
    раунд прочитать не может. Разбираются все четыре артефакта — теми же
    моделями, которыми их читает история.
    """
    base = _head(git_repo)

    def author_work() -> None:
        (git_repo / "feature.py").write_text(
            "print('работа автора')\n", encoding="utf-8"
        )

    author = QueueAgent(
        replies=[_proposal("попытка один"), _proposal("попытка два")],
        on_run=author_work,
    )
    reviewer = QueueAgent(replies=[_reply("свод первой"), _reply("свод второй")])
    verifier = QueueVerifier(reports=[_verification(), _verification()])
    git = RealGit(git_repo)

    for _ in range(2):
        anyio.run(
            steps.propose,
            _context(
                git_repo,
                phase=SessionPhase.PROPOSING,
                author=author,
                git=git,
                base_commit=base,
            ),
        )
    for _ in range(2):
        steps.verify(
            _context(
                git_repo,
                phase=SessionPhase.VERIFYING,
                verifier=verifier,
                gates=_gates(),
            )
        )
    for _ in range(2):
        anyio.run(
            steps.review,
            _context(git_repo, phase=SessionPhase.REVIEWING, reviewer=reviewer),
        )
    for _ in range(2):
        steps.decide_step(_context(git_repo, phase=SessionPhase.DECIDING, git=git))

    frontmatter, body = parse_proposal(_artifact_text(git_repo, PROPOSAL_NAME))
    assert frontmatter.round == _ROUND
    assert "попытка два" in body
    report = VerificationReport.model_validate_json(
        _artifact_text(git_repo, VERIFICATION_NAME)
    )
    assert report.round == _ROUND
    written = Review.model_validate_json(_artifact_text(git_repo, REVIEW_NAME))
    assert written.summary == "свод второй"
    assert _artifact_text(git_repo, DECISION_NAME)

    assert _temp_leftovers(git_repo) == []
    assert _unexpected(git_repo) == []
    assert _round_commit_subjects(git_repo) == ["disputatio: round 001"]


def test_replayed_step_asks_the_agent_again_and_keeps_no_ledger(
    tmp_path: Path,
) -> None:
    """Осознанное отступление [DESIGN-015]/[ADR-004]: бюджет считается дважды.

    Переигранный шаг разговаривает с агентом ЗАНОВО — ответ прошлой попытки
    не кэшируется и не переиспользуется, поэтому его токены попадут в
    счётчик второй раз. Отступление осознанное: оверкаунт консервативен
    (`BUDGET_HIT` сработает не позже, чем следует), а реестр «за что уже
    заплачено» был бы ещё одним состоянием на диске, которое само переживает
    обрыв неполным. Пинятся обе половины: второй вызов агента состоялся, и
    никакой бухгалтерии рядом с артефактами раунда не завелось.
    """
    _seed_for_review(tmp_path)
    reviewer = QueueAgent(replies=[_reply("свод первой"), _reply("свод второй")])

    for _ in range(2):
        anyio.run(
            steps.review,
            _context(tmp_path, phase=SessionPhase.REVIEWING, reviewer=reviewer),
        )

    assert len(reviewer.prompts) == 2
    assert reviewer.replies == []
    assert _unexpected(tmp_path) == []
