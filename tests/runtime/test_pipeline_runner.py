"""Runner пайплайна: фазы, интенты, контуры, возврат (SPEC-002 §2, §4.2–4.3, §7, §10).

Пять утверждений, вокруг которых собран файл, — ровно те, на которых runner
ломается тихо, если сделать «как проще».

* **Анкер создаётся первым действием `run`.** §3.1 требует этого прямо, а
  §8.1 шаг 0 делает «файла анкера нет» безусловным отказом resume — и
  обосновывает отказ именно тем, что `run` анкер уже создал. Крах на первом
  же интенте обязан оставить пустой существующий журнал, иначе пайплайн
  невозобновим по собственному правилу.
* **Терминал читается по durable-состоянию, а не по возврату драйвера.**
  У припаркованного раунда `decision.json` не существует вовсе (§7.1:
  `decide()` не вызывался), и это сам по себе признак парковки. Драйвер,
  вернувший `DECIDING`, и драйвер, вернувший `DONE`, различаются здесь тем,
  что легло на диск.
* **`budget_used` пересчитывается, а не инкрементируется** (§4.2).
  Повторный `run_session` после краха не удваивает расход по построению —
  проверяется именно повтором, а не единичным прогоном.
* **Приоритет P6 абсолютен.** Смешанное ревью (architectural + execution)
  ведёт в `SPEC_LOOP` независимо от того, сколько execution-находок рядом.
* **Crash-границы — внутри многошаговых интентов, а не «по одной на kind».**
  Каждая запись манифеста — отдельная точка обрыва, и `_CrashingStore`
  позволяет остановиться ровно на ней; §10 перечисляет одиннадцать
  сценариев, и каждый из них здесь проверяем по отдельности.

Подменены четыре порта: git (настоящий репозиторий тут ничего не доказывал бы
— все семь операций участвуют только как `head_sha`/`reset_hard`/предусловия),
драйвер сессии, фабрика сессии и экспортёр. Хранилище манифеста, журнал
событий и анкер — настоящие: все утверждения этого файла — утверждения о том,
что легло на диск.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import pytest

from disputatio.contracts import (
    SCHEMA_V2,
    AgentRef,
    BoundaryVerdict,
    BudgetUsed,
    Decision,
    Issue,
    Limits,
    Mode,
    Outcome,
    PipelinePhase,
    PipelineState,
    Review,
    Role,
    SessionOutcome,
    SessionPhase,
    SessionState,
    Severity,
    TaskSpec,
    TransitionReason,
    Verdict,
)
from disputatio.events import (
    FilePipelineStateStore,
    FileStateStore,
    IntegrityAnchor,
    PipelineEventSink,
    bootstrap_session,
    read_pipeline_events,
)
from disputatio.events.pipeline_paths import pipeline_dir, session_artifact_root
from disputatio.runtime import PipelineAlreadyExists, PipelineConfig, StatusEntry
from disputatio.runtime.pipeline_runner import (
    ArchitecturalDefectPolicy,
    PipelineRunner,
    SessionCreation,
)

SLUG: Final = "pair-docs"
SPEC_PATH: Final = "docs/spec.md"
PLAN_PATH: Final = "docs/plan.md"


class _Boom(RuntimeError):
    """Крах, инжектированный тестом: обрыв процесса в конкретной точке."""


# --------------------------------------------------------------------------
# Фейки портов
# --------------------------------------------------------------------------


class _FakeGit:
    """`GitOps` без репозитория: предусловия `run` + reset возврата (§7.3)."""

    def __init__(self) -> None:
        self.head = "a" * 40
        self.branch = "docs/pair"
        self.resets: list[str] = []
        self.entries: tuple[StatusEntry, ...] = ()
        self.raise_on_reset = False

    def diff_head(self) -> str:
        return ""

    def commit_round(self, round_no: int) -> None:
        raise AssertionError("runner раунды не коммитит")

    def reset_hard(self, rev: str) -> None:
        if self.raise_on_reset:
            raise _Boom("процесс умер между записью интента и reset'ом")
        self.resets.append(rev)

    def clean(self) -> None:
        raise AssertionError("runner дерево не убирает")

    def head_sha(self) -> str:
        return self.head

    def current_branch(self) -> str | None:
        return self.branch

    def status_entries(self) -> tuple[StatusEntry, ...]:
        return self.entries

    def diff_readonly(self) -> str:
        return ""

    def commit_paths(self, paths: Sequence[str], subject: str, *, trailer: str) -> str:
        raise AssertionError("runner чекпоинты оператора не пишет")

    def find_commit_by_trailer(self, trailer: str) -> str | None:
        return None

    def toplevel_prefix(self) -> str:
        return ""


@dataclass(slots=True)
class Script:
    """Что фейковый драйвер материализует на диске для одной сессии.

    `park` — раунд без `decision.json`: ровно то durable-состояние, по
    которому runner обязан опознать парковку (§7.1).
    """

    outcome: str = "converged"
    issues: tuple[Issue, ...] = ()
    tokens: int = 0
    wall_seconds: float = 0.0
    raise_before_write: bool = False
    raise_after_write: bool = False


class _FakeDriver:
    """`SessionDriver`: пишет артефакты раунда по скрипту и двигает `session.json`."""

    def __init__(self, scripts: dict[str, Script]) -> None:
        self.scripts = scripts
        self.calls: list[tuple[Path, str, object]] = []

    def __call__(
        self, artifact_root: Path, session_id: str, policy: object
    ) -> SessionState:
        self.calls.append((artifact_root, session_id, policy))
        script = self.scripts[session_id]
        if script.raise_before_write:
            raise _Boom(f"драйвер упал до записи артефактов {session_id}")

        store = FileStateStore(artifact_root)
        state = store.load(session_id)
        round_dir = artifact_root / ".disputatio" / "rounds" / "001"
        round_dir.mkdir(parents=True, exist_ok=True)
        _write_json(round_dir / "review.json", _review(script))

        if script.outcome == "park":
            phase = SessionPhase.DECIDING
        else:
            outcome = {
                "converged": Outcome.CONVERGED,
                "deadlock": Outcome.DEADLOCK,
                "budget_hit": Outcome.BUDGET_HIT,
                "failed": Outcome.FAILED,
            }[script.outcome]
            _write_json(
                round_dir / "decision.json",
                Decision(
                    round=1,
                    outcome=outcome,
                    reason=f"scripted_{script.outcome}",
                    next_round_directive=None,
                ),
            )
            phase = (
                SessionPhase.FAILED if script.outcome == "failed" else SessionPhase.DONE
            )

        state = state.model_copy(
            update={
                "state": phase,
                "current_round": 1,
                "budget_used": BudgetUsed(
                    tokens=script.tokens, wall_seconds=script.wall_seconds
                ),
            }
        )
        store.save(state)
        if script.raise_after_write:
            raise _Boom(f"драйвер упал после записи артефактов {session_id}")
        return state


class _FakeFactory:
    """`SessionFactory`: bootstrap каталога + стартовый `session.json`."""

    def __init__(self) -> None:
        self.creations: list[SessionCreation] = []
        self.raise_next = False

    def __call__(self, creation: SessionCreation) -> SessionState:
        self.creations.append(creation)
        if self.raise_next:
            raise _Boom(f"фабрика упала на {creation.session_id}")
        bootstrap_session(creation.artifact_root)
        state = SessionState(
            schema=SCHEMA_V2,
            session_id=creation.session_id,
            created_at=datetime(2026, 8, 28, tzinfo=UTC),
            state=SessionPhase.IDLE,
            current_round=0,
            task=TaskSpec(prompt=creation.task_text, mode=Mode.DOCUMENT),
            agents={
                Role.AUTHOR: AgentRef(adapter="fake", model="m"),
                Role.REVIEWER: AgentRef(adapter="fake", model="m"),
            },
            limits=Limits(
                max_rounds=5,
                max_total_tokens=10_000,
                max_wall_seconds=600,
                schema_retries=2,
            ),
            budget_used=BudgetUsed(),
        )
        FileStateStore(creation.artifact_root).save(state)
        return state


class _FakeExporter:
    """`ExportFn`: считает вызовы и пишет пустой commit marker."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_next = False

    def __call__(self, state: PipelineState, **kwargs: Any) -> Path:
        self.calls.append({"pipeline_id": state.pipeline_id, **kwargs})
        if self.raise_next:
            raise _Boom("экспорт прерван до manifest.json")
        directory = kwargs["workspace_root"] / "export"
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / f"{state.pipeline_id}.json"
        marker.write_text("{}", encoding="utf-8")
        return marker


