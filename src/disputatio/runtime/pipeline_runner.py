"""Runner пайплайна: фазы, интенты, контуры, возврат (SPEC-002 §2, §4.3, §7).

Центр всего пайплайна и единственное место, где живёт цикл §4.3
«intent → действие → результат либо chained-преемник». Пять решений
определяют его форму; каждое принято против варианта, который выглядит проще
и ломается тихо.

**Терминал читается по durable-состоянию, а не по возврату драйвера** (§7.2).
`SessionDriver` возвращает `SessionState`, и соблазн поверить возврату велик —
но припаркованная сессия (§7.1) возвращается нетерминальной, а упавший процесс
не возвращается вовсе. Истина одна: `session.json` плюс `decision.json`
последнего раунда. У припаркованного раунда решения НЕ существует —
`decide()` не вызывался, — и это не пробел, а сам признак парковки.

**`budget_used` пересчитывается, а не инкрементируется** (§4.2). Расход
считается заново из `session.json` ВСЕХ сессий — включая припаркованные, их
расход тоже потрачен — при каждой записи манифеста (`_write`). Поэтому
повторное исполнение интента после краха не даёт двойного начисления по
построению, а не «потому что мы аккуратно расставили инкременты»: хранилище
принимает записанное значение как есть и такой ошибки не поймает.

**Возврат — reconciliation от durable-состояния** (§7.3). Runner узнаёт о
дефекте после того, как сессия уже записала ревью, поэтому identity
checkpoint'а выводится из того, что записано: `{session_id, round,
sha256(review.json)}` — только из ревью, потому что `decision.json` у
припаркованного раунда нет. Commit point — ОДНА атомарная запись манифеста
(transition + outcome + superseded_by + chained `create_session`): до неё
возврат не случился, после — случился необратимо.

**Приоритет P6 абсолютен.** Архитектурная находка ведёт в spec-контур,
сколько бы execution-находок ни было рядом; обеспечено это точкой опроса
политики (в `drive()`, до `decide()`), а здесь — тем, что парковка
проверяется ПЕРВОЙ, раньше любого терминала и любого лимита сессии.

**Границы обрыва — внутри интентов, а не по одной на `kind`.** Каждая запись
манифеста и каждый внешний эффект (создание каталога, `git reset`, вызов
экспортёра) — самостоятельная точка обрыва, и идемпотентность требуется от
каждой. Общий приём один: результат действия и следующий intent ложатся ОДНОЙ
атомарной записью, поэтому повтор возможен ровно до неё, а после — интент уже
другой. Внешние эффекты идемпотентны сами: каталог создаётся `exist_ok`,
сессия не пересоздаётся поверх durable `session.json`, `reset --hard` к тому
же коммиту — no-op, экспорт идемпотентен по контракту §8.2.

Долг задачи 13, закрываемый здесь: `run` **первым действием** создаёт пустой
анкер (§3.1). §8.1 шаг 0 делает «файла анкера нет» безусловным отказом
resume и обосновывает отказ именно этим — значит крах на первом же интенте
обязан оставить существующий пустой журнал, иначе правило отказывало бы
ложно. Путь анкера несёт `workspace_fingerprint` (P9), поэтому один слаг в
двух репозиториях — два независимых журнала.

Чего здесь нет и быть не должно: §8.1 целиком (сверка worktree, операторские
`--discard-round`/`--adopt-external`) — это отдельная задача; их `kind`
намеренно не зарегистрированы, и встреча с ними — громкий отказ, а не тихая
ветка.
"""

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol

from disputatio.contracts import (
    CONTOURS_BY_KIND,
    ENTRY_PHASE,
    SESSIONS_FIELD_BY_CONTOUR,
    TERMINAL_CONTOUR,
    BoundaryVerdict,
    BudgetUsed,
    Documents,
    EvidenceLink,
    FileRef,
    Issue,
    NextAction,
    Outcome,
    PairDocuments,
    PipelineKind,
    PipelinePhase,
    PipelineState,
    PipelineStateStore,
    Review,
    RoundBoundaryPolicy,
    SessionOutcome,
    SessionPhase,
    SessionRecord,
    SessionState,
    Severity,
    SingleDocument,
    Transition,
    TransitionReason,
)
from disputatio.core import TERMINAL_PHASES
from disputatio.events import (
    FileStateStore,
    IntegrityAnchor,
    PipelineEvent,
    PipelineEventType,
    atomic_write,
)
from disputatio.runtime.errors import ConfigError, PipelineAlreadyExists
from disputatio.runtime.git import SESSION_DIR_NAME, GitOps
from disputatio.runtime.history import load_decision, load_review
from disputatio.runtime.layout import REVIEW_NAME, round_dir
from disputatio.runtime.pipeline_config import (
    PIPELINES_DIR_NAME,
    PipelineConfig,
    check_run_preconditions,
)
from disputatio.runtime.pipeline_export import ExportFn
from disputatio.runtime.pipeline_semantic_proof import write_semantic_proof
from disputatio.verifier import resolve_inside

#: Имена контуров (§2): spec — полировка спеки, pair — перепроверка пары,
#: doc — единственный контур вида `document`.
CONTOUR_SPEC: Final = "spec"
CONTOUR_PAIR: Final = "pair"
CONTOUR_DOC: Final = "doc"

#: Каталог ревизий внутри `pipelines/<slug>/` (§4.1).
SESSIONS_DIR_NAME: Final = "sessions"

#: Снапшоты верхнего уровня (§4.1); их пути попадают в манифест относительными.
TASK_SNAPSHOT_NAME: Final = "task.md"
CONFIG_SNAPSHOT_NAME: Final = "config.toml"
CHECKLISTS_SNAPSHOT_NAME: Final = "checklists.toml"

#: Серьёзности, с которых находка считается существенной (§7.1).
_SUBSTANTIVE: Final = frozenset({Severity.BLOCKER, Severity.MAJOR})

#: Причина ребра «терминальный контур сошёлся → EXPORTING» по виду (§2).
_CONVERGED_REASON: Final[dict[PipelineKind, TransitionReason]] = {
    PipelineKind.PAIR: TransitionReason.PAIR_CONVERGED,
    PipelineKind.DOCUMENT: TransitionReason.DOCUMENT_CONVERGED,
}

#: Потолок числа интентов на один `advance`. Цикл пайплайна конечен сам по
#: себе (возвраты ограничены `max_architectural_returns`, остальные фазы
#: монотонны), поэтому упереться в потолок можно только багом диспетчера — и
#: тогда честнее упасть, чем крутиться молча.
_MAX_INTENTS: Final = 500


@dataclass(frozen=True, slots=True)
class SessionCreation:
    """Аргументы создания одной ревизии сессии — вход `SessionFactory`.

    Отдельный тип, а не кортеж позиционных аргументов: тесты возврата
    проверяют именно ЭТИ поля (P5 — «pair-rN+1 стартует без унаследованного
    approve/checklist/carried issues»), и утверждение про аргументы создания
    должно читаться, а не расшифровываться по индексам.

    `findings` непуст ровно у spec-ревизии, открытой возвратом: это
    архитектурные находки pair-ревью, которые §7.3 требует донести до автора
    новой спеки **как недоверенные данные**. У всех остальных ревизий — пусто.

    `base_commit` заполняет ровно один вызывающий — операторский adoption
    (§3.1): новая ревизия обязана стартовать от чекпоинта, которым принята
    внешняя правка, иначе первый же `PROPOSING` сбросил бы дерево к
    состоянию ДО правки и стёр её. `None` означает «как обычно» — база
    ревизии определяется фабрикой, а не решением оператора.
    """

    artifact_root: Path
    session_id: str
    contour: str
    revision: int
    task_text: str
    findings: tuple[Issue, ...] = ()
    base_commit: str | None = None


SessionDriver = Callable[[Path, str, RoundBoundaryPolicy | None], SessionState]
"""Прогон сессии до её собственной остановки: `(artifact_root, session_id, policy)`.

Инъекция, а не прямой вызов `drive`/`resume_session`: runner обязан быть
проверяем на скриптованных артефактах, а настоящий цикл асинхронен и тянет за
собой адаптеры. Возврат драйвера runner **не интерпретирует** — истина
читается с диска (§7.2); значение принимается только чтобы вызов имел тип.
"""

