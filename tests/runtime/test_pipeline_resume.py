"""Порядок resume пайплайна и модель внешней правки (SPEC-002 §8.1, P9).

Пять утверждений набора — ровно те места, где «как проще» ломается тихо.

* **Порядок шагов §8.1 — не косметика.** Обнаружение архитектурного дефекта
  обязано быть read-only, а сверка worktree — предшествовать ЛЮБОМУ
  мутирующему шагу: и replay интента, и возврату §7.3, потому что оба несут
  `git reset --hard`, который поверх непроверенного дерева уничтожил бы
  внешнюю правку раньше, чем её можно заметить.
* **Сверка worktree не трогает даже индекс.** Проверяется не обещанием, а
  `git status --porcelain` пользователя до и после вызова: `diff_head`
  начинается с `add --intent-to-add` и оставил бы новый файл в индексе —
  вывод git у пользователя менялся бы от того, что он запустил `resume`.
* **P9-сверка идёт до чтения манифеста, и identity берётся из анкера.**
  Манифест автору достижим, анкер — нет; взяв `session_id` из манифеста,
  сверка проверяла бы ту сессию, которую назвал подменённый файл.
* **Отсутствие файла анкера — отказ, а не пропуск.** Иначе пайплайн с
  нестандартным `anchor_path` при `resume` без того же конфига смотрел бы в
  дефолтный журнал, не находил записей и молча пропускал сверку.
* **Припаркованная сессия не возобновляется.** Reconciliation приоритетнее
  сохранённой фазы сессии: после `CONTINUE` write-ahead уже указывает на
  следующий `PROPOSING`, и session-resume, запущенный первым, продолжил бы
  сессию, которую надлежит припарковать.

Стенд — `_pipeline_stand`: настоящий git, настоящий манифест, фейковые
драйвер/фабрика/экспортёр.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

from disputatio.contracts import (
    IntegritySnapshot,
    PipelinePhase,
    SessionOutcome,
    TransitionReason,
)
from disputatio.runtime import StatusEntry
from disputatio.runtime.errors import (
    ConfigError,
    ControlPlaneTampered,
    ExternalEditError,
    PipelineNotResumable,
)
from disputatio.runtime.layout import CHANGES_PATCH_NAME, round_dir
from disputatio.runtime.pipeline_integrity import ControlPlane
from disputatio.runtime.pipeline_resume import classify_worktree

from ._fakes import GitOpsFakeBase
from ._pipeline_stand import (
    PLAN_PATH,
    SLUG,
    SPEC_PATH,
    Script,
    Stand,
    build_stand,
    git,
    live_pair,
    parked_pair,
    porcelain,
    start,
)

EXTERNAL_TEXT: Final = "# спека\n\nправка, которую никто не санкционировал\n"


def _edit_spec(stand: Stand, text: str = EXTERNAL_TEXT) -> None:
    """Внешняя правка документа — то, чего пайплайн не записывал."""
    (stand.workspace / SPEC_PATH).write_text(text, encoding="utf-8")


def _record_patch(stand: Stand, session_id: str, patch: str) -> None:
    """Кладёт `changes.patch` раунда — окно «proposal записан, раунд не принят»."""
    directory = round_dir(stand.artifact_root(session_id), 1)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / CHANGES_PATCH_NAME).write_text(patch, encoding="utf-8")


def _seed_pre_turn(stand: Stand, session_id: str) -> None:
    """Пишет в анкер `pre_turn`-снапшот ревизии — ход, прерванный на середине.

    Пути журналов подаёт тест: `runtime` путь `events.jsonl` не вычисляет
    (скан-правило [DESIGN-016]), а сверка на resume берёт их из самой записи.
    """
    plane = ControlPlane(
        workspace_root=stand.workspace,
        pipeline_dir=stand.pipeline_dir(),
        artifact_root=stand.artifact_root(session_id),
        append_only_paths=(
            stand.pipeline_dir() / "events.jsonl",
            stand.artifact_root(session_id) / ".disputatio" / "events.jsonl",
        ),
    )
    stand.anchor().append_pre_turn(
        plane.snapshot(session_id=session_id, round_no=1, operation_id="turn-seeded")
    )


def test_resume_stops_before_any_mutation_on_an_unattributed_tree(
    tmp_path: Path,
) -> None:
    """Дефект + грязное неатрибутируемое дерево → остановка ДО мутаций.

    Возврат §7.3 несёт `reset --hard`, а replay интента — свой; оба обязаны
    ждать сверки. Проверяется тремя следами: история git, вывод `git status`
    (то есть индекс) и байты манифеста.

    Грязь намеренно обоих видов. `git add --intent-to-add`, с которого
    начинается мутирующий `diff_head`, на изменённый tracked-файл не влияет
    вовсе — след он оставляет только на UNTRACKED, поднимая его в индекс
    (`??` → `A `). Без untracked-половины утверждение «индекс не тронут»
    было бы вакуумным.
    """
    stand = build_stand(tmp_path, parked_pair())
    start(stand)
    _edit_spec(stand)
    (stand.workspace / "docs" / "draft.md").write_text("черновик\n", encoding="utf-8")

    head_before = stand.git.head_sha()
    status_before = porcelain(stand.workspace)
    manifest_before = (stand.pipeline_dir() / "pipeline.json").read_bytes()
    calls_before = len(stand.driver.calls)

    with pytest.raises(ExternalEditError) as excinfo:
        stand.resume.resume(SLUG)

    assert SPEC_PATH in str(excinfo.value), "диф обязан быть в тексте отказа"
    assert stand.git.head_sha() == head_before
    assert porcelain(stand.workspace) == status_before
    assert (stand.pipeline_dir() / "pipeline.json").read_bytes() == manifest_before
    assert len(stand.driver.calls) == calls_before


def test_resume_accepts_a_diff_that_reduces_to_the_recorded_patch(
    tmp_path: Path,
) -> None:
    """Dirty diff, байт-в-байт сводящийся к `changes.patch`, — легален."""
    stand = build_stand(tmp_path, live_pair())
    start(stand)
    _edit_spec(stand)
    _record_patch(stand, "pair-r1", stand.git.diff_readonly())
    stand.scripts["pair-r1"].outcome = "converged"

    calls_before = len(stand.driver.calls)
    stand.resume.resume(SLUG)

    assert len(stand.driver.calls) > calls_before, (
        "легальный патч обязан пропустить resume к сессии, а не остановить его"
    )


def test_resume_stops_when_the_diff_diverges_from_the_recorded_patch(
    tmp_path: Path,
) -> None:
    """Тот же патч плюс одна строка — уже неатрибутируемое состояние."""
    stand = build_stand(tmp_path, live_pair())
    start(stand)
    _edit_spec(stand)
    _record_patch(stand, "pair-r1", stand.git.diff_readonly() + "\n")

    with pytest.raises(ExternalEditError):
        stand.resume.resume(SLUG)


def test_resume_stops_on_a_commit_the_pipeline_did_not_make(tmp_path: Path) -> None:
    """Чистое дерево на чужом коммите — тоже неатрибутируемое состояние.

    Вторая половина модели §8.1: «HEAD совпадает с записанным **и** дерево
    чистое». Человеческий коммит поверх раунда оставляет дерево чистым, и
    сверка по одному дифу пропустила бы resume — а `reset --hard` первого же
    `PROPOSING` увёл бы ветку с этого коммита без всякой санкции.
    """
    stand = build_stand(tmp_path, live_pair())
    start(stand)
    _edit_spec(stand)
    git(stand.workspace, "add", SPEC_PATH)
    git(stand.workspace, "commit", "--quiet", "-m", "правка человека мимо пайплайна")
    # Чисто именно в том смысле, в каком чистоту понимает классификация:
    # канонический дифф пуст (`.disputatio/` из него исключён всегда).
    assert stand.git.diff_readonly() == "", "дифф обязан быть пуст"

    head_before = stand.git.head_sha()
    calls_before = len(stand.driver.calls)

    with pytest.raises(ExternalEditError) as excinfo:
        stand.resume.resume(SLUG)

    # Именно SHA чужого коммита, а не слово «HEAD»: оно есть и в шаблонной
    # прозе отказа, и утверждение по нему прошло бы, даже перестань отказ
    # называть расхождение вовсе.
    assert head_before in str(excinfo.value)
    assert "не совпадает ни с одним ожидаемым коммитом" in str(excinfo.value)
    assert stand.git.head_sha() == head_before
    assert len(stand.driver.calls) == calls_before


def test_head_at_the_committed_round_is_not_a_foreign_commit(tmp_path: Path) -> None:
    """Коммит текущего раунда — ожидаемый `HEAD`, а не подмена.

    `commit_round(N)` исполняется ДО `apply_decision` (`runtime/steps.py`),
    поэтому убитый в этом окне процесс штатно оставляет `HEAD` на коммите
    раунда N при `session.json`, всё ещё называющем раунд N. Сверка по одной
    только цели сброса объявила бы это состояние внешней правкой.
    """
    stand = build_stand(tmp_path, live_pair())
    start(stand)
    (stand.workspace / PLAN_PATH).write_text(
        "# план\n\nработа раунда\n", encoding="utf-8"
    )
    stand.git.commit_round(1)
    stand.scripts["pair-r1"].outcome = "converged"

    calls_before = len(stand.driver.calls)
    stand.resume.resume(SLUG)

    assert len(stand.driver.calls) > calls_before


def test_discard_drops_the_commit_made_outside_the_pipeline(tmp_path: Path) -> None:
    """`--discard-round` сбрасывает к цели раунда, а не к текущему `HEAD`.

    Сброс «на самого себя» сохранил бы ровно тот коммит, из-за которого
    resume и остановился, — санкция оператора была бы исполнена наполовину.
    """
    stand = build_stand(tmp_path, live_pair())
    start(stand)
    expected_head = stand.git.head_sha()
    _edit_spec(stand)
    git(stand.workspace, "add", SPEC_PATH)
    git(stand.workspace, "commit", "--quiet", "-m", "правка человека мимо пайплайна")
    stand.scripts["pair-r1"].outcome = "converged"

    state = stand.resume.resume(SLUG, decision="discard_round")

    assert stand.git.head_sha() == expected_head
    assert EXTERNAL_TEXT not in (stand.workspace / SPEC_PATH).read_text(
        encoding="utf-8"
    )
    assert [decision.kind for decision in state.operator_decisions] == ["discard_round"]


def test_parked_session_is_returned_and_never_resumed(tmp_path: Path) -> None:
    """Припаркованная pair-сессия не возобновляется — исполняется возврат §7.3."""
    scripts = parked_pair()
    scripts["spec-r2"] = Script(outcome="deadlock")
    stand = build_stand(tmp_path, scripts)
    start(stand)
    pair_calls_before = _calls_for(stand, "pair-r1")

    state = stand.resume.resume(SLUG)

    assert _calls_for(stand, "pair-r1") == pair_calls_before, (
        "session-resume припаркованной сессии — нарушение §8.1 шага 2"
    )
    assert TransitionReason.ARCHITECTURAL_DEFECT in [
        transition.reason for transition in state.transitions
    ]
    pair = {record.session_id: record for record in state.pair_sessions}
    assert pair["pair-r1"].outcome is SessionOutcome.ARCHITECTURAL_DEFECT
    assert pair["pair-r1"].superseded_by == "spec-r2"


def test_resume_refuses_a_session_that_already_has_an_outcome(tmp_path: Path) -> None:
    """Сессия с `outcome ≠ null` не возобновляется никогда (§8.1 шаг 1)."""
    stand = build_stand(tmp_path, live_pair())
    start(stand)
    state = stand.manifest()
    stand.store.save(
        state.model_copy(
            update={
                "pair_sessions": [
                    record.model_copy(update={"outcome": SessionOutcome.ABANDONED})
                    for record in state.pair_sessions
                ]
            }
        )
    )

    with pytest.raises(PipelineNotResumable) as excinfo:
        stand.resume.resume(SLUG)
    assert "abandoned" in str(excinfo.value)


def test_resume_refuses_a_terminal_pipeline(tmp_path: Path) -> None:
    """Пайплайн в `DONE` возобновлению не подлежит — переходов из DONE нет."""
    stand = build_stand(tmp_path, {"spec-r1": Script(), "pair-r1": Script()})
    start(stand)
    assert stand.manifest().phase is PipelinePhase.DONE

    with pytest.raises(PipelineNotResumable):
        stand.resume.resume(SLUG)


def test_resume_verifies_the_anchor_before_reading_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Анкер прочитан раньше `pipeline.json` — сверка до потенциальной подмены."""
    scripts = parked_pair()
    scripts["spec-r2"] = Script(outcome="deadlock")
    stand = build_stand(tmp_path, scripts)
    start(stand)
    anchor_path = stand.anchor().path
    manifest = stand.pipeline_dir() / "pipeline.json"
    reads = _spy_reads(monkeypatch, (anchor_path, manifest))

    stand.resume.resume(SLUG)

    assert reads, "ни анкер, ни манифест не прочитаны — сверять было нечего"
    assert reads[0] == anchor_path, f"порядок чтений: {reads[:3]}"