class _CrashingStore:
    """Обёртка `PipelineStateStore`, обрывающая процесс на N-й записи.

    Каждая запись манифеста — самостоятельная граница write-ahead (§4.3), и
    обрыв ровно на ней — единственный способ проверить, что интент
    допроигрывается, а не выполняется второй раз.
    """

    def __init__(self, inner: FilePipelineStateStore, crash_on_save: int) -> None:
        self._inner = inner
        self._crash_on_save = crash_on_save
        self.saves = 0

    def load(self, pipeline_id: str) -> PipelineState:
        return self._inner.load(pipeline_id)

    def save(self, state: PipelineState) -> None:
        self.saves += 1
        if self.saves == self._crash_on_save:
            raise _Boom(f"обрыв на записи манифеста №{self.saves}")
        self._inner.save(state)


# --------------------------------------------------------------------------
# Стенд
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Harness:
    """Собранный стенд: рабочий корень, порты и сам runner."""

    workspace: Path
    anchor_root: Path
    config: PipelineConfig
    git: _FakeGit
    driver: _FakeDriver
    factory: _FakeFactory
    exporter: _FakeExporter
    store: FilePipelineStateStore
    runner: PipelineRunner
    clock: list[datetime] = field(default_factory=list)

    def manifest(self) -> PipelineState:
        return self.store.load(SLUG)

    def anchor(self) -> IntegrityAnchor:
        return IntegrityAnchor(self.anchor_root, self.workspace, SLUG)


def _clock() -> Any:
    moment = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    counter = {"n": 0}

    def now() -> datetime:
        counter["n"] += 1
        return moment + timedelta(seconds=counter["n"])

    return now


def build_harness(
    tmp_path: Path,
    scripts: dict[str, Script],
    *,
    workspace_name: str = "repo",
    crash_on_save: int = 0,
    max_architectural_returns: int = 2,
    soft_max_pipeline_tokens: int = 0,
    soft_max_pipeline_wall_seconds: int = 0,
    plan_present: bool = False,
) -> Harness:
    """Стенд с настоящими хранилищем/журналом/анкером и фейковыми портами."""
    workspace = tmp_path / workspace_name
    (workspace / "docs").mkdir(parents=True, exist_ok=True)
    (workspace / SPEC_PATH).write_text("# спека\n", encoding="utf-8")
    if plan_present:
        (workspace / PLAN_PATH).write_text("# план\n", encoding="utf-8")

    anchor_root = tmp_path / "anchors"
    config = PipelineConfig(
        spec_path=Path(SPEC_PATH),
        plan_path=Path(PLAN_PATH),
        anchor_path=anchor_root,
        max_architectural_returns=max_architectural_returns,
        soft_max_pipeline_tokens=soft_max_pipeline_tokens,
        soft_max_pipeline_wall_seconds=soft_max_pipeline_wall_seconds,
    )
    real_store = FilePipelineStateStore(workspace)
    store: Any = (
        _CrashingStore(real_store, crash_on_save) if crash_on_save else real_store
    )
    git = _FakeGit()
    driver = _FakeDriver(scripts)
    factory = _FakeFactory()
    exporter = _FakeExporter()
    runner = PipelineRunner(
        store=store,
        sink=_LazySink(workspace),
        git=git,
        session_driver=driver,
        session_factory=factory,
        exporter=exporter,
        now=_clock(),
        config=config,
        workspace_root=workspace,
    )
    return Harness(
        workspace=workspace,
        anchor_root=anchor_root,
        config=config,
        git=git,
        driver=driver,
        factory=factory,
        exporter=exporter,
        store=real_store,
        runner=runner,
    )


