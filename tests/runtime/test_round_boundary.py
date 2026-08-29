"""Граница раунда: `RoundBoundaryPolicy` в `drive()` (SPEC-002 §7.1, P4/P6).

Три утверждения, и первое из них — про отсутствие изменений.

* **Без политики `drive` работает как сегодня.** Эталон записан здесь
  списком: последовательность фаз из `events.jsonl` и содержимое `rounds/`
  и `result/` на диске. Регресс-гарантия дефолта `None` — не «тесты
  зелёные», а именно этот список: расширение цикла, сдвинувшее хоть один
  переход, обязано упасть тут, а не в чужом наборе.
* **`PARK` возвращает управление ДО следующего шага.** Не «после того, как
  цикл всё-таки сходил в `decide()`»: у припаркованного раунда
  `decision.json` не существует вовсе, и именно на этом факте §8.1 строит
  identity checkpoint'а. Автор следующего раунда не зовётся.
* **`PARK` сильнее стоп-условий §5.** Точка опроса выбрана до `decide()`
  ровно ради этого: `decide()` идёт строго top-down, и на последнем
  разрешённом раунде (`max_rounds`) или при исчерпанном бюджете он вернул
  бы `DEADLOCK`/`BUDGET_HIT` раньше ветки `CONTINUE`. Опроси политику после
  решения — и архитектурная находка ушла бы в эскалацию вместо
  обязательного возврата к спеке (P6). Оба стоп-условия проверяются
  параметрами одного теста: они возвращаются из разных веток `decide()`, и
  пройденный `max_rounds` ничего не говорит про бюджет.
* **Прокладка `resume_session` везёт политику до цикла.** Отдельным
  утверждением, потому что теряется она отдельно: тесты, зовущие `drive`
  напрямую, потерю kwarg'а в `resume_session` не видят вовсе. А поднимает
  припаркованные сессии runner пайплайна именно этим вызовом.

Подменены ровно три порта — автор, ревьюер и верификатор: первые два
разговаривают с сетью, третий запускает чужие процессы. Git настоящий во
временном репозитории: цель сброса раунда 2 вычисляется по истории
(`base_rev`), и фейковый `GitOps` её не подделал бы. Журнал и состояние тоже
настоящие (`JsonlEventSink`, `FileStateStore`) — оба утверждения про `PARK`
это утверждения о том, что легло на диск.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any

import anyio
import pytest

from disputatio.contracts import (
    SCHEMA_V2,
    AgentRef,
    AgentTurn,
    BoundaryVerdict,
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
from disputatio.events import (
    FileStateStore,
    JsonlEventSink,
    bootstrap_session,
    write_config_snapshot,
)
from disputatio.events.paths import events_jsonl_path, result_dir, session_dir
from disputatio.runtime import (
    AgentConfig,
    GitCli,
    LimitsConfig,
    RuntimeConfig,
    RuntimeDeps,
)
from disputatio.runtime.layout import DECISION_NAME, round_artifact, round_dir
from disputatio.runtime.loop import drive, resume_session
from disputatio.runtime.steps import StepContext
from disputatio.verifier import GateSpec

_SESSION_ID = "20260828-120000-bnd0"
_CREATED_AT = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
_CLOCK_STEP = timedelta(seconds=1)
_MONOTONIC_STEP = 0.25

_GATE = GateSpec(name="pytest", cmd="uv run pytest -q", enabled=True)

# Имя фейкового адаптера в реестре композиции — нужно только
# `resume_session`, который собирает порты сам, из снапшота конфига.
_ADAPTER_NAME = "boundary_fake"

# Эталон дефолтного прогона: две итерации раунда и терминальная цепочка §5.
# Список сверяется целиком, а не по вхождению: пропущенный переход и лишний
# переход — одинаково поломка цикла.
_BASELINE_PHASES = [
    ("IDLE", "PROPOSING"),
    ("PROPOSING", "VERIFYING"),
    ("VERIFYING", "REVIEWING"),
    ("REVIEWING", "DECIDING"),
    ("DECIDING", "PROPOSING"),
    ("PROPOSING", "VERIFYING"),
    ("VERIFYING", "REVIEWING"),
    ("REVIEWING", "DECIDING"),
    ("DECIDING", "CONVERGED"),
    ("CONVERGED", "EXPORTING"),
    ("EXPORTING", "DONE"),
]

_BASELINE_ROUND_FILES = [
    ".finalized",
    "changes.patch",
    "decision.json",
    "proposal.md",
    "review.json",
    "verification.json",
]

_BASELINE_RESULT_FILES = ["manifest.json", "result.md", "result.patch"]


@dataclass
class ScriptedAuthor:
    """`AgentAdapter`-фейк автора: очередь ответов и журнал промптов.

    Правит рабочее дерево, как правил бы настоящий: без этого
    `changes.patch` был бы пуст, `commit_round` не создал бы коммита, а
    раунд 2 сбрасывался бы не на работу раунда 1.
    """

    root: Path
    replies: list[str]
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Отдаёт следующий ответ очереди; лишний вызов — ошибка сценария."""
        self.prompts.append(prompt)
        assert self.replies, (
            f"автора позвали лишний раз (вызов {len(self.prompts)}): "
            "цикл не остановился там, где обязан был"
        )
        (self.root / f"feature_{len(self.prompts)}.py").write_text(
            f"# работа автора, вызов {len(self.prompts)}\n"
            f"VALUE = {len(self.prompts)}\n",
            encoding="utf-8",
        )
        return AgentTurn(text=self.replies.pop(0), session_ref=session_ref)