SessionFactory = Callable[[SessionCreation], SessionState]
"""Материализация новой ревизии: каталог сессии + стартовый `session.json`."""


class PipelineSink(Protocol):
    """Порт журнала событий пайплайна — best-effort поток P8."""

    def emit(self, event: PipelineEvent) -> None:
        """Дописывает событие в `pipelines/<slug>/events.jsonl`."""
        ...


class ArchitecturalDefectPolicy:
    """`RoundBoundaryPolicy` pair-контура: архитектурный дефект → `PARK` (§7.1).

    Чистая функция над валидированным ревью. Порог — `blocker|major`: minor и
    nit не обесценивают спеку и возврата не стоят, а §4.4 SPEC-001 и без того
    требует evidence именно у существенных находок.
    """

    def after_deciding(self, review: Review) -> BoundaryVerdict:
        """`PARK`, если есть blocker/major с `defect_class: architectural`."""
        return (
            BoundaryVerdict.PARK
            if architectural_findings(review)
            else BoundaryVerdict.PROCEED
        )


def architectural_findings(review: Review) -> tuple[Issue, ...]:
    """Существенные архитектурные находки ревью — вход и политики, и evidence."""
    return tuple(
        issue
        for issue in review.issues
        if issue.defect_class == "architectural" and issue.severity in _SUBSTANTIVE
    )


def revision_id(contour: str, revision: int) -> str:
    """Детерминированное имя ревизии: `spec-r2`, `pair-r1` (§4.1, §7.3)."""
    return f"{contour}-r{revision}"


def pipeline_dir_of(workspace_root: Path, slug: str) -> Path:
    """`.disputatio/pipelines/<slug>` (§4.1) — раскладка, а не метод runner'а.

    Модульная функция, потому что путь нужен и операторским решениям (§3.1),
    и resume (§8.1), а собирать его там заново значило бы завести третью
    копию знания о раскладке.
    """
    return workspace_root / SESSION_DIR_NAME / PIPELINES_DIR_NAME / slug


def artifact_root_of(workspace_root: Path, slug: str, session_id: str) -> Path:
    """`artifact_root` одной ревизии: `sessions/<revision>` (§4.1)."""
    return pipeline_dir_of(workspace_root, slug) / SESSIONS_DIR_NAME / session_id


def load_session_state(artifact_root: Path, session_id: str) -> SessionState | None:
    """`session.json` ревизии либо `None`, если её ещё нет на диске."""
    try:
        return FileStateStore(artifact_root).load(session_id)
    except KeyError:
        return None


def all_session_records(state: PipelineState) -> tuple[SessionRecord, ...]:
    """Ревизии ВСЕХ коллекций манифеста в порядке контуров (§4.2).

    Обход по `SESSIONS_FIELD_BY_CONTOUR`, а не по паре полей: коллекций
    три, и перечисление, которое забывают дополнить, теряет ревизии молча —
    а на этом обходе стоят и «активная сессия», и пересчёт бюджета.
    Пустые коллекции чужого вида вклада не вносят по построению: их
    непустота отвергается схемой как `invariant_violation`.
    """
    return tuple(
        record
        for field_name in SESSIONS_FIELD_BY_CONTOUR.values()
        for record in getattr(state, field_name)
    )


def active_session(state: PipelineState) -> SessionRecord | None:
    """Единственная незакрытая ревизия манифеста либо `None` (§4.2, §8.1).

    Закрытой считается ревизия с записанным `outcome` ЛИБО с
    `superseded_by`: §8.1 запрещает возобновлять и ту, и другую, а
    перекрытая ревизия исход получает не всегда (сошедшаяся spec-rN
    сохраняет `converged`, P3).
    """
    live = [
        record
        for record in all_session_records(state)
        if record.outcome is None and record.superseded_by is None
    ]
    return live[-1] if live else None


def recompute_budget(pipeline_dir: Path, state: PipelineState) -> BudgetUsed:
    """Сумма расхода по `session.json` ВСЕХ сессий манифеста (§4.2).

    Пересчёт, а не инкремент: повторное исполнение интента после краха не
    даёт двойного начисления по построению. Припаркованные ревизии входят
    наравне — их расход тоже потрачен; ревизия без `session.json` (интент
    создания ещё не исполнен) вносит ноль — её расхода нет, а не
    «неизвестен».
    """
    tokens = 0
    wall_seconds = 0.0
    cost = 0.0
    for record in all_session_records(state):
        session = load_session_state(pipeline_dir / record.path, record.session_id)
        if session is None:
            continue
        tokens += session.budget_used.tokens
        wall_seconds += session.budget_used.wall_seconds
        cost += session.budget_used.cost_usd_est
    return BudgetUsed(tokens=tokens, wall_seconds=wall_seconds, cost_usd_est=cost)


def split_revision(session_id: str) -> tuple[str, int]:
    """Обратная операция к `revision_id`: `pair-r2` → `("pair", 2)`."""
    contour, _, number = session_id.partition("-r")
    return contour, int(number)


def return_operation_id(session_id: str, round_no: int, review_sha256: str) -> str:
    """`operation_id` возврата из identity checkpoint'а (§7.3 шаг 2).

    Вход — ровно `{session_id, round, sha256(review.json)}`, и `decision.json`
    в нём нет намеренно: у припаркованного раунда решения не существует
    (§7.1), а identity обязана опираться на то, что записано. Повторное
    обнаружение того же checkpoint'а при resume даёт тот же идентификатор —
    на этом и держится идемпотентность шага.
    """
    digest = hashlib.sha256(
        f"{session_id}\x00{round_no}\x00{review_sha256}".encode()
    ).hexdigest()
    return f"return-{digest[:32]}"


@dataclass(frozen=True, slots=True)
class _Interpretation:
    """Итог сессии по durable-состоянию (§7.2): что случилось и почему."""

    kind: str  # converged | escalated | failed | parked
    reason: TransitionReason | None = None
    round_no: int = 0
    detail: str = ""