def test_resume_takes_the_verified_identity_from_the_anchor_record(
    tmp_path: Path,
) -> None:
    """Сверяется сессия из ЗАПИСИ анкера, а не активная сессия манифеста.

    Манифест автору достижим: взяв identity оттуда, сверка проверяла бы ту
    ревизию, которую назвал подменённый файл, и подмена в другой прошла бы.
    """
    stand = build_stand(tmp_path, parked_pair())
    start(stand)
    # Активная сессия манифеста — pair-r1; анкер описывает ход spec-r1.
    _seed_pre_turn(stand, "spec-r1")
    review = round_dir(stand.artifact_root("spec-r1"), 1) / "review.json"
    review.write_text('{"verdict": "approve"}', encoding="utf-8")

    with pytest.raises(ControlPlaneTampered) as excinfo:
        stand.resume.resume(SLUG)
    # Не просто «отказал», а отказал ИМЕННО по подменённому файлу spec-r1:
    # сверка чужой ревизии тоже дала бы расхождение (наборы путей разошлись
    # бы), и утверждение «упало» одно её от нужной не отличает.
    assert (
        "sessions/spec-r1/.disputatio/rounds/001/review.json: содержимое изменилось"
        in str(excinfo.value)
    )


def test_tampered_control_plane_fails_the_pipeline(tmp_path: Path) -> None:
    """Расхождение со снапшотом → `FAILED (invariant_violation)` в манифесте."""
    stand = build_stand(tmp_path, parked_pair())
    start(stand)
    _seed_pre_turn(stand, "pair-r1")
    review = round_dir(stand.artifact_root("pair-r1"), 1) / "review.json"
    review.write_text('{"verdict": "approve"}', encoding="utf-8")

    with pytest.raises(ControlPlaneTampered):
        stand.resume.resume(SLUG)

    state = stand.manifest()
    assert state.phase is PipelinePhase.FAILED
    assert state.transitions[-1].reason is TransitionReason.INVARIANT_VIOLATION