@dataclass
class ScriptedReviewer:
    """`AgentAdapter`-фейк ревьюера: очередь ответов и журнал промптов."""

    replies: list[str]
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Отдаёт следующий ответ очереди; лишний вызов — ошибка сценария."""
        self.prompts.append(prompt)
        assert self.replies, f"ревьюера позвали лишний раз (вызов {len(self.prompts)})"
        return AgentTurn(text=self.replies.pop(0), session_ref=session_ref)


@dataclass
class BarrenAgent:
    """`AgentAdapter`-фейк, у которого нет ответа ни на один промпт.

    Существует ради диагноза, а не ради сценария: промпт ему приходит
    только если припаркованная сессия двинулась дальше, и сообщение обязано
    назвать роль и фазу — иначе потерянная прокладка политики читалась бы
    как «у фейка кончилась очередь ответов».
    """

    role: Role

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Любой вызов — ошибка: припаркованная сессия не зовёт никого."""
        raise AssertionError(
            f"припаркованная сессия позвала агента ({self.role.value}): "
            "resume прошёл границу раунда, то есть политика до цикла не "
            "доехала"
        )


@dataclass
class GreenVerifier:
    """`Verifier`-фейк: зелёный отчёт по каждому запрошенному раунду."""

    def verify(self, round_no: int) -> VerificationReport:
        """Отдаёт `overall == pass` — гейты не предмет этого набора."""
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
class RecordingPolicy:
    """`RoundBoundaryPolicy`-фейк: вердикт по таблице, вход журналируется.

    Фейк, а не будущая политика пары: задача 9 подключает ПОРТ, а предикат
    архитектурного дефекта приходит своей задачей. Но вход у фейка тот же,
    что будет у настоящей политики, — валидированный `review.json`, — и
    журнал `seen` доказывает, что цикл подаёт ревью того раунда, о котором
    спрашивает.
    """

    verdicts: list[BoundaryVerdict]
    seen: list[Review] = field(default_factory=list)

    def after_deciding(self, review: Review) -> BoundaryVerdict:
        """Отдаёт очередной вердикт; лишний опрос — ошибка сценария."""
        self.seen.append(review)
        assert self.verdicts, "политику опросили лишний раз"
        return self.verdicts.pop(0)


@dataclass
class ArchitecturalPolicy:
    """`RoundBoundaryPolicy`-фейк pair-контура: park на architectural (§7.1)."""

    seen: list[Review] = field(default_factory=list)

    def after_deciding(self, review: Review) -> BoundaryVerdict:
        """`PARK`, если ревью назвало существенный архитектурный дефект."""
        self.seen.append(review)
        parks = any(
            issue.defect_class == "architectural"
            and issue.severity in (Severity.BLOCKER, Severity.MAJOR)
            for issue in review.issues
        )
        return BoundaryVerdict.PARK if parks else BoundaryVerdict.PROCEED


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
        """Монотонные часы бюджета."""
        self.monotonic_ticks += 1
        return self.monotonic_ticks * _MONOTONIC_STEP


def test_drive_without_policies_byte_identical(git_repo: Path) -> None:
    """Без политик цикл проходит эталонную последовательность фаз и файлов."""
    ctx, author, reviewer = _prepared(git_repo)

    final = anyio.run(lambda: drive(ctx))

    assert final.state is SessionPhase.DONE
    assert _phases(git_repo) == _BASELINE_PHASES
    assert _round_files(git_repo, 1) == _BASELINE_ROUND_FILES
    assert _round_files(git_repo, 2) == _BASELINE_ROUND_FILES
    assert _result_files(git_repo) == _BASELINE_RESULT_FILES
    assert len(author.prompts) == 2
    assert len(reviewer.prompts) == 2


