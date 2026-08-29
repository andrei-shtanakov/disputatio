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
from disputatio.events import IntegrityAnchor
from disputatio.runtime.errors import (
    ConfigError,
    ControlPlaneTampered,
    ExternalEditError,
    PipelineNotResumable,
)
from disputatio.runtime.git import GitOps
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


def classify_worktree(
    git: GitOps,
    state: PipelineState,
    *,
    workspace_root: Path,
    diff: str | None = None,
) -> WorktreeClass:
    """Происхождение состояния рабочего дерева (§8.1, модель внешней правки).

    `diff_readonly`, а не `diff_head`: классификация обязана быть
    по-настоящему read-only — иначе шаг 3 сам мутировал бы индекс до решения
    оператора, и вывод `git status` у пользователя менялся бы от того, что он
    запустил `resume`.

    `legal_patch` — окно «proposal записан, раунд не принят»: дифф
    байт-в-байт совпадает с `changes.patch` текущего раунда активной
    ревизии. Сравнение именно байтовое: «похожий» патч ничего не доказывает,
    а `changes.patch` снят тем же каноническим диффом.

    `diff` подаётся вызывающим, когда тот УЖЕ снял его тем же способом:
    решению оператора нужны те же байты, и снимать их дважды значило бы
    классифицировать одно состояние, а принимать другое.
    """
    if diff is None:
        diff = git.diff_readonly()
    if not diff:
        return "clean"
    recorded = _recorded_patch(state, workspace_root)
    if recorded is not None and recorded == diff.encode("utf-8"):
        return "legal_patch"
    return "unattributed"


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
        """Продолжает пайплайн `slug`, соблюдая порядок §8.1."""
        anchor = self._verify_integrity(slug)
        state = self._load(slug, anchor)
        parked = self._runner.detect_parked(state)
        pending = _pending_operator_intent(state)
        diff = self._git.diff_readonly()
        verdict = classify_worktree(
            self._git, state, workspace_root=self._workspace_root, diff=diff
        )

        self._check_decision(decision, pending, verdict, diff)
        if pending is not None:
            self._intents.replay(state, pending, diff=diff)
        elif decision == "adopt_external":
            self._intents.adopt(state, diff=diff, parked=parked)
        elif decision == "discard_round":
            self._intents.discard(state, diff=diff)
        return self._runner.advance(slug)

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
        except ControlPlaneTampered:
            # Пайплайн обязан остаться закрытым: сессия, оставшаяся активной,
            # была бы возобновлена следующим resume, и подмену никто не
            # заметил бы во второй раз. Отсутствующий манифест этому не
            # мешает — диагноз человеку несёт само исключение.
            try:
                self._runner.fail(slug, reason=TransitionReason.INVARIANT_VIOLATION)
            except KeyError:
                pass
            raise
        return anchor

    # ------------------------------------------------------------------
    # Шаг 1: манифест
    # ------------------------------------------------------------------

    def _load(self, slug: str, anchor: IntegrityAnchor) -> PipelineState:
        """Манифест пайплайна; его отсутствие — инструкция человеку, не KeyError."""
        try:
            state = self._store.load(slug)
        except KeyError as exc:
            raise PipelineNotResumable(
                _missing_manifest_message(self._workspace_root, slug, anchor)
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
            raise PipelineNotResumable(
                f"ревизия {session_id!r} закрыта ({closed}"
                f"{'' if record.superseded_by is None else f', перекрыта {record.superseded_by}'})"
                ": сессия с записанным исходом не возобновляется никогда "
                "(§8.1 шаг 1), а незавершённый интент указывает на неё"
            )

    # ------------------------------------------------------------------
    # Шаг 3: санкция
    # ------------------------------------------------------------------

    def _check_decision(
        self,
        decision: str | None,
        pending: NextAction | None,
        verdict: WorktreeClass,
        diff: str,
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
                "не доказано — оно не чисто и не сводится к записанному "
                "changes.patch. Destructive reset поверх такого состояния "
                "запрещён (§8.1); выберите явно: `--discard-round` "
                "(санкционировать сброс, ручные правки будут потеряны) либо "
                "`--adopt-external` (принять правку как внешнюю и уйти в "
                f"новую ревизию).\n{diff}"
            )

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------


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


def _missing_manifest_message(
    workspace_root: Path, slug: str, anchor: IntegrityAnchor
) -> str:
    """Инструкция для окна «каталог создан, манифеста ещё нет».

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