class PipelineRunner:
    """Цикл §4.3 над манифестом пайплайна: интенты, контуры, возврат, экспорт.

    `workspace_root` — единственный вход сверх перечисленных брифом: без него
    не построить ни `sessions/<revision>/`, ни путь анкера
    (`<anchor_root>/<workspace_fingerprint>/<anchor_id>.jsonl`, P9), а
    вывести его из портов неоткуда — хранилище держит корень приватно, а
    `GitOps` отдаёт только относительный префикс toplevel.

    `store.load` отсутствующего пайплайна поднимает `KeyError` — контракт
    порта; перевод в пользовательское сообщение остаётся границе CLI, ровно
    как `resume_session` переводит `KeyError` в `SessionNotFound`.
    """

    def __init__(
        self,
        *,
        boundary_policies: Mapping[str, RoundBoundaryPolicy],
        store: PipelineStateStore,
        sink: PipelineSink,
        git: GitOps,
        session_driver: SessionDriver,
        session_factory: SessionFactory,
        exporter: ExportFn,
        now: Callable[[], datetime],
        config: PipelineConfig,
        workspace_root: Path,
    ) -> None:
        self._boundary_policies = dict(boundary_policies)
        self._store = store
        self._sink = sink
        self._git = git
        self._session_driver = session_driver
        self._session_factory = session_factory
        self._exporter = exporter
        self._now = now
        self._config = config
        self._workspace_root = workspace_root
        self._handlers: dict[
            str, Callable[[PipelineState, NextAction], PipelineState]
        ] = {
            "create_session": self._do_create_session,
            "run_session": self._do_run_session,
            "finish_session": self._do_finish_session,
            "record_return": self._do_record_return,
            "export": self._do_export,
        }

    # ------------------------------------------------------------------
    # Публичный вход
    # ------------------------------------------------------------------

    @property
    def boundary_policies(self) -> Mapping[str, RoundBoundaryPolicy]:
        """Таблица политик по контурам, собранная при построении (§7.1).

        Публично — потому что пустота таблицы у вида `document` это
        наблюдаемое свойство СБОРКИ, а тест на неё не вправе лезть в
        приватные поля (§10 SPEC-002). Поведенческий тест «drive() ведёт
        себя как без политики» здесь недостаточен: он прошёл бы и у
        политики, всегда отвечающей `proceed`.

        Отдаётся неизменяемое представление, а не сам словарь: «собранная
        ПРИ ПОСТРОЕНИИ» — половина утверждения P10, и аксессор, сквозь
        который таблицу можно дописать после сборки, эту половину отменял бы.
        Тип возврата (`Mapping`) запрещает мутацию только на бумаге —
        `MappingProxyType` запрещает её на исполнении.
        """
        return MappingProxyType(self._boundary_policies)

    def run(self, slug: str, task_text: str) -> PipelineState:
        """Заводит новый пайплайн и крутит его до остановки (§3.1, §4.3).

        Порядок первых четырёх действий не переставляется:

        1. **предусловия** — они ничего не создают и не мутируют, поэтому
           идут раньше всего: анкер, созданный перед отказом по грязному
           дереву, остался бы мусором, блокирующим законный повтор;
        2. **пустой анкер** — первая сохраняемая мутация (§3.1). Крах сразу
           после неё оставляет пайплайн возобновимым; существующий файл
           отвергает старт (`O_EXCL`), а не переиспользуется — затирать
           накопленную доверенную историю нельзя ни при коллизии слага, ни
           при повторном запуске;
        3. **снапшоты** task/config/checklists, а следом — версионированное
           доказательство итоговой immutable-проекции (WS-disputatio-65
           BEH-01): все шаги до манифеста, потому что манифест несёт их
           sha256 и обязан описывать то, что уже лежит;
        4. **манифест** с входной фазой ВИДА, переходом `started` и первым
           intent'ом — дальше работает `advance`. Ни фаза, ни имя первого
           контура здесь не зашиты: они читаются из `ENTRY_PHASE` и
           `CONTOURS_BY_KIND`, потому что вид — свойство конфига (P0).

        Окно между созданием каталога и первой записью манифеста не
        восстановимо и не притворяется таковым: манифеста нет, `resume` его
        не найдёт, а `run` откажет по существующему каталогу. Честная цена
        четырёх файлов (task/config/checklists/semantic_proof), которые
        нельзя записать одной атомарной операцией.
        """
        check_run_preconditions(self._git, self._workspace_root, self._config, slug)

        anchor = IntegrityAnchor(self._config.anchor_path, self._workspace_root, slug)
        try:
            anchor.create_empty()
        except FileExistsError as exc:
            raise PipelineAlreadyExists(
                f"журнал целостности {anchor.path} уже существует: слаг "
                f"{slug!r} в этом репозитории уже запускался — продолжите "
                f"его через `disp pipeline resume --slug {slug}`, а не `run`"
            ) from exc

        directory = self._pipeline_dir(slug)
        directory.mkdir(parents=True, exist_ok=True)
        task = self._snapshot(directory, TASK_SNAPSHOT_NAME, task_text)
        config = self._snapshot(
            directory, CONFIG_SNAPSHOT_NAME, self._config_snapshot()
        )
        checklists = self._snapshot(
            directory, CHECKLISTS_SNAPSHOT_NAME, self._checklists_snapshot()
        )
        semantic_proof = write_semantic_proof(
            directory,
            pipeline_id=slug,
            config=self._config,
            config_ref=config,
            checklists_ref=checklists,
        )

        kind = self._config.kind
        first_contour = CONTOURS_BY_KIND[kind][0]
        entry = ENTRY_PHASE[kind]
        first = NextAction(
            operation_id=f"create-{revision_id(first_contour, 1)}",
            kind="create_session",
            args={"contour": first_contour, "revision": 1},
        )
        state = PipelineState(
            pipeline_id=slug,
            created_at=self._now(),
            phase=entry,
            task=task,
            config=config,
            checklists=checklists,
            semantic_proof=semantic_proof,
            documents=self._documents(),
            transitions=[
                Transition(
                    from_=PipelinePhase.IDLE,
                    to=entry,
                    reason=TransitionReason.STARTED,
                    at=self._now(),
                )
            ],
            budget_used=BudgetUsed(),
            anchor_id=slug,
            next_action=first,
        )
        self._write(
            state,
            self._event(state, PipelineEventType.PHASE_CHANGE, first.operation_id),
        )
        return self.advance(slug)

    def advance(self, slug: str) -> PipelineState:
        """Допроигрывает `next_action` до остановки пайплайна (§4.3).

        Единственный движок и холодного старта, и resume: `run` доходит до
        первой записи манифеста и передаёт управление сюда. Незарегистрированный
        `kind` — громкий отказ: `adopt_external`/`discard_round` принадлежат
        операторскому resume (§3.1, §8.1) и молча пропущенными быть не вправе.
        """
        state = self._store.load(slug)
        for _ in range(_MAX_INTENTS):
            action = state.next_action
            if action is None:
                return state
            handler = self._handlers.get(action.kind)
            if handler is None:
                raise NotImplementedError(
                    f"intent {action.kind!r} этим runner'ом не исполняется: "
                    "операторские решения (§3.1) принадлежат resume, и тихо "
                    "пропустить их значило бы потерять санкцию человека"
                )
            state = handler(state, action)
        raise AssertionError(
            f"пайплайн {slug!r} не остановился за {_MAX_INTENTS} интентов: "
            "цикл фаз конечен по построению, значит диспетчер зациклен"
        )

    def fail(
        self,
        slug: str,
        *,
        reason: TransitionReason = TransitionReason.SESSION_FAILED,
        evidence: Sequence[EvidenceLink] = (),
    ) -> PipelineState:
        """Переводит пайплайн в `FAILED` (P7, P8); повтор идемпотентен.

        Публичный вход, потому что уронить пайплайн вправе не только
        интерпретация сессии: сверка целостности P9 на resume (§8.1 шаг 0)
        приходит к тому же исходу с причиной `invariant_violation`.
        """
        return self._fail(self._store.load(slug), reason, evidence)

    def detect_parked(self, state: PipelineState) -> tuple[str, int] | None:
        """Активная ревизия, припаркованная дефектом, и её раунд (§8.1 шаг 2).

        Read-only по построению: только чтение `session.json` и `review.json`
        плюс вердикт той же политики, которой опрашивался `drive()`. Ни
        манифест, ни рабочее дерево не трогаются — §8.1 требует, чтобы
        обнаружение предшествовало сверке worktree, а значит не имело права
        мутировать ничего.
        """
        record = active_session(state)
        if record is None:
            return None
        contour, _ = split_revision(record.session_id)
        artifact_root = self._artifact_root(state.pipeline_id, record.session_id)
        session = self._session_state(artifact_root, record.session_id)
        if session is None:
            return None
        parked = self._parked_round(artifact_root, session, contour)
        return None if parked is None else (record.session_id, parked)

    # ------------------------------------------------------------------
    # Интенты §4.3
    # ------------------------------------------------------------------

    def _do_create_session(
        self, state: PipelineState, action: NextAction
    ) -> PipelineState:
        """Материализует ревизию: каталог, `session.json`, запись в манифесте.

        Три отдельные границы обрыва, и каждая закрыта своим приёмом: каталог
        создаётся `exist_ok`; фабрика **не** зовётся поверх durable
        `session.json` (иначе повтор затёр бы уже начатую сессию); запись в
        манифест добавляется, только если ревизии там ещё нет.

        Снапшоты верхнего уровня здесь не пишутся, а требуются: их байты
        зафиксированы `run` до первой записи манифеста, и хеши в манифесте
        описывают именно их. Переписать снапшот на повторе значило бы дать
        манифесту ссылаться на другие байты.
        """
        contour = str(action.args["contour"])
        revision = int(action.args["revision"])
        session_id = revision_id(contour, revision)
        self._require_snapshots(state)

        artifact_root = self._artifact_root(state.pipeline_id, session_id)
        artifact_root.mkdir(parents=True, exist_ok=True)
        if self._session_state(artifact_root, session_id) is None:
            base_commit = action.args.get("base_commit")
            self._session_factory(
                SessionCreation(
                    artifact_root=artifact_root,
                    session_id=session_id,
                    contour=contour,
                    revision=revision,
                    task_text=self._task_text(state),
                    findings=self._carried_findings(state, action.args),
                    base_commit=base_commit if isinstance(base_commit, str) else None,
                )
            )

        records = list(self._records(state, contour))
        if all(record.session_id != session_id for record in records):
            records.append(
                SessionRecord(
                    revision=revision,
                    session_id=session_id,
                    path=f"{SESSIONS_DIR_NAME}/{session_id}",
                    entry_hashes=self._entry_hashes(state),
                )
            )
        successor = NextAction(
            operation_id=f"run-{session_id}",
            kind="run_session",
            args={"session_id": session_id},
            predecessor_operation_id=action.operation_id,
        )
        return self._write(
            state.model_copy(
                update={
                    **self._records_update(contour, records),
                    "next_action": successor,
                }
            ),
            self._event(
                state,
                PipelineEventType.SESSION_STARTED,
                action.operation_id,
                session_id=session_id,
            ),
        )

    def _do_run_session(
        self, state: PipelineState, action: NextAction
    ) -> PipelineState:
        """Гонит сессию — если её durable-состояние ещё не финально.

        Проверка перед вызовом драйвера и есть идемпотентность шага: обрыв
        между возвратом драйвера и записью результата оставляет на диске
        терминальную (или припаркованную) сессию, и прогнать её второй раз
        значило бы переиграть уже сыгранные раунды.

        Политика границы раунда берётся ИЗ ТАБЛИЦЫ по контуру ревизии
        (§7.1): её выбрал composition root, и runner о видах не знает. У
        контура без записи (spec, doc) политики нет — сессия гонится до
        собственного терминала.
        """
        session_id = str(action.args["session_id"])
        contour, _ = split_revision(session_id)
        artifact_root = self._artifact_root(state.pipeline_id, session_id)
        session = self._session_state(artifact_root, session_id)
        if session is None or not self._is_settled(artifact_root, session, contour):
            self._session_driver(
                artifact_root, session_id, self._boundary_policies.get(contour)
            )
        successor = NextAction(
            operation_id=f"finish-{session_id}",
            kind="finish_session",
            args={"session_id": session_id},
            predecessor_operation_id=action.operation_id,
        )
        return self._write(state.model_copy(update={"next_action": successor}))

    def _do_finish_session(
        self, state: PipelineState, action: NextAction
    ) -> PipelineState:
        """Интерпретирует итог сессии и выбирает следующий шаг (§7.2).

        Интерпретация — чистая функция durable-состояния, поэтому обрыв между
        нею и записью outcome безопасен: replay читает тот же диск и приходит
        к тому же исходу. Записывается всё одной атомарной записью, так что
        «наполовину завершённой» сессии в манифесте не бывает.
        """
        session_id = str(action.args["session_id"])
        contour, revision = split_revision(session_id)
        artifact_root = self._artifact_root(state.pipeline_id, session_id)
        session = self._session_state(artifact_root, session_id)
        if session is None:
            return self._fail(state, TransitionReason.INVARIANT_VIOLATION)

        verdict = self._interpret(artifact_root, session, contour)
        if verdict.kind == "parked":
            return self._open_return(state, session_id, revision, verdict, action)
        if verdict.kind == "failed":
            return self._fail(
                state,
                verdict.reason or TransitionReason.SESSION_FAILED,
                updates=self._records_update(
                    contour,
                    with_session_fields(
                        self._records(state, contour),
                        session_id,
                        outcome=SessionOutcome.FAILED,
                    ),
                ),
                detail=verdict.detail,
            )

        finished = self._event(
            state,
            PipelineEventType.SESSION_FINISHED,
            action.operation_id,
            session_id=session_id,
            outcome=verdict.kind,
        )
        if verdict.kind == "escalated":
            assert verdict.reason is not None
            return self._escalate(
                state,
                verdict.reason,
                updates=self._records_update(
                    contour,
                    with_session_fields(
                        self._records(state, contour),
                        session_id,
                        outcome=SessionOutcome.ESCALATED,
                    ),
                ),
                extra_events=(finished,),
            )

        records_update = self._records_update(
            contour,
            with_session_fields(
                self._records(state, contour),
                session_id,
                outcome=SessionOutcome.CONVERGED,
            ),
        )
        if contour == TERMINAL_CONTOUR[state.kind]:
            return self._enter_export(
                state,
                records_update,
                finished,
                from_phase=state.phase,
                reason=_CONVERGED_REASON[state.kind],
            )
        return self._start_pair(state, revision, records_update, finished, action)

    def _do_record_return(
        self, state: PipelineState, action: NextAction
    ) -> PipelineState:
        """Возврат по архитектурному дефекту: cleanup + commit point (§7.3).

        Два шага, и оба идемпотентны. Cleanup — `reset --hard` к последнему
        принятому коммиту плюс `clean`, тот же механизм, каким SPEC-001 делает
        `PROPOSING` re-runnable (§7.3 шаг 3, `steps.propose`). Цель сброса —
        HEAD: принятые раунды закоммичены, а припаркованный не принят, поэтому
        повтор сброса к тому же коммиту — no-op; повтор уборки — тоже, убирать
        второй раз уже нечего.

        `clean` здесь не довесок к сбросу, а условие честности манифеста.
        `reset` untracked-файлы не видит (см. его контракт), поэтому новый
        документ, созданный припаркованной попыткой, пережил бы возврат — и
        `entry_hashes` преемника, снимаемые в `create_session` ДО запуска его
        сессии, зафиксировали бы SHA файла, которого на входе ревизии не было:
        собственные `reset`+`clean` преемника снесут его только позже, внутри
        `PROPOSING`. Манифест утверждал бы вход, которого автор не видел.

        Commit point — ровно ОДНА запись манифеста, несущая всё сразу: transition,
        `outcome` припаркованной pair-ревизии, `superseded_by` перекрытых
        ревизий и chained `create_session` преемника. До неё возврат не
        случился (повтор идёт с шага 2 по тому же `operation_id`), после —
        случился необратимо, и предшественник больше не исполняется.

        `superseded_by` обеих перекрытых ревизий называет **spec-ревизию**
        преемника, а не будущую pair-rN+1: §7.3 разрешает ссылаться на ещё не
        материализованную ревизию только пока висит её `create_session` либо
        после материализации, а висит здесь именно spec-rN+1. Сошедшаяся
        spec-rN сохраняет `outcome: converged` — перекрытие выражается только
        отношением (P3).
        """
        session_id = str(action.args["session_id"])
        round_no = int(action.args["round"])
        _, revision = split_revision(session_id)
        successor_id = revision_id(CONTOUR_SPEC, revision + 1)

        self._git.reset_hard(self._git.head_sha())
        self._git.clean()

        transition = Transition(
            from_=PipelinePhase.PAIR_LOOP,
            to=PipelinePhase.SPEC_LOOP,
            reason=TransitionReason.ARCHITECTURAL_DEFECT,
            evidence=self._return_evidence(state, session_id, round_no),
            at=self._now(),
        )
        successor = NextAction(
            operation_id=f"create-{successor_id}",
            kind="create_session",
            args={
                "contour": CONTOUR_SPEC,
                "revision": revision + 1,
                "findings_session_id": session_id,
                "findings_round": round_no,
            },
            predecessor_operation_id=action.operation_id,
        )
        return self._write(
            state.model_copy(
                update={
                    "phase": PipelinePhase.SPEC_LOOP,
                    "transitions": [*state.transitions, transition],
                    "spec_sessions": with_session_fields(
                        state.spec_sessions,
                        revision_id(CONTOUR_SPEC, revision),
                        superseded_by=successor_id,
                    ),
                    "pair_sessions": with_session_fields(
                        state.pair_sessions,
                        session_id,
                        outcome=SessionOutcome.ARCHITECTURAL_DEFECT,
                        superseded_by=successor_id,
                    ),
                    "next_action": successor,
                }
            ),
            self._event(
                state,
                PipelineEventType.RETURN_RECORDED,
                action.operation_id,
                session_id=session_id,
                round=round_no,
                successor=successor_id,
            ),
        )

    def _do_export(self, state: PipelineState, action: NextAction) -> PipelineState:
        """Зовёт экспортёр и закрывает пайплайн (§8.2, P7).

        Экспорт идемпотентен по контракту (`manifest.json` — commit marker),
        поэтому повтор после обрыва чинит частичный набор тем же кодовым
        путём. `remote_url` не подставляется: порт `GitOps` операции «узнать
        remote» не имеет, а придумать её значило бы выдать за факт догадку —
        §8.2 требует ровно обратного, и экспортёр в этом случае пишет
        параметризованный шаблон с предупреждением.
        """
        partial = bool(action.args.get("partial", False))
        self._exporter(
            state,
            workspace_root=self._workspace_root,
            remote_url=None,
            branch=self._git.current_branch(),
            partial=partial,
        )
        transition = Transition(
            from_=PipelinePhase.EXPORTING,
            to=PipelinePhase.DONE,
            reason=TransitionReason.EXPORTED,
            at=self._now(),
        )
        return self._write(
            state.model_copy(
                update={
                    "phase": PipelinePhase.DONE,
                    "transitions": [*state.transitions, transition],
                    "next_action": None,
                }
            ),
            self._event(
                state,
                PipelineEventType.EXPORTED,
                action.operation_id,
                partial=partial,
            ),
        )

    # ------------------------------------------------------------------
    # Маршруты после интерпретации (§7.2, §7.3)
    # ------------------------------------------------------------------

    def _start_pair(
        self,
        state: PipelineState,
        revision: int,
        records_update: dict[str, Any],
        finished: PipelineEvent,
        action: NextAction,
    ) -> PipelineState:
        """Спека сошлась → новая pair-ревизия того же номера (P5), либо soft-лимит.

        Soft-лимиты проверяются здесь, «между сессиями» (§7.2): сессия атомарна
        для бюджета пайплайна, и потому последняя вправе лимит превысить —
        имя `soft_` это честно фиксирует. Ветка возврата такой проверки не
        несёт сознательно: P6 объявляет архитектурную находку приоритетнее
        стоп-условий, а её собственный потолок — `max_architectural_returns`.
        """
        budget = self._recompute_budget(state)
        if self._soft_limit_hit(budget):
            return self._escalate(
                state,
                TransitionReason.PIPELINE_BUDGET_HIT,
                updates=records_update,
                extra_events=(finished,),
            )
        pair_id = revision_id(CONTOUR_PAIR, revision)
        transition = Transition(
            from_=PipelinePhase.SPEC_LOOP,
            to=PipelinePhase.PAIR_LOOP,
            reason=TransitionReason.SPEC_CONVERGED,
            at=self._now(),
        )
        successor = NextAction(
            operation_id=f"create-{pair_id}",
            kind="create_session",
            args={"contour": CONTOUR_PAIR, "revision": revision},
            predecessor_operation_id=action.operation_id,
        )
        return self._write(
            state.model_copy(
                update={
                    **records_update,
                    "phase": PipelinePhase.PAIR_LOOP,
                    "transitions": [*state.transitions, transition],
                    "next_action": successor,
                }
            ),
            finished,
            self._event(state, PipelineEventType.PHASE_CHANGE, successor.operation_id),
        )

    def _enter_export(
        self,
        state: PipelineState,
        records_update: dict[str, Any],
        finished: PipelineEvent,
        *,
        from_phase: PipelinePhase,
        reason: TransitionReason,
    ) -> PipelineState:
        """Терминальный контур вида сошёлся → `EXPORTING` (§7.2).

        Фаза и причина приходят параметрами, а не зашиты: у пары это
        `PAIR_LOOP → EXPORTING (pair_converged)`, у документного вида —
        `DOC_LOOP → EXPORTING (document_converged)`, и общая таблица §2
        различает эти рёбра по виду.
        """
        transition = Transition(
            from_=from_phase,
            to=PipelinePhase.EXPORTING,
            reason=reason,
            at=self._now(),
        )
        successor = NextAction(
            operation_id=f"export-{state.pipeline_id}",
            kind="export",
            args={"partial": False},
        )
        return self._write(
            state.model_copy(
                update={
                    **records_update,
                    "phase": PipelinePhase.EXPORTING,
                    "transitions": [*state.transitions, transition],
                    "next_action": successor,
                }
            ),
            finished,
            self._event(state, PipelineEventType.PHASE_CHANGE, successor.operation_id),
        )

    def _open_return(
        self,
        state: PipelineState,
        session_id: str,
        revision: int,
        verdict: _Interpretation,
        action: NextAction,
    ) -> PipelineState:
        """Записывает intent возврата — либо эскалирует по потолку возвратов.

        `outcome` припаркованной сессии здесь НЕ пишется: его пишет commit
        point (§7.3 шаг 4), и до него возврат не случился. Исключение —
        превышенный `max_architectural_returns`: возврата не будет вовсе,
        поэтому исход припаркованной сессии фиксируется сразу, а пайплайн
        уходит в честный частичный результат (§7.2, P7).
        """
        review_sha = self._review_sha256(state, session_id, verdict.round_no)
        if returns_exhausted(state, self._config.max_architectural_returns):
            return self._escalate(
                state,
                TransitionReason.MAX_ARCHITECTURAL_RETURNS,
                updates=self._records_update(
                    CONTOUR_PAIR,
                    with_session_fields(
                        state.pair_sessions,
                        session_id,
                        outcome=SessionOutcome.ARCHITECTURAL_DEFECT,
                    ),
                ),
                evidence=self._return_evidence(state, session_id, verdict.round_no),
            )
        successor = NextAction(
            operation_id=return_operation_id(session_id, verdict.round_no, review_sha),
            kind="record_return",
            args={
                "session_id": session_id,
                "round": verdict.round_no,
                "revision": revision,
            },
            predecessor_operation_id=action.operation_id,
        )
        return self._write(state.model_copy(update={"next_action": successor}))

    def _escalate(
        self,
        state: PipelineState,
        reason: TransitionReason,
        *,
        updates: Mapping[str, Any] | None = None,
        evidence: Sequence[EvidenceLink] = (),
        extra_events: Sequence[PipelineEvent] = (),
    ) -> PipelineState:
        """`→ ESCALATED → EXPORTING(partial)` одной записью (P7).

        Оба перехода ложатся вместе намеренно: `ESCALATED` без немедленного
        интента экспорта был бы состоянием, из которого пайплайн сам не
        выходит, а P7 требует честного частичного результата, а не остановки.
        """
        escalation = escalation_update(
            state, reason, evidence=evidence, moment=self._now()
        )
        successor_id = escalation["next_action"].operation_id
        return self._write(
            state.model_copy(
                update={**(dict(updates) if updates else {}), **escalation}
            ),
            *extra_events,
            self._event(state, PipelineEventType.PHASE_CHANGE, successor_id),
        )

    def _fail(
        self,
        state: PipelineState,
        reason: TransitionReason,
        evidence: Sequence[EvidenceLink] = (),
        *,
        updates: Mapping[str, Any] | None = None,
        detail: str = "",
    ) -> PipelineState:
        """`FAILED` без экспорта (P7); повтор не добавляет transition (P8).

        Из `DONE` рёбер нет вовсе (§2): ретроактивно признать завершённый
        результат failed значило бы править историю, и попытка отвергается
        громко.

        `detail` уходит только в событие: `reason` манифеста — закрытый enum
        §2, и расширять его свободным текстом нельзя, а диагностика, по
        которой человек поймёт, ЧТО именно разошлось, обязана где-то быть.
        """
        if state.phase is PipelinePhase.FAILED:
            return state
        if state.phase is PipelinePhase.DONE:
            raise ValueError(
                f"пайплайн {state.pipeline_id!r} уже DONE: переходов из DONE "
                "в таблице §2 нет — завершённый результат не переписывается"
            )
        transition = Transition(
            from_=state.phase,
            to=PipelinePhase.FAILED,
            reason=reason,
            evidence=list(evidence),
            at=self._now(),
        )
        return self._write(
            state.model_copy(
                update={
                    **(dict(updates) if updates else {}),
                    "phase": PipelinePhase.FAILED,
                    "transitions": [*state.transitions, transition],
                    "next_action": None,
                }
            ),
            self._event(
                state,
                PipelineEventType.ERROR,
                f"fail-{state.pipeline_id}-{len(state.transitions)}",
                reason=reason.value,
                detail=detail,
            ),
        )

    # ------------------------------------------------------------------
    # Интерпретация durable-состояния (§7.2)
    # ------------------------------------------------------------------

    def _interpret(
        self, artifact_root: Path, session: SessionState, contour: str
    ) -> _Interpretation:
        """Итог сессии по `session.json` + `decision.json` последнего раунда.

        Парковка проверяется ПЕРВОЙ — в этом и состоит исполнение P6: пока
        припаркованный раунд не опознан, любое стоп-условие сессии выглядело
        бы законным исходом, и дефектная спека ушла бы в эскалацию вместо
        переработки.

        Две причины `FAILED` различаются, потому что §2 их различает:
        `session_failed` — сессия сама пришла в свой терминал отказа;
        `invariant_violation` — durable-состояние противоречит контракту
        (нетерминальная сессия без парковки, `DONE` без решения, решение с
        нетерминальным исходом). Свести их к одной значило бы отчитаться о
        сломанном рантайме как о честно упавшей сессии.
        """
        parked = self._parked_round(artifact_root, session, contour)
        if parked is not None:
            return _Interpretation(kind="parked", round_no=parked)
        if session.state is SessionPhase.FAILED:
            return _Interpretation(
                kind="failed", reason=TransitionReason.SESSION_FAILED
            )
        if session.state is not SessionPhase.DONE:
            return _broken("драйвер вернул нетерминальную непарковку")
        decision = load_decision(artifact_root, session.current_round)
        if decision is None:
            return _broken("сессия DONE, а решения последнего раунда нет")
        if decision.outcome is Outcome.CONVERGED:
            return _Interpretation(kind="converged")
        if decision.outcome is Outcome.DEADLOCK:
            return _Interpretation(
                kind="escalated", reason=TransitionReason.SESSION_DEADLOCK
            )
        if decision.outcome is Outcome.BUDGET_HIT:
            return _Interpretation(
                kind="escalated", reason=TransitionReason.SESSION_BUDGET_HIT
            )
        if decision.outcome is Outcome.FAILED:
            return _Interpretation(
                kind="failed", reason=TransitionReason.SESSION_FAILED
            )
        return _broken(f"решение DONE-сессии несёт исход {decision.outcome.value}")

    def _parked_round(
        self, artifact_root: Path, session: SessionState, contour: str
    ) -> int | None:
        """Номер припаркованного раунда либо `None` (§7.1, §8.1 шаг 2).

        Признак парковки — не фаза сессии, а сочетание трёх durable-фактов:
        ревью раунда записано, решения по нему нет, и политика на этом ревью
        даёт `PARK`. Двух первых мало: обрыв процесса сразу после записи
        `review.json` даёт ровно ту же пару, и принять его за парковку значило
        бы объявить архитектурный дефект там, где его никто не находил.
        Третий факт — та же самая политика, которой опрашивался `drive()`:
        она берётся из ОДНОЙ таблицы, а не создаётся здесь заново. Второй
        экземпляр развёл бы два источника истины — подменённая реализация
        паркует по своему условию, а обнаружение её парковку не признаёт, и
        resume продолжил бы сессию, которую надлежит вернуть.
        """
        policy = self._boundary_policies.get(contour)
        if policy is None:
            # У контура без политики парковки не бывает: парковать нечем, и
            # «не припарковано» здесь единственный правдивый ответ. Это не
            # оптимизация — у вида `document` таблица пуста целиком (P10).
            return None
        if session.state in TERMINAL_PHASES:
            return None
        round_no = session.current_round
        if round_no < 1:
            return None
        review = load_review(artifact_root, round_no)
        if review is None:
            return None
        if load_decision(artifact_root, round_no) is not None:
            return None
        if policy.after_deciding(review) is not BoundaryVerdict.PARK:
            return None
        return round_no

    def _is_settled(
        self, artifact_root: Path, session: SessionState, contour: str
    ) -> bool:
        """Сессия дальше не двигается: терминал либо парковка (§7.2)."""
        if session.state in TERMINAL_PHASES:
            return True
        return self._parked_round(artifact_root, session, contour) is not None

    # ------------------------------------------------------------------
    # Запись манифеста и бюджет (§4.2)
    # ------------------------------------------------------------------

    def _write(self, state: PipelineState, *events: PipelineEvent) -> PipelineState:
        """Единственная точка записи манифеста: пересчёт бюджета → save → события.

        Пересчёт живёт здесь, а не в вызывающих: §4.2 требует его при КАЖДОЙ
        записи, и распределённый по девяти местам инкремент разошёлся бы с
        истиной ровно на повторе после краха. События идут после `save` и
        best-effort (P8): манифест — единственный source of truth, журнал —
        производный поток, и потерянное событие истину не портит.
        """
        persisted = state.model_copy(
            update={"budget_used": self._recompute_budget(state)}
        )
        self._store.save(persisted)
        for event in events:
            self._sink.emit(event)
        return persisted

    def _recompute_budget(self, state: PipelineState) -> BudgetUsed:
        """Пересчёт бюджета по диску (§4.2) — общий с операторскими решениями."""
        return recompute_budget(self._pipeline_dir(state.pipeline_id), state)

    def _soft_limit_hit(self, budget: BudgetUsed) -> bool:
        """Достигнут ли soft-лимит пайплайна; ноль означает «лимита нет» (§3.2)."""
        tokens_limit = self._config.soft_max_pipeline_tokens
        wall_limit = self._config.soft_max_pipeline_wall_seconds
        if tokens_limit and budget.tokens >= tokens_limit:
            return True
        return bool(wall_limit and budget.wall_seconds >= wall_limit)

    def _event(
        self,
        state: PipelineState,
        event_type: PipelineEventType,
        operation_id: str,
        **payload: Any,
    ) -> PipelineEvent:
        """Событие пайплайна с обязательным ключом дедупликации (P8)."""
        return PipelineEvent(
            ts=self._now(),
            pipeline=state.pipeline_id,
            type=event_type,
            payload={"operation_id": operation_id, **payload},
        )

    # ------------------------------------------------------------------
    # Пути, снапшоты, чтение артефактов
    # ------------------------------------------------------------------

    def _pipeline_dir(self, slug: str) -> Path:
        """`.disputatio/pipelines/<slug>` (§4.1).

        Считается в `runtime`, а не импортируется из `events.pipeline_paths`:
        тот модуль — внутренняя деталь раскладки и наружу пакетом не
        экспортируется, а оба сегмента пути `runtime` уже знает (тот же приём
        применён в `pipeline_export._result_dir`).
        """
        return pipeline_dir_of(self._workspace_root, slug)

    def _artifact_root(self, slug: str, session_id: str) -> Path:
        """`artifact_root` одной ревизии: `sessions/<revision>` (§4.1)."""
        return artifact_root_of(self._workspace_root, slug, session_id)

    def _snapshot(self, directory: Path, name: str, text: str) -> FileRef:
        """Пишет снапшот и возвращает ссылку на него для манифеста (§4.2)."""
        data = text.encode("utf-8")
        atomic_write(directory / name, data)
        return FileRef(path=name, sha256=hashlib.sha256(data).hexdigest())

    def _require_snapshots(self, state: PipelineState) -> None:
        """Fail-closed: снапшоты, на которые ссылается манифест, обязаны быть.

        `semantic_proof` — тем же приёмом, но опционально: манифесты,
        записанные до BEH-01 (issue #65), легитимно несут `None`, и требовать
        файл, на который они не ссылаются, значило бы придумать отсутствующую
        ссылку задним числом.
        """
        directory = self._pipeline_dir(state.pipeline_id)
        refs = (state.task, state.config, state.checklists, state.semantic_proof)
        for ref in refs:
            if ref is not None and not (directory / ref.path).is_file():
                raise FileNotFoundError(
                    f"снапшот {ref.path} каталога пайплайна {directory} "
                    "отсутствует, а манифест ссылается на его sha256: "
                    "восстановить его нечем — это потеря данных, а не "
                    "состояние, которое допроигрывается"
                )

    def _documents(self) -> Documents:
        """Артефактная форма документов вида (§4.2).

        Ветвление здесь, а не в схеме: манифест обязан делать чужую форму
        невыразимой, и union это уже обеспечивает. Задача метода — выбрать
        ветку по РАЗОБРАННОМУ конфигу, чья форма установлена fail-closed
        разбором `_resolve_kind` (§3.2).
        """
        if self._config.kind is PipelineKind.DOCUMENT:
            (document_path,) = self._config.documents()
            return SingleDocument(kind="document", document_path=document_path)
        spec_path, plan_path = self._config.documents()
        return PairDocuments(spec_path=spec_path, plan_path=plan_path)

    def _config_snapshot(self) -> str:
        """Детерминированный TOML секции `[pipeline]` (§4.1 `config.toml`).

        `anchor_path` в снапшот не входит намеренно: он машинно-зависим, а
        §8.1 прямо говорит, что для резолва анкера снапшот в каталоге
        пайплайна не годится — он лежит в недоступном доверию дереве.
        Включить его значило бы сделать хеш снапшота (и через него манифест)
        машинно-зависимым ради значения, которым всё равно нельзя пользоваться.
        """
        branches = ", ".join(
            _toml_string(branch) for branch in self._config.protected_branches
        )
        wall = self._config.soft_max_pipeline_wall_seconds
        lines = ["[pipeline]", *self._document_lines()]
        if self._config.kind is PipelineKind.PAIR:
            # `max_architectural_returns` принадлежит форме пары и у вида
            # document отвергается загрузкой (§3.2). Написать его в снапшот
            # значило бы удостоверить настройку, которой у пайплайна нет.
            lines.append(
                f"max_architectural_returns = {self._config.max_architectural_returns}"
            )
        lines += [
            f"soft_max_pipeline_tokens = {self._config.soft_max_pipeline_tokens}",
            f"soft_max_pipeline_wall_seconds = {wall}",
            f"protected_branches = [{branches}]",
        ]
        for gate in self._config.extra_gates:
            lines += [
                "",
                "[[pipeline.gates]]",
                f"name = {_toml_string(gate.name)}",
                f"cmd = {_toml_string(gate.cmd)}",
                f"enabled = {str(gate.enabled).lower()}",
            ]
        return "\n".join(lines) + "\n"

    def _document_lines(self) -> list[str]:
        """Строки путей секции `[pipeline]` снапшота — по форме вида (§3.2)."""
        if self._config.kind is PipelineKind.DOCUMENT:
            (document_path,) = self._config.documents()
            return [f"document_path = {_toml_string(document_path)}"]
        spec_path, plan_path = self._config.documents()
        return [
            f"spec_path = {_toml_string(spec_path)}",
            f"plan_path = {_toml_string(plan_path)}",
        ]

    def _checklists_snapshot(self) -> str:
        """Детерминированный TOML действующих чеклистов (§5.3).

        Сортировка по контуру — не косметика: манифест хранит sha256 этих
        байтов, и порядок словаря, зависящий от порядка ключей конфига,
        давал бы разный хеш для одного и того же чеклиста.

        Внутри контура правило разное, и это тоже не косметика. У встроенных
        состав фиксирован вендоренным набором, поэтому пункты сортируются по
        id — сортировка защищает хеш от случайного порядка ключей конфига.
        У операторского контура `doc` так делать НЕЛЬЗЯ: порядок объявления
        входит в identity чеклиста, и отсортированный снапшот его бы потерял.

        **Известное ограничение (issue #65):** снапшот пишется честно, но
        `resume` его не читает — он сверяет с манифестом только вид (P0), а
        чеклист и пути документов берёт из живого конфига. Значит промпт
        воспроизводим ровно до тех пор, пока конфиг между запусками не
        менялся, и снапшот сегодня — доказательство того, ЧТО было объявлено
        при `run`, а не то, чем `resume` пользуется. Ограничение общее для
        обоих видов пайплайна.

        Назначенный `findings_item` несут ВСЕ контуры: роль — часть критерия,
        и вопрос «по какому пункту судил V8 в этом прогоне» обязан иметь
        ответ из артефакта, а не из версии кода. Пустая роль пишется явным
        `false`, а не пропуском строки: пропуск неотличим от потери.
        """
        lines: list[str] = []
        for contour in sorted(self._config.checklists):
            checklist = self._config.checklists[contour]
            lines.append(f"[{contour}]")
            role = checklist.findings_item
            lines.append(
                f"findings_item = {_toml_string(role)}"
                if role is not None
                else "findings_item = false"
            )
            order = (
                checklist.order
                if contour == CONTOUR_DOC
                else tuple(sorted(checklist.order))
            )
            for item_id in order:
                lines.append(f"{item_id} = {_toml_string(checklist.texts[item_id])}")
            lines.append("")
        return "\n".join(lines)

    def _task_text(self, state: PipelineState) -> str:
        """Текст задачи из снапшота — не из аргумента `run` (§4.1).

        Источник истины один и durable: после краха `advance` поднимает
        пайплайн без `task_text` на руках, и сессия обязана получить ровно тот
        текст, чей хеш записан в манифесте.
        """
        return (self._pipeline_dir(state.pipeline_id) / state.task.path).read_text(
            encoding="utf-8"
        )

    def _entry_hashes(self, state: PipelineState) -> dict[str, str]:
        """Состояние документов вида на входе сессии (§4.2 `entry_hashes`).

        Отсутствующий файл — явный маркер `absent`, а не пропуск ключа: план
        законно отсутствует в spec-r1 (и так же законно отсутствует ещё не
        написанный документ в doc-r1), и молчание не отличалось бы от «файл
        был, но мы его не прочли».

        Состав документов спрашивается у самой формы (`documents.paths()`),
        а не выписывается парой полей: у ветки `SingleDocument` их нет.
        """
        hashes: dict[str, str] = {}
        for relative in state.documents.paths():
            path = self._resolve_document(relative)
            hashes[relative] = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else "absent"
            )
        return hashes

    def _resolve_document(self, relative: str) -> Path:
        """Путь документа внутри рабочего дерева — с проверкой containment (§6).

        Лексическую половину закрывает схема: `..`, абсолютная и
        неканоническая формы в манифест не попадают
        (`contracts.validate_relative_path`). Остаётся то, чего в тексте
        пути не видно, — символическая ссылка, ведущая наружу: `docs/spec.md`
        безупречен, а `docs` может указывать куда угодно. Резолвер тот же,
        которым doc-гейты проверяют containment ссылок
        (`verifier.doc_gates.resolve_inside`): containment — одно правило
        репозитория, и второй его реализации здесь заводить нечем.

        Отказ — `ConfigError`, как и у `validate_anchor_path` на нарушении
        containment анкера: диагноз человеку один и тот же — путь ведёт не
        туда, куда ему положено, и правится он снаружи оркестратора.
        """
        resolved = resolve_inside(self._workspace_root, relative)
        if resolved is None:
            raise ConfigError(
                f"документ {relative} резолвится за пределы репозитория "
                f"({self._workspace_root}) — вероятно, symlink в пути ведёт "
                "наружу; пайплайн ведёт пару документов внутри репозитория и "
                "не читает файлы за его границей (§6)"
            )
        return resolved

    def _carried_findings(
        self, state: PipelineState, args: Mapping[str, Any]
    ) -> tuple[Issue, ...]:
        """Архитектурные находки для spec-ревизии, открытой возвратом (§7.3).

        Читаются с диска по ссылке из intent'а, а не переносятся в аргументах:
        replay после краха обязан дать те же находки, а единственный durable
        их источник — `review.json` припаркованного раунда. Все остальные
        ревизии получают пустой набор — в том числе pair-rN+1, который
        перепроверяет пару целиком и без унаследованного (P5).
        """
        session_id = args.get("findings_session_id")
        if not isinstance(session_id, str):
            return ()
        round_no = int(args["findings_round"])
        return architectural_findings(self._round_review(state, session_id, round_no))

    def _round_review(
        self, state: PipelineState, session_id: str, round_no: int
    ) -> Review:
        """`review.json` раунда сессии; его отсутствие — сломанный инвариант."""
        artifact_root = self._artifact_root(state.pipeline_id, session_id)
        review = load_review(artifact_root, round_no)
        if review is None:
            raise AssertionError(
                f"нет review.json раунда {round_no:03d} сессии {session_id!r}: "
                "возврат строится от durable-артефакта ревью (§7.3 шаг 1), и "
                "без него identity checkpoint'а не существует"
            )
        return review

    def _review_sha256(
        self, state: PipelineState, session_id: str, round_no: int
    ) -> str:
        """sha256 БАЙТОВ `review.json` — третий вход identity (§7.3 шаг 1).

        Считается по файлу, а не по перенормализованной модели: identity
        обязана опираться на то, что записано, и повторная сериализация могла
        бы дать другие байты при том же смысле.
        """
        path = (
            round_dir(self._artifact_root(state.pipeline_id, session_id), round_no)
            / REVIEW_NAME
        )
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _return_evidence(
        self, state: PipelineState, session_id: str, round_no: int
    ) -> list[EvidenceLink]:
        """Ссылки на архитектурные находки раунда (§4.2 `transitions[].evidence`)."""
        return [
            EvidenceLink(session_id=session_id, round=round_no, finding_id=issue.id)
            for issue in architectural_findings(
                self._round_review(state, session_id, round_no)
            )
        ]

    def _session_state(
        self, artifact_root: Path, session_id: str
    ) -> SessionState | None:
        """`session.json` ревизии либо `None`, если её ещё нет на диске."""
        return load_session_state(artifact_root, session_id)

    @staticmethod
    def _records(state: PipelineState, contour: str) -> Sequence[SessionRecord]:
        """Список ревизий нужного контура (§4.2).

        Имя коллекции берётся из `SESSIONS_FIELD_BY_CONTOUR` — единственного
        места, где оно записано. Тернарник «spec или pair» третий контур
        молча отправил бы в чужую коллекцию.
        """
        records: Sequence[SessionRecord] = getattr(
            state, SESSIONS_FIELD_BY_CONTOUR[contour]
        )
        return records

    @staticmethod
    def _records_update(
        contour: str, records: Sequence[SessionRecord]
    ) -> dict[str, Any]:
        """`model_copy(update=…)` для списка ревизий нужного контура."""
        return {SESSIONS_FIELD_BY_CONTOUR[contour]: list(records)}