def test_proceed_policy_leaves_the_baseline_run_untouched(
    git_repo: Path,
) -> None:
    """`PROCEED` на каждой границе даёт ту же последовательность, что дефолт.

    Утверждение не дублирует предыдущий тест: там проверяется отсутствие
    политики, здесь — что опрос политики сам по себе ничего не сдвигает.
    """
    ctx, author, reviewer = _prepared(git_repo)
    policy = RecordingPolicy(verdicts=[BoundaryVerdict.PROCEED] * 2)

    final = anyio.run(lambda: drive(ctx, round_boundary=policy))

    assert final.state is SessionPhase.DONE
    assert _phases(git_repo) == _BASELINE_PHASES
    assert _round_files(git_repo, 1) == _BASELINE_ROUND_FILES
    assert _result_files(git_repo) == _BASELINE_RESULT_FILES
    assert [review.round for review in policy.seen] == [1, 2]
    assert len(author.prompts) == 2
    assert len(reviewer.prompts) == 2


def test_park_returns_before_next_step(git_repo: Path) -> None:
    """`PARK` раунда 1: решения нет, автор раунда 2 не звался, фаза `DECIDING`."""
    ctx, author, reviewer = _prepared(git_repo, architectural=True)
    policy = ArchitecturalPolicy()

    final = anyio.run(lambda: drive(ctx, round_boundary=policy))

    assert final.state is SessionPhase.DECIDING
    assert final.current_round == 1
    assert _session_json(git_repo)["state"] == SessionPhase.DECIDING.value
    assert _session_json(git_repo)["current_round"] == 1

    assert not round_artifact(git_repo, 1, DECISION_NAME).exists()
    assert _round_files(git_repo, 1) == [
        "changes.patch",
        "proposal.md",
        "review.json",
        "verification.json",
    ]
    assert not _result_files(git_repo)

    assert len(author.prompts) == 1
    assert len(reviewer.prompts) == 1
    assert [review.round for review in policy.seen] == [1]
    assert _phases(git_repo) == _BASELINE_PHASES[:4]


@pytest.mark.parametrize(
    ("limits", "would_be"),
    [
        pytest.param(
            Limits(
                max_rounds=1,
                max_total_tokens=100_000,
                max_wall_seconds=600,
                schema_retries=1,
            ),
            SessionPhase.DEADLOCK,
            id="max_rounds",
        ),
        pytest.param(
            Limits(
                max_rounds=5,
                max_total_tokens=1_000,
                max_wall_seconds=600,
                schema_retries=1,
            ),
            SessionPhase.BUDGET_HIT,
            id="budget",
        ),
    ],
)
def test_park_wins_over_stop_conditions(
    git_repo: Path, limits: Limits, would_be: SessionPhase
) -> None:
    """Архитектурная находка паркует сессию, а не уходит в эскалацию (P6).

    `would_be` — фаза, в которую увёл бы `decide()`, будь политика опрошена
    после него: обе ветки §5 возвращаются раньше `CONTINUE`, поэтому опрос
    после решения не случился бы вовсе.
    """
    ctx, author, _ = _prepared(
        git_repo, architectural=True, limits=limits, tokens_used=9_999
    )
    policy = ArchitecturalPolicy()

    final = anyio.run(lambda: drive(ctx, round_boundary=policy))

    assert final.state is SessionPhase.DECIDING
    assert final.state is not would_be
    assert _session_json(git_repo)["state"] == SessionPhase.DECIDING.value
    assert not round_artifact(git_repo, 1, DECISION_NAME).exists()
    assert len(author.prompts) == 1
    assert [review.round for review in policy.seen] == [1]


