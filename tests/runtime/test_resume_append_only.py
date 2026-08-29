"""Иммутабельность и append-only при resume: TASK-017, [REQ-016], [DESIGN-016].

Обе гарантии проверяются с двух сторон, и ни одна сторона не заменяет другую.

* **Поведение.** Resume, поднявший сессию в раунде, который уже закрыт
  маркером I3, обязан упасть `RoundImmutableError` — на каждом из трёх шагов,
  пишущих артефакт раунда. Пинится ТИП исключения: «на диске ничего не
  изменилось» — утверждение слабее, оно зеленело бы и на шаге, который запись
  молча пропустил, то есть ровно на том дефекте, ради которого I3 и заведён.
* **Форма кода.** Тихую правку одним прогоном не опровергнуть: `except`
  вокруг записи артефакта и второй писатель `events.jsonl` — это свойства
  текста runtime, а не одного сценария. Их проверяет AST-скан
  `runtime/append_only.py`. Сканер живёт в `src`, а не под `tests/`
  ([ADR-006]): `commit_red` фиксирует весь `tests/`, поэтому анализатор внутри
  тестов сделал бы red-чекпоинт нечестным. Позитивная половина скана идёт по
  фактическому `disputatio.runtime`, негативная — по временному дереву с
  подложенным нарушением: без неё тест зеленел бы на сканере, всегда
  возвращающем `[]`.

Журнал сравнивается ПОБАЙТОВО: снимок до обрыва обязан быть префиксом файла
после resume. Сравнение по множеству строк или по их числу пропустило бы и
перезапись первой строки той же длины, и пересортировку — а подписчик §8
читает `events.jsonl` как поток байтов от начала, а не как множество.

Терпимость `_write_decision` к повтору ([REQ-015]) под запрет не подпадает и
пинится отдельно: обработчик обязан быть ровно один, и в нём обязан быть
`raise`. Поглощение без единого `raise` — уже тихая правка.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest

import disputatio.runtime as _runtime_package
from disputatio.contracts import (
    AgentRef,
    AgentTurn,
    BudgetUsed,
    DiffStats,
    Event,
    EventType,
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
from disputatio.events import (
    FileStateStore,
    RoundImmutableError,
    bootstrap_session,
    finalize_round,
    write_config_snapshot,
    write_round_artifact,
)
from disputatio.runtime import AgentConfig, LimitsConfig, RuntimeConfig
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    PROPOSAL_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    session_dir,
)
from disputatio.verifier import GateSpec

from ._fakes import GitOpsFakeBase

_FROZEN_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "20260810-120000-a1b2"
_ADAPTER = "resume_cli"
_GATE = "pytest"

# Цель сброса без обращения к истории git: [DESIGN-012] пинится своей задачей,
# здесь важна только запись артефактов и журнал.
_FIXED_BASE_REV = "resolved-base-rev"

_PATCH = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-СТАРО\n+НОВО\n"

_JOURNAL_NAME = "events.jsonl"
_SCHEMA = "disputatio/v1"

# Фаза, в которой resume поднимает сессию, и артефакт, который её шаг пишет в
# раунд. Все три пишут в раунд `ctx.round`, поэтому финализированный раунд
# закрыт для каждой из них — и ни одна не вправе обойти маркер молча.
_WRITING_PHASES = (
    (SessionPhase.PROPOSING, PROPOSAL_NAME),
    (SessionPhase.VERIFYING, VERIFICATION_NAME),
    (SessionPhase.REVIEWING, REVIEW_NAME),
)

# `except`, которым позволено видеть I3. Первый — повтор прерванного
# DECIDING: он обязан дойти до конца ([REQ-015], [DESIGN-015]), и терпимость
# держится на равенстве решения тому, что уже лежит в раунде. `raise` в нём
# обязателен — расхождение решений тихой правкой не лечится.
#
# Второй — fail-closed отказа политики P9 (SPEC-002 §7.1). Он широкий, потому
# что политику пишет пайплайн и тип её отказа runtime не назначает; в список
# он попал по той же причине, по какой сканер широкую ловлю и считает
# обработчиком. Артефакт под ним не пишется вовсе — в `try` ровно один вызов
# хука, — а `raise` безусловен: сессия переводится в `FAILED` и ошибка уходит
# наружу как есть. Ловля именно `Exception`, а не `BaseException` и не
# `finally`: kill во время хода автора обязан оставлять сессию в `PROPOSING`,
# то есть переигрываемой, а не помечать её отказавшей.
_TOLERATED_HANDLERS = [
    ("retry.py", "_run_lifecycle_hook", "Exception", True),
    ("steps.py", "_write_decision", "RoundImmutableError", True),
]

# Пишущие вызовы, законные в runtime. Оба — не про журнал: правило
# `.git/info/exclude` ([DESIGN-011]) и уборка огрызков записи в раунде
# ([REQ-015]). Список точный, а не «не меньше»: новый писатель в runtime
# обязан быть замечен этим тестом, даже если пишет он не в `events.jsonl`.
_ALLOWED_WRITES = {
    ("git.py", "exclude_file.write_text"),
    ("steps.py", "leftover.unlink"),
}

_ROGUE_JOURNAL_LITERAL = (
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def dump(root: Path, line: str) -> None:\n"
    '    with (root / ".disputatio" / "events.jsonl").open("a") as handle:\n'
    "        handle.write(line)\n"
)

_ROGUE_JOURNAL_HELPER = (
    "from disputatio.events.paths import events_jsonl_path\n"
    "\n"
    "\n"
    "def truncate(root):\n"
    '    events_jsonl_path(root).write_text("")\n'
)

_READ_ONLY_MODULE = (
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def load(path: Path) -> str:\n"
    '    with open(path, "r") as handle:\n'
    "        head = handle.read()\n"
    "    with path.open() as handle:\n"
    "        return head + handle.read() + path.read_text()\n"
)

_ROGUE_SWALLOW = (
    "from disputatio.events import RoundImmutableError, write_round_artifact\n"
    "\n"
    "\n"
    "def save(root, round_no, text):\n"
    "    try:\n"
    '        write_round_artifact(root, round_no, "review.json", text)\n'
    "    except RoundImmutableError:\n"
    "        pass\n"
)

_ROGUE_BROAD_SWALLOW = (
    "from disputatio.events import write_round_artifact\n"
    "\n"
    "\n"
    "def save(root, round_no, text):\n"
    "    try:\n"
    '        write_round_artifact(root, round_no, "review.json", text)\n'
    "    except Exception:\n"
    "        return None\n"
)

_ROGUE_BARE_EXCEPT = (
    "from disputatio.events import write_round_artifact\n"
    "\n"
    "\n"
    "def save(root, round_no, text):\n"
    "    try:\n"
    '        write_round_artifact(root, round_no, "review.json", text)\n'
    "    except:  # noqa: E722\n"
    "        return None\n"
)

_HONEST_RERAISE = (
    "from disputatio.events import RoundImmutableError, write_round_artifact\n"
    "\n"
    "\n"
    "def save(root, round_no, text):\n"
    "    try:\n"
    '        write_round_artifact(root, round_no, "review.json", text)\n'
    "    except RoundImmutableError:\n"
    "        if text != read(root, round_no):\n"
    "            raise\n"
)


class StepBoom(Exception):
    """Обрыв внутри тела шага — «процесс умер, не дойдя до результата».

    Не из `SCHEMA_INVALID_ERRORS`: schema-retry лечит вывод агента не той
    формы, а здесь моделируется отказ самого порта.
    """


def _guard() -> ModuleType:
    """Модуль сканера; на red-фазе — `AssertionError` вместо `ImportError`."""
    try:
        from disputatio.runtime import append_only
    except ImportError as exc:  # red-фаза: runtime/append_only.py ещё не создан
        raise AssertionError(
            "src/disputatio/runtime/append_only.py ещё не создан"
        ) from exc
    return append_only


def _runtime_dir() -> Path:
    """Каталог фактического пакета `disputatio.runtime`."""
    package_file = _runtime_package.__file__
    assert package_file is not None, "у disputatio.runtime нет __file__"
    return Path(package_file).parent


def _rogue_package(tmp_path: Path, name: str, source: str) -> Path:
    """Дерево из одного модуля `name` с исходником `source` — площадка скана."""
    package = tmp_path / "rogue_package"
    package.mkdir(exist_ok=True)
    (package / name).write_text(source, encoding="utf-8")
    return package


@dataclass
class Clock:
    """Часы сессии: каждый вызов на секунду позже предыдущего.

    Общие на оба прогона теста — время не откатывается при перезапуске
    процесса, и монотонность `ts` в журнале остаётся наблюдаемой.
    """

    ticks: int = 0

    def __call__(self) -> datetime:
        """Следующий момент времени сессии."""
        self.ticks += 1
        return _FROZEN_NOW + timedelta(seconds=self.ticks)


@dataclass
class SpyAdapter:
    """`AgentAdapter`-фейк: отдаёт ответы очереди и падает на `fail_at`-м."""

    name: str
    replies: list[str] = field(default_factory=list)
    fail_at: int | None = None
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Считает вызов, затем падает либо отдаёт следующий ответ очереди."""
        self.prompts.append(prompt)
        if self.fail_at is not None and len(self.prompts) >= self.fail_at:
            raise StepBoom(f"{self.name}: обрыв на вызове {len(self.prompts)}")
        assert self.replies, f"SpyAdapter {self.name}: очередь ответов исчерпана"
        return AgentTurn(text=self.replies.pop(0), session_ref=session_ref)