def test_forged_terminal_phase_does_not_swallow_the_tamper(tmp_path: Path) -> None:
    """Подменённая фаза `DONE` не отменяет отказ P9 (§8.1 шаг 0).

    Подделка, оставляющая манифест схемно валидным, но объявляющая пайплайн
    завершённым, бьёт ровно в обработку, ради которой инвариант и заведён:
    `_fail` отвергает переход из `DONE` (§2), и его отказ, выпущенный
    наружу, унёс бы с собой диагноз подмены — то есть подделка терминальной
    фазы отключала бы fail-closed обработку P9.

    Утверждается пересечение границы, а не половинка: подмена делается через
    штатное хранилище манифеста, а сверка вызывается настоящим
    `PipelineResume.resume`, а не хуком политики напрямую.
    """
    stand = build_stand(tmp_path, parked_pair())
    start(stand)
    _seed_pre_turn(stand, "pair-r1")
    # Схемно валиден — подделан ровно в одном поле, как и в модели атаки.
    stand.store.save(stand.manifest().model_copy(update={"phase": PipelinePhase.DONE}))
    calls_before = len(stand.driver.calls)

    with pytest.raises(ControlPlaneTampered) as excinfo:
        stand.resume.resume(SLUG)

    # Именно по подменённому манифесту: сверка упала бы и от постороннего
    # файла, и утверждение «упало» этих двух причин не различает.
    assert f"pipelines/{SLUG}/pipeline.json: содержимое изменилось" in str(
        excinfo.value
    )
    assert len(stand.driver.calls) == calls_before, "resume продолжил пайплайн"
    # Отказ записи не потерян, а стал причиной: человеку нужны оба факта —
    # что control plane подменён и что закрыть пайплайн не удалось.
    assert isinstance(excinfo.value.__cause__, ValueError)
    # Fail-closed держится анкером, а не манифестом: записать `FAILED` поверх
    # подделанного `DONE` нельзя (§2), и всё же второй resume отказывает так же.
    with pytest.raises(ControlPlaneTampered):
        stand.rebuild().resume.resume(SLUG)


