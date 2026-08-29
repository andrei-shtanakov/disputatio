"""Операторские решения `--adopt-external` / `--discard-round` (SPEC-002 §3.1).

Четыре утверждения, каждое из которых спека формулирует как fail-closed
норму, а не как удобство.

* **Scope принимаемого дифа fail-closed.** Из полного `git status`
  вычитается ровно одно: СОБСТВЕННЫЕ untracked-файлы пайплайна под
  `.disputatio/` (иначе adoption не проходил бы никогда — пайплайн порождает
  их непрерывно). Tracked-изменённый путь под тем же каталогом, наоборот,
  adoption отклоняет: это внешняя правка control plane. В остатке допустимы
  только `spec_path`/`plan_path`, и любой иной путь отклоняет adoption
  целиком.
* **Маршрут определяют пути дифа, а не только классификация ревьюера.**
  Правка `spec_path` в pair-контуре ведёт в spec-ревизию даже без
  architectural finding: спека, изменившаяся после своей сходимости, обязана
  пройти spec-контур заново. P6 действует поверх — дефект ведёт в spec и при
  чистом plan-дифе.
* **Commit point один, и он единственный пишет `outcome`.** Обе причины
  сразу дают ОДИН `abandoned`; `record_return` в adoption-пути не участвует
  и никогда не перезаписывает уже записанный исход (P3).
* **Каждая граница обрыва идемпотентна.** Патч пишется по имени
  `operation_id`, чекпоинт узнаётся по трейлеру, решение дописывается
  повтором — падение посреди многошаговой мутации не теряет санкции
  человека и не создаёт второго коммита.

git настоящий: идемпотентность чекпоинта — это утверждение про реальный
трейлер в реальной истории, а «принятая правка переживает первый
`PROPOSING`» — про реальный `git reset --hard`.
"""

import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

import pytest

from disputatio.contracts import (
    PipelinePhase,
    PipelineState,
    SessionOutcome,
    TransitionReason,
)
from disputatio.events import FilePipelineStateStore
from disputatio.runtime import GitCli, PipelineConfig, StatusEntry
from disputatio.runtime.errors import (
    AdoptionScopeError,
    ExternalEditError,
    PipelineNotResumable,
)
from disputatio.runtime.git import OPERATION_TRAILER_KEY, base_rev
from disputatio.runtime.pipeline_adopt import compute_scope

from ._fakes import GitOpsFakeBase
from ._pipeline_stand import (
    ARCHITECTURAL,
    PLAN_PATH,
    SLUG,
    SPEC_PATH,
    Boom,
    Script,
    Stand,
    build_stand,
    git,
    live_pair,
    parked_pair,
    start,
)

ADOPTED_SPEC: Final = "# спека\n\nправка оператора, принятая как внешняя\n"
ADOPTED_PLAN: Final = "# план\n\nправка оператора, принятая как внешняя\n"


class CrashingStore:
    """`PipelineStateStore`, обрывающий процесс на конкретной записи манифеста.

    Каждая запись — самостоятельная граница write-ahead (§4.3), и обрыв ровно
    на ней (`crash_on_save`) либо сразу после неё (`crash_after_save`) —
    единственный способ проверить, что шаг допроигрывается, а не выполняется
    второй раз.
    """

    def __init__(
        self,
        inner: FilePipelineStateStore,
        *,
        crash_on_save: int = 0,
        crash_after_save: int = 0,
    ) -> None:
        self._inner = inner
        self._crash_on_save = crash_on_save
        self._crash_after_save = crash_after_save
        self.saves = 0

    def load(self, pipeline_id: str) -> PipelineState:
        """Читает манифест — чтение обрывов не моделирует."""
        return self._inner.load(pipeline_id)

    def save(self, state: PipelineState) -> None:
        """Пишет манифест, обрываясь на заданной границе."""
        self.saves += 1
        if self.saves == self._crash_on_save:
            raise Boom(f"обрыв ДО записи манифеста №{self.saves}")
        self._inner.save(state)
        if self.saves == self._crash_after_save:
            raise Boom(f"обрыв ПОСЛЕ записи манифеста №{self.saves}")


