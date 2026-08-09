"""Шаг PROPOSING ([REQ-003], [REQ-012], [REQ-013], [DESIGN-003], [ADR-002]).

[TASK-008]. Шаг склеивает четыре чужих механизма — git-дисциплину §3,
сборку промпта §6.1, вызов адаптера и запись артефактов раунда, — и почти
всё, что в нём может сломаться, ломается тихо. Поэтому тест пинит не
«артефакты появились», а порядок и источники:

* **порядок операций** проверяется одним общим spy-логом, а не по одному
  вызову на ассерт: `reset_hard` → `clean` → сборка промпта → `author.run`
  → `proposal.md` → `diff_head` → `changes.patch`. Переставь `clean` за
  вызов автора — и уборка снесёт работу автора; переставь `diff_head` перед
  `author.run` — и патч окажется пустым при любой правке;
* **сброс — до автора и по вычисленной цели** ([REQ-012]): дерево входит в
  шаг очищенным от прерванной попытки, а целью служит `base_rev`, а не
  `HEAD`, — на входе в раунд `HEAD` может быть чем угодно;
* **промпт — результат `context.build_author_prompt`**, а не склейка вокруг
  него: спай подменяет функцию в модуле шага, но возвращает настоящий
  результат, и промпт, ушедший адаптеру, обязан быть равен ему байт-в-байт;
* **материалы промпта — строго раунда N−1** ([REQ-003]): при трёх раундах
  на диске в промпт обязаны попасть директива, замечание и гейт раунда 002
  и не попасть — раунда 001. Прошлые proposal не попадают вовсе, и это
  пинится сигнатурой `build_author_prompt`, а не только текстом;
* **`changes.patch` пишется всегда** ([REQ-013]), в т. ч. пустым: «нечего
  показать» и «шаг не дошёл до патча» — разные факты, и различает их только
  наличие файла;
* **ADR-002**: путь, возвращённый `write_round_artifact`, равен пути от
  `runtime.layout` — иначе read-side зеркало раскладки разъедется с
  писателем молча;
* **писатель** берётся у `core.active_writer` (I1), своей таблицы runtime не
  заводит — проверяется AST-сканом модуля шагов.

`runtime.steps`/`runtime.layout` до реализации не существуют, поэтому
доступ к ним идёт через `_steps`/`_layout`/`_steps_attr`: отсутствие модуля
или символа переводится в `AssertionError`, а не в ошибку коллекции —
red-чекпоинт обязан быть падением assertion'ом.
"""

import ast
import inspect
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest

from disputatio.context import build_author_prompt
from disputatio.contracts import (
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
    ProposalParseError,
    Review,
    Role,
    SessionPhase,
    SessionState,
    Severity,
    TaskSpec,
    Verdict,
    VerificationReport,
)
from disputatio.core import SessionFsm, Writer, active_writer
from disputatio.events import write_round_artifact
from disputatio.runtime import GitCli, RuntimeDeps
from disputatio.runtime.git import ROUND_COMMIT_TEMPLATE

_FROZEN_NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
_SESSION_DIR = ".disputatio"

# Ожидаемая последовательность шага целиком. Список, а не множество: цена
# ошибки здесь — не «забыли вызвать», а «вызвали не в том порядке».
_EXPECTED_ORDER = [
    "reset_hard",
    "clean",
    "build_author_prompt",
    "author.run",
    "write:proposal.md",
    "diff_head",
    "write:changes.patch",
]

# Ровно те параметры, которые §6.1 разрешает промпту автора. Равенство, а не
# вхождение: параметр под прошлые proposal обязан отсутствовать физически.
_AUTHOR_PROMPT_PARAMS = frozenset(
    {"task", "round", "prior_review", "prior_verification", "prior_decision"}
)


