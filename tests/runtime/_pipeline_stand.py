"""Общий стенд пайплайновых наборов resume и adoption (SPEC-002 §3.1, §8.1).

Оба набора утверждают про НАСТОЯЩИЙ git и настоящий диск, и это не роскошь:

* «сверка worktree не трогает индекс» проверяется только тем, что
  `git status --porcelain` пользователя до и после вызова совпадает — фейк
  порта такого утверждения не несёт вовсе;
* идемпотентность операторского чекпоинта держится на `commit_paths` +
  `find_commit_by_trailer`, то есть на реальном трейлере в реальной истории;
* «принятая правка переживает первый `PROPOSING` новой ревизии» — это
  утверждение про `git reset --hard base_rev` в настоящем репозитории.

Поэтому подменены ровно три порта — драйвер сессии, фабрика сессии и
экспортёр, — а git, хранилище манифеста, журнал событий и анкер настоящие.

Состояние пайплайна собирается не руками, а штатным `PipelineRunner.run`:
манифест, каталоги ревизий и артефакты раундов ложатся ровно так, как их
кладёт продакшен, и тест не вправе разойтись с ним в раскладке. Обрыв
посреди прогона моделируется `Script.raise_after_write` — драйвер, упавший
после записи артефактов раунда, оставляет ровно то durable-состояние, с
которого начинается reconciliation §7.3.

Модуль назван с подчёркивания по той же причине, что и `_fakes.py`: pytest
собирает `test_*.py`, а стенд набором не является.
"""

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from disputatio.contracts import (
    SCHEMA_V2,
    AgentRef,
    BudgetUsed,
    Decision,
    Issue,
    Limits,
    Mode,
    Outcome,
    PipelineKind,
    PipelineState,
    Review,
    Role,
    SessionPhase,
    SessionState,
    Severity,
    TaskSpec,
    Verdict,
)
from disputatio.events import (
    FilePipelineStateStore,
    FileStateStore,
    IntegrityAnchor,
    PipelineEvent,
    PipelineEventSink,
    bootstrap_session,
    write_config_snapshot,
)
from disputatio.runtime import (
    AgentConfig,
    GitCli,
    LimitsConfig,
    PipelineConfig,
    RuntimeConfig,
)
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    DECISION_NAME,
    REVIEW_NAME,
    round_dir,
)
from disputatio.runtime.pipeline_adopt import OperatorIntents
from disputatio.runtime.pipeline_config import DEFAULT_MAX_ARCHITECTURAL_RETURNS
from disputatio.runtime.pipeline_resume import PipelineResume
from disputatio.runtime.pipeline_runner import PipelineRunner, SessionCreation

SLUG: Final = "pair-docs"
SPEC_PATH: Final = "docs/spec.md"
PLAN_PATH: Final = "docs/plan.md"
TASK_TEXT: Final = "ЗАДАЧА: отполировать пару «спека + план»"
WORK_BRANCH: Final = "docs/pair"

#: Имя каталога рабочего репозитория внутри `tmp_path`. Репозиторий обязан
#: быть ВЛОЖЕННЫМ, а не самим `tmp_path`: анкер P9 живёт вне рабочего дерева,
#: и `tmp_path/anchors` рядом с `tmp_path/repo` — единственная раскладка, где
#: это верно без записи за пределы каталога теста.
REPO_DIR_NAME: Final = "repo"


class Boom(RuntimeError):
    """Крах, инжектированный тестом: обрыв процесса в конкретной точке."""


def git(workdir: Path, *args: str) -> str:
    """Вспомогательный git теста; ненулевой код — `RuntimeError` со stderr."""
    completed = subprocess.run(
        ["git", *args],
        cwd=workdir,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} упал с кодом {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )
    return completed.stdout


def porcelain(workdir: Path) -> str:
    """`git status --porcelain -uall` глазами пользователя, а не порта."""
    return git(workdir, "status", "--porcelain", "--untracked-files=all")


@dataclass(slots=True)
class Script:
    """Что фейковый драйвер материализует на диске для одной ревизии.

    `park` — раунд с `review.json` и без `decision.json`: durable-состояние,
    по которому §7.1 опознаёт парковку. `raise_after_write` обрывает процесс
    сразу после записи артефактов — состояние, с которого §7.3 начинает
    reconciliation.
    """

    outcome: str = "converged"
    issues: tuple[Issue, ...] = ()
    raise_after_write: bool = False
    patch: str | None = None
    tokens: int = 0