class CrashingCheckpoint:
    """`GitOps`-обёртка: чекпоинт оператора обрывает процесс.

    Делегирует всё настоящему `GitCli` — обрыв нужен ровно на одной
    границе §3.1 (patch записан, чекпоинт не сделан), и подменять ради неё
    остальные шесть операций значило бы проверять фейк, а не порядок.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        """Всё, кроме чекпоинта, — настоящий git."""
        return getattr(self._inner, name)

    def commit_paths(self, paths: Sequence[str], subject: str, *, trailer: str) -> str:
        """Обрыв между патч-файлом и чекпоинтом."""
        raise Boom(f"процесс убит до чекпоинта операции {trailer}")


def _crash(stand: Stand, **kwargs: int) -> Stand:
    """Ставит стенду обрывающееся хранилище и пересобирает runner с resume."""
    stand.store = CrashingStore(  # type: ignore[assignment]
        FilePipelineStateStore(stand.workspace), **kwargs
    )
    return stand.rebuild()


def _heal(stand: Stand) -> Stand:
    """Второй процесс после обрыва: настоящее хранилище и настоящий git."""
    stand.store = FilePipelineStateStore(stand.workspace)
    stand.git = GitCli(stand.workspace)
    return stand.rebuild()


def _adopt_stand(tmp_path: Path, **kwargs: object) -> Stand:
    """Стенд с оборванной pair-сессией без архитектурных находок."""
    scripts = live_pair()
    scripts["pair-r2"] = Script(outcome="deadlock")
    scripts["spec-r2"] = Script(outcome="deadlock")
    stand = build_stand(tmp_path, scripts, **kwargs)  # type: ignore[arg-type]
    start(stand)
    return stand


def _defect_stand(tmp_path: Path, **kwargs: object) -> Stand:
    """Стенд с pair-раундом, припаркованным архитектурной находкой."""
    scripts = parked_pair()
    scripts["spec-r2"] = Script(outcome="deadlock")
    stand = build_stand(tmp_path, scripts, **kwargs)  # type: ignore[arg-type]
    start(stand)
    return stand


def _records(state: PipelineState) -> dict[str, object]:
    """Записи обеих коллекций ревизий по `session_id`."""
    return {
        record.session_id: record
        for record in (*state.spec_sessions, *state.pair_sessions)
    }


def test_plan_only_edit_opens_a_new_pair_revision(tmp_path: Path) -> None:
    """Затронут только `plan_path` — новая pair-ревизия, фаза не меняется."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")

    state = stand.resume.resume(SLUG, decision="adopt_external")

    records = _records(state)
    assert records["pair-r1"].outcome is SessionOutcome.ABANDONED  # type: ignore[attr-defined]
    assert records["pair-r1"].superseded_by == "pair-r2"  # type: ignore[attr-defined]
    assert "pair-r2" in records
    assert TransitionReason.EXTERNAL_SPEC_ADOPT not in [
        transition.reason for transition in state.transitions
    ]
    assert [decision.kind for decision in state.operator_decisions] == [
        "adopt_external"
    ]


def test_spec_edit_forces_a_spec_revision_without_any_finding(tmp_path: Path) -> None:
    """Правка `spec_path` в pair-контуре → spec-ревизия и `external_spec_adopt`.

    Architectural finding здесь нет вовсе: маршрут определяют пути дифа, а
    спека, изменившаяся после своей сходимости, обязана пройти контур заново.
    """
    stand = _adopt_stand(tmp_path)
    (stand.workspace / SPEC_PATH).write_text(ADOPTED_SPEC, encoding="utf-8")

    state = stand.resume.resume(SLUG, decision="adopt_external")

    returns = [
        transition
        for transition in state.transitions
        if transition.reason is TransitionReason.EXTERNAL_SPEC_ADOPT
    ]
    assert len(returns) == 1
    assert (returns[0].from_, returns[0].to) == (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.SPEC_LOOP,
    )
    records = _records(state)
    assert records["pair-r1"].outcome is SessionOutcome.ABANDONED  # type: ignore[attr-defined]
    assert records["pair-r1"].superseded_by == "spec-r2"  # type: ignore[attr-defined]