@dataclass
class SpyVerifier:
    """`Verifier`-фейк: зелёный отчёт по запрошенному раунду."""

    rounds: list[int] = field(default_factory=list)

    def verify(self, round_no: int) -> VerificationReport:
        """Отмечает прогон гейтов раунда и отдаёт зелёный отчёт."""
        self.rounds.append(round_no)
        return _verification(round_no)


@dataclass
class SpyGit(GitOpsFakeBase):
    """`GitOps`-фейк: рабочего дерева не трогает, историю не заводит."""

    resets: list[str] = field(default_factory=list)
    commits: list[int] = field(default_factory=list)

    def diff_head(self) -> str:
        """Патч раунда — его содержимое здесь роли не играет."""
        return _PATCH

    def commit_round(self, round_no: int) -> None:
        """Отмечает фиксацию принятого раунда."""
        self.commits.append(round_no)

    def reset_hard(self, rev: str) -> None:
        """Отмечает сброс дерева на цель раунда."""
        self.resets.append(rev)

    def clean(self) -> None:
        """Уборка untracked-файлов прерванной попытки — здесь пустая."""


@dataclass
class Bench:
    """Порты одного прогона: журнал и состояние настоящие, остальное — спаи."""

    root: Path
    clock: Clock
    verifier: SpyVerifier
    git: SpyGit

    def overrides(self) -> dict[str, Any]:
        """Подмены портов: `sink` и `store` НЕ подменяются — они предмет теста.

        Журнал обязан лечь на диск тем самым `JsonlEventSink`, который
        собирает композиция: спай в этом тесте подменил бы собой ровно ту
        реализацию append-only, которую проверяют байты файла.
        """
        return {
            "git": self.git,
            "verifier": self.verifier,
            "now": self.clock,
            "monotonic": lambda: 0.0,
        }