def _broken(what: str) -> _Interpretation:
    """Durable-состояние противоречит контракту → `FAILED (invariant_violation)`.

    Текст причины в манифест не попадает (`reason` — закрытый enum §2), но
    остаётся в коде единственным местом, где перечислено, ЧТО именно считается
    нарушением: три случая, и все три — про рантайм, а не про пользователя.
    """
    return _Interpretation(
        kind="failed", reason=TransitionReason.INVARIANT_VIOLATION, detail=what
    )


def escalation_update(
    state: PipelineState,
    reason: TransitionReason,
    *,
    evidence: Sequence[EvidenceLink] = (),
    moment: datetime,
) -> dict[str, Any]:
    """Обновление манифеста «`→ ESCALATED → EXPORTING(partial)`» (§7.2, P7).

    Оба перехода ложатся вместе намеренно: `ESCALATED` без немедленного
    интента экспорта был бы состоянием, из которого пайплайн сам не выходит,
    а P7 требует честного частичного результата, а не остановки.

    Функция, а не метод runner'а, потому что эскалировать вправе оба
    исполнителя интентов: runner — по исходу сессии и лимитам, а
    `PipelineAdopt` — по исчерпанному потолку возвратов на операторском
    пути (§3.1). Вторая копия этой пары переходов разошлась бы с первой
    ровно в том, что читатель манифеста заметит последним.
    """
    return {
        "phase": PipelinePhase.EXPORTING,
        "transitions": [
            *state.transitions,
            Transition(
                from_=state.phase,
                to=PipelinePhase.ESCALATED,
                reason=reason,
                evidence=list(evidence),
                at=moment,
            ),
            Transition(
                from_=PipelinePhase.ESCALATED,
                to=PipelinePhase.EXPORTING,
                reason=TransitionReason.EXPORT_PARTIAL,
                at=moment,
            ),
        ],
        "next_action": NextAction(
            operation_id=f"export-partial-{state.pipeline_id}",
            kind="export",
            args={"partial": True},
        ),
    }