def test_resume_skips_verification_after_a_completed_turn(tmp_path: Path) -> None:
    """`turn_completed` — сверять нечего: штатные записи подменой не являются.

    Ход завершён успешно, runtime законно записал артефакты раунда и двинул
    `session.json`, процесс убит до следующего `before_author_turn`.
    """
    scripts = parked_pair()
    scripts["spec-r2"] = Script(outcome="deadlock")
    stand = build_stand(tmp_path, scripts)
    start(stand)
    _seed_pre_turn(stand, "pair-r1")
    anchor = stand.anchor()
    last = anchor.last_record()
    assert last is not None
    anchor.append_completion(_identity(last.session_id, last.round, last.operation_id))
    # Ровно те штатные записи, которые runtime делает сразу после сверки.
    review = round_dir(stand.artifact_root("pair-r1"), 1) / "review.json"
    review.write_text(review.read_text(encoding="utf-8") + " ", encoding="utf-8")

    state = stand.resume.resume(SLUG)

    assert state.phase is not PipelinePhase.FAILED


def test_resume_without_the_anchor_file_refuses(tmp_path: Path) -> None:
    """Файла анкера нет — отказ с требованием указать `--config`, не пропуск."""
    stand = build_stand(tmp_path, parked_pair())
    start(stand)
    stand.anchor().path.unlink()

    with pytest.raises(ConfigError) as excinfo:
        stand.resume.resume(SLUG)
    assert "--config" in str(excinfo.value)