def _resume(root: Path, session_id: str, **overrides: Any) -> SessionState:
    """`resume_session(root, session_id, **overrides)` под `anyio.run`."""
    loop = import_module("disputatio.runtime.loop")

    async def call() -> SessionState:
        result: SessionState = await loop.resume_session(root, session_id, **overrides)
        return result

    return anyio.run(call)


def _pin_base_rev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет `base_rev` в модуле шагов фиксированной ревизией."""
    steps = import_module("disputatio.runtime.steps")
    assert hasattr(steps, "base_rev"), (
        "runtime/steps.py обязан брать цель сброса у git.base_rev"
    )
    monkeypatch.setattr(
        steps, "base_rev", lambda root, round_no, *, base_commit: _FIXED_BASE_REV
    )


def _register_adapters(
    monkeypatch: pytest.MonkeyPatch,
    *,
    author_replies: Iterable[str],
    author_fail_at: int | None,
    reviewer_replies: Iterable[str],
    reviewer_fail_at: int | None,
) -> None:
    """Регистрирует фейковую фабрику адаптера в реестре композиции."""
    composition = import_module("disputatio.runtime.composition")
    settings: Mapping[Role, tuple[list[str], int | None]] = {
        Role.AUTHOR: (list(author_replies), author_fail_at),
        Role.REVIEWER: (list(reviewer_replies), reviewer_fail_at),
    }

    def factory(
        *, role: Role, session_dir: Path, event_sink: object, session: str
    ) -> SpyAdapter:
        replies, fail_at = settings[role]
        return SpyAdapter(name=role.value, replies=replies, fail_at=fail_at)

    monkeypatch.setitem(composition.ADAPTER_FACTORIES, _ADAPTER, factory)


def _bench(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    *,
    author_replies: Iterable[str] = (),
    author_fail_at: int | None = None,
    reviewer_replies: Iterable[str] = (),
    reviewer_fail_at: int | None = None,
) -> Bench:
    """Готовит реестр адаптеров и спаи одного прогона под `root`."""
    _register_adapters(
        monkeypatch,
        author_replies=author_replies,
        author_fail_at=author_fail_at,
        reviewer_replies=reviewer_replies,
        reviewer_fail_at=reviewer_fail_at,
    )
    return Bench(root=root, clock=clock, verifier=SpyVerifier(), git=SpyGit())


def _proposal(round_no: int) -> str:
    """Валидный `proposal.md` раунда `round_no` — ответ автора."""
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
        f"Работа раунда {round_no:03d}.\n"
    )


def _review_reply(round_no: int) -> str:
    """Ответ ревьюера: `request_changes` с major-замечанием и evidence."""
    marker = f"{round_no:03d}"
    review = Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.8,
        issues=[
            Issue(
                id=f"I-{marker}",
                severity=Severity.MAJOR,
                file=f"feature{marker}.py",
                claim=f"замечание раунда {marker}",
                evidence=f"feature{marker}.py:1 — свидетельство раунда {marker}",
            )
        ],
        checked=[f"feature{marker}.py"],
        summary=f"свод раунда {marker}",
    )
    return review.model_dump_json(by_alias=True)


def _verification(round_no: int) -> VerificationReport:
    """Зелёный отчёт проверок раунда `round_no`."""
    return VerificationReport(
        round=round_no,
        gates=[
            GateResult(
                name=_GATE,
                cmd="uv run pytest -q",
                status=GateStatus.PASS,
                exit_code=0,
                duration_s=1.25,
                tail=f"вывод гейта раунда {round_no:03d}",
            )
        ],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=1, deletions=1),
    )


def _config() -> RuntimeConfig:
    """Конфиг сессии — тот, что ложится снапшотом в `.disputatio/config.toml`."""
    return RuntimeConfig(
        session_id=_SESSION_ID,
        mode=Mode.DEVELOP,
        base_commit=_FIXED_BASE_REV,
        task_prompt="Почини экспорт CSV",
        author=AgentConfig(adapter=_ADAPTER, model="opus"),
        reviewer=AgentConfig(adapter=_ADAPTER, model="sonnet"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        gates=(GateSpec(name=_GATE, cmd="uv run pytest -q", enabled=True),),
        attachments=(),
    )


def _state(phase: SessionPhase, round_no: int) -> SessionState:
    """`SessionState` в фазе `phase` раунда `round_no`."""
    return SessionState(
        session_id=_SESSION_ID,
        created_at=_FROZEN_NOW,
        state=phase,
        current_round=round_no,
        task=TaskSpec(prompt="Почини экспорт CSV", attachments=[], mode=Mode.DEVELOP),
        agents={
            Role.AUTHOR: AgentRef(adapter=_ADAPTER, model="opus"),
            Role.REVIEWER: AgentRef(adapter=_ADAPTER, model="sonnet"),
        },
        limits=Limits(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        budget_used=BudgetUsed(),
    )


def _seed_session(root: Path, state: SessionState) -> None:
    """Кладёт на диск `.disputatio/`, снапшот конфига и состояние — вход resume."""
    bootstrap_session(root)
    write_config_snapshot(root, _config().render_toml())
    FileStateStore(root).save(state)


def _seed_finalized_round(root: Path, round_no: int) -> None:
    """Записывает артефакты раунда и закрывает его маркером I3 ([REQ-009])."""
    write_round_artifact(root, round_no, PROPOSAL_NAME, _proposal(round_no))
    write_round_artifact(root, round_no, CHANGES_PATCH_NAME, _PATCH)
    write_round_artifact(
        root,
        round_no,
        VERIFICATION_NAME,
        _verification(round_no).model_dump_json(by_alias=True),
    )
    write_round_artifact(root, round_no, REVIEW_NAME, _review_reply(round_no))
    finalize_round(root, round_no)


def _journal_bytes(root: Path) -> bytes:
    """Содержимое `events.jsonl` как байты — без декодирования и разбора."""
    return (session_dir(root) / _JOURNAL_NAME).read_bytes()


def _journal_lines(payload: bytes) -> list[bytes]:
    """Строки журнала как байты, в порядке файла."""
    return payload.splitlines()


def _state_changes(lines: Iterable[bytes]) -> list[str]:
    """Фазы, в которые уходили события `state_change`, в порядке строк."""
    parsed = [json.loads(line) for line in lines]
    return [
        record["payload"]["to"]
        for record in parsed
        if record["type"] == EventType.STATE_CHANGE.value
    ]


def _crash_then_resume(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[bytes, bytes]:
    """Прогон до обрыва и resume после него; отдаёт журнал до и после.

    Первый прогон умирает в теле `PROPOSING` раунда 1 — до записи артефакта,
    но ПОСЛЕ перехода, то есть журнал уже не пуст. Второй начинает с той же
    фазы (write-ahead, [REQ-014]) и доходит до ревьюера, который обрывается
    сам: терминальная точка тесту не нужна, ему нужны новые строки в конце.
    """
    _pin_base_rev(monkeypatch)
    clock = Clock()
    _seed_session(root, _state(SessionPhase.IDLE, 0))

    crashed = _bench(root, monkeypatch, clock, author_fail_at=1)
    with pytest.raises(StepBoom):
        _resume(root, _SESSION_ID, **crashed.overrides())
    before = _journal_bytes(root)

    resumed = _bench(
        root,
        monkeypatch,
        clock,
        author_replies=[_proposal(1)],
        reviewer_fail_at=1,
    )
    with pytest.raises(StepBoom):
        _resume(root, _SESSION_ID, **resumed.overrides())
    return before, _journal_bytes(root)


@pytest.mark.parametrize(
    ("phase", "artifact"),
    _WRITING_PHASES,
    ids=[phase.value for phase, _ in _WRITING_PHASES],
)
def test_resume_into_a_finalized_round_raises_round_immutable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: SessionPhase,
    artifact: str,
) -> None:
    """Запись в закрытый раунд после resume — `RoundImmutableError` наружу.

    Сессия ушла в раунд 2, раунд 1 финализирован, а `session.json` называет
    раунд 1: так выглядит откат состояния (восстановленная копия каталога,
    незавершённый сброс буферов) — и так шаг оказывается нацелен на раунд,
    который история уже закрыла. Runtime обязан упасть: правка `NNN` после
    маркера означала бы, что артефакт, по которому принято решение, больше не
    тот, по которому его принимали.

    Пинится ТИП исключения. «Файл не изменился» — утверждение слабее: шаг,
    молча пропустивший запись, прошёл бы его и оставил сессию с раундом без
    артефакта, то есть ровно с той тихой правкой, которую I3 запрещает.
    """
    _pin_base_rev(monkeypatch)
    _seed_session(tmp_path, _state(phase, 1))
    _seed_finalized_round(tmp_path, 1)
    bench = _bench(
        tmp_path,
        monkeypatch,
        Clock(),
        author_replies=[_proposal(1)],
        reviewer_replies=[_review_reply(1)],
    )

    with pytest.raises(RoundImmutableError) as caught:
        _resume(tmp_path, _SESSION_ID, **bench.overrides())

    assert artifact in str(caught.value)


def test_runtime_never_swallows_the_round_immutable_error() -> None:
    """Ни один `except` в runtime не поглощает I3 ([DESIGN-016]).

    Поглощение — это `except` без единого `raise`: он превращает отказ в
    успех, и сессия продолжается с раундом, чей артефакт остался чужим.
    """
    assert _guard().swallowed_immutability(_runtime_dir()) == []


def test_the_only_immutability_handlers_are_the_two_named_ones() -> None:
    """Обработчиков, видящих I3, в runtime ровно два — и оба поимённо.

    Список точный: `except` вокруг записи артефакта, которого нет в списке,
    означал бы второе мнение о том, когда закрытый раунд можно переписать, а
    именно этого [DESIGN-016] и не допускает. Заодно пинится широкая ловля —
    `except Exception` поймал бы I3 заодно, и сканер обязан считать её
    обработчиком; поэтому fail-closed политики P9 стоит здесь наравне с
    терпимостью повтора DECIDING, а не мимо проверки.
    """
    handlers = _guard().scan_immutability_handlers(_runtime_dir())

    assert [
        (handler.module.name, handler.function, handler.caught, handler.reraises)
        for handler in handlers
    ] == _TOLERATED_HANDLERS


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("rogue_swallow.py", _ROGUE_SWALLOW),
        ("rogue_broad.py", _ROGUE_BROAD_SWALLOW),
        ("rogue_bare.py", _ROGUE_BARE_EXCEPT),
    ],
    ids=["named-immutability-error", "broad-exception", "bare-except"],
)
def test_handler_without_reraise_is_reported_as_swallowed(
    tmp_path: Path, name: str, source: str
) -> None:
    """Негатив: `except` без `raise` вокруг записи артефакта — нарушение.

    Без этой половины позитивный тест вакуумен: сканер, всегда возвращающий
    пустой список, прошёл бы его.
    """
    package = _rogue_package(tmp_path, name, source)

    swallowed = _guard().swallowed_immutability(package)

    assert [handler.function for handler in swallowed] == ["save"]


def test_conditional_reraise_is_not_reported_as_swallowed(tmp_path: Path) -> None:
    """Условный `raise` — законная терпимость к повтору шага ([REQ-015]).

    Обратная половина негатива: сканер, объявляющий нарушением ЛЮБОЙ
    обработчик, запретил бы идемпотентный повтор DECIDING и был бы отвергнут
    первым же прогоном — а этот тест называет границу явно.
    """
    package = _rogue_package(tmp_path, "honest.py", _HONEST_RERAISE)

    assert _guard().swallowed_immutability(package) == []
    assert [h.reraises for h in _guard().scan_immutability_handlers(package)] == [True]


def test_runtime_has_no_writer_of_the_journal_besides_the_sink() -> None:
    """В runtime нет кода, открывающего `events.jsonl` ([DESIGN-016]).

    Ни собранного руками пути (литерал с именем журнала), ни обращения к
    `events_jsonl_path`: append-only держится на том, что журнал пишет один
    `JsonlEventSink` в `O_APPEND`, и второй писатель усёк бы его молча.
    Упоминания журнала в docstring'ах нарушением не считаются — запретить о
    нём писать значило бы проверять документацию, а не код.
    """
    assert _guard().scan_journal_references(_runtime_dir()) == []


def test_the_file_writers_of_runtime_are_the_two_known_ones() -> None:
    """Пишущих вызовов в runtime ровно два, и оба не про журнал.

    Список-разрешение точный: писатель журнала приходит не под своим именем,
    а обычным `open`/`write_text` по собранному пути, и отличить его от
    любого другого нового писателя статически нельзя. Значит замечен обязан
    быть каждый.
    """
    writes = _guard().scan_file_writes(_runtime_dir())

    assert {(write.module.name, write.target) for write in writes} == _ALLOWED_WRITES


@pytest.mark.parametrize(
    ("name", "source", "kinds", "writes"),
    [
        (
            "rogue_literal.py",
            _ROGUE_JOURNAL_LITERAL,
            ["journal-literal"],
            ["(root / '.disputatio' / 'events.jsonl').open", "handle.write"],
        ),
        (
            "rogue_helper.py",
            _ROGUE_JOURNAL_HELPER,
            ["journal-path-helper", "journal-path-helper"],
            ["events_jsonl_path(root).write_text"],
        ),
    ],
    ids=["hand-built-path", "path-helper"],
)
def test_journal_writer_planted_in_a_module_is_reported(
    tmp_path: Path, name: str, source: str, kinds: list[str], writes: list[str]
) -> None:
    """Негатив: писатель журнала виден и по литералу, и по построителю пути.

    Оба модуля пишут в `events.jsonl` мимо sink'а — один собирает путь
    руками, другой берёт его у `events_jsonl_path` (импорт плюс вызов, отсюда
    две отметки). Открытие в `"a"` при этом обязано попасть и в список
    пишущих вызовов: дописывание — тоже запись, и режим `append` от неё не
    освобождает. Без этой половины позитивный скан вакуумен.
    """
    package = _rogue_package(tmp_path, name, source)

    references = _guard().scan_journal_references(package)

    assert [reference.kind for reference in references] == kinds
    assert {reference.module.name for reference in references} == {name}
    assert [write.target for write in _guard().scan_file_writes(package)] == writes


def test_reading_a_file_is_not_reported_as_a_write(tmp_path: Path) -> None:
    """Чтение писателем не считается — иначе список-разрешение бессмыслен.

    Обратная сторона `test_the_file_writers_of_runtime_are_the_two_known_ones`:
    сканер, считающий записью каждый `open`, выдал бы столько отметок, что
    точный список стал бы недостижим, и проверку пришлось бы ослабить до
    «не меньше», то есть до неспособности заметить нового писателя.
    """
    package = _rogue_package(tmp_path, "reader.py", _READ_ONLY_MODULE)

    assert _guard().scan_file_writes(package) == []


def test_the_journal_before_the_crash_is_a_byte_prefix_after_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Снимок `events.jsonl` до обрыва — префикс журнала после resume.

    Побайтово, а не по числу строк: усечение и перезапись первой строки
    другой строкой той же длины отличаются только байтами, и подписчик §8,
    дочитавший файл до обрыва, увидел бы разное продолжение одного и того же
    смещения. Новые строки при этом обязаны появиться — префикс, равный
    файлу целиком, означал бы, что resume не записал ничего.
    """
    before, after = _crash_then_resume(tmp_path, monkeypatch)

    assert before, "до обрыва журнал пуст — сравнивать нечего"
    assert after.startswith(before)
    assert len(after) > len(before)
    assert after.endswith(b"\n")