def test_resume_forwards_the_boundary_policy(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Припаркованная сессия, поднятая `resume_session`, паркуется снова.

    Шов проверяется отдельно от `drive`, потому что теряется он отдельно:
    политику в `resume_session` передаёт вызывающий, а до цикла её везёт
    прокладка — и молчаливая её потеря не видна ни одному тесту, который
    зовёт `drive` напрямую. Цена конкретна: припаркованные сессии
    runner пайплайна поднимает именно этим вызовом, и сессия без политики
    ушла бы в `decide()` и в эскалацию — ровно та поломка P6, ради которой
    точка опроса и выбрана до решения.

    Сессия для resume не сочиняется руками, а получается прогоном: `drive`
    с парковочной политикой оставляет на диске раунд 1 без `decision.json` и
    `session.json` в `DECIDING` — то самое состояние, которое runner увидит.
    """
    _register_agents_for_resume(monkeypatch)
    ctx, author, reviewer = _prepared(git_repo, architectural=True)
    write_config_snapshot(git_repo, _config().render_toml())
    parked = anyio.run(lambda: drive(ctx, round_boundary=ArchitecturalPolicy()))
    assert parked.state is SessionPhase.DECIDING

    journal_before = events_jsonl_path(git_repo).read_bytes()
    policy = ArchitecturalPolicy()

    resumed = anyio.run(lambda: _resume(git_repo, round_boundary=policy))

    assert resumed.state is SessionPhase.DECIDING
    assert resumed.current_round == 1
    assert [review.round for review in policy.seen] == [1]
    # Потеряй прокладка политику — сессия дошла бы до `decide()`: появился бы
    # `decision.json`, раунд закрылся бы коммитом, а автор получил бы промпт
    # раунда 2. Каждый из трёх фактов проверяется отдельно, потому что
    # потерять их можно порознь.
    assert not round_artifact(git_repo, 1, DECISION_NAME).exists()
    assert len(author.prompts) == 1
    assert len(reviewer.prompts) == 1
    # Опрос стоит до первого шага, поэтому resume припаркованной сессии не
    # пишет в журнал вовсе: ни перехода, ни события.
    assert events_jsonl_path(git_repo).read_bytes() == journal_before


async def _resume(root: Path, *, round_boundary: ArchitecturalPolicy) -> SessionState:
    """Поднимает сессию штатным `resume_session` — тем же, что зовёт CLI."""
    clocks = Clocks()
    return await resume_session(
        root,
        _SESSION_ID,
        round_boundary=round_boundary,
        git=GitCli(root),
        verifier=GreenVerifier(),
        now=clocks.now,
        monotonic=clocks.monotonic,
    )


def _register_agents_for_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ставит запрет на агентов в реестр композиции для `resume_session`.

    `resume_session` собирает порты сам, из снапшота конфига, и подсунуть
    ему готовый адаптер больше негде. Фабрика отдаёт агента, который падает
    от первого же промпта: припаркованная сессия не вправе звать никого, и
    молчаливый лишний вызов обязан упасть там, где он случился, назвав роль.
    """
    composition = import_module("disputatio.runtime.composition")

    def factory(
        *, role: Role, session_dir: Path, event_sink: object, session: str
    ) -> "BarrenAgent":
        """Агент, которому нечего ответить: любой промпт ему фатален."""
        return BarrenAgent(role=role)

    monkeypatch.setitem(composition.ADAPTER_FACTORIES, _ADAPTER_NAME, factory)


def _config() -> RuntimeConfig:
    """Снапшот конфига сессии — вход `resume_session`."""
    return RuntimeConfig(
        session_id=_SESSION_ID,
        mode=Mode.DEVELOP,
        base_commit="HEAD",
        task_prompt="ЗАДАЧА-ПОЛЬЗОВАТЕЛЯ: почини экспорт CSV",
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


def _prepared(
    root: Path,
    *,
    architectural: bool = False,
    limits: Limits | None = None,
    tokens_used: int = 0,
) -> tuple[StepContext, ScriptedAuthor, ScriptedReviewer]:
    """Контекст холодного старта на фейковых портах плюс сами фейки.

    Сценарий один на весь набор: раунд 1 — `request_changes`, раунд 2 —
    `approve` при зелёных гейтах, то есть `CONVERGED → EXPORTING → DONE`.
    `architectural=True` помечает находку раунда 1 классом дефекта — вход,
    на котором политика пары обязана припарковать сессию.
    """
    bootstrap_session(root)
    author = ScriptedAuthor(root=root, replies=[_proposal(1), _proposal(2)])
    reviewer = ScriptedReviewer(
        replies=[_request_changes(1, architectural=architectural), _approve(2)]
    )
    clocks = Clocks()
    state = _state(limits=limits, tokens_used=tokens_used)
    deps = _deps(root, author=author, reviewer=reviewer, clocks=clocks)
    deps.store.save(state)
    ctx = StepContext(
        deps=deps,
        fsm=SessionFsm(state, store=deps.store, sink=deps.sink, now=deps.now),
        base_commit="HEAD",
        gates=(_GATE,),
    )
    return ctx, author, reviewer


def _deps(
    root: Path,
    *,
    author: ScriptedAuthor,
    reviewer: ScriptedReviewer,
    clocks: Clocks,
) -> RuntimeDeps:
    """`RuntimeDeps` с настоящими журналом и хранилищем и фейковым окружением."""
    return RuntimeDeps(
        workspace_root=root.resolve(),
        artifact_root=root.resolve(),
        store=FileStateStore(root),
        sink=JsonlEventSink(root),
        author=author,
        reviewer=reviewer,
        verifier=GreenVerifier(),
        git=GitCli(root),
        now=clocks.now,
        monotonic=clocks.monotonic,
    )


def _state(*, limits: Limits | None = None, tokens_used: int = 0) -> SessionState:
    """Стартовое состояние сессии: `IDLE`, раунд 0."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_CREATED_AT,
        state=SessionPhase.IDLE,
        current_round=0,
        task=TaskSpec(
            prompt="ЗАДАЧА-ПОЛЬЗОВАТЕЛЯ: почини экспорт CSV",
            attachments=[],
            mode=Mode.DEVELOP,
        ),
        agents={
            Role.AUTHOR: AgentRef(
                adapter=_ADAPTER_NAME, model="opus", session_ref=None
            ),
            Role.REVIEWER: AgentRef(
                adapter=_ADAPTER_NAME, model="sonnet", session_ref=None
            ),
        },
        limits=limits
        if limits is not None
        else Limits(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        budget_used=BudgetUsed(tokens=tokens_used, wall_seconds=0.0, cost_usd_est=0.0),
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
        "  - feature.py\n"
        "self_declared_status: complete\n"
        "---\n"
        f"Работа раунда {round_no:03d}: разделитель вынесен в конфиг.\n"
    )


def _request_changes(round_no: int, *, architectural: bool) -> str:
    """Ревью раунда 1: `request_changes` с major-замечанием.

    `defect_class` — поле `disputatio/v2` (SPEC-002 §5.1), поэтому ревью с
    ним несёт тег v2: под v1 схема отвергает его сама, и подать политике
    архитектурную находку под старым тегом было бы нельзя.
    """
    payload: dict[str, Any] = {
        "round": round_no,
        "role": Role.REVIEWER,
        "verdict": Verdict.REQUEST_CHANGES,
        "confidence": 0.7,
        "issues": [
            Issue(
                id=f"I-{round_no:03d}-1",
                severity=Severity.MAJOR,
                file="feature.py",
                claim="разделитель всё ещё читается из локали процесса",
                evidence="feature.py:2 — VALUE зависит от окружения",
                suggestion="взять разделитель из конфига сессии",
                defect_class="architectural" if architectural else None,
            )
        ],
        "checked": ["feature.py", f"rounds/{round_no:03d}/changes.patch"],
        "summary": "правка в нужном месте, но источник разделителя не изменился",
    }
    if architectural:
        payload["schema"] = SCHEMA_V2
    return Review(**payload).model_dump_json(by_alias=True)


def _approve(round_no: int) -> str:
    """Ревью раунда 2: `approve` при зелёных гейтах."""
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.APPROVE,
        confidence=0.9,
        issues=[],
        checked=["feature.py", f"rounds/{round_no:03d}/changes.patch"],
        summary="замечание раунда 1 закрыто, гейты зелёные",
    ).model_dump_json(by_alias=True)


def _phases(root: Path) -> list[tuple[str, str]]:
    """Последовательность переходов из `events.jsonl` — пары «откуда, куда»."""
    lines = events_jsonl_path(root).read_text(encoding="utf-8").splitlines()
    events = [Event.model_validate_json(line) for line in lines if line]
    return [
        (str(event.payload["from"]), str(event.payload["to"]))
        for event in events
        if event.type.value == "state_change"
    ]


def _round_files(root: Path, round_no: int) -> list[str]:
    """Имена файлов раунда, отсортированные; пусто — раунда на диске нет."""
    directory = round_dir(root, round_no)
    if not directory.exists():
        return []
    return sorted(path.name for path in directory.iterdir())


def _result_files(root: Path) -> list[str]:
    """Имена файлов `result/`, отсортированные."""
    directory = result_dir(root)
    if not directory.exists():
        return []
    return sorted(path.name for path in directory.iterdir())


def _session_json(root: Path) -> dict[str, Any]:
    """`session.json` как он лежит на диске — байты, а не объект в памяти."""
    payload = (session_dir(root) / "session.json").read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(payload)
    return parsed