class ScriptedDriver:
    """`SessionDriver`: пишет артефакты раунда по скрипту и двигает `session.json`."""

    def __init__(self, scripts: dict[str, Script]) -> None:
        self.scripts = scripts
        self.calls: list[tuple[Path, str, object]] = []

    def __call__(
        self, artifact_root: Path, session_id: str, policy: object
    ) -> SessionState:
        self.calls.append((artifact_root, session_id, policy))
        script = self.scripts[session_id]
        store = FileStateStore(artifact_root)
        state = store.load(session_id)

        directory = round_dir(artifact_root, 1)
        directory.mkdir(parents=True, exist_ok=True)
        _write_json(directory / REVIEW_NAME, _review(script))
        if script.patch is not None:
            (directory / CHANGES_PATCH_NAME).write_text(script.patch, encoding="utf-8")

        if script.outcome == "park":
            phase = SessionPhase.DECIDING
        else:
            _write_json(
                directory / DECISION_NAME,
                Decision(
                    round=1,
                    outcome={
                        "converged": Outcome.CONVERGED,
                        "deadlock": Outcome.DEADLOCK,
                        "failed": Outcome.FAILED,
                    }[script.outcome],
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
                "budget_used": BudgetUsed(tokens=script.tokens),
            }
        )
        store.save(state)
        if script.raise_after_write:
            # Процесс умирает ОДИН раз: следующий вызов — уже второй процесс,
            # доигрывающий тот же раунд, как это сделал бы session-resume.
            script.raise_after_write = False
            raise Boom(f"процесс убит сразу после артефактов раунда {session_id}")
        return state


class ScriptedFactory:
    """`SessionFactory`: bootstrap ревизии, снапшот конфига, `session.json`.

    Снапшот конфига обязателен, а не декоративен: `base_commit` из него —
    половина вычисления ожидаемого `HEAD` (§8.1, `base_rev`), и фабрика без
    снапшота дала бы стенду сверку, которой в продакшене есть на чём стоять,
    а в тесте не на чем. `base_commit` берётся из `SessionCreation`, если его
    назвал adoption, иначе — текущий `HEAD`, как и у настоящей фабрики.
    """

    def __init__(self, workspace: Path) -> None:
        self.creations: list[SessionCreation] = []
        self._workspace = workspace

    def __call__(self, creation: SessionCreation) -> SessionState:
        self.creations.append(creation)
        bootstrap_session(creation.artifact_root)
        base_commit = creation.base_commit or GitCli(self._workspace).head_sha()
        write_config_snapshot(
            creation.artifact_root,
            _session_config(creation, base_commit).render_toml(),
        )
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


class NullExporter:
    """`ExportFn`: считает вызовы и ничего не публикует."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, state: PipelineState, **kwargs: Any) -> Path:
        self.calls.append(state.pipeline_id)
        directory = kwargs["workspace_root"] / ".disputatio" / "export"
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / f"{state.pipeline_id}.json"
        marker.write_text("{}", encoding="utf-8")
        return marker


class LazySink:
    """Журнал пайплайна, открываемый при первом событии."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._sink: PipelineEventSink | None = None
        self.events: list[PipelineEvent] = []

    def emit(self, event: PipelineEvent) -> None:
        """Пишет событие в `pipelines/<slug>/events.jsonl` и запоминает его."""
        self.events.append(event)
        if self._sink is None:
            self._sink = PipelineEventSink(self._workspace, SLUG)
        self._sink.emit(event)


@dataclass(slots=True)
class Stand:
    """Собранный стенд: настоящий репозиторий, фейковые драйвер/фабрика/экспортёр."""

    workspace: Path
    anchor_root: Path
    config: PipelineConfig
    git: GitCli
    driver: ScriptedDriver
    factory: ScriptedFactory
    exporter: NullExporter
    sink: LazySink
    store: FilePipelineStateStore
    runner: PipelineRunner
    resume: PipelineResume
    scripts: dict[str, Script] = field(default_factory=dict)

    def manifest(self) -> PipelineState:
        """Манифест, как он лежит на диске."""
        return self.store.load(SLUG)

    def anchor(self) -> IntegrityAnchor:
        """Журнал целостности этого пайплайна."""
        return IntegrityAnchor(self.anchor_root, self.workspace, SLUG)

    def pipeline_dir(self) -> Path:
        """`.disputatio/pipelines/<slug>` рабочего дерева."""
        return self.workspace / ".disputatio" / "pipelines" / SLUG

    def artifact_root(self, session_id: str) -> Path:
        """`artifact_root` одной ревизии."""
        return self.pipeline_dir() / "sessions" / session_id

    def rebuild(self) -> "Stand":
        """Второй процесс поверх того же диска: новые runner и resume."""
        self.runner = _runner(self)
        self.resume = _resume(self)
        return self