def test_resume_names_every_place_of_a_directory_without_a_manifest(
    tmp_path: Path,
) -> None:
    """Каталог пайплайна есть, манифеста нет: отказ называет все три места.

    Окно между созданием каталога и первой записью манифеста невосстановимо
    автоматически, и человек обязан услышать сразу и про каталог, и про
    анкер вне рабочего дерева, и про то, чем продолжать.
    """
    stand = build_stand(tmp_path, parked_pair())
    start(stand)
    (stand.pipeline_dir() / "pipeline.json").unlink()

    with pytest.raises(PipelineNotResumable) as excinfo:
        stand.resume.resume(SLUG)
    message = str(excinfo.value)
    assert str(stand.pipeline_dir()) in message
    assert str(stand.anchor().path) in message
    assert "run" in message


def test_resume_refuses_a_decision_when_there_is_nothing_to_decide(
    tmp_path: Path,
) -> None:
    """Операторский флаг без остановки — ошибка «нечего решать» (§3.1)."""
    scripts = parked_pair()
    scripts["spec-r2"] = Script(outcome="deadlock")
    stand = build_stand(tmp_path, scripts)
    start(stand)

    with pytest.raises(PipelineNotResumable) as excinfo:
        stand.resume.resume(SLUG, decision="discard_round")
    assert "нечего решать" in str(excinfo.value)


