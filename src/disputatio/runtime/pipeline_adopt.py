"""Операторские решения `--adopt-external` / `--discard-round` (SPEC-002 §3.1).

Оба решения — это интенты §4.3 с полным write-ahead протоколом, и без него
падение посреди многошаговой мутации было бы неразрешимо: санкция человека
durable раньше первой мутации, исполнение идемпотентно по `operation_id`,
commit point — одна атомарная запись манифеста.

**Scope принимаемого дифа fail-closed.** Из полного `git status` вычитается
ровно одно — собственные untracked-файлы пайплайна под `.disputatio/`: он
порождает их непрерывно (§4.1), и буквальный полный status отвергал бы каждый
adoption по собственному журналу. Исключение узкое: tracked-изменённый путь
под тем же каталогом adoption отклоняет — это как раз внешняя правка control
plane, которую fail-closed норма обязана поймать, а не спрятать. В остатке
допустимы только `spec_path`/`plan_path`; любой иной путь — tracked или
untracked — отклоняет решение целиком, потому что посторонний untracked не
должен переживать adoption молча (а `commit_paths` его и не заберёт).

**Пути статуса приходят от TOPLEVEL, а не от корня пайплайна.** Наивный
фильтр `path.startswith(".disputatio/")` промахнулся бы в обе стороны у
пайплайна в подкаталоге чужого репозитория: собственный журнал выглядел бы
посторонним, а сам документ пары — не совпал бы с `spec_path`. Обе стороны
приводятся к общей базе через `GitOps.toplevel_prefix()` — тем же приёмом,
что и предусловия `run` (`pipeline_config`).

**Маршрут определяют пути дифа, а не только классификация ревьюера.** Правка
`spec_path` в pair-контуре ведёт в spec-ревизию даже без architectural
finding: спека, изменившаяся после своей сходимости, обязана пройти контур
заново. P6 действует поверх — обнаруженный дефект ведёт в spec и при чистом
plan-дифе. Причина перехода при обеих причинах сразу — `external_spec_adopt`,
а архитектурные находки уходят в evidence того же перехода.

**Потолок возвратов §7.2 проверяется и здесь.** Лимит
`max_architectural_returns` назначен РЕБРУ `PAIR_LOOP → SPEC_LOOP`, а по
этому ребру ходят обе причины — и находка ревьюера, и принятая правка
спеки. Операторский путь спрашивал бы лимит вторым голосом, если бы
спрашивал его вовсе: пока он молчал, возврат оператора был бесплатным и
невидимым счётчику runner'а. Исчерпанный потолок перекрывает обе причины
(`returns_exhausted`, общий предикат с runner'ом): ревизии-преемника не
будет, commit point уводит пайплайн в `ESCALATED → EXPORTING(partial)` —
дословно §7.2. Правку человека это не теряет: чекпоинт сделан, решение
записано, `outcome` припаркованной сессии — `abandoned`.

**Commit point один и единственный пишет `outcome`.** `record_return` в
adoption-пути не участвует вовсе: он определён исключительно для настоящего
architectural finding и никогда не перезаписывает уже записанный `abandoned`
(P3). Поэтому commit point сразу заменяет интент решения chained-преемником
`create_session` — с `base_commit` операторского чекпоинта, чтобы первый
`PROPOSING` новой ревизии сбрасывал дерево к нему, а не к состоянию до
правки.
"""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from disputatio.contracts import (
    EvidenceLink,
    NextAction,
    OperatorDecision,
    PipelinePhase,
    PipelineState,
    PipelineStateStore,
    SessionOutcome,
    SessionRecord,
    Transition,
    TransitionReason,
)
from disputatio.events import PipelineEvent, PipelineEventType, atomic_write
from disputatio.runtime.errors import AdoptionScopeError, PipelineNotResumable
from disputatio.runtime.git import SESSION_DIR_NAME, GitOps
from disputatio.runtime.history import load_review
from disputatio.runtime.pipeline_config import PipelineConfig
from disputatio.runtime.pipeline_runner import (
    CONTOUR_PAIR,
    CONTOUR_SPEC,
    PipelineSink,
    active_session,
    architectural_findings,
    artifact_root_of,
    escalation_update,
    load_session_state,
    pipeline_dir_of,
    recompute_budget,
    returns_exhausted,
    revision_id,
    split_revision,
    with_session_fields,
)