def test_both_causes_write_one_outcome_and_skip_record_return(
    tmp_path: Path,
) -> None:
    """Дефект и правка спеки сразу: один `abandoned`, `record_return` не звучит.

    `record_return` определён исключительно для настоящего architectural
    finding и никогда не перезаписывает уже записанный исход (P3); признак
    его исполнения — переход с причиной `architectural_defect`.
    """
    stand = _defect_stand(tmp_path)
    (stand.workspace / SPEC_PATH).write_text(ADOPTED_SPEC, encoding="utf-8")

    state = stand.resume.resume(SLUG, decision="adopt_external")

    reasons = [transition.reason for transition in state.transitions]
    assert TransitionReason.EXTERNAL_SPEC_ADOPT in reasons
    assert TransitionReason.ARCHITECTURAL_DEFECT not in reasons
    records = _records(state)
    assert records["pair-r1"].outcome is SessionOutcome.ABANDONED  # type: ignore[attr-defined]
    # Находки припаркованного ревью не теряются: они — evidence перехода.
    evidence = [
        link
        for transition in state.transitions
        if transition.reason is TransitionReason.EXTERNAL_SPEC_ADOPT
        for link in transition.evidence
    ]
    assert [link.finding_id for link in evidence] == ["F-ARCH"]


def test_defect_with_a_plan_only_diff_still_returns_to_spec(tmp_path: Path) -> None:
    """P6 поверх маршрута: дефект ведёт в spec-ревизию и при plan-only дифе."""
    stand = _defect_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")

    state = stand.resume.resume(SLUG, decision="adopt_external")

    reasons = [transition.reason for transition in state.transitions]
    assert TransitionReason.ARCHITECTURAL_DEFECT in reasons
    assert _records(state)["pair-r1"].superseded_by == "spec-r2"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("stand_factory", "edited"),
    [(_adopt_stand, SPEC_PATH), (_defect_stand, PLAN_PATH)],
)
def test_adoption_past_the_return_ceiling_escalates_instead_of_returning(
    tmp_path: Path, stand_factory: Callable[..., Stand], edited: str
) -> None:
    """Потолок §7.2 — на РЕБРЕ `PAIR_LOOP → SPEC_LOOP`, обе причины считаются.

    `external_spec_adopt` и `architectural_defect` ведут по одному и тому же
    ребру, и §7.2 назначает лимит именно ребру, а не причине. Пока adopt
    потолок не проверял вовсе, оператор возвращал пайплайн в spec-контур
    сколько угодно раз — а счётчик runner'а этих возвратов не видел, потому
    что считал по причине `architectural_defect`.

    Превышение — `ESCALATED` (§7.2 дословно), а не отказ: правку человека
    пайплайн всё равно принимает чекпоинтом, но следующей ревизии не
    открывает и уходит в честный частичный результат.
    """
    stand = stand_factory(tmp_path, max_architectural_returns=0)
    (stand.workspace / edited).write_text(ADOPTED_SPEC, encoding="utf-8")

    state = stand.resume.resume(SLUG, decision="adopt_external")

    edges = [(transition.from_, transition.to) for transition in state.transitions]
    assert (PipelinePhase.PAIR_LOOP, PipelinePhase.SPEC_LOOP) not in edges
    assert (PipelinePhase.PAIR_LOOP, PipelinePhase.ESCALATED) in edges
    reasons = [transition.reason for transition in state.transitions]
    assert TransitionReason.MAX_ARCHITECTURAL_RETURNS in reasons
    assert "spec-r2" not in _records(state)
    # Санкция человека не потеряна: решение записано, сессия закрыта.
    assert [decision.kind for decision in state.operator_decisions] == [
        "adopt_external"
    ]
    assert _records(state)["pair-r1"].outcome is SessionOutcome.ABANDONED  # type: ignore[attr-defined]