def _steps() -> ModuleType:
    """Модуль `runtime/steps.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.steps")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/steps.py: {exc}") from exc


def _layout() -> ModuleType:
    """Модуль `runtime/layout.py` — read-side зеркало раскладки (ADR-002)."""
    try:
        return import_module("disputatio.runtime.layout")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/layout.py: {exc}") from exc


def _steps_attr(name: str) -> Any:
    """Символ модуля шагов; отсутствие — `AssertionError`, не `AttributeError`."""
    steps = _steps()
    assert hasattr(steps, name), f"runtime/steps.py не определяет {name!r}"
    return getattr(steps, name)


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
class FakeVerifier:
    """`Verifier`-фейк: шаг PROPOSING его не касается вовсе."""

    rounds: list[int] = field(default_factory=list)

    def verify(self, round_no: int) -> VerificationReport:
        """Шагом PROPOSING не вызывается — вызов означает ошибку шага."""
        self.rounds.append(round_no)
        raise AssertionError(f"PROPOSING не вправе гонять гейты (раунд {round_no})")


@dataclass
class SpyGit:
    """`GitOps` поверх настоящего `GitCli` с общим spy-логом.

    Делегирование, а не подмена: `reset_hard`/`clean`/`diff_head` обязаны
    реально тронуть дерево, иначе [REQ-012] проверялся бы на фейке, который
    сам же и обещает нужное поведение.
    """

    root: Path
    log: list[str]
    reset_targets: list[str] = field(default_factory=list)

    @property
    def _cli(self) -> GitCli:
        return GitCli(self.root)

    def diff_head(self) -> str:
        """`git diff HEAD` рабочего дерева."""
        self.log.append("diff_head")
        return self._cli.diff_head()

    def commit_round(self, round_no: int) -> None:
        """Коммит принятого раунда; шагом PROPOSING не вызывается."""
        self.log.append(f"commit_round:{round_no}")
        self._cli.commit_round(round_no)

    def reset_hard(self, rev: str) -> None:
        """Сброс дерева на `rev`; цель запоминается для проверки."""
        self.log.append("reset_hard")
        self.reset_targets.append(rev)
        self._cli.reset_hard(rev)

    def clean(self) -> None:
        """Уборка untracked-файлов прерванной попытки."""
        self.log.append("clean")
        self._cli.clean()


@dataclass
class SpyAuthor:
    """`AgentAdapter`-фейк автора: журнал, снимок писателя, побочный эффект.

    `on_run` — «работа автора»: правка дерева ровно в тот момент, когда её
    делает настоящий адаптер. Так проверяется и то, что сброс уже позади, и
    то, что `diff_head` видит именно эту правку.
    """

    log: list[str]
    reply: str
    fsm: SessionFsm | None = None
    on_run: Callable[[], None] | None = None
    prompts: list[str] = field(default_factory=list)
    session_refs: list[str | None] = field(default_factory=list)
    writers: list[Writer | None] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует вызов, правит дерево и отдаёт заготовленный ответ."""
        self.log.append("author.run")
        self.prompts.append(prompt)
        self.session_refs.append(session_ref)
        if self.fsm is not None:
            self.writers.append(active_writer(self.fsm.state.state))
        if self.on_run is not None:
            self.on_run()
        return AgentTurn(text=self.reply, session_ref=session_ref)


@dataclass(frozen=True)
class Written:
    """Один вызов `write_round_artifact`, пойманный спаем."""

    round_no: int
    name: str
    content: str
    path: Path


@dataclass
class Harness:
    """Собранное окружение шага: контекст, спаи и общий лог порядка."""

    root: Path
    ctx: Any
    fsm: SessionFsm
    author: SpyAuthor
    git: SpyGit
    log: list[str]
    prompt_calls: list[dict[str, Any]]
    writes: list[Written]


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


def _commit(root: Path, subject: str, *, path: str, text: str) -> str:
    """Коммитит правку с точным subject'ом и возвращает её SHA."""
    (root / path).write_text(text, encoding="utf-8")
    _git(root, "add", path)
    _git(root, "commit", "--quiet", "-m", subject)
    return _head(root)


def _state(
    round_no: int, *, phase: SessionPhase = SessionPhase.PROPOSING
) -> SessionState:
    """`SessionState` раунда `round_no` в фазе `phase`."""
    return SessionState(
        session_id="s-proposing",
        created_at=_FROZEN_NOW,
        state=phase,
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


def _write_prior_round(root: Path, round_no: int) -> None:
    """Кладёт review/verification/decision раунда `round_no` на диск.

    Все тексты помечены номером раунда: перепутанный раунд виден в промпте
    как чужая метка, а не как отсутствие секции.
    """
    marker = f"{round_no:03d}"
    review = Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.9,
        issues=[
            Issue(
                id=f"I-{marker}",
                severity=Severity.MAJOR,
                file="feature.py",
                claim=f"замечание раунда {marker}",
                evidence=f"feature.py:1 — свидетельство раунда {marker}",
            )
        ],
        checked=["feature.py"],
        summary=f"свод раунда {marker}",
    )
    verification = VerificationReport(
        round=round_no,
        gates=[
            GateResult(
                name=f"gate-{marker}",
                cmd="uv run pytest -q",
                status=GateStatus.FAIL,
                exit_code=1,
                tail=f"падение раунда {marker}",
            )
        ],
        overall=OverallStatus.FAIL,
        diff_stats=DiffStats(files=1, insertions=2, deletions=0),
    )
    decision = Decision(
        round=round_no,
        outcome=Outcome.CONTINUE,
        reason=f"причина раунда {marker}",
        open_issues_carried=[f"I-{marker}"],
        next_round_directive=f"директива раунда {marker}",
    )
    write_round_artifact(
        root, round_no, "review.json", review.model_dump_json(by_alias=True)
    )
    write_round_artifact(
        root,
        round_no,
        "verification.json",
        verification.model_dump_json(by_alias=True),
    )
    write_round_artifact(
        root, round_no, "decision.json", decision.model_dump_json(by_alias=True)
    )
    write_round_artifact(
        root,
        round_no,
        "proposal.md",
        _proposal(round_no, body=f"ТЕЛО-PROPOSAL-РАУНДА-{marker}"),
    )


