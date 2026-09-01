"""Resume пайплайна: порядок §8.1, сверка P9, санкция оператора (SPEC-002 §8.1).

Порядок шагов строгий, и каждый его пункт куплен отдельным способом сломаться.

0. **P9-сверка по последней записи анкера** — до чтения манифеста и артефактов
   сессии, которые могли быть подменены. Оба входа, нужных, чтобы найти
   журнал, лежат вне рабочего дерева: `anchor_root` из живой конфигурации
   (снапшот в каталоге пайплайна не годится — он в недоверенном дереве),
   `anchor_id` — из `--slug`. Identity берётся из самой записи. Сверка
   применяется, только если последняя запись — `pre_turn`: `turn_completed`
   и пустой журнал означают, что сверять нечего, а **отсутствие файла** —
   отказ, потому что иначе пайплайн с нестандартным `anchor_path` смотрел бы
   в дефолтный журнал, не находил записей и молча пропускал сверку.
1. **чтение манифеста**; сессии с `outcome`/`superseded_by` ≠ null не
   возобновляются никогда.
2. **read-only обнаружение** архитектурного дефекта: только вердикт, никаких
   мутаций — ни манифеста, ни рабочего дерева.
3. **сверка worktree**, сама немутирующая (`diff_readonly`, не `diff_head`:
   последний начинается с `add --intent-to-add` и оставил бы новый файл в
   индексе, изменив вывод `git status` у пользователя до всякого решения).
   Она предшествует ЛЮБОМУ мутирующему шагу: и replay интента, и возврат §7.3
   несут destructive reset, который поверх непроверенного дерева уничтожил бы
   внешнюю правку.
4. **мутирующая фаза** — только после чистой/атрибутированной сверки либо
   явной санкции оператора.
5. **session-resume** — внутри `PipelineRunner.advance`, который остаётся
   единственным движком цикла; припаркованную сессию он не продвигает.

Модель внешней правки — та же, что в §8.1: продолжать можно поверх чистого
дерева либо поверх дифа, байт-в-байт сводящегося к записанному
`changes.patch`. Всё остальное — включая dirty state в окне in-flight
`PROPOSING`, где происхождение байтов неразличимо в принципе, — остановка с
показом дифа и требованием выбора человека.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from disputatio.contracts import (
    NextAction,
    PipelinePhase,
    PipelineState,
    PipelineStateStore,
    SessionRecord,
    TransitionReason,
)
from disputatio.events import AnchorCorrupted, IntegrityAnchor
from disputatio.runtime.config import load_config
from disputatio.runtime.errors import (
    BaseRevisionNotFound,
    ConfigError,
    ControlPlaneTampered,
    ExternalEditError,
    GitCommandError,
    PipelineNotResumable,
)
from disputatio.runtime.git import GitOps, base_rev
from disputatio.runtime.layout import CHANGES_PATCH_NAME, round_artifact
from disputatio.runtime.pipeline_adopt import OPERATOR_KINDS, OperatorIntents
from disputatio.runtime.pipeline_config import (
    PipelineConfig,
    toplevel_root,
    validate_anchor_path,
)
from disputatio.runtime.pipeline_integrity import ControlPlane, verify_or_raise
from disputatio.runtime.pipeline_runner import (
    PipelineRunner,
    active_session,
    artifact_root_of,
    load_session_state,
    pipeline_dir_of,
)

WorktreeClass = Literal["clean", "legal_patch", "unattributed"]

#: Терминальные фазы пайплайна (§2): рёбер из них в таблице нет вовсе.
_TERMINAL_PHASES: Final = (PipelinePhase.DONE, PipelinePhase.FAILED)

#: `kind` интентов, продвигающих конкретную ревизию: их replay обязан
#: убедиться, что ревизия ещё жива (§8.1 шаг 1).
_SESSION_INTENTS: Final = frozenset({"run_session", "finish_session"})


@dataclass(frozen=True, slots=True)
class WorktreeAnchorage:
    """Где обязан стоять `HEAD` активной ревизии (§8.1, модель внешней правки).

    Оба значения **вычисляются**, а не хранятся: §4.2 запрещает манифесту
    нести машинно-зависимые значения, и SHA там нет по этой причине. Входы
    вычисления durable и уже лежат на диске — история git плюс `base_commit`
    из снапшота конфига ревизии (`base_rev`, [DESIGN-012]).

    Значений два, потому что окно обрыва между ними штатное: `commit_round(N)`
    исполняется ДО `apply_decision` (`runtime/steps.py`), поэтому убитый в
    этом окне процесс оставляет `HEAD` на коммите раунда N при `session.json`,
    всё ещё называющем раунд N. Сверка по одному только `reset_target` дала бы
    там ложное срабатывание на штатном состоянии.

    `None` в обоих полях означает «вычислить нечем» (снапшота нет, история
    переписана): сверка HEAD тогда не делается вовсе, и разбираться с
    оборванной историей остаётся `base_rev` внутри самой сессии — у него для
    этого есть и своя ошибка, и свой текст. Изобретать здесь второй диагноз
    значило бы дать два разных ответа на один вопрос.
    """

    reset_target: str | None = None
    round_commit: str | None = None

    @property
    def expected_heads(self) -> tuple[str, ...]:
        """Допустимые значения `HEAD`; пусто — сверять нечем."""
        return tuple(sha for sha in (self.reset_target, self.round_commit) if sha)


def worktree_anchorage(
    state: PipelineState, *, workspace_root: Path
) -> WorktreeAnchorage:
    """Ожидаемые значения `HEAD` активной ревизии (§8.1).

    Между сессиями (активной ревизии нет) — пустая привязка: ожидаемое
    состояние там «последний принятый коммит», а вычислить его не из чего —
    ни раунда, ни `base_commit` не существует. Dirty diff в этом окне
    останавливает resume и без сверки HEAD (§8.1), а нового коммита сверять
    не с чем.
    """
    record = active_session(state)
    if record is None:
        return WorktreeAnchorage()
    artifact_root = artifact_root_of(
        workspace_root, state.pipeline_id, record.session_id
    )
    session = load_session_state(artifact_root, record.session_id)
    if session is None:
        return WorktreeAnchorage()
    try:
        base_commit = load_config(artifact_root).base_commit
    except ConfigError:
        return WorktreeAnchorage()
    # Раунд 0 (ревизия создана, `PROPOSING` ещё не начинался) ожидает
    # `base_commit`, и `base_rev(1, …)` отвечает ровно им.
    round_no = max(session.current_round, 1)
    return WorktreeAnchorage(
        reset_target=_revision_or_none(workspace_root, round_no, base_commit),
        round_commit=_revision_or_none(workspace_root, round_no + 1, base_commit),
    )


def _revision_or_none(
    workspace_root: Path, round_no: int, base_commit: str
) -> str | None:
    """`base_rev` раунда либо `None`, если такой цели в истории нет."""
    try:
        return base_rev(workspace_root, round_no, base_commit=base_commit)
    except (BaseRevisionNotFound, GitCommandError):
        return None


def classify_worktree(
    git: GitOps,
    state: PipelineState,
    *,
    workspace_root: Path,
    diff: str | None = None,
) -> WorktreeClass:
    """Происхождение состояния рабочего дерева (§8.1, модель внешней правки).

    Состояние — это ПАРА «HEAD плюс дифф», и проверяются обе половины.
    Чистое дерево на неожиданном `HEAD` — это внешний коммит, сделанный мимо
    пайплайна: сброс первого же `PROPOSING` увёл бы с него ветку без всякой
    санкции. Поэтому identity `HEAD` сверяется ПЕРВОЙ, и её несовпадение
    делает состояние неатрибутируемым независимо от дифа.

    `diff_readonly`, а не `diff_head`: классификация обязана быть
    по-настоящему read-only — иначе шаг 3 сам мутировал бы индекс до решения
    оператора, и вывод `git status` у пользователя менялся бы от того, что он
    запустил `resume`. `head_sha` и `base_rev` немутирующи оба (`rev-parse`,
    `merge-base`, `log`).

    `legal_patch` — окно «proposal записан, раунд не принят»: дифф
    байт-в-байт совпадает с `changes.patch` текущего раунда активной
    ревизии. Сравнение именно байтовое: «похожий» патч ничего не доказывает,
    а `changes.patch` снят тем же каноническим диффом.

    `diff` подаётся вызывающим, когда тот УЖЕ снял его тем же способом:
    решению оператора нужны те же байты, и снимать их дважды значило бы
    классифицировать одно состояние, а принимать другое.
    """
    if head_mismatch(git, state, workspace_root=workspace_root) is not None:
        return "unattributed"
    if diff is None:
        diff = git.diff_readonly()
    if not diff:
        return "clean"
    recorded = _recorded_patch(state, workspace_root)
    if recorded is not None and recorded == diff.encode("utf-8"):
        return "legal_patch"
    return "unattributed"


def head_mismatch(
    git: GitOps, state: PipelineState, *, workspace_root: Path
) -> str | None:
    """Описание расхождения `HEAD` с ожидаемым либо `None`, если сошлось."""
    return describe_head_mismatch(
        git.head_sha(), worktree_anchorage(state, workspace_root=workspace_root)
    )


def describe_head_mismatch(head: str, anchorage: WorktreeAnchorage) -> str | None:
    """Текст расхождения `HEAD` с привязкой; `None` — сошлось или сверять нечем.

    Возвращает текст, а не флаг: он же идёт человеку в отказ §8.1 — без него
    остановка на ЧИСТОМ дереве выглядела бы отказом без причины (дифа-то
    нет). Один источник и для вердикта, и для объяснения.
    """
    expected = anchorage.expected_heads
    if not expected or head in expected:
        return None
    return (
        f"HEAD {head} не совпадает ни с одним ожидаемым коммитом "
        f"({', '.join(expected)}): в дереве коммит, которого пайплайн не "
        "делал, и сброс раунда увёл бы с него ветку"
    )


class PipelineResume:
    """`disp pipeline resume`: порядок §8.1 и два операторских решения §3.1.

    Единственным движком цикла остаётся `PipelineRunner.advance` — resume
    доводит пайплайн до состояния, из которого движок вправе продолжить, и
    передаёт управление ему. Своей ветки «а теперь погоним сессию» здесь нет
    намеренно: два движка разошлись бы ровно на crash-путях.
    """

    def __init__(
        self,
        *,
        runner: PipelineRunner,
        store: PipelineStateStore,
        git: GitOps,
        config: PipelineConfig,
        workspace_root: Path,
        intents: OperatorIntents,
    ) -> None:
        self._runner = runner
        self._store = store
        self._git = git
        self._config = config
        self._workspace_root = workspace_root
        self._intents = intents

    def resume(
        self,
        slug: str,
        *,
        decision: Literal["discard_round", "adopt_external"] | None = None,
    ) -> PipelineState:
        """Продолжает пайплайн `slug`, соблюдая порядок §8.1.

        Сверка вида (P0) стоит **сразу после чтения манифеста** — не раньше
        и не позже. Раньше нельзя: вид живёт в манифесте, а манифесту нельзя
        верить, пока не сверена целостность control plane (шаг 0), и её
        отказ сам мутирует — переводит пайплайн в `FAILED`; §2 P0 объявляет
        эту мутацию единственной, законно предшествующей проверке вида.
        Позже нельзя: следом идут `detect_parked`, классификация дерева и
        мутирующая фаза, а P0 запрещает мутации шагов 3–5 до сверки.
        """
        anchor = self._verify_integrity(slug)
        state = self._load(slug, anchor)
        _require_same_kind(state, self._config, slug)
        parked = self._runner.detect_parked(state)
        pending = _pending_operator_intent(state)
        diff = self._git.diff_readonly()
        anchorage = worktree_anchorage(state, workspace_root=self._workspace_root)
        mismatch = describe_head_mismatch(self._git.head_sha(), anchorage)
        verdict = classify_worktree(
            self._git, state, workspace_root=self._workspace_root, diff=diff
        )

        self._check_decision(decision, pending, verdict, _stop_detail(mismatch, diff))
        if pending is not None:
            self._intents.replay(state, pending, diff=diff)
        elif decision == "adopt_external":
            self._intents.adopt(state, diff=diff, parked=parked)
        elif decision == "discard_round":
            self._intents.discard(
                state, diff=diff, reset_to=self._reset_to(state, anchorage)
            )
        return self._runner.advance(slug)

    def _reset_to(self, state: PipelineState, anchorage: WorktreeAnchorage) -> str:
        """Цель сброса `--discard-round`: куда обязан вернуться раунд (§3.1).

        Именно `reset_target` привязки, а не текущий `HEAD`: санкция
        оператора отменяет раунд целиком, а `HEAD` в этот момент вправе нести
        и коммит отменяемого раунда (окно `commit_round` до `apply_decision`),
        и посторонний коммит, из-за которого resume и остановился, — сброс на
        самого себя сохранил бы ровно то, что оператор велел выбросить.

        `HEAD` — не запасной вариант «на всякий случай», а ответ ровно для
        одного состояния: **между сессиями** активной ревизии нет, и §8.1
        прямо называет ожидаемым состоянием последний принятый коммит, то
        есть текущий `HEAD`; незакоммиченное снимет `clean`.

        Активная ревизия при невычислимой привязке (снапшота конфига нет,
        историю переписали) — **отказ**, а не сброс на `HEAD`. «Мы не знаем,
        куда раунд обязан вернуться» — ровно то состояние, в котором
        destructive reset не обоснован: он оставил бы в истории чужой коммит
        и выдал бы это за исполненную санкцию.
        """
        if anchorage.reset_target is not None:
            return anchorage.reset_target
        record = active_session(state)
        if record is None:
            return self._git.head_sha()
        raise PipelineNotResumable(
            f"цель сброса ревизии {record.session_id!r} не "
            "вычисляется: нет снапшота конфига ревизии либо история под ней "
            "переписана, и «куда обязан вернуться раунд» неизвестно. "
            "`--discard-round` в этом состоянии сбросил бы дерево на текущий "
            "HEAD — то есть сохранил бы ровно то, что решение оператора "
            "велит выбросить. Восстановите снапшот конфига ревизии либо "
            "разберите историю вручную"
        )

    # ------------------------------------------------------------------
    # Шаг 0: целостность control plane
    # ------------------------------------------------------------------

    def _verify_integrity(self, slug: str) -> IntegrityAnchor:
        """Сверка P9 до чтения манифеста; расхождение → `FAILED` (§8.1 шаг 0)."""
        validate_anchor_path(
            self._config.anchor_path, toplevel_root(self._git, self._workspace_root)
        )
        anchor = IntegrityAnchor(self._config.anchor_path, self._workspace_root, slug)
        try:
            record = anchor.last_record()
        except FileNotFoundError as exc:
            raise ConfigError(
                f"журнала целостности {anchor.path} не существует, а `run` "
                "создаёт его первым действием (§3.1) — значит resume ищет не "
                "там: укажите тот же конфиг, что и при запуске (`--config`), "
                "чтобы `anchor_path` совпал. Пропустить сверку P9 resume не "
                "вправе: молча она проверяла бы чужой журнал"
            ) from exc
        except AnchorCorrupted as exc:
            # Порча анкера — нарушение control plane, а не сбой чтения:
            # журнал вынесен из дерева автора именно затем, чтобы его
            # содержимое никто мимо оркестратора не менял. Диагноз и исход
            # те же, что у любого расхождения P9, — иначе повреждение одной
            # строки давало бы более мягкий ответ, чем подмена файла,
            # который эта строка описывает.
            tampered = ControlPlaneTampered(str(exc))
            self._close_tampered(slug, tampered)
            raise tampered from exc
        if record is None or record.kind != "pre_turn":
            # Пустой журнал и `turn_completed` означают, что ход не прерван:
            # штатные записи runtime после успешного хода подменой не являются.
            return anchor
        plane = ControlPlane(
            workspace_root=self._workspace_root,
            pipeline_dir=pipeline_dir_of(self._workspace_root, slug),
            artifact_root=artifact_root_of(
                self._workspace_root, slug, record.session_id
            ),
        )
        try:
            verify_or_raise(anchor, record, plane)
        except ControlPlaneTampered as tampered:
            self._close_tampered(slug, tampered)
            raise
        return anchor

    def _close_tampered(self, slug: str, tampered: ControlPlaneTampered) -> None:
        """Отметка `FAILED (invariant_violation)` по подмене (§8.1 шаг 0).

        Закрыть пайплайн стоит попытки: сессия, оставшаяся активной, была бы
        возобновлена следующим resume, и подмену пришлось бы ловить второй
        раз. Но пишется файл, про который ровно сейчас доказано, что верить
        ему нельзя, — поэтому запись здесь **не условие**, а попытка, и
        неудача её обязана уйти человеку ВНУТРИ диагноза подмены, а не вместо
        него.

        Отсюда `Exception` целиком, а не перечень классов. Перечня хватало,
        пока в нём числился один отказ (`KeyError` — манифеста нет), и он же
        оставлял дыру: подделка, выставившая манифесту терминальную фазу,
        получала от `_fail` `ValueError` (из `DONE` рёбер нет, §2), тот
        вылетал наружу ВМЕСТО `ControlPlaneTampered` — то есть подделка одного
        поля отключала fail-closed обработку P9 и приходила человеку под видом
        внутренней ошибки. Любой ДРУГОЙ способ записи не удаться воспроизвёл
        бы ту же дыру, поэтому ловится не список причин, а сам факт неудачи.

        Проглатывания при этом нет ни одной ветки ([DESIGN-016]): пойманное
        уходит наружу причиной (`from`) того же `ControlPlaneTampered`, с
        которым сюда вошли. Так у человека остаются оба факта — что control
        plane подменён и что закрыть пайплайн не удалось, — а вызывающий видит
        один и тот же класс независимо от исхода записи.

        Терминальную фазу `_fail` не глотает и глотать не должен: «завершённый
        результат не переписывается» — инвариант §2, а не помеха, и снимать
        его ради подделанного `DONE` значило бы отдать перезапись истории
        всякому, кто сумел подделать фазу. Fail-closed держится не манифестом,
        а анкером: его последняя запись остаётся `pre_turn`, и следующий
        resume приходит ровно к тому же отказу.
        """
        try:
            self._runner.fail(slug, reason=TransitionReason.INVARIANT_VIOLATION)
        except Exception as exc:
            raise tampered from exc

    # ------------------------------------------------------------------
    # Шаг 1: манифест
    # ------------------------------------------------------------------

    def _load(self, slug: str, anchor: IntegrityAnchor) -> PipelineState:
        """Манифест пайплайна; его отсутствие — инструкция человеку, не KeyError."""
        try:
            state = self._store.load(slug)
        except KeyError as exc:
            raise PipelineNotResumable(
                missing_manifest_message(self._workspace_root, slug, anchor)
            ) from exc
        if state.phase in _TERMINAL_PHASES:
            raise PipelineNotResumable(
                f"пайплайн {slug!r} в терминальной фазе {state.phase.value}: "
                "рёбер из неё в таблице §2 нет — возобновлять нечего"
            )
        self._guard_settled(state)
        return state

    def _guard_settled(self, state: PipelineState) -> None:
        """Сессия с записанным исходом не возобновляется никогда (§8.1 шаг 1)."""
        action = state.next_action
        if action is None or action.kind not in _SESSION_INTENTS:
            return
        session_id = str(action.args.get("session_id", ""))
        for record in (*state.spec_sessions, *state.pair_sessions):
            if record.session_id != session_id:
                continue
            if record.outcome is None and record.superseded_by is None:
                return
            closed = (
                record.outcome.value if record.outcome is not None else "superseded"
            )
            overlap = (
                ""
                if record.superseded_by is None
                else f", перекрыта {record.superseded_by}"
            )
            raise PipelineNotResumable(
                f"ревизия {session_id!r} закрыта ({closed}{overlap}): сессия с "
                "записанным исходом не возобновляется никогда (§8.1 шаг 1), а "
                "незавершённый интент указывает на неё"
            )

    # ------------------------------------------------------------------
    # Шаг 3: санкция
    # ------------------------------------------------------------------

    def _check_decision(
        self,
        decision: str | None,
        pending: NextAction | None,
        verdict: WorktreeClass,
        detail: str,
    ) -> None:
        """Сводит вердикт сверки с тем, что попросил человек (§3.1, §8.1)."""
        if pending is not None:
            if decision is not None and decision != pending.kind:
                raise PipelineNotResumable(
                    f"пайплайн уже несёт санкцию {pending.kind!r} (операция "
                    f"{pending.operation_id}), а запрошено {decision!r}: "
                    "записанное решение допроигрывается, а не заменяется"
                )
            return
        if decision is not None and verdict != "unattributed":
            raise PipelineNotResumable(
                f"нечего решать: рабочее дерево классифицировано как "
                f"{verdict!r}, и resume продолжается без санкции — флаг "
                f"{decision!r} применим только к остановке §8.1"
            )
        if verdict == "unattributed" and decision is None:
            raise ExternalEditError(
                "resume остановлен: происхождение состояния рабочего дерева "
                "не доказано — HEAD либо диф не сводятся к записанному "
                "пайплайном. Destructive reset поверх такого состояния "
                "запрещён (§8.1); выберите явно: `--discard-round` "
                "(санкционировать сброс, ручные правки будут потеряны) либо "
                "`--adopt-external` (принять правку как внешнюю и уйти в "
                f"новую ревизию).\n{detail}"
            )

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------


def _stop_detail(mismatch: str | None, diff: str) -> str:
    """Что показать человеку при остановке: расхождение HEAD и/или диф."""
    return "\n".join(part for part in (mismatch, diff) if part)


def _pending_operator_intent(state: PipelineState) -> NextAction | None:
    """Незавершённый операторский интент манифеста либо `None` (§4.3)."""
    action = state.next_action
    if action is not None and action.kind in OPERATOR_KINDS:
        return action
    return None


def _recorded_patch(state: PipelineState, workspace_root: Path) -> bytes | None:
    """Байты `changes.patch` текущего раунда активной ревизии либо `None`."""
    record: SessionRecord | None = active_session(state)
    if record is None:
        return None
    artifact_root = artifact_root_of(
        workspace_root, state.pipeline_id, record.session_id
    )
    session = load_session_state(artifact_root, record.session_id)
    if session is None or session.current_round < 1:
        return None
    patch = round_artifact(artifact_root, session.current_round, CHANGES_PATCH_NAME)
    return patch.read_bytes() if patch.is_file() else None


def missing_manifest_message(
    workspace_root: Path, slug: str, anchor: IntegrityAnchor
) -> str:
    """Инструкция для окна «каталог создан, манифеста ещё нет».

    Публичная, потому что читателей двое: `resume`, для которого это отказ
    возобновления, и `disp pipeline status`, для которого это единственный
    честный ответ на вопрос «что с пайплайном». Второй текст об одном и том
    же состоянии разошёлся бы с первым ровно в перечне ручных шагов.

    Окно между созданием каталога пайплайна и первой записью манифеста
    невосстановимо автоматически: `resume` не находит манифеста, `run`
    упирается в существующий каталог, а после его удаления — ещё и в
    существующий анкер, который `run` намеренно не переиспользует (§3.1).
    Два из трёх мест лежат вне рабочего дерева, поэтому текст называет их
    все сразу — иначе человек узнаёт о следующем препятствии, только
    наткнувшись на него.
    """
    directory = pipeline_dir_of(workspace_root, slug)
    if not directory.is_dir():
        return (
            f"пайплайна {slug!r} нет: каталог {directory} не существует — "
            f"начните его командой `disp pipeline run --slug {slug}`"
        )
    return (
        f"каталог пайплайна {directory} существует, а манифеста "
        f"{directory / 'pipeline.json'} в нём нет: процесс оборвался между "
        "созданием каталога и первой записью манифеста, и допроигрывать "
        "нечего — состояния пайплайна не существует. Восстановление ручное и "
        "требует трёх шагов, два из которых вне рабочего дерева:\n"
        f"  1. удалить каталог пайплайна: {directory}\n"
        f"  2. удалить журнал целостности: {anchor.path}\n"
        f"  3. запустить заново: `disp pipeline run --slug {slug}`\n"
        "Артефакты сессий в каталоге, если они есть, перед удалением стоит "
        "сохранить: пайплайн их уже не прочитает."
    )


def _require_same_kind(state: PipelineState, config: PipelineConfig, slug: str) -> None:
    """Вид пайплайна неизменяем (§2 P0): чужой конфиг — отказ, не переключение.

    Сменить вид значило бы объявить накопленную историю переходов
    принадлежащей другой механике: рёбра `SPEC_LOOP → PAIR_LOOP` в
    документном пайплайне не «лишние данные», а нарушение инварианта, и
    доигрывать такую историю чужим движком нечем.
    """
    if state.kind is not config.kind:
        raise ConfigError(
            f"пайплайн {slug!r} создан как вид {state.kind.value!r}, а "
            f"поданный конфиг описывает {config.kind.value!r}: вид неизменяем "
            "(P0) — сменить его значило бы объявить накопленную историю "
            "переходов принадлежащей другой механике"
        )