#: `kind` операторских интентов (§4.3). Runner их намеренно не исполняет:
#: санкция человека принадлежит resume, и тихо пропущенной быть не вправе.
ADOPT_KIND: Final = "adopt_external"
DISCARD_KIND: Final = "discard_round"
OPERATOR_KINDS: Final = frozenset({ADOPT_KIND, DISCARD_KIND})

#: Каталог канонических патчей принятых правок (§4.1).
ADOPTIONS_DIR_NAME: Final = "adoptions"

#: Заголовок операторского чекпоинта (§3.1); идентичность операции — в
#: трейлере, потому что заголовок одинаков у всех adoption'ов пайплайна.
CHECKPOINT_SUBJECT: Final = "disputatio: operator adopt {slug}"


@dataclass(frozen=True, slots=True)
class AdoptionScope:
    """Что именно принимает решение оператора: затронутые документы контура.

    Только пути. «Затронута ли спека» — факт вида `pair`, и живёт он там,
    где принимается решение о маршруте: держать его здесь значило бы нести
    механику пары внутри общей структуры, которой пользуется и вид
    `document` (P10).
    """

    paths: tuple[str, ...]


def compute_scope(git: GitOps, *, allowed_paths: Sequence[str]) -> AdoptionScope:
    """Fail-closed область принимаемого дифа (§3.1); нарушение — отказ целиком.

    Разрешённые пути приходят параметром, а не выводятся из конфига здесь:
    их состав — свойство КОНТУРА (spec-контур допускает только спеку;
    pair-контур — оба документа, потому что правка спеки после её
    сходимости обязана вернуть пайплайн в spec-ревизию; контур `doc` —
    единственный документ), и знание о формах живёт в конфиге, а не
    размазано по исполнителю решений.
    """
    prefix = git.toplevel_prefix()
    control_prefix = f"{prefix}{SESSION_DIR_NAME}/"
    allowed = {f"{prefix}{path}": path for path in allowed_paths}

    touched: list[str] = []
    foreign: list[str] = []
    for entry in git.status_entries():
        if entry.path.startswith(control_prefix):
            if entry.tracked:
                foreign.append(entry.path)
            continue
        document = allowed.get(entry.path)
        if document is None:
            foreign.append(entry.path)
        elif document not in touched:
            touched.append(document)

    if foreign:
        listing = "\n".join(f"  - {path}" for path in sorted(foreign))
        raise AdoptionScopeError(
            "`--adopt-external` отклонён: диф выходит за пару документов "
            f"пайплайна ({', '.join(sorted(allowed.values()))}) — такие правки "
            "пайплайн не принимает, они разбираются вне его:\n"
            f"{listing}"
        )
    if not touched:
        raise AdoptionScopeError(
            "`--adopt-external` отклонён: в дифе нет ни одного документа пары "
            "— принимать как внешнюю правку нечего"
        )
    return AdoptionScope(paths=tuple(sorted(touched)))