def _spy_prompt_builder(
    monkeypatch: pytest.MonkeyPatch, log: list[str], calls: list[dict[str, Any]]
) -> None:
    """Подменяет сборщик промпта в модуле шага, сохраняя его результат.

    Спай возвращает настоящий текст: тест сравнивает с ним промпт, ушедший
    адаптеру, и ловит любую склейку вокруг вызова.
    """
    steps = _steps()
    assert hasattr(steps, "build_author_prompt"), (
        "runtime/steps.py обязан импортировать context.build_author_prompt "
        "именем build_author_prompt — промпт собирается вызовом, не склейкой"
    )

    def spy(**kwargs: Any) -> str:
        log.append("build_author_prompt")
        calls.append(kwargs)
        return build_author_prompt(**kwargs)

    monkeypatch.setattr(steps, "build_author_prompt", spy)


def _spy_writer(
    monkeypatch: pytest.MonkeyPatch, log: list[str], writes: list[Written]
) -> None:
    """Подменяет `write_round_artifact` в модуле шага, сохраняя запись.

    Настоящая запись выполняется — тест смотрит и на возвращённый путь
    (ADR-002), и на содержимое файла на диске.
    """
    steps = _steps()
    assert hasattr(steps, "write_round_artifact"), (
        "runtime/steps.py обязан писать артефакты через events.write_round_artifact"
    )

    def spy(root: Path, round_no: int, name: str, content: str) -> Path:
        log.append(f"write:{name}")
        path = write_round_artifact(root, round_no, name, content)
        writes.append(Written(round_no=round_no, name=name, content=content, path=path))
        return path

    monkeypatch.setattr(steps, "write_round_artifact", spy)


def _make_harness(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    round_no: int,
    base_commit: str,
    reply: str,
    phase: SessionPhase = SessionPhase.PROPOSING,
    on_run: Callable[[], None] | None = None,
) -> Harness:
    """Собирает `StepContext` со спаями на всех наблюдаемых границах шага."""
    log: list[str] = []
    store = FakeStore()
    sink = FakeSink()
    fsm = SessionFsm(
        _state(round_no, phase=phase),
        store=store,
        sink=sink,
        now=lambda: _FROZEN_NOW,
    )
    git = SpyGit(root=root, log=log)
    author = SpyAuthor(log=log, reply=reply, fsm=fsm, on_run=on_run)
    deps = RuntimeDeps(
        root=root,
        store=store,
        sink=sink,
        author=author,
        reviewer=SpyAuthor(log=[], reply=""),
        verifier=FakeVerifier(),
        git=git,
        now=lambda: _FROZEN_NOW,
        monotonic=lambda: 0.0,
    )
    prompt_calls: list[dict[str, Any]] = []
    writes: list[Written] = []
    _spy_prompt_builder(monkeypatch, log, prompt_calls)
    _spy_writer(monkeypatch, log, writes)
    ctx = _steps_attr("StepContext")(deps=deps, fsm=fsm, base_commit=base_commit)
    return Harness(
        root=root,
        ctx=ctx,
        fsm=fsm,
        author=author,
        git=git,
        log=log,
        prompt_calls=prompt_calls,
        writes=writes,
    )


def _propose(harness: Harness) -> None:
    """Выполняет шаг: единственный await шага крутится через `anyio.run`."""
    anyio.run(_steps_attr("propose"), harness.ctx)


def _written(harness: Harness, name: str) -> Written:
    """Единственная запись артефакта `name`; иное количество — ошибка шага."""
    found = [write for write in harness.writes if write.name == name]
    assert len(found) == 1, f"{name} записан {len(found)} раз(а), ожидалась одна запись"
    return found[0]