def test_return_ceiling_counts_the_edge_not_the_reason(tmp_path: Path) -> None:
    """Возврат оператора занимает место в лимите наравне с дефектом (§7.2).

    Потолок в единицу: adoption с правкой спеки расходует его целиком, и
    следующий возврат — уже по архитектурной находке — обязан эскалировать.
    Пока счётчик runner'а смотрел на причину `architectural_defect`,
    `external_spec_adopt` был ему невидим, и лимит обходился бесплатно.
    """
    scripts = live_pair()
    scripts["spec-r2"] = Script()
    scripts["pair-r2"] = Script(outcome="park", issues=(ARCHITECTURAL,))
    stand = build_stand(tmp_path, scripts, max_architectural_returns=1)
    start(stand)
    (stand.workspace / SPEC_PATH).write_text(ADOPTED_SPEC, encoding="utf-8")
    state = stand.resume.resume(SLUG, decision="adopt_external")
    assert TransitionReason.EXTERNAL_SPEC_ADOPT in [
        transition.reason for transition in state.transitions
    ]

    after = stand.runner.advance(SLUG)

    reasons = [transition.reason for transition in after.transitions]
    assert reasons.count(TransitionReason.MAX_ARCHITECTURAL_RETURNS) == 1
    assert TransitionReason.ARCHITECTURAL_DEFECT not in reasons


def test_foreign_tracked_path_rejects_the_whole_adoption(tmp_path: Path) -> None:
    """Правка постороннего tracked-файла отклоняет adoption целиком."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    (stand.workspace / "README.md").write_text("посторонняя правка\n", encoding="utf-8")
    git(stand.workspace, "add", "README.md")
    git(stand.workspace, "commit", "--quiet", "-m", "посторонний файл")
    (stand.workspace / "README.md").write_text("правка вне пары\n", encoding="utf-8")

    with pytest.raises(AdoptionScopeError) as excinfo:
        stand.resume.resume(SLUG, decision="adopt_external")
    assert "README.md" in str(excinfo.value)


def test_foreign_untracked_path_rejects_the_whole_adoption(tmp_path: Path) -> None:
    """Посторонний untracked-файл adoption не переживает молча."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    (stand.workspace / "notes.txt").write_text("черновик\n", encoding="utf-8")

    with pytest.raises(AdoptionScopeError) as excinfo:
        stand.resume.resume(SLUG, decision="adopt_external")
    assert "notes.txt" in str(excinfo.value)


def test_tracked_change_under_the_session_dir_rejects_adoption(
    tmp_path: Path,
) -> None:
    """Tracked-изменённый путь под `.disputatio/` — внешняя правка control plane."""
    stand = build_stand(tmp_path, live_pair())
    control = stand.workspace / ".disputatio" / "kept.txt"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text("версионированный файл пользователя\n", encoding="utf-8")
    git(stand.workspace, "add", "--force", ".disputatio/kept.txt")
    git(stand.workspace, "commit", "--quiet", "-m", "файл в control plane")
    start(stand)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    control.write_text("переписан снаружи\n", encoding="utf-8")

    with pytest.raises(AdoptionScopeError) as excinfo:
        stand.resume.resume(SLUG, decision="adopt_external")
    assert "kept.txt" in str(excinfo.value)


def test_own_untracked_pipeline_files_do_not_break_adoption(tmp_path: Path) -> None:
    """Собственные untracked-файлы пайплайна из scope вычитаются.

    Иначе adoption не проходил бы никогда: пайплайн порождает их непрерывно,
    и буквальный полный status отвергал бы каждое решение оператора по
    собственному журналу.
    """
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    assert any(
        entry.path.startswith(".disputatio/") and not entry.tracked
        for entry in stand.git.status_entries()
    ), "стенд не воспроизводит собственные untracked-файлы пайплайна"

    state = stand.resume.resume(SLUG, decision="adopt_external")

    assert state.operator_decisions