def architectural_returns_done(state: PipelineState) -> int:
    """Сколько возвратов `PAIR_LOOP → SPEC_LOOP` уже совершено (§7.2).

    Считается **ребро**, а не причина. §7.2 определяет `max_architectural_
    returns` как «число возвратов `PAIR_LOOP → SPEC_LOOP`», и по этому ребру
    ходят две причины: `architectural_defect` (находка ревьюера) и
    `external_spec_adopt` (правка спеки, принятая оператором, §3.1). Пока
    счётчик смотрел на первую, второй возврат был ему невидим — лимит
    обходился бесплатно, причём тем самым путём, где решение принимает
    человек и оглядка на лимит нужнее всего.

    `spec-r1` в счёт не идёт по построению: первый вход в spec-контур — это
    ребро `IDLE → SPEC_LOOP`, а не возврат.
    """
    return sum(
        1
        for transition in state.transitions
        if transition.from_ is PipelinePhase.PAIR_LOOP
        and transition.to is PipelinePhase.SPEC_LOOP
    )


def returns_exhausted(state: PipelineState, limit: int) -> bool:
    """Исчерпан ли потолок возвратов §7.2 — общий предикат обоих путей.

    Оба пути к ребру `PAIR_LOOP → SPEC_LOOP` обязаны спрашивать одно и то
    же: runner — перед интентом возврата по находке (`_open_return`),
    `PipelineAdopt` — перед маршрутизацией операторской правки. Второй
    экземпляр этого условия разошёлся бы с первым молча.
    """
    return architectural_returns_done(state) >= limit