class _LazySink:
    """Журнал пайплайна, открываемый при первом событии.

    `PipelineEventSink` пишет в `pipelines/<slug>/events.jsonl`, а каталога до
    `run` ещё нет: открывать журнал в конструкторе стенда значило бы требовать
    от него порядка, которого у настоящего CLI тоже не будет.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._sink: PipelineEventSink | None = None

    def emit(self, event: Any) -> None:
        if self._sink is None:
            self._sink = PipelineEventSink(self._workspace, SLUG)
        self._sink.emit(event)


def rebuild(harness: Harness, **kwargs: Any) -> Harness:
    """Новый runner поверх того же диска — второй процесс после краха."""
    fresh = PipelineRunner(
        store=harness.store,
        sink=_LazySink(harness.workspace),
        git=harness.git,
        session_driver=harness.driver,
        session_factory=harness.factory,
        exporter=harness.exporter,
        now=_clock(),
        config=harness.config,
        workspace_root=harness.workspace,
        **kwargs,
    )
    harness.runner = fresh
    return harness


# --------------------------------------------------------------------------
# Вспомогательные конструкторы артефактов
# --------------------------------------------------------------------------


def _write_json(path: Path, model: Any) -> None:
    path.write_text(
        json.dumps(model.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )


def _review(script: Script) -> Review:
    verdict = (
        Verdict.APPROVE if script.outcome == "converged" else Verdict.REQUEST_CHANGES
    )
    return Review(
        schema=SCHEMA_V2,
        round=1,
        role=Role.REVIEWER,
        verdict=verdict,
        confidence=0.9,
        issues=list(script.issues),
        checked=[SPEC_PATH, PLAN_PATH],
        summary=f"scripted {script.outcome}",
    )


def _issue(
    issue_id: str,
    defect_class: Literal["architectural", "execution"],
    severity: Severity,
) -> Issue:
    return Issue(
        id=issue_id,
        severity=severity,
        file=PLAN_PATH,
        claim=f"{issue_id}: находка",
        evidence="строки 1-2",
        defect_class=defect_class,
    )


ARCH = _issue("F-ARCH", "architectural", Severity.BLOCKER)
EXEC_MAJOR = _issue("F-EXEC-1", "execution", Severity.MAJOR)
EXEC_MINOR = _issue("F-EXEC-2", "execution", Severity.MINOR)


def converged_pair() -> dict[str, Script]:
    return {"spec-r1": Script(), "pair-r1": Script()}


def structure(state: PipelineState) -> dict[str, Any]:
    """Структурная часть манифеста — всё, кроме меток времени."""
    return {
        "phase": state.phase.value,
        "transitions": [
            (t.from_.value, t.to.value, t.reason.value) for t in state.transitions
        ],
        "spec": [
            (
                r.session_id,
                r.outcome.value if r.outcome else None,
                r.superseded_by,
            )
            for r in state.spec_sessions
        ],
        "pair": [
            (
                r.session_id,
                r.outcome.value if r.outcome else None,
                r.superseded_by,
            )
            for r in state.pair_sessions
        ],
        "next_action": None if state.next_action is None else state.next_action.kind,
    }


# --------------------------------------------------------------------------
# Анкер: долг задачи 13 (§3.1, §8.1 шаг 0)
# --------------------------------------------------------------------------


def test_run_creates_anchor_before_any_mutation(tmp_path: Path) -> None:
    """Крах на первом интенте оставляет пустой существующий анкер (§8.1 шаг 0)."""
    harness = build_harness(tmp_path, converged_pair())
    harness.factory.raise_next = True

    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    anchor = harness.anchor()
    assert anchor.path.is_file(), (
        "анкер обязан быть создан ДО первой сохраняемой мутации: §8.1 делает "
        "«файла анкера нет» безусловным отказом resume"
    )
    assert anchor.path.read_bytes() == b""
    # Ровно предикат §8.1 шага 0: журнал существует и пуст → сверять нечего,
    # отказа нет. FileNotFoundError здесь означал бы отказ resume.
    assert anchor.last_record() is None

    harness.factory.raise_next = False
    state = rebuild(harness).runner.advance(SLUG)
    assert state.phase is PipelinePhase.DONE


def test_run_refuses_existing_anchor(tmp_path: Path) -> None:
    """Существующий анкер отвергает старт — журнал не переиспользуется."""
    harness = build_harness(tmp_path, converged_pair())
    harness.runner.run(SLUG, "полировать пару")

    # Обычный повторный `run` останавливают предусловия — каталогом пайплайна.
    with pytest.raises(PipelineAlreadyExists):
        build_harness(tmp_path, converged_pair()).runner.run(SLUG, "ещё раз")

    # Каталог пайплайна убран, анкер остался: без проверки анкера повторный
    # `run` молча начал бы дописывать в чужой доверенный журнал.
    import shutil

    shutil.rmtree(pipeline_dir(harness.workspace, SLUG))
    fresh = build_harness(tmp_path, converged_pair())
    with pytest.raises(PipelineAlreadyExists):
        fresh.runner.run(SLUG, "полировать пару")


def test_same_slug_two_repos_no_collision(tmp_path: Path) -> None:
    """Тот же слаг в двух рабочих корнях — два независимых анкера (P9)."""
    first = build_harness(tmp_path, converged_pair(), workspace_name="repo-a")
    second = build_harness(tmp_path, converged_pair(), workspace_name="repo-b")
    first.runner.run(SLUG, "пара A")
    second.runner.run(SLUG, "пара B")

    assert first.anchor().path != second.anchor().path
    assert first.anchor().path.is_file()
    assert second.anchor().path.is_file()


# --------------------------------------------------------------------------
# Happy path (§4.2, §7.2)
# --------------------------------------------------------------------------


def test_happy_path_two_contours_to_done(tmp_path: Path) -> None:
    """spec сошёлся → pair сошёлся → EXPORTING → DONE; экспорт ровно один."""
    harness = build_harness(tmp_path, converged_pair())
    state = harness.runner.run(SLUG, "полировать пару")

    assert structure(state) == {
        "phase": "DONE",
        "transitions": [
            ("IDLE", "SPEC_LOOP", "started"),
            ("SPEC_LOOP", "PAIR_LOOP", "spec_converged"),
            ("PAIR_LOOP", "EXPORTING", "pair_converged"),
            ("EXPORTING", "DONE", "exported"),
        ],
        "spec": [("spec-r1", "converged", None)],
        "pair": [("pair-r1", "converged", None)],
        "next_action": None,
    }
    assert len(harness.exporter.calls) == 1
    assert harness.exporter.calls[0]["partial"] is False


def test_happy_path_snapshots_hashed_and_written(tmp_path: Path) -> None:
    """Снапшоты task/config/checklists лежат на диске, хеши сходятся (§4.2)."""
    harness = build_harness(tmp_path, converged_pair())
    state = harness.runner.run(SLUG, "полировать пару")

    directory = pipeline_dir(harness.workspace, SLUG)
    for ref in (state.task, state.config, state.checklists):
        path = directory / ref.path
        assert path.is_file(), f"снапшот {ref.path} не записан"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == ref.sha256, f"хеш снапшота {ref.path} разошёлся"
    assert (directory / state.task.path).read_text(
        encoding="utf-8"
    ) == "полировать пару"


def test_manifest_carries_no_integrity_snapshots(tmp_path: Path) -> None:
    """Снапшот целостности живёт только в анкере (§4.2, P9)."""
    harness = build_harness(tmp_path, converged_pair())
    harness.runner.run(SLUG, "полировать пару")
    payload = json.loads(
        (pipeline_dir(harness.workspace, SLUG) / "pipeline.json").read_text(
            encoding="utf-8"
        )
    )
    assert "immutable" not in json.dumps(payload)
    assert payload["anchor_id"] == SLUG


def test_entry_hashes_mark_absent_plan(tmp_path: Path) -> None:
    """Отсутствующий план в spec-r1 — явный маркер `absent` (§4.2)."""
    harness = build_harness(tmp_path, converged_pair())
    state = harness.runner.run(SLUG, "полировать пару")
    entry = state.spec_sessions[0].entry_hashes
    assert entry[PLAN_PATH] == "absent"
    assert entry[SPEC_PATH] != "absent"


def test_session_artifact_roots_are_separate(tmp_path: Path) -> None:
    """Каждая ревизия получает свой `artifact_root` (§4.1)."""
    harness = build_harness(tmp_path, converged_pair())
    harness.runner.run(SLUG, "полировать пару")
    roots = {call[1]: call[0] for call in harness.driver.calls}
    assert roots["spec-r1"] == session_artifact_root(harness.workspace, SLUG, "spec-r1")
    assert roots["pair-r1"] == session_artifact_root(harness.workspace, SLUG, "pair-r1")


def test_pair_contour_gets_boundary_policy_spec_does_not(tmp_path: Path) -> None:
    """Политика границы — только pair-контуру (§7.1)."""
    harness = build_harness(tmp_path, converged_pair())
    harness.runner.run(SLUG, "полировать пару")
    policies = {call[1]: call[2] for call in harness.driver.calls}
    assert policies["spec-r1"] is None
    assert isinstance(policies["pair-r1"], ArchitecturalDefectPolicy)


def test_events_carry_operation_id(tmp_path: Path) -> None:
    """Журнал пайплайна ведётся и дедуплицируем по `operation_id` (P8)."""
    harness = build_harness(tmp_path, converged_pair())
    harness.runner.run(SLUG, "полировать пару")
    events = read_pipeline_events(
        pipeline_dir(harness.workspace, SLUG) / "events.jsonl"
    )
    kinds = [event.type.value for event in events]
    assert "phase_change" in kinds
    assert "exported" in kinds
    assert all(event.payload.get("operation_id") for event in events)


# --------------------------------------------------------------------------
# Политика границы раунда (§7.1, P6)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("issues", "expected"),
    [
        ((), BoundaryVerdict.PROCEED),
        ((EXEC_MAJOR, EXEC_MINOR), BoundaryVerdict.PROCEED),
        ((ARCH,), BoundaryVerdict.PARK),
        ((EXEC_MAJOR, ARCH, EXEC_MINOR), BoundaryVerdict.PARK),
        (
            (_issue("F-ARCH-MINOR", "architectural", Severity.MINOR),),
            BoundaryVerdict.PROCEED,
        ),
    ],
)
def test_pair_policy_parks_on_architectural_blocker_or_major(
    issues: tuple[Issue, ...], expected: BoundaryVerdict
) -> None:
    """`PARK` ровно при blocker/major с `defect_class: architectural` (§7.1)."""
    policy = ArchitecturalDefectPolicy()
    assert policy.after_deciding(_review(Script(issues=issues))) is expected


# --------------------------------------------------------------------------
# Возврат по архитектурному дефекту (§7.3, P3, P5, P6)
# --------------------------------------------------------------------------


def returning_scripts() -> dict[str, Script]:
    """pair-r1 паркуется смешанным ревью, вторая пара сходится."""
    return {
        "spec-r1": Script(),
        "pair-r1": Script(outcome="park", issues=(EXEC_MAJOR, ARCH, EXEC_MINOR)),
        "spec-r2": Script(),
        "pair-r2": Script(),
    }


def test_architectural_return_walks_back_to_spec_loop(tmp_path: Path) -> None:
    """Смешанное ревью → приоритет P6: возврат в spec-контур, spec-r2 создан."""
    harness = build_harness(tmp_path, returning_scripts())
    state = harness.runner.run(SLUG, "полировать пару")

    assert structure(state) == {
        "phase": "DONE",
        "transitions": [
            ("IDLE", "SPEC_LOOP", "started"),
            ("SPEC_LOOP", "PAIR_LOOP", "spec_converged"),
            ("PAIR_LOOP", "SPEC_LOOP", "architectural_defect"),
            ("SPEC_LOOP", "PAIR_LOOP", "spec_converged"),
            ("PAIR_LOOP", "EXPORTING", "pair_converged"),
            ("EXPORTING", "DONE", "exported"),
        ],
        "spec": [
            ("spec-r1", "converged", "spec-r2"),
            ("spec-r2", "converged", None),
        ],
        "pair": [
            ("pair-r1", "architectural_defect", "spec-r2"),
            ("pair-r2", "converged", None),
        ],
        "next_action": None,
    }


def test_return_transition_carries_architectural_evidence(tmp_path: Path) -> None:
    """Evidence возврата — только архитектурные находки (§7.3 шаг 2)."""
    harness = build_harness(tmp_path, returning_scripts())
    state = harness.runner.run(SLUG, "полировать пару")
    ret = next(
        t
        for t in state.transitions
        if t.reason is TransitionReason.ARCHITECTURAL_DEFECT
    )
    assert [(e.session_id, e.round, e.finding_id) for e in ret.evidence] == [
        ("pair-r1", 1, "F-ARCH")
    ]


def test_return_resets_worktree_to_head(tmp_path: Path) -> None:
    """Cleanup worktree — reset к последнему принятому коммиту (§7.3 шаг 3)."""
    harness = build_harness(tmp_path, returning_scripts())
    harness.runner.run(SLUG, "полировать пару")
    assert harness.git.resets == [harness.git.head]


def test_spec_r2_gets_findings_pair_r2_starts_clean(tmp_path: Path) -> None:
    """spec-r2 получает находки как данные; pair-r2 — без унаследованного (P5)."""
    harness = build_harness(tmp_path, returning_scripts())
    harness.runner.run(SLUG, "полировать пару")
    creations = {c.session_id: c for c in harness.factory.creations}

    spec_r2 = creations["spec-r2"]
    assert [issue.id for issue in spec_r2.findings] == ["F-ARCH"]
    assert spec_r2.contour == "spec"

    pair_r2 = creations["pair-r2"]
    assert pair_r2.findings == ()
    assert pair_r2.revision == 2
    assert pair_r2.artifact_root == session_artifact_root(
        harness.workspace, SLUG, "pair-r2"
    )


def test_return_operation_id_is_deterministic_from_review(tmp_path: Path) -> None:
    """`operation_id` выводится из `{session_id, round, sha256(review.json)}`.

    Обрыв ровно перед записью результата `finish_session` (седьмая запись
    манифеста): интент возврата ещё не лёг, и replay обязан обнаружить тот же
    checkpoint заново — reconciliation §7.3 шага 1, а не чтение сохранённого
    `operation_id`.
    """
    harness = build_harness(tmp_path, returning_scripts(), crash_on_save=7)
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")
    assert harness.manifest().next_action is not None
    assert harness.manifest().next_action.kind == "finish_session"  # type: ignore[union-attr]

    rebuild(harness).runner.advance(SLUG)

    review_bytes = (
        session_artifact_root(harness.workspace, SLUG, "pair-r1")
        / ".disputatio"
        / "rounds"
        / "001"
        / "review.json"
    ).read_bytes()
    expected = hashlib.sha256(
        b"pair-r1\x001\x00" + hashlib.sha256(review_bytes).hexdigest().encode()
    ).hexdigest()
    decisions = [
        event.payload["operation_id"]
        for event in read_pipeline_events(
            pipeline_dir(harness.workspace, SLUG) / "events.jsonl"
        )
        if event.type.value == "return_recorded"
    ]
    assert decisions == [f"return-{expected[:32]}"]


# --------------------------------------------------------------------------
# Бюджет (§4.2)
# --------------------------------------------------------------------------


def test_budget_recomputed_no_double_count(tmp_path: Path) -> None:
    """Повторный `run_session` после краха не удваивает расход (§4.2).

    Обрыв на третьей записи манифеста оставляет на диске `next_action =
    run_session` сессии, которая УЖЕ отработала: `session.json` терминален и
    несёт свои 100 токенов, а манифест их ещё не видел. Replay исполняет тот
    же интент второй раз — и обязан прийти к 100, а не к 200.

    Гарантия структурная: `budget_used` не хранит собственного счётчика
    вовсе, поэтому последняя проверка сверяет манифест с суммой по
    `session.json` напрямую — ровно формулировкой §4.2.
    """
    scripts = {
        "spec-r1": Script(tokens=100, wall_seconds=5.0),
        "pair-r1": Script(tokens=40, wall_seconds=2.0),
    }
    harness = build_harness(tmp_path, scripts, crash_on_save=3)
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    crashed = harness.manifest()
    assert crashed.next_action is not None
    assert crashed.next_action.kind == "run_session"
    assert crashed.budget_used.tokens == 0, "запись сделана до прогона сессии"

    state = rebuild(harness).runner.advance(SLUG)

    assert state.budget_used.tokens == 140
    assert state.budget_used.wall_seconds == pytest.approx(7.0)
    assert [call[1] for call in harness.driver.calls] == ["spec-r1", "pair-r1"], (
        "durable-состояние спеки уже терминально — второй прогон запрещён"
    )
    assert state.budget_used.tokens == sum_session_budgets(harness, state)


def sum_session_budgets(harness: Harness, state: PipelineState) -> int:
    """Сумма токенов по `session.json` всех сессий манифеста — формула §4.2."""
    total = 0
    for record in (*state.spec_sessions, *state.pair_sessions):
        root = pipeline_dir(harness.workspace, SLUG) / record.path
        total += FileStateStore(root).load(record.session_id).budget_used.tokens
    return total


def test_parked_session_counts_toward_budget(tmp_path: Path) -> None:
    """Расход припаркованной сессии тоже потрачен (§4.2)."""
    scripts = returning_scripts()
    scripts["spec-r1"].tokens = 10
    scripts["pair-r1"].tokens = 33
    scripts["spec-r2"].tokens = 5
    scripts["pair-r2"].tokens = 2
    harness = build_harness(tmp_path, scripts)
    state = harness.runner.run(SLUG, "полировать пару")
    assert state.budget_used.tokens == 50


# --------------------------------------------------------------------------
# Эскалации и отказ (§7.2, P7)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("deadlock", "session_deadlock"), ("budget_hit", "session_budget_hit")],
)
def test_done_through_deadlock_escalates(
    tmp_path: Path, outcome: str, reason: str
) -> None:
    """DONE через DEADLOCK/BUDGET_HIT → ESCALATED, причина из `decision.json`."""
    harness = build_harness(tmp_path, {"spec-r1": Script(outcome=outcome)})
    state = harness.runner.run(SLUG, "полировать пару")

    assert structure(state)["transitions"] == [
        ("IDLE", "SPEC_LOOP", "started"),
        ("SPEC_LOOP", "ESCALATED", reason),
        ("ESCALATED", "EXPORTING", "export_partial"),
        ("EXPORTING", "DONE", "exported"),
    ]
    assert state.spec_sessions[0].outcome is SessionOutcome.ESCALATED
    assert harness.exporter.calls[0]["partial"] is True


def test_failed_session_fails_pipeline_without_export(tmp_path: Path) -> None:
    """`FAILED` — невосстановимая ошибка: экспорт не выполняется (P7)."""
    harness = build_harness(tmp_path, {"spec-r1": Script(outcome="failed")})
    state = harness.runner.run(SLUG, "полировать пару")

    assert state.phase is PipelinePhase.FAILED
    assert structure(state)["transitions"][-1] == (
        "SPEC_LOOP",
        "FAILED",
        "session_failed",
    )
    assert state.spec_sessions[0].outcome is SessionOutcome.FAILED
    assert harness.exporter.calls == []


def test_nonterminal_session_without_park_is_invariant_violation(
    tmp_path: Path,
) -> None:
    """Нетерминальная сессия, которую политика не паркует, → `invariant_violation`.

    Для spec-контура политики нет вовсе (§7.1), поэтому раунд без решения тут
    парковкой быть не может: это нарушенное обещание драйвера «крутит до
    терминала», и оно обязано читаться иначе, чем честно упавшая сессия, —
    §2 держит для этого отдельную причину.
    """
    harness = build_harness(tmp_path, {"spec-r1": Script(outcome="park")})
    state = harness.runner.run(SLUG, "полировать пару")

    assert state.phase is PipelinePhase.FAILED
    assert structure(state)["transitions"][-1] == (
        "SPEC_LOOP",
        "FAILED",
        "invariant_violation",
    )
    assert harness.exporter.calls == []


def test_max_architectural_returns_escalates(tmp_path: Path) -> None:
    """Превышение `max_architectural_returns` → ESCALATED (§7.2)."""
    scripts = {
        "spec-r1": Script(),
        "pair-r1": Script(outcome="park", issues=(ARCH,)),
        "spec-r2": Script(),
        "pair-r2": Script(outcome="park", issues=(ARCH,)),
    }
    harness = build_harness(tmp_path, scripts, max_architectural_returns=1)
    state = harness.runner.run(SLUG, "полировать пару")

    assert structure(state)["transitions"] == [
        ("IDLE", "SPEC_LOOP", "started"),
        ("SPEC_LOOP", "PAIR_LOOP", "spec_converged"),
        ("PAIR_LOOP", "SPEC_LOOP", "architectural_defect"),
        ("SPEC_LOOP", "PAIR_LOOP", "spec_converged"),
        ("PAIR_LOOP", "ESCALATED", "max_architectural_returns"),
        ("ESCALATED", "EXPORTING", "export_partial"),
        ("EXPORTING", "DONE", "exported"),
    ]
    assert state.pair_sessions[1].outcome is SessionOutcome.ARCHITECTURAL_DEFECT
    assert harness.exporter.calls[0]["partial"] is True


def test_soft_budget_limit_checked_between_sessions(tmp_path: Path) -> None:
    """Soft-лимит проверяется между сессиями — pair-r1 не создаётся (§7.2)."""
    scripts = {"spec-r1": Script(tokens=500), "pair-r1": Script()}
    harness = build_harness(tmp_path, scripts, soft_max_pipeline_tokens=100)
    state = harness.runner.run(SLUG, "полировать пару")

    assert structure(state)["transitions"] == [
        ("IDLE", "SPEC_LOOP", "started"),
        ("SPEC_LOOP", "ESCALATED", "pipeline_budget_hit"),
        ("ESCALATED", "EXPORTING", "export_partial"),
        ("EXPORTING", "DONE", "exported"),
    ]
    assert state.pair_sessions == []
    assert [c.session_id for c in harness.factory.creations] == ["spec-r1"]


def test_architectural_defect_beats_pipeline_budget_limit(tmp_path: Path) -> None:
    """P6: находка соседствует с исчерпанным бюджетом — возврат всё равно побеждает.

    Soft-лимит пайплайна на момент парковки уже превышен (spec-r1 и pair-r1
    вместе выбрали 300 при лимите 250), и «между сессиями» он увёл бы пайплайн
    в эскалацию. Но P6 объявляет архитектурную находку приоритетнее стоп-условий:
    спека, признанная дефектной, обесценивает и исчерпанный бюджет — продолжать
    дебатировать план по неверной спеке незачем. Поэтому проверка soft-лимита
    стоит только в ветке сходимости, а у возврата свой потолок
    (`max_architectural_returns`), и он здесь не достигнут.

    Эскалация по бюджету всё равно случается — но на следующей границе между
    сессиями, уже ПОСЛЕ того, как возврат записан: лимит не потерян, он
    отложен ровно на один возврат.
    """
    scripts = {
        "spec-r1": Script(tokens=200),
        "pair-r1": Script(outcome="park", issues=(EXEC_MAJOR, ARCH), tokens=100),
        "spec-r2": Script(tokens=10),
        "pair-r2": Script(),
    }
    harness = build_harness(tmp_path, scripts, soft_max_pipeline_tokens=250)
    state = harness.runner.run(SLUG, "полировать пару")

    assert structure(state)["transitions"] == [
        ("IDLE", "SPEC_LOOP", "started"),
        ("SPEC_LOOP", "PAIR_LOOP", "spec_converged"),
        ("PAIR_LOOP", "SPEC_LOOP", "architectural_defect"),
        ("SPEC_LOOP", "ESCALATED", "pipeline_budget_hit"),
        ("ESCALATED", "EXPORTING", "export_partial"),
        ("EXPORTING", "DONE", "exported"),
    ]
    assert state.pair_sessions[0].outcome is SessionOutcome.ARCHITECTURAL_DEFECT
    assert [c.session_id for c in harness.factory.creations] == [
        "spec-r1",
        "pair-r1",
        "spec-r2",
    ], "возврат исполнен несмотря на превышенный бюджет"


def test_soft_wall_limit_checked_between_sessions(tmp_path: Path) -> None:
    """Второй soft-лимит — время стены — работает тем же путём (§7.2)."""
    scripts = {"spec-r1": Script(wall_seconds=99.0), "pair-r1": Script()}
    harness = build_harness(tmp_path, scripts, soft_max_pipeline_wall_seconds=10)
    state = harness.runner.run(SLUG, "полировать пару")
    assert state.phase is PipelinePhase.DONE
    assert state.pair_sessions == []


def test_failed_to_failed_is_idempotent(tmp_path: Path) -> None:
    """Повторный перевод `FAILED → FAILED` не даёт дубликата transition (P8)."""
    harness = build_harness(tmp_path, {"spec-r1": Script(outcome="failed")})
    first = harness.runner.run(SLUG, "полировать пару")
    again = harness.runner.fail(SLUG, reason=TransitionReason.SESSION_FAILED)

    assert again.phase is PipelinePhase.FAILED
    assert len(again.transitions) == len(first.transitions)
    assert structure(again) == structure(first)


def test_fail_refuses_to_rewrite_done(tmp_path: Path) -> None:
    """Из `DONE` рёбер нет вовсе — ретроактивный `FAILED` отвергается (§2)."""
    harness = build_harness(tmp_path, converged_pair())
    harness.runner.run(SLUG, "полировать пару")
    with pytest.raises(ValueError, match="DONE"):
        harness.runner.fail(SLUG, reason=TransitionReason.INVARIANT_VIOLATION)


# --------------------------------------------------------------------------
# Crash-минимум §10: одиннадцать границ
# --------------------------------------------------------------------------


def test_crash_1_intent_recorded_directory_missing(tmp_path: Path) -> None:
    """(1) intent `create_session` записан, каталог не создан.

    Фабрика падает до записи `session.json`, каталог ревизии затем убран —
    ровно состояние «намерение персистировано, действие не начиналось».
    """
    import shutil

    harness = build_harness(tmp_path, converged_pair())
    harness.factory.raise_next = True
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")
    shutil.rmtree(session_artifact_root(harness.workspace, SLUG, "spec-r1"))

    state = harness.manifest()
    assert state.next_action is not None
    assert state.next_action.kind == "create_session"
    assert state.spec_sessions == []

    harness.factory.raise_next = False
    final = rebuild(harness).runner.advance(SLUG)
    assert final.phase is PipelinePhase.DONE
    assert [r.session_id for r in final.spec_sessions] == ["spec-r1"]


def test_crash_2_directory_created_session_not_recorded(tmp_path: Path) -> None:
    """(2) каталог создан, `session_started` не записан — без дубликата."""
    harness = build_harness(tmp_path, converged_pair(), crash_on_save=2)
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")
    assert harness.factory.creations, "фабрика обязана была отработать до записи"

    final = rebuild(harness).runner.advance(SLUG)
    assert [r.session_id for r in final.spec_sessions] == ["spec-r1"]
    spec_creations = [c for c in harness.factory.creations if c.session_id == "spec-r1"]
    assert len(spec_creations) == 1, (
        "повторный create_session обязан увидеть durable session.json и не "
        "создавать сессию заново"
    )


def test_crash_3_driver_dies_before_result(tmp_path: Path) -> None:
    """(3) `run_session` записан, драйвер упал до записи результата."""
    scripts = converged_pair()
    scripts["spec-r1"].raise_before_write = True
    harness = build_harness(tmp_path, scripts)
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    state = harness.manifest()
    assert state.next_action is not None and state.next_action.kind == "run_session"

    scripts["spec-r1"].raise_before_write = False
    final = rebuild(harness).runner.advance(SLUG)
    assert final.phase is PipelinePhase.DONE
    assert [call[1] for call in harness.driver.calls] == [
        "spec-r1",
        "spec-r1",
        "pair-r1",
    ]


def test_crash_4_return_intent_recorded_reset_not_done(tmp_path: Path) -> None:
    """(4) `record_return`: intent записан, reset не выполнен."""
    harness = build_harness(tmp_path, returning_scripts())
    harness.git.raise_on_reset = True
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    state = harness.manifest()
    assert state.next_action is not None
    assert state.next_action.kind == "record_return"
    assert harness.git.resets == []
    assert state.pair_sessions[0].outcome is None

    harness.git.raise_on_reset = False
    final = rebuild(harness).runner.advance(SLUG)
    assert final.phase is PipelinePhase.DONE
    assert harness.git.resets == [harness.git.head]


def test_crash_5_reset_done_commit_point_missing(tmp_path: Path) -> None:
    """(5) reset выполнен, commit point не записан — идемпотентный повтор."""
    harness = build_harness(tmp_path, returning_scripts(), crash_on_save=8)
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    state = harness.manifest()
    assert state.next_action is not None
    assert state.next_action.kind == "record_return"
    assert harness.git.resets == [harness.git.head]
    assert state.pair_sessions[0].outcome is None

    final = rebuild(harness).runner.advance(SLUG)
    assert harness.git.resets == [harness.git.head, harness.git.head]
    assert final.pair_sessions[0].outcome is SessionOutcome.ARCHITECTURAL_DEFECT
    assert (
        sum(
            1
            for t in final.transitions
            if t.reason is TransitionReason.ARCHITECTURAL_DEFECT
        )
        == 1
    ), "возврат обязан быть записан ровно один раз"


def test_crash_6_commit_point_recorded_successor_pending(tmp_path: Path) -> None:
    """(6) commit point записан, chained `create_session` не исполнен."""
    harness = build_harness(tmp_path, returning_scripts(), crash_on_save=9)
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    state = harness.manifest()
    assert state.phase is PipelinePhase.SPEC_LOOP
    assert state.next_action is not None
    assert state.next_action.kind == "create_session"
    assert state.next_action.predecessor_operation_id is not None
    assert state.pair_sessions[0].outcome is SessionOutcome.ARCHITECTURAL_DEFECT
    resets_before = len(harness.git.resets)

    final = rebuild(harness).runner.advance(SLUG)
    assert len(harness.git.resets) == resets_before, (
        "преемник допроигрывается, предшественник не повторяется"
    )
    assert [r.session_id for r in final.spec_sessions] == ["spec-r1", "spec-r2"]


def test_crash_7_export_interrupted_before_manifest(tmp_path: Path) -> None:
    """(7) `export` записан, экспорт прерван до `manifest.json`."""
    harness = build_harness(tmp_path, converged_pair())
    harness.exporter.raise_next = True
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    state = harness.manifest()
    assert state.phase is PipelinePhase.EXPORTING
    assert state.next_action is not None and state.next_action.kind == "export"

    harness.exporter.raise_next = False
    final = rebuild(harness).runner.advance(SLUG)
    assert final.phase is PipelinePhase.DONE
    assert len(harness.exporter.calls) == 2, "повтор экспорта идемпотентен (§8.2)"
    assert (
        sum(1 for t in final.transitions if t.reason is TransitionReason.EXPORTED) == 1
    )


def test_crash_8_failed_replay_adds_no_duplicate(tmp_path: Path) -> None:
    """(8) `FAILED → FAILED` идемпотентен и на повторном `advance` (P8)."""
    harness = build_harness(tmp_path, {"spec-r1": Script(outcome="failed")})
    first = harness.runner.run(SLUG, "полировать пару")
    replayed = rebuild(harness).runner.advance(SLUG)
    assert structure(replayed) == structure(first)


def test_crash_9_snapshots_written_entry_hashes_missing(tmp_path: Path) -> None:
    """(9) снапшоты записаны, `entry_hashes` ещё нет — те же байты при повторе."""
    harness = build_harness(tmp_path, converged_pair(), crash_on_save=2)
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    directory = pipeline_dir(harness.workspace, SLUG)
    before = {
        name: (directory / name).read_bytes()
        for name in ("task.md", "config.toml", "checklists.toml")
    }
    assert harness.manifest().spec_sessions == []

    rebuild(harness).runner.advance(SLUG)
    after = {name: (directory / name).read_bytes() for name in before}
    assert after == before, "повтор обязан дать те же байты снапшотов"


def test_crash_10_driver_returned_result_not_recorded(tmp_path: Path) -> None:
    """(10) драйвер вернулся, результат не записан — сессия не гонится заново."""
    scripts = converged_pair()
    scripts["spec-r1"].raise_after_write = True
    harness = build_harness(tmp_path, scripts)
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    scripts["spec-r1"].raise_after_write = False
    final = rebuild(harness).runner.advance(SLUG)
    assert final.phase is PipelinePhase.DONE
    assert [call[1] for call in harness.driver.calls] == ["spec-r1", "pair-r1"], (
        "durable-состояние сессии уже терминально — повтор драйвера запрещён"
    )


def test_crash_11_finish_session_replays_same_outcome(tmp_path: Path) -> None:
    """(11) интерпретация выполнена, outcome не записан — тот же outcome."""
    harness = build_harness(tmp_path, converged_pair(), crash_on_save=4)
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    state = harness.manifest()
    assert state.next_action is not None
    assert state.next_action.kind == "finish_session"
    assert state.spec_sessions[0].outcome is None

    final = rebuild(harness).runner.advance(SLUG)
    assert final.spec_sessions[0].outcome is SessionOutcome.CONVERGED
    assert final.phase is PipelinePhase.DONE


@pytest.mark.parametrize("crash_on_save", list(range(2, 16)))
def test_every_manifest_write_is_a_replayable_boundary(
    tmp_path: Path, crash_on_save: int
) -> None:
    """Обрыв на ЛЮБОЙ записи манифеста допроигрывается до того же результата.

    Именно «на каждой границе внутри многошаговых kind'ов», а не «по одной на
    kind»: границы нумеруются записями манифеста, а у возврата их четыре —
    результат `finish_session` с интентом, commit point, преемник, результат
    его прогона. Прогон с возвратом даёт пятнадцать записей; первая (создание
    манифеста в `run`) в выборку не входит — до неё манифеста ещё нет вовсе,
    и §8.1 в таком состоянии не resume'ит, а отказывает.
    """
    reference = build_harness(tmp_path / "ref", returning_scripts())
    expected = structure(reference.runner.run(SLUG, "полировать пару"))

    harness = build_harness(
        tmp_path / "case", returning_scripts(), crash_on_save=crash_on_save
    )
    with pytest.raises(_Boom):
        harness.runner.run(SLUG, "полировать пару")

    final = rebuild(harness).runner.advance(SLUG)
    assert structure(final) == expected
    assert len(harness.exporter.calls) <= 2