def test_a_brand_new_plan_document_is_adopted_and_checkpointed(
    tmp_path: Path,
) -> None:
    """Новый untracked документ пары легален и входит в чекпоинт."""
    stand = _adopt_stand(tmp_path, plan_present=False)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")

    stand.resume.resume(SLUG, decision="adopt_external")

    tracked = git(stand.workspace, "ls-tree", "--name-only", "HEAD", "docs/")
    assert PLAN_PATH.split("/")[-1] in tracked


def test_the_checkpoint_carries_the_operation_trailer(tmp_path: Path) -> None:
    """Чекпоинт оператора несёт трейлер операции — по нему его и узнают."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")

    state = stand.resume.resume(SLUG, decision="adopt_external")

    operation_id = state.operator_decisions[-1].operation_id
    body = git(stand.workspace, "log", "-2", "--format=%B")
    assert f"{OPERATION_TRAILER_KEY}: {operation_id}" in body
    patch = stand.pipeline_dir() / "adoptions" / f"{operation_id}.patch"
    assert patch.is_file(), "канонический патч решения не сохранён артефактом"


def test_the_adopted_edit_survives_the_first_proposing_of_the_new_revision(
    tmp_path: Path,
) -> None:
    """`base_commit` новой ревизии — чекпоинт: reset её раунда 1 правку не стирает."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")

    stand.resume.resume(SLUG, decision="adopt_external")

    creation = stand.factory.creations[-1]
    assert creation.session_id == "pair-r2"
    assert creation.base_commit is not None
    # Ровно та пара операций, которой начинается PROPOSING (`runtime/steps.py`).
    stand.git.reset_hard(base_rev(stand.workspace, 1, base_commit=creation.base_commit))
    stand.git.clean()

    assert (stand.workspace / PLAN_PATH).read_text(encoding="utf-8") == ADOPTED_PLAN


def test_adoption_replays_after_a_crash_before_the_patch_file(tmp_path: Path) -> None:
    """Интент записан, патч не создан: повтор дописывает недостающее."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    _crash(stand, crash_after_save=1)

    with pytest.raises(Boom):
        stand.resume.resume(SLUG, decision="adopt_external")
    assert stand.manifest().next_action is not None
    assert stand.manifest().next_action.kind == "adopt_external"  # type: ignore[union-attr]

    state = _heal(stand).resume.resume(SLUG)

    assert state.operator_decisions
    operation_id = state.operator_decisions[-1].operation_id
    assert (stand.pipeline_dir() / "adoptions" / f"{operation_id}.patch").is_file()


def test_adoption_replays_after_a_crash_before_the_checkpoint(tmp_path: Path) -> None:
    """Патч создан, чекпоинт не сделан: повтор делает ровно один коммит."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    commits_before = _commit_count(stand)
    stand.git = CrashingCheckpoint(stand.git)  # type: ignore[assignment]
    stand.rebuild()

    with pytest.raises(Boom):
        stand.resume.resume(SLUG, decision="adopt_external")

    state = _heal(stand).resume.resume(SLUG)

    assert _commit_count(stand) == commits_before + 1
    assert state.operator_decisions


def test_adoption_replays_after_a_crash_before_the_commit_point(
    tmp_path: Path,
) -> None:
    """Чекпоинт сделан, commit point не записан: второго коммита не будет."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    commits_before = _commit_count(stand)
    _crash(stand, crash_on_save=2)

    with pytest.raises(Boom):
        stand.resume.resume(SLUG, decision="adopt_external")
    assert _commit_count(stand) == commits_before + 1

    state = _heal(stand).resume.resume(SLUG)

    assert _commit_count(stand) == commits_before + 1, (
        "повтор обязан узнать свой чекпоинт по трейлеру, а не создать второй"
    )
    assert _records(state)["pair-r1"].outcome is SessionOutcome.ABANDONED  # type: ignore[attr-defined]


def test_adoption_replays_the_chained_create_session(tmp_path: Path) -> None:
    """Commit point записан, `create_session` не исполнен: преемник доигрывается."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    _crash(stand, crash_after_save=2)

    with pytest.raises(Boom):
        stand.resume.resume(SLUG, decision="adopt_external")
    assert stand.manifest().next_action.kind == "create_session"  # type: ignore[union-attr]

    state = _heal(stand).resume.resume(SLUG)

    assert "pair-r2" in _records(state)
    assert len([d for d in state.operator_decisions if d.kind == "adopt_external"]) == 1