def test_step_order_is_reset_clean_prompt_author_artifacts(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порядок шага фиксирован общим spy-логом ([DESIGN-003])."""
    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=1,
        base_commit=_head(git_repo),
        reply=_proposal(1, body="раунд один"),
        on_run=lambda: (git_repo / "feature.py").write_text(
            "print('раунд один')\n", encoding="utf-8"
        ),
    )

    _propose(harness)

    assert harness.log == _EXPECTED_ORDER


def test_reset_and_clean_wipe_the_aborted_attempt_before_the_author_runs(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дерево входит в шаг без следов прерванной попытки ([REQ-012])."""
    readme = git_repo / "README.md"
    original = readme.read_text(encoding="utf-8")
    readme.write_text("правка прерванной попытки\n", encoding="utf-8")
    (git_repo / "stray.txt").write_text(
        "черновик прерванной попытки\n", encoding="utf-8"
    )
    journal = git_repo / _SESSION_DIR / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text('{"type": "state_change"}\n', encoding="utf-8")

    seen: dict[str, Any] = {}

    def author_work() -> None:
        seen["readme"] = readme.read_text(encoding="utf-8")
        seen["stray"] = (git_repo / "stray.txt").exists()
        seen["journal"] = journal.exists()
        (git_repo / "feature.py").write_text(
            "print('работа автора')\n", encoding="utf-8"
        )

    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=1,
        base_commit=_head(git_repo),
        reply=_proposal(1, body="раунд один"),
        on_run=author_work,
    )

    _propose(harness)

    assert seen["readme"] == original, "reset --hard не вернул tracked-файл к базе"
    assert seen["stray"] is False, "clean не убрал untracked-черновик прошлой попытки"
    assert seen["journal"] is True, "уборка снесла журнал сессии — resume потерян"
    patch = _written(harness, "changes.patch").content
    assert "feature.py" in patch
    assert "stray.txt" not in patch


def test_round_one_passes_no_prior_artifacts_to_the_prompt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При N == 1 все `prior_*` — `None` ([REQ-003])."""
    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=1,
        base_commit=_head(git_repo),
        reply=_proposal(1, body="раунд один"),
    )

    _propose(harness)

    assert len(harness.prompt_calls) == 1
    call = harness.prompt_calls[0]
    assert call["round"] == 1
    assert call["task"] == harness.fsm.state.task
    assert call["prior_review"] is None
    assert call["prior_verification"] is None
    assert call["prior_decision"] is None


def test_prompt_is_the_builder_result_and_is_not_hand_glued(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Адаптер получает ровно то, что вернул `build_author_prompt` ([REQ-003])."""
    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=1,
        base_commit=_head(git_repo),
        reply=_proposal(1, body="раунд один"),
    )

    _propose(harness)

    expected = build_author_prompt(**harness.prompt_calls[0])
    assert harness.author.prompts == [expected]
    assert harness.author.session_refs == [
        harness.fsm.state.agents[Role.AUTHOR].session_ref
    ]


def test_prompt_carries_only_round_n_minus_one_artifacts(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Директива, замечание и гейт — раунда N−1, не N−2 ([REQ-003])."""
    _commit(git_repo, ROUND_COMMIT_TEMPLATE.format(round=1), path="r1.txt", text="1\n")
    round_two = _commit(
        git_repo, ROUND_COMMIT_TEMPLATE.format(round=2), path="r2.txt", text="2\n"
    )
    # Коммит поверх раунда 002: если целью сброса берут `HEAD`, а не
    # `base_rev`, дерево останется с этой правкой и тест это увидит.
    _commit(git_repo, "посторонний коммит", path="alien.txt", text="чужое\n")
    _write_prior_round(git_repo, 1)
    _write_prior_round(git_repo, 2)

    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=3,
        base_commit=_head(git_repo),
        reply=_proposal(3, body="раунд три"),
    )

    _propose(harness)

    assert harness.git.reset_targets == [round_two]
    assert _head(git_repo) == round_two
    call = harness.prompt_calls[0]
    assert call["prior_review"].round == 2
    assert call["prior_verification"].round == 2
    assert call["prior_decision"].round == 2
    prompt = harness.author.prompts[0]
    assert "директива раунда 002" in prompt
    assert "замечание раунда 002" in prompt
    assert "gate-002" in prompt
    assert "001" not in prompt


def test_past_author_proposals_never_reach_the_prompt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прошлые proposal не попадают в промпт — гарантия структурная (§6.1)."""
    assert set(inspect.signature(build_author_prompt).parameters) == (
        _AUTHOR_PROMPT_PARAMS
    ), "у build_author_prompt появился параметр под прошлые proposal автора"

    _commit(git_repo, ROUND_COMMIT_TEMPLATE.format(round=1), path="r1.txt", text="1\n")
    _write_prior_round(git_repo, 1)
    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=2,
        base_commit=_head(git_repo),
        reply=_proposal(2, body="раунд два"),
    )

    _propose(harness)

    assert "ТЕЛО-PROPOSAL-РАУНДА-001" not in harness.author.prompts[0]