class OperatorIntents:
    """Исполнитель интентов §3.1: write-ahead, идемпотентность, commit point.

    Живёт отдельно от `PipelineRunner`, потому что исполняет решения
    ЧЕЛОВЕКА: runner встречает `adopt_external`/`discard_round` громким
    отказом, и это правильно — тихо исполнить их изнутри цикла значило бы
    потерять место, где санкция вошла в систему.
    """

    def __init__(
        self,
        *,
        store: PipelineStateStore,
        sink: PipelineSink,
        git: GitOps,
        config: PipelineConfig,
        workspace_root: Path,
        now: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._sink = sink
        self._git = git
        self._config = config
        self._workspace_root = workspace_root
        self._now = now

    # ------------------------------------------------------------------
    # Открытие интента (write-ahead §4.3)
    # ------------------------------------------------------------------

    def adopt(
        self,
        state: PipelineState,
        *,
        diff: str,
        parked: tuple[str, int] | None,
    ) -> PipelineState:
        """Принимает внешнюю правку: scope → intent → исполнение (§3.1).

        Scope считается ПЕРВЫМ и ничего не мутирует: отказ обязан застать
        дерево ровно таким, каким его оставил человек.
        """
        record = self._require_active(state)
        contour, revision = split_revision(record.session_id)
        scope = compute_scope(
            self._git, allowed_paths=self._config.contour_documents(contour)
        )
        round_no = self._round_of(state, record)
        successor_contour, reason = _route(
            contour,
            parked,
            exhausted=returns_exhausted(state, self._config.max_architectural_returns),
            spec_touched=self._spec_touched(scope),
        )
        args: dict[str, Any] = {
            "session_id": record.session_id,
            "round": round_no,
            "diff_sha256": _sha256(diff),
            "paths": list(scope.paths),
            "contour": successor_contour,
            "revision": revision + 1,
            "reason": None if reason is None else reason.value,
        }
        if parked is not None:
            args["findings_session_id"] = parked[0]
            args["findings_round"] = parked[1]
        action = self._intent(state, ADOPT_KIND, _adopt_operation_id(args), args)
        return self.replay(self._write(state, action), action, diff=diff)

    def discard(
        self, state: PipelineState, *, diff: str, reset_to: str
    ) -> PipelineState:
        """Санкционирует reset и переигровку раунда (§3.1).

        **Активной ревизии не требует**, в отличие от adoption'а: §8.1
        распространяет модель внешней правки и на окно «между сессиями»
        (активной нет — например, крах между commit point'ом
        `finish_session` и chained `create_session`), а сброс к последнему
        принятому коммиту там определён ничуть не хуже. Парковать в этом окне
        действительно нечего — но `discard` ничего и не паркует.

        `reset_to` считает вызывающий (§8.1: цель — то, куда обязан
        вернуться раунд, а не текущий `HEAD`) и кладёт в аргументы интента:
        цель обязана быть durable ДО первой мутации, иначе повтор после
        частичного сброса вычислял бы её уже по изменённому дереву.

        Вытесненный интент едет в аргументах решения: слот `next_action`
        один, и без этого commit point не смог бы вернуть пайплайн туда,
        откуда его забрало решение человека.
        """
        record = active_session(state)
        args: dict[str, Any] = {
            "session_id": "" if record is None else record.session_id,
            "round": 0 if record is None else self._round_of(state, record),
            "diff_sha256": _sha256(diff),
            "reset_to": reset_to,
            "resume_action": (
                None
                if state.next_action is None
                else state.next_action.model_dump(mode="json", by_alias=True)
            ),
        }
        action = self._intent(state, DISCARD_KIND, _discard_operation_id(args), args)
        return self.replay(self._write(state, action), action, diff=diff)

    def replay(
        self, state: PipelineState, action: NextAction, *, diff: str
    ) -> PipelineState:
        """Допроигрывает операторский интент — тем же кодом, что и первый проход.

        Второго кодового пути для recovery нет намеренно: он расходится с
        первым ровно тогда, когда его труднее всего проверить.
        """
        if action.kind == ADOPT_KIND:
            return self._execute_adopt(state, action, diff=diff)
        if action.kind == DISCARD_KIND:
            return self._execute_discard(state, action)
        raise PipelineNotResumable(
            f"интент {action.kind!r} операторским не является: исполнять его "
            "решениями §3.1 нельзя"
        )

    # ------------------------------------------------------------------
    # Исполнение
    # ------------------------------------------------------------------

    def _execute_adopt(
        self, state: PipelineState, action: NextAction, *, diff: str
    ) -> PipelineState:
        """Патч → чекпоинт → commit point; каждый шаг идемпотентен (§3.1)."""
        self._write_patch(state, action, diff)
        checkpoint = self._checkpoint(state, action)
        return self._commit_adoption(state, action, checkpoint)

    def _write_patch(self, state: PipelineState, action: NextAction, diff: str) -> Path:
        """Канонический патч решения — артефакт с именем `operation_id`.

        Существующий файл не перезаписывается: он снят ДО чекпоинта, а после
        чекпоинта дифа в дереве уже нет — восстановить его повтору было бы
        неоткуда, и запись «пустого патча» поверх сохранённого была бы
        потерей provenance.
        """
        directory = pipeline_dir_of(self._workspace_root, state.pipeline_id)
        patch = directory / ADOPTIONS_DIR_NAME / f"{action.operation_id}.patch"
        if patch.is_file():
            return patch
        if _sha256(diff) != action.args["diff_sha256"]:
            raise PipelineNotResumable(
                f"патч решения {action.operation_id} не сохранён, а дерево уже "
                "не то, по которому решение принималось: восстановить "
                "канонический диф нечем — разберите состояние вручную"
            )
        patch.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(patch, diff.encode("utf-8"))
        return patch

    def _checkpoint(self, state: PipelineState, action: NextAction) -> str:
        """Операторский чекпоинт; повтор узнаёт свой коммит по трейлеру (§3.1)."""
        found = self._git.find_commit_by_trailer(action.operation_id)
        if found is not None:
            return found
        return self._git.commit_paths(
            [str(path) for path in action.args["paths"]],
            CHECKPOINT_SUBJECT.format(slug=state.pipeline_id),
            trailer=action.operation_id,
        )

    def _commit_adoption(
        self, state: PipelineState, action: NextAction, checkpoint: str
    ) -> PipelineState:
        """Одна атомарная запись: decision + `abandoned` + маршрут + преемник.

        Три исхода, и различает их одна `reason` из аргументов интента —
        durable с момента write-ahead, поэтому повтор после обрыва приходит
        к тому же исходу, а не пересчитывает потолок по уже изменившемуся
        манифесту. `None` — преемник в том же контуре без смены фазы;
        возврат — ребро `PAIR_LOOP → SPEC_LOOP` с преемником-ревизией;
        `max_architectural_returns` — преемника нет вовсе, пайплайн уходит
        в честный частичный результат (§7.2).
        """
        session_id = str(action.args["session_id"])
        contour, _ = split_revision(session_id)
        successor_contour = str(action.args["contour"])
        successor_revision = int(action.args["revision"])
        successor_id = revision_id(successor_contour, successor_revision)
        reason = (
            None
            if action.args["reason"] is None
            else TransitionReason(action.args["reason"])
        )
        escalating = reason is TransitionReason.MAX_ARCHITECTURAL_RETURNS

        session_fields: dict[str, Any] = {"outcome": SessionOutcome.ABANDONED}
        if not escalating:
            session_fields["superseded_by"] = successor_id
        updates: dict[str, Any] = {
            "operator_decisions": [
                *state.operator_decisions,
                OperatorDecision(
                    operation_id=action.operation_id,
                    kind="adopt_external",
                    at=self._now(),
                    worktree_diff_sha256=str(action.args["diff_sha256"]),
                ),
            ],
            **_records_update(
                contour,
                with_session_fields(
                    _records(state, contour), session_id, **session_fields
                ),
            ),
        }
        if escalating:
            updates.update(
                escalation_update(
                    state,
                    reason,
                    evidence=self._evidence(state, action),
                    moment=self._now(),
                )
            )
        else:
            updates["next_action"] = NextAction(
                operation_id=f"create-{successor_id}",
                kind="create_session",
                args=_create_args(
                    action, successor_contour, successor_revision, checkpoint
                ),
                predecessor_operation_id=action.operation_id,
            )
        if reason is not None and not escalating:
            updates["phase"] = PipelinePhase.SPEC_LOOP
            updates["transitions"] = [
                *state.transitions,
                Transition(
                    from_=PipelinePhase.PAIR_LOOP,
                    to=PipelinePhase.SPEC_LOOP,
                    reason=reason,
                    evidence=self._evidence(state, action),
                    at=self._now(),
                ),
            ]
            updates["spec_sessions"] = _supersede_spec(state, successor_id)
        return self._save(
            state.model_copy(update=updates),
            PipelineEventType.PHASE_CHANGE if reason is not None else None,
            action.operation_id,
        )

    def _execute_discard(
        self, state: PipelineState, action: NextAction
    ) -> PipelineState:
        """Reset к цели из интента + запись решения (§3.1).

        Цель взята из аргументов, а не вычислена здесь: она durable с момента
        записи интента, поэтому повтор после частичного сброса приходит к
        тому же коммиту, а не к тому, куда дерево успело съехать. Сброс
        идемпотентен — повтор к тому же коммиту no-op; `clean` снимает новые
        файлы прерванной попытки, минуя каталог оркестратора.
        """
        self._git.reset_hard(str(action.args["reset_to"]))
        self._git.clean()
        restored = action.args.get("resume_action")
        successor = (
            None
            if restored is None
            else NextAction.model_validate(restored).model_copy(
                update={"predecessor_operation_id": action.operation_id}
            )
        )
        return self._save(
            state.model_copy(
                update={
                    "operator_decisions": [
                        *state.operator_decisions,
                        OperatorDecision(
                            operation_id=action.operation_id,
                            kind="discard_round",
                            at=self._now(),
                            worktree_diff_sha256=str(action.args["diff_sha256"]),
                        ),
                    ],
                    "next_action": successor,
                }
            ),
            None,
            action.operation_id,
        )

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------

    def _intent(
        self,
        state: PipelineState,
        kind: str,
        operation_id: str,
        args: Mapping[str, Any],
    ) -> NextAction:
        """Интент решения с provenance вытесненного предшественника."""
        return NextAction(
            operation_id=operation_id,
            kind=kind,  # type: ignore[arg-type]
            args=dict(args),
            predecessor_operation_id=(
                None if state.next_action is None else state.next_action.operation_id
            ),
        )

    def _write(self, state: PipelineState, action: NextAction) -> PipelineState:
        """Write-ahead запись интента — до первой мутации (§4.3)."""
        return self._save(state.model_copy(update={"next_action": action}), None, "")

    def _save(
        self,
        state: PipelineState,
        event_type: PipelineEventType | None,
        operation_id: str,
    ) -> PipelineState:
        """Сохраняет манифест с пересчитанным бюджетом; событие — best-effort."""
        persisted = state.model_copy(
            update={
                "budget_used": recompute_budget(
                    pipeline_dir_of(self._workspace_root, state.pipeline_id), state
                )
            }
        )
        self._store.save(persisted)
        if event_type is not None:
            self._sink.emit(
                PipelineEvent(
                    ts=self._now(),
                    pipeline=persisted.pipeline_id,
                    type=event_type,
                    payload={"operation_id": operation_id},
                )
            )
        return persisted

    def _spec_touched(self, scope: AdoptionScope) -> bool:
        """Затронул ли диф спеку — факт вида `pair` и только его (§3.1).

        У вида `document` спеки не существует, и вопрос не имеет смысла:
        `spec_path` там `None`, ответ — всегда `False`, и никакой ветки
        pair-механики за этим не стоит.
        """
        spec_path = self._config.spec_path
        return spec_path is not None and spec_path.as_posix() in scope.paths

    def _require_active(self, state: PipelineState) -> SessionRecord:
        """Активная ревизия; без неё решение оператора не к чему привязать."""
        record = active_session(state)
        if record is None:
            raise PipelineNotResumable(
                "у пайплайна нет активной ревизии: решение оператора паркует "
                "сессию и открывает следующую, а парковать здесь нечего — "
                "разберите состояние рабочего дерева вручную"
            )
        return record

    def _round_of(self, state: PipelineState, record: SessionRecord) -> int:
        """Текущий раунд ревизии по её `session.json`; без файла — ноль."""
        artifact_root = artifact_root_of(
            self._workspace_root, state.pipeline_id, record.session_id
        )
        session = load_session_state(artifact_root, record.session_id)
        return 0 if session is None else session.current_round

    def _evidence(self, state: PipelineState, action: NextAction) -> list[EvidenceLink]:
        """Архитектурные находки припаркованного ревью — evidence перехода."""
        session_id = action.args.get("findings_session_id")
        if not isinstance(session_id, str):
            return []
        round_no = int(action.args["findings_round"])
        review = load_review(
            artifact_root_of(self._workspace_root, state.pipeline_id, session_id),
            round_no,
        )
        if review is None:
            return []
        return [
            EvidenceLink(session_id=session_id, round=round_no, finding_id=issue.id)
            for issue in architectural_findings(review)
        ]


def _route(
    contour: str,
    parked: tuple[str, int] | None,
    *,
    exhausted: bool,
    spec_touched: bool,
) -> tuple[str, TransitionReason | None]:
    """Контур преемника и причина перехода — по путям дифа, затем по P6.

    Порядок ветвей и есть норма §3.1: правка спеки ведёт в spec-контур
    независимо от находок ревьюера, а обнаруженный дефект — независимо от
    того, что диф трогал только план. Обе причины сразу дают
    `external_spec_adopt`: он основной, а находки уходят в evidence того же
    перехода, без второй записи исхода.

    `exhausted` — исчерпанный потолок §7.2 — перекрывает обе причины сразу,
    и по одной причине: обе ведут по ребру `PAIR_LOOP → SPEC_LOOP`, а лимит
    назначен именно ребру. Возврата тогда не будет вовсе — вместо контура
    преемника возвращается `max_architectural_returns`, и commit point
    уводит пайплайн в честный частичный результат (§7.2 дословно:
    «Превышение → `ESCALATED`»). Отказать вместо этого значило бы оставить
    правку человека в дереве, из которого пайплайн уже не выйдет.
    """
    if contour == CONTOUR_SPEC:
        return CONTOUR_SPEC, None
    if spec_touched or parked is not None:
        if exhausted:
            return CONTOUR_SPEC, TransitionReason.MAX_ARCHITECTURAL_RETURNS
        if spec_touched:
            return CONTOUR_SPEC, TransitionReason.EXTERNAL_SPEC_ADOPT
        return CONTOUR_SPEC, TransitionReason.ARCHITECTURAL_DEFECT
    return CONTOUR_PAIR, None


def _create_args(
    action: NextAction, contour: str, revision: int, checkpoint: str
) -> dict[str, Any]:
    """Аргументы chained `create_session`: маршрут, чекпоинт, находки.

    `base_commit` — sha операторского чекпоинта: первый `PROPOSING` новой
    ревизии сбрасывает дерево именно к нему, и принятая правка сброс
    переживает (§3.1).
    """
    args: dict[str, Any] = {
        "contour": contour,
        "revision": revision,
        "base_commit": checkpoint,
    }
    if (
        isinstance(action.args.get("findings_session_id"), str)
        and contour == CONTOUR_SPEC
    ):
        args["findings_session_id"] = action.args["findings_session_id"]
        args["findings_round"] = action.args["findings_round"]
    return args


def _supersede_spec(state: PipelineState, successor_id: str) -> list[SessionRecord]:
    """Помечает перекрытой последнюю незакрытую spec-ревизию (§7.3, P3).

    Только незакрытую: `superseded_by` заполняется однократно, и повторная
    запись того же значения хранилищем принимается, а другого — отвергается.
    """
    overridden = [
        record for record in state.spec_sessions if record.superseded_by is None
    ]
    if not overridden:
        return list(state.spec_sessions)
    return with_session_fields(
        state.spec_sessions, overridden[-1].session_id, superseded_by=successor_id
    )


def _records(state: PipelineState, contour: str) -> Sequence[SessionRecord]:
    """Список ревизий нужного контура (§4.2)."""
    return state.spec_sessions if contour == CONTOUR_SPEC else state.pair_sessions


def _records_update(contour: str, records: Sequence[SessionRecord]) -> dict[str, Any]:
    """`model_copy(update=…)` для списка ревизий нужного контура."""
    key = "spec_sessions" if contour == CONTOUR_SPEC else "pair_sessions"
    return {key: list(records)}


def _adopt_operation_id(args: Mapping[str, Any]) -> str:
    """`operation_id` adoption'а — sha256 дифа плюс identity чекпоинта (§3.1).

    Диф один и тот же при повторе после краха, identity берётся из манифеста
    и `session.json`, поэтому повтор приходит к тому же идентификатору — на
    этом держится и имя патч-файла, и поиск чекпоинта по трейлеру.
    """
    return _operation_id("adopt", args)


def _discard_operation_id(args: Mapping[str, Any]) -> str:
    """`operation_id` отказа от раунда — тот же принцип, что у adoption'а."""
    return _operation_id("discard", args)


def _operation_id(prefix: str, args: Mapping[str, Any]) -> str:
    """Детерминированный идентификатор операции из дифа и identity сессии."""
    seed = f"{args['diff_sha256']}\x00{args['session_id']}\x00{args['round']}"
    return f"{prefix}-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _sha256(text: str) -> str:
    """sha256 текста дифа — provenance решения оператора (§4.2)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