def test_classify_worktree_never_reaches_the_mutating_diff(tmp_path: Path) -> None:
    """Классификация берёт `diff_readonly`, а не `diff_head` (§8.1 шаг 3).

    `diff_head` начинается с `add --intent-to-add`: вызвав его, сверка
    оставила бы новый файл автора в индексе — до всякого решения оператора.
    """
    stand = build_stand(tmp_path, parked_pair())
    start(stand)

    # `HEAD` фейка — настоящий: сверка identity обязана СОЙТИСЬ, иначе
    # классификация вернула бы `unattributed`, не дойдя до дифа вовсе, и
    # утверждение про `diff_head` стало бы вакуумным.
    verdict = classify_worktree(
        _ReadOnlyGit("diff --git a/x b/x\n", stand.git.head_sha()),
        stand.manifest(),
        workspace_root=stand.workspace,
    )

    assert verdict == "unattributed"


def test_classify_worktree_calls_a_clean_tree_clean(tmp_path: Path) -> None:
    """Пустой дифф — `clean`: продолжать можно без санкции оператора."""
    stand = build_stand(tmp_path, parked_pair())
    start(stand)

    assert (
        classify_worktree(stand.git, stand.manifest(), workspace_root=stand.workspace)
        == "clean"
    )


class _ReadOnlyGit(GitOpsFakeBase):
    """`GitOps`, у которого мутирующий дифф — провал теста."""

    def __init__(self, diff: str, head: str) -> None:
        self._diff = diff
        self._head = head

    def head_sha(self) -> str:
        """Identity дерева — половина модели внешней правки §8.1."""
        return self._head

    def diff_head(self) -> str:
        """Мутирует индекс — сверке worktree он запрещён."""
        raise AssertionError(
            "classify_worktree вызвал diff_head: сверка §8.1 обязана быть "
            "немутирующей, а diff_head начинается с add --intent-to-add"
        )

    def diff_readonly(self) -> str:
        """Канонический дифф поверх одноразового индекса."""
        return self._diff

    def commit_round(self, round_no: int) -> None:
        """Классификация раундов не коммитит."""
        raise AssertionError("classify_worktree коммитит раунд")

    def reset_hard(self, rev: str) -> None:
        """Классификация дерево не сбрасывает."""
        raise AssertionError("classify_worktree сбрасывает дерево")

    def clean(self) -> None:
        """Классификация дерево не убирает."""
        raise AssertionError("classify_worktree убирает дерево")

    def status_entries(self) -> tuple[StatusEntry, ...]:
        """Статус классификации не нужен — она смотрит на дифф."""
        return ()


def _identity(session_id: str, round_no: int, operation_id: str) -> IntegritySnapshot:
    """`IntegritySnapshot` только с identity — вход `append_completion`."""
    return IntegritySnapshot(
        session_id=session_id, round=round_no, operation_id=operation_id
    )


def _calls_for(stand: Stand, session_id: str) -> int:
    """Сколько раз драйвер сессии звали для этой ревизии."""
    return sum(1 for call in stand.driver.calls if call[1] == session_id)


def _spy_reads(monkeypatch: pytest.MonkeyPatch, watched: Sequence[Path]) -> list[Path]:
    """Журналирует чтения интересующих файлов в порядке обращений."""
    seen: list[Path] = []
    original = Path.read_text

    def spy(self: Path, *args: object, **kwargs: object) -> str:
        if self in watched:
            seen.append(self)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy)
    return seen