def with_session_fields(
    records: Iterable[SessionRecord], session_id: str, **fields: Any
) -> list[SessionRecord]:
    """Заполняет поля одной записи сессии, остальные оставляя как есть.

    Заполняемые позже поля — ровно `outcome` и `superseded_by` (§4.2);
    prefix-equality остальных проверяет хранилище, и обойти его этой функцией
    нельзя: она копирует записи, а не пересобирает их.

    Неизвестный `session_id` — `ValueError`, а не список без изменений.
    Функция пишет ровно те два факта, потерю которых по построению нечем
    заметить: `superseded_by` — единственное выражение перекрытия ревизии
    (P3), и пока оно не записано, §8.1 шаг 1 считает сессию возобновляемой;
    `outcome` — единственный признак закрытой сессии там же. Молчаливый
    no-op отдавал бы вызывающей стороне список, выглядящий обновлённым, и
    факт исчезал бы без следа в том самом месте, где его пишут.
    """
    updated = [
        record.model_copy(update=fields) if record.session_id == session_id else record
        for record in records
    ]
    known = [record.session_id for record in updated]
    if session_id not in known:
        raise ValueError(
            f"сессии {session_id!r} нет среди записей контура ({known}): "
            f"поля {sorted(fields)} записывать некуда, а молча вернуть "
            "список без изменений значило бы потерять перекрытие/исход "
            "(§4.2, P3)"
        )
    return updated


def _toml_string(value: str) -> str:
    """TOML basic string. `json.dumps` даёт совместимое экранирование."""
    return json.dumps(value, ensure_ascii=False)