def test_discard_replays_after_a_crash_before_the_reset(tmp_path: Path) -> None:
    """Интент записан, reset не выполнен: санкция durable, повтор доигрывает."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    _crash(stand, crash_after_save=1)

    with pytest.raises(Boom):
        stand.resume.resume(SLUG, decision="discard_round")
    assert stand.manifest().next_action.kind == "discard_round"  # type: ignore[union-attr]
    assert (stand.workspace / PLAN_PATH).read_text(encoding="utf-8") == ADOPTED_PLAN

    stand.scripts["pair-r1"].outcome = "converged"
    state = _heal(stand).resume.resume(SLUG)

    assert (stand.workspace / PLAN_PATH).read_text(encoding="utf-8") != ADOPTED_PLAN
    assert [decision.kind for decision in state.operator_decisions] == ["discard_round"]


def test_discard_records_provenance_after_a_crash_before_the_decision(
    tmp_path: Path,
) -> None:
    """Reset выполнен, решение не записано — provenance не теряется."""
    stand = _adopt_stand(tmp_path)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    digest_source = stand.git.diff_readonly()
    _crash(stand, crash_on_save=2)

    with pytest.raises(Boom):
        stand.resume.resume(SLUG, decision="discard_round")
    assert (stand.workspace / PLAN_PATH).read_text(encoding="utf-8") != ADOPTED_PLAN

    stand.scripts["pair-r1"].outcome = "converged"
    state = _heal(stand).resume.resume(SLUG)

    assert [decision.kind for decision in state.operator_decisions] == ["discard_round"]
    assert state.operator_decisions[0].worktree_diff_sha256 == _sha256(digest_source), (
        "хеш дифа обязан прийти из интента: после reset'а вычислить его негде"
    )


def test_discard_restores_the_displaced_intent(tmp_path: Path) -> None:
    """Решение оператора возвращает вытесненный интент, а не обнуляет его."""
    stand = _adopt_stand(tmp_path)
    displaced = stand.manifest().next_action
    assert displaced is not None
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    stand.scripts["pair-r1"].outcome = "converged"
    seen = len(stand.driver.calls)

    stand.resume.resume(SLUG, decision="discard_round")

    assert any(call[1] == "pair-r1" for call in stand.driver.calls[seen:]), (
        f"вытесненный интент {displaced.kind} не восстановлен: сессия не пошла"
    )


def test_discard_between_sessions_needs_no_active_revision(tmp_path: Path) -> None:
    """`--discard-round` работает и когда активной ревизии нет (§8.1).

    Окно естественное: крах между commit point'ом `finish_session` и
    chained `create_session` оставляет манифест без единой незакрытой
    ревизии. Парковать там нечего — но `discard` ничего и не паркует: сброс
    к последнему принятому коммиту определён и в этом состоянии, а §8.1
    прямо требует того же явного выбора «между сессиями».
    """
    scripts = live_pair()
    scripts["pair-r1"].raise_after_write = False
    stand = build_stand(tmp_path, scripts)
    # Четвёртая запись манифеста — commit point `finish_session` спеки:
    # spec-r1 получил исход, `create_session` пары ещё не исполнен.
    _crash(stand, crash_after_save=4)
    start(stand)
    state = stand.manifest()
    assert state.next_action is not None
    assert state.next_action.kind == "create_session"
    assert all(
        record.outcome is not None
        for record in (*state.spec_sessions, *state.pair_sessions)
    ), "стенд не воспроизвёл окно «между сессиями»"

    _heal(stand)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    stand.scripts["pair-r1"].outcome = "converged"

    state = stand.resume.resume(SLUG, decision="discard_round")

    assert (stand.workspace / PLAN_PATH).read_text(encoding="utf-8") != ADOPTED_PLAN
    assert [decision.kind for decision in state.operator_decisions] == ["discard_round"]
    assert "pair-r1" in _records(state), "вытесненный create_session не доигран"


def test_a_created_revision_expects_its_base_commit_as_head(tmp_path: Path) -> None:
    """Ревизия создана, `PROPOSING` не начинался — ожидаемый `HEAD` = `base_commit`.

    Раунд здесь ещё нулевой, и наивное `base_rev(0)` цели не даёт вовсе —
    сверка HEAD молча выключилась бы ровно в том окне, где новая ревизия
    ещё ничего не закоммитила и внешний коммит виднее всего.
    """
    scripts = live_pair()
    stand = build_stand(tmp_path, scripts)
    # Вторая запись манифеста — commit point `create_session` спеки:
    # каталог ревизии создан, `run_session` ещё не исполнен.
    _crash(stand, crash_after_save=2)
    start(stand)
    _heal(stand)
    (stand.workspace / SPEC_PATH).write_text(
        "# спека\n\nчужая правка\n", encoding="utf-8"
    )
    git(stand.workspace, "add", SPEC_PATH)
    git(stand.workspace, "commit", "--quiet", "-m", "коммит мимо пайплайна")

    with pytest.raises(ExternalEditError) as excinfo:
        stand.resume.resume(SLUG)
    # SHA чужого коммита, а не слово «HEAD»: оно встречается и в шаблонной
    # прозе отказа, и такое утверждение прошло бы, даже перестань отказ
    # называть расхождение.
    assert stand.git.head_sha() in str(excinfo.value)
    assert "не совпадает ни с одним ожидаемым коммитом" in str(excinfo.value)


def test_discard_refuses_when_the_reset_target_is_unknown(tmp_path: Path) -> None:
    """Активная ревизия без снапшота конфига: `--discard-round` отказывает.

    Привязку вычислить нечем — значит неизвестно, куда обязан вернуться
    раунд. Сброс на текущий `HEAD` оставил бы в истории чужой коммит и выдал
    бы это за исполненную санкцию: тот же half-measure, что и сброс «на
    самого себя», просто в узком окне.
    """
    stand = _adopt_stand(tmp_path)
    snapshot = stand.artifact_root("pair-r1") / ".disputatio" / "config.toml"
    snapshot.unlink()
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")
    head_before = stand.git.head_sha()

    with pytest.raises(PipelineNotResumable) as excinfo:
        stand.resume.resume(SLUG, decision="discard_round")

    assert "pair-r1" in str(excinfo.value)
    assert stand.git.head_sha() == head_before
    assert (stand.workspace / PLAN_PATH).read_text(encoding="utf-8") == ADOPTED_PLAN, (
        "отказ обязан застать дерево нетронутым"
    )
    assert not stand.manifest().operator_decisions, (
        "решение не исполнено — provenance записывать нечего"
    )


def test_discard_in_a_created_revision_returns_to_the_base_commit(
    tmp_path: Path,
) -> None:
    """Цель сброса нулевого раунда — `base_commit`, а не текущий `HEAD`.

    Отличие от предыдущего теста не в обнаружении, а в исполнении санкции:
    ожидаемый набор HEAD чужой коммит отвергнет в обоих случаях, но сброс «на
    самого себя» оставил бы его в истории — то есть выполнил бы решение
    оператора наполовину.
    """
    scripts = live_pair()
    scripts["pair-r1"] = Script(outcome="converged")
    stand = build_stand(tmp_path, scripts)
    _crash(stand, crash_after_save=2)
    start(stand)
    _heal(stand)
    base_commit = stand.git.head_sha()
    (stand.workspace / SPEC_PATH).write_text(
        "# спека\n\nчужая правка\n", encoding="utf-8"
    )
    git(stand.workspace, "add", SPEC_PATH)
    git(stand.workspace, "commit", "--quiet", "-m", "коммит мимо пайплайна")

    stand.resume.resume(SLUG, decision="discard_round")

    assert stand.git.head_sha() == base_commit


def test_adopt_between_sessions_refuses_loudly(tmp_path: Path) -> None:
    """`--adopt-external` без активной ревизии — громкий отказ, а не догадка.

    Решение паркует активную сессию и открывает следующую; парковать здесь
    нечего, и молча принять правку «куда-нибудь» значило бы придумать
    маршрут, которого §3.1 не определяет.
    """
    scripts = live_pair()
    scripts["pair-r1"].raise_after_write = False
    stand = build_stand(tmp_path, scripts)
    _crash(stand, crash_after_save=4)
    start(stand)
    _heal(stand)
    (stand.workspace / PLAN_PATH).write_text(ADOPTED_PLAN, encoding="utf-8")

    with pytest.raises(PipelineNotResumable) as excinfo:
        stand.resume.resume(SLUG, decision="adopt_external")
    assert "активной ревизии" in str(excinfo.value)


class _StubGit(GitOpsFakeBase):
    """`GitOps` для scope: только статус и префикс toplevel, всё прочее — провал."""

    def __init__(self, prefix: str, entries: tuple[StatusEntry, ...]) -> None:
        self._prefix = prefix
        self._entries = entries

    def toplevel_prefix(self) -> str:
        """Путь корня пайплайна относительно toplevel репозитория."""
        return self._prefix

    def status_entries(self) -> tuple[StatusEntry, ...]:
        """Статус целиком — фильтрует потребитель, а не порт."""
        return self._entries

    def diff_head(self) -> str:
        """Scope дифф не читает — он смотрит на статус."""
        raise AssertionError("compute_scope читает дифф")

    def diff_readonly(self) -> str:
        """Scope дифф не читает — он смотрит на статус."""
        raise AssertionError("compute_scope читает дифф")

    def commit_round(self, round_no: int) -> None:
        """Scope ничего не коммитит."""
        raise AssertionError("compute_scope коммитит")

    def reset_hard(self, rev: str) -> None:
        """Scope дерево не сбрасывает."""
        raise AssertionError("compute_scope сбрасывает дерево")

    def clean(self) -> None:
        """Scope дерево не убирает."""
        raise AssertionError("compute_scope убирает дерево")


def test_scope_compares_paths_from_the_repository_toplevel(tmp_path: Path) -> None:
    """Пайплайн в подкаталоге чужого репозитория: обе стороны — от toplevel.

    Наивный фильтр по `.disputatio/` и наивное сравнение с `spec_path`
    промахиваются здесь в обе стороны: собственный журнал пайплайна выглядел
    бы посторонним файлом (adoption не прошёл бы никогда), а сам документ
    пары — не совпал бы ни с одним допустимым путём.
    """
    git = _StubGit(
        "proj/",
        (
            StatusEntry(
                path="proj/.disputatio/pipelines/pair-docs/pipeline.json", tracked=False
            ),
            StatusEntry(path="proj/docs/plan.md", tracked=True),
        ),
    )
    config = PipelineConfig(
        spec_path=Path(SPEC_PATH),
        plan_path=Path(PLAN_PATH),
        anchor_path=tmp_path / "anchors",
    )

    scope = compute_scope(git, config, allow_plan=True)

    assert scope.paths == (PLAN_PATH,)
    assert not scope.spec_touched


def _commit_count(stand: Stand) -> int:
    """Число коммитов в истории `HEAD` — детектор второго чекпоинта."""
    return int(git(stand.workspace, "rev-list", "--count", "HEAD").strip())


def _sha256(text: str) -> str:
    """sha256 текста дифа — provenance решения оператора (§4.2)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