def build_stand(
    tmp_path: Path,
    scripts: dict[str, Script],
    *,
    plan_present: bool = True,
    max_architectural_returns: int = DEFAULT_MAX_ARCHITECTURAL_RETURNS,
) -> Stand:
    """Репозиторий с парой документов на рабочей ветке + собранный пайплайн."""
    workspace = tmp_path / REPO_DIR_NAME
    (workspace / "docs").mkdir(parents=True)
    git(workspace, "init", "--quiet", "-b", "master")
    git(workspace, "config", "user.name", "disputatio-tests")
    git(workspace, "config", "user.email", "tests@disputatio.local")
    (workspace / SPEC_PATH).write_text(
        "# спека\n\nисходная редакция\n", encoding="utf-8"
    )
    tracked = [SPEC_PATH]
    if plan_present:
        (workspace / PLAN_PATH).write_text(
            "# план\n\nисходная редакция\n", encoding="utf-8"
        )
        tracked.append(PLAN_PATH)
    git(workspace, "add", *tracked)
    git(workspace, "commit", "--quiet", "-m", "исходная пара")
    git(workspace, "switch", "--quiet", "-c", WORK_BRANCH)

    stand = Stand(
        workspace=workspace,
        anchor_root=tmp_path / "anchors",
        config=PipelineConfig(
            kind=PipelineKind.PAIR,
            spec_path=Path(SPEC_PATH),
            plan_path=Path(PLAN_PATH),
            anchor_path=tmp_path / "anchors",
            max_architectural_returns=max_architectural_returns,
        ),
        git=GitCli(workspace),
        driver=ScriptedDriver(scripts),
        factory=ScriptedFactory(workspace),
        exporter=NullExporter(),
        sink=LazySink(workspace),
        store=FilePipelineStateStore(workspace),
        runner=None,  # type: ignore[arg-type]
        resume=None,  # type: ignore[arg-type]
        scripts=scripts,
    )
    stand.runner = _runner(stand)
    stand.resume = _resume(stand)
    return stand


def start(stand: Stand) -> None:
    """Заводит пайплайн штатным `run`; инжектированный крах — не провал теста."""
    try:
        stand.runner.run(SLUG, TASK_TEXT)
    except Boom:
        pass


def _runner(stand: Stand) -> PipelineRunner:
    """Runner поверх стенда — тот же, что собрал бы composition root."""
    return PipelineRunner(
        store=stand.store,
        sink=stand.sink,
        git=stand.git,
        session_driver=stand.driver,
        session_factory=stand.factory,
        exporter=stand.exporter,
        now=_clock(),
        config=stand.config,
        workspace_root=stand.workspace,
    )


def _resume(stand: Stand) -> PipelineResume:
    """Операторский resume поверх того же стенда."""
    return PipelineResume(
        runner=stand.runner,
        store=stand.store,
        git=stand.git,
        config=stand.config,
        workspace_root=stand.workspace,
        intents=OperatorIntents(
            store=stand.store,
            sink=stand.sink,
            git=stand.git,
            config=stand.config,
            workspace_root=stand.workspace,
            now=_clock(),
        ),
    )


def _clock() -> Any:
    """Детерминированные часы: каждый вызов на секунду позже предыдущего."""
    moment = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    counter = {"n": 0}

    def now() -> datetime:
        counter["n"] += 1
        return moment + timedelta(seconds=counter["n"])

    return now


def _session_config(creation: SessionCreation, base_commit: str) -> RuntimeConfig:
    """Снапшот конфига ревизии — тот же формат, что читает `resume_session`."""
    return RuntimeConfig(
        session_id=creation.session_id,
        mode=Mode.DOCUMENT,
        base_commit=base_commit,
        task_prompt=creation.task_text,
        author=AgentConfig(adapter="fake", model="m"),
        reviewer=AgentConfig(adapter="fake", model="m"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=10_000,
            max_wall_seconds=600,
            schema_retries=2,
        ),
        gates=(),
        attachments=(),
    )


def _write_json(path: Path, model: Any) -> None:
    """Артефакт раунда на диск — байтами, а не через писателя сессии."""
    path.write_text(
        json.dumps(model.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )


def _review(script: Script) -> Review:
    """Ревью раунда по скрипту: approve у сошедшейся ревизии, иначе замечания."""
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


def issue(
    issue_id: str,
    defect_class: Literal["architectural", "execution"],
    severity: Severity = Severity.BLOCKER,
) -> Issue:
    """Находка ревью нужного класса — вход политики границы раунда."""
    return Issue(
        id=issue_id,
        severity=severity,
        file=PLAN_PATH,
        claim=f"{issue_id}: находка",
        evidence="строки 1-2",
        defect_class=defect_class,
    )


ARCHITECTURAL: Final = issue("F-ARCH", "architectural")
EXECUTION: Final = issue("F-EXEC", "execution", Severity.MAJOR)


def parked_pair() -> dict[str, Script]:
    """Спека сошлась, pair-раунд припаркован архитектурной находкой."""
    return {
        "spec-r1": Script(),
        "pair-r1": Script(
            outcome="park", issues=(ARCHITECTURAL,), raise_after_write=True
        ),
    }


def live_pair(*, patch: str | None = None) -> dict[str, Script]:
    """Спека сошлась, pair-сессия оборвана посреди хода без архитектурных находок."""
    return {
        "spec-r1": Script(),
        "pair-r1": Script(
            outcome="park",
            issues=(EXECUTION,),
            raise_after_write=True,
            patch=patch,
        ),
    }