def test_new_events_are_appended_to_the_end_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Новое дописано в конец, порядок сохранён ([REQ-016]).

    Три утверждения об одном: строки прошлого прогона стоят на своих местах,
    новые идут строго после них, а `ts` по всему файлу возрастает — журнал
    остаётся последовательностью, а не набором. Порядок фаз читается по
    `state_change`: `PROPOSING` до обрыва не повторяется, потому что resume
    начинает с сохранённой фазы, а не с её перехода ([DESIGN-008]).
    """
    before, after = _crash_then_resume(tmp_path, monkeypatch)
    old_lines = _journal_lines(before)
    new_lines = _journal_lines(after)

    assert new_lines[: len(old_lines)] == old_lines
    assert _state_changes(old_lines) == [SessionPhase.PROPOSING.value]
    assert _state_changes(new_lines[len(old_lines) :]) == [
        SessionPhase.VERIFYING.value,
        SessionPhase.REVIEWING.value,
    ]

    stamps = [Event.model_validate_json(line).ts for line in new_lines]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_every_line_of_the_journal_is_a_v1_event_after_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каждая строка журнала после resume — `Event` схемы `disputatio/v1`.

    Проверяются обе стороны строки: сырой JSON несёт ключ `schema` (по нему
    подписчик отличает версию, не разбирая модель), и та же строка проходит
    валидацию `Event`. Схема сверяется у КАЖДОЙ строки, включая дописанные
    после resume: слипшаяся с хвостом строка — это потерянное событие, о
    котором подписчик узнает только по ошибке разбора.
    """
    _, after = _crash_then_resume(tmp_path, monkeypatch)
    lines = _journal_lines(after)

    assert len(lines) >= 2, "в журнале нет ни строки до обрыва, ни строки после"
    for number, line in enumerate(lines, start=1):
        record = json.loads(line)
        assert record["schema"] == _SCHEMA, f"строка {number}"
        assert Event.model_validate_json(line).schema_ == _SCHEMA, f"строка {number}"