def test_changes_patch_is_written_even_when_the_author_changed_nothing(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой дифф — не ошибка шага, но файл обязан быть ([REQ-013])."""
    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=1,
        base_commit=_head(git_repo),
        reply=_proposal(1, body="анализ без правок"),
    )

    _propose(harness)

    patch = _written(harness, "changes.patch")
    assert patch.content == ""
    assert patch.path.exists()
    assert patch.path.read_text(encoding="utf-8") == ""


def test_written_paths_equal_the_layout_paths(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-002: read-side зеркало не расходится с писателем."""
    layout = _layout()
    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=1,
        base_commit=_head(git_repo),
        reply=_proposal(1, body="раунд один"),
        on_run=lambda: (git_repo / "feature.py").write_text("x\n", encoding="utf-8"),
    )

    _propose(harness)

    assert [write.name for write in harness.writes] == ["proposal.md", "changes.patch"]
    for write in harness.writes:
        mirrored = layout.round_artifact(git_repo, write.round_no, write.name)
        assert write.path == mirrored, (
            f"layout разошёлся с write_round_artifact на {write.name}"
        )
        assert mirrored.exists()
        assert mirrored.parent == layout.round_dir(git_repo, write.round_no)
    assert layout.PROPOSAL_NAME == "proposal.md"
    assert layout.CHANGES_PATCH_NAME == "changes.patch"


def test_invalid_frontmatter_is_not_written_to_disk(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ответ без фронтматтера не становится `proposal.md` ([REQ-003])."""
    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=1,
        base_commit=_head(git_repo),
        reply="просто текст без фронтматтера\n",
        on_run=lambda: (git_repo / "feature.py").write_text("x\n", encoding="utf-8"),
    )

    with pytest.raises(ProposalParseError):
        _propose(harness)

    assert harness.writes == []
    round_dir = _layout().round_dir(git_repo, 1)
    assert not (round_dir / "proposal.md").exists()
    assert not (round_dir / "changes.patch").exists()


def test_active_writer_is_author_during_the_step_and_orchestrator_after(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Писатель шага — автор; вне шага — оркестратор (I1, [REQ-003])."""
    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=1,
        base_commit=_head(git_repo),
        reply=_proposal(1, body="раунд один"),
    )

    _propose(harness)

    assert harness.author.writers == [Writer.AUTHOR]
    harness.fsm.transition(SessionPhase.VERIFYING)
    assert active_writer(harness.fsm.state.state) is Writer.ORCHESTRATOR


def test_step_refuses_to_run_when_the_phase_writer_is_not_the_author(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не та фаза — шаг не начинается: ни git, ни адаптер не тронуты (I1)."""
    harness = _make_harness(
        git_repo,
        monkeypatch,
        round_no=1,
        base_commit=_head(git_repo),
        reply=_proposal(1, body="раунд один"),
        phase=SessionPhase.VERIFYING,
    )

    with pytest.raises(AssertionError):
        _propose(harness)

    assert harness.log == []


def test_runtime_keeps_no_writer_table_of_its_own(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Писатель берётся у `core.active_writer`, а не у копии таблицы (I1)."""
    path = _steps().__file__
    assert path is not None, "у runtime/steps.py нет файла на диске"
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))

    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "active_writer" in calls, "runtime/steps.py не спрашивает core.active_writer"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for value in node.values:
            assert not _mentions_writer(value), (
                "runtime/steps.py завёл собственную таблицу писателей"
            )


def _mentions_writer(node: ast.expr | None) -> bool:
    """`True`, если выражение ссылается на члена `core.Writer`."""
    if node is None:
        return False
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and inner.id == "Writer":
            return True
        if isinstance(inner, ast.Attribute) and inner.attr in {
            "AUTHOR",
            "ORCHESTRATOR",
        }:
            return True
    return False
