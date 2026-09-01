"""Экспорт готовой к публикации пары (SPEC-002 §8.2, P7).

Четыре свойства, ради которых написан модуль:

* **идемпотентность** — повтор без изменения входа даёт байт-в-байт тот же
  `result/`, включая `manifest.json` (никакого времени экспорта в байтах);
* **commit marker** — `manifest.json` пишется последним и перечисляет полный
  ожидаемый набор файлов с их sha256, а снимается ПЕРВЫМ, до перезаписи
  содержимого (SPEC-002 §8.2): уже экспортированный отдельный файл остаётся
  целым, при прерванном повторном экспорте marker удаляется, и набор
  считается невалидным до успешного нового marker; повторный вызов чинит
  его и убирает stale-остаток;
* **честный partial** — `converged: false`, причина эскалации и открытые
  находки в манифесте при `partial=True`, и другой набор при `partial=False`;
* **`publish.txt` без выдумки** — шаблон с предупреждением, когда remote/
  branch не определены, и корректный shell quoting, когда определены.
"""

import hashlib
import json
import shlex
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from disputatio.contracts import (
    BudgetUsed,
    EvidenceLink,
    FileRef,
    PairDocuments,
    PipelinePhase,
    PipelineState,
    SessionOutcome,
    SessionRecord,
    Transition,
    TransitionReason,
)
from disputatio.events.atomic import atomic_write as real_atomic_write
from disputatio.events.pipeline_paths import result_dir
from disputatio.runtime.pipeline_export import (
    MANIFEST_NAME,
    PR_BODY_NAME,
    PR_TITLE_NAME,
    PUBLISH_NAME,
    ExportFn,
    export_pipeline,
)

_CREATED_AT = datetime(2026, 8, 28, 9, 0, 0, tzinfo=UTC)
_STARTED_AT = datetime(2026, 8, 28, 9, 1, 0, tzinfo=UTC)
_ESCALATED_AT = datetime(2026, 8, 28, 10, 30, 0, tzinfo=UTC)
_PIPELINE_ID = "docs-foo"


def _file_ref(name: str, marker: str) -> FileRef:
    """`FileRef` со стабильным sha256-подобным значением из `marker`."""
    return FileRef(path=name, sha256=(marker * 64)[:64])


def _base_state() -> PipelineState:
    """Минимально полный `PipelineState` — для тестов достаточно этого набора."""
    return PipelineState(
        pipeline_id=_PIPELINE_ID,
        created_at=_CREATED_AT,
        phase=PipelinePhase.PAIR_LOOP,
        task=_file_ref("task.md", "a"),
        config=_file_ref("config.toml", "b"),
        checklists=_file_ref("checklists.toml", "c"),
        documents=PairDocuments(
            spec_path="docs/specs/foo.md", plan_path="docs/plans/foo.md"
        ),
        spec_sessions=[
            SessionRecord(
                revision=1,
                session_id="20260828-090100-aaaa",
                path="sessions/1",
                entry_hashes={"docs/specs/foo.md": "absent"},
                outcome=SessionOutcome.CONVERGED,
            )
        ],
        pair_sessions=[
            SessionRecord(
                revision=1,
                session_id="20260828-091500-bbbb",
                path="sessions/2",
                entry_hashes={"docs/plans/foo.md": _file_ref("x", "d").sha256},
                outcome=None,
            )
        ],
        transitions=[
            Transition(
                from_=PipelinePhase.IDLE,
                to=PipelinePhase.SPEC_LOOP,
                reason=TransitionReason.STARTED,
                at=_STARTED_AT,
            ),
            Transition(
                from_=PipelinePhase.SPEC_LOOP,
                to=PipelinePhase.PAIR_LOOP,
                reason=TransitionReason.SPEC_CONVERGED,
                at=_STARTED_AT,
            ),
        ],
        budget_used=BudgetUsed(tokens=4242, wall_seconds=12.5, cost_usd_est=0.5),
        anchor_id=_PIPELINE_ID,
    )


def _escalated_state() -> PipelineState:
    """Состояние, эскалированное из `PAIR_LOOP` с одной открытой находкой."""
    base = _base_state()
    return base.model_copy(
        update={
            "phase": PipelinePhase.ESCALATED,
            "transitions": [
                *base.transitions,
                Transition(
                    from_=PipelinePhase.PAIR_LOOP,
                    to=PipelinePhase.ESCALATED,
                    reason=TransitionReason.SESSION_DEADLOCK,
                    evidence=[
                        EvidenceLink(
                            session_id="20260828-091500-bbbb",
                            round=3,
                            finding_id="I-003-A",
                        )
                    ],
                    at=_ESCALATED_AT,
                ),
            ],
        }
    )


def _converged_state() -> PipelineState:
    """Сошедшаяся пара на входе в экспорт — фаза `EXPORTING` (§2, §8.2)."""
    base = _base_state()
    return base.model_copy(
        update={
            "phase": PipelinePhase.EXPORTING,
            "transitions": [
                *base.transitions,
                Transition(
                    from_=PipelinePhase.PAIR_LOOP,
                    to=PipelinePhase.EXPORTING,
                    reason=TransitionReason.PAIR_CONVERGED,
                    at=_ESCALATED_AT,
                ),
            ],
        }
    )


def _failed_state() -> PipelineState:
    """Состояние, упавшее в `FAILED` из `PAIR_LOOP` (§2, `session_failed`)."""
    base = _base_state()
    return base.model_copy(
        update={
            "phase": PipelinePhase.FAILED,
            "transitions": [
                *base.transitions,
                Transition(
                    from_=PipelinePhase.PAIR_LOOP,
                    to=PipelinePhase.FAILED,
                    reason=TransitionReason.SESSION_FAILED,
                    at=_ESCALATED_AT,
                ),
            ],
        }
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _manifest(directory: Path) -> dict[str, Any]:
    return json.loads(_read(directory / MANIFEST_NAME))


def test_export_fn_alias_matches_the_produced_signature() -> None:
    """`ExportFn` — тот самый порт, который получает инъекцией runner (задача 15)."""
    assert callable(export_pipeline)
    result: ExportFn = export_pipeline  # noqa: F841 - проверка совместимости типа


def test_export_writes_the_four_canonical_files(tmp_path: Path) -> None:
    """`result/` содержит ровно четыре файла: три содержимых и манифест."""
    state = _base_state()

    manifest_path = export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    directory = result_dir(tmp_path, _PIPELINE_ID)
    assert manifest_path == directory / MANIFEST_NAME
    on_disk = {entry.name for entry in directory.iterdir()}
    assert on_disk == {PR_TITLE_NAME, PR_BODY_NAME, PUBLISH_NAME, MANIFEST_NAME}


def test_export_is_byte_for_byte_idempotent(tmp_path: Path) -> None:
    """Повторный экспорт без изменения входа не меняет ни одного байта."""
    state = _base_state()
    kwargs = {
        "workspace_root": tmp_path,
        "remote_url": "git@github.com:acme/repo.git",
        "branch": "docs/foo",
        "partial": False,
    }

    export_pipeline(state, **kwargs)
    directory = result_dir(tmp_path, _PIPELINE_ID)
    first = {entry.name: entry.read_bytes() for entry in directory.iterdir()}

    export_pipeline(state, **kwargs)
    second = {entry.name: entry.read_bytes() for entry in directory.iterdir()}

    assert first == second
    manifest = _manifest(directory)
    assert "exported_at" not in manifest
    assert "export_time" not in json.dumps(manifest)


def test_manifest_carries_the_declared_created_at_and_transition_timestamps(
    tmp_path: Path,
) -> None:
    """Метки фактов (`created_at`, `at`) сохраняются — не время самого экспорта."""
    state = _base_state()

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url=None,
        branch=None,
        partial=False,
    )

    manifest = _manifest(result_dir(tmp_path, _PIPELINE_ID))
    assert manifest["created_at"] == _CREATED_AT.isoformat().replace("+00:00", "Z")
    at_values = {entry["at"] for entry in manifest["transitions"]}
    assert _STARTED_AT.isoformat().replace("+00:00", "Z") in at_values


def test_manifest_is_the_commit_marker_written_last(tmp_path: Path) -> None:
    """Манифест перечисляет полный набор файлов с их sha256 — это commit marker."""
    state = _base_state()

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url="https://github.com/acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    directory = result_dir(tmp_path, _PIPELINE_ID)
    manifest = _manifest(directory)
    files = manifest["files"]
    assert set(files) == {PR_TITLE_NAME, PR_BODY_NAME, PUBLISH_NAME}
    for name, recorded_sha256 in files.items():
        assert isinstance(recorded_sha256, str)
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        assert recorded_sha256 == actual


def test_interrupted_export_leaves_no_manifest_and_repair_fixes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обрыв перед манифестом — набор без манифеста; повтор его чинит."""
    state = _base_state()
    directory = result_dir(tmp_path, _PIPELINE_ID)

    def _boom(path: Path, content: str | bytes, *, encoding: str = "utf-8") -> None:
        if path.name == MANIFEST_NAME:
            raise RuntimeError("симулированный обрыв перед commit marker'ом")
        real_atomic_write(path, content, encoding=encoding)

    monkeypatch.setattr("disputatio.runtime.pipeline_export.atomic_write", _boom)
    with pytest.raises(RuntimeError):
        export_pipeline(
            state,
            workspace_root=tmp_path,
            remote_url="git@github.com:acme/repo.git",
            branch="docs/foo",
            partial=False,
        )

    assert not (directory / MANIFEST_NAME).exists()
    assert (directory / PR_TITLE_NAME).exists()

    monkeypatch.undo()
    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    manifest = _manifest(directory)
    for name in manifest["files"]:
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        assert manifest["files"][name] == actual


def test_interrupted_re_export_leaves_no_stale_commit_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обрыв ПОВТОРНОГО экспорта не оставляет манифест прошлого набора.

    Отличие от предыдущего теста — старт: там `result/` пуст, и обрыв
    оставляет набор без манифеста сам собой. Здесь экспорт уже отработал,
    и рвётся перезапись поверх готового набора — случай, ради которого
    `cli.py` и предлагает повторный экспорт как починку. Манифест —
    commit marker (P8): пережив обрыв, он объявляет валидным набор, чьё
    содержимое обновлено наполовину, а записанные в нём sha256 больше не
    соответствуют файлам.
    """
    directory = result_dir(tmp_path, _PIPELINE_ID)
    export_pipeline(
        _converged_state(),
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )
    first_title = (directory / PR_TITLE_NAME).read_bytes()
    first_manifest = (directory / MANIFEST_NAME).read_bytes()

    def _boom(path: Path, content: str | bytes, *, encoding: str = "utf-8") -> None:
        if path.name == PUBLISH_NAME:
            raise RuntimeError("симулированный обрыв посреди перезаписи набора")
        real_atomic_write(path, content, encoding=encoding)

    monkeypatch.setattr("disputatio.runtime.pipeline_export.atomic_write", _boom)
    with pytest.raises(RuntimeError):
        export_pipeline(
            _escalated_state(),
            workspace_root=tmp_path,
            remote_url="git@github.com:acme/repo.git",
            branch="docs/foo",
            partial=True,
        )

    # Набор действительно обновлён наполовину — иначе тест был бы пустым.
    assert (directory / PR_TITLE_NAME).read_bytes() != first_title
    assert not (directory / MANIFEST_NAME).exists(), (
        "манифест прошлого экспорта пережил обрыв повторного: commit marker "
        f"объявляет валидным набор с чужими sha256 ({first_manifest[:32]!r}…)"
    )


def test_start_of_export_removes_stale_files_outside_the_new_set(
    tmp_path: Path,
) -> None:
    """Stale-остаток прежнего экспорта не переживает повтор."""
    state = _base_state()
    directory = result_dir(tmp_path, _PIPELINE_ID)
    directory.mkdir(parents=True)
    stale = directory / "old_leftover.txt"
    stale.write_text("мусор прошлой версии экспортёра", encoding="utf-8")

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    assert not stale.exists()
    on_disk = {entry.name for entry in directory.iterdir()}
    assert on_disk == {PR_TITLE_NAME, PR_BODY_NAME, PUBLISH_NAME, MANIFEST_NAME}


def test_honest_partial_export_reports_not_converged_with_reason_and_findings(
    tmp_path: Path,
) -> None:
    """`partial=True` — `converged: false`, причина эскалации, открытые находки."""
    state = _escalated_state()

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=True,
    )

    manifest = _manifest(result_dir(tmp_path, _PIPELINE_ID))
    assert manifest["converged"] is False
    assert manifest["escalation_reason"] == TransitionReason.SESSION_DEADLOCK.value
    assert manifest["open_issues"] == [
        {
            "session_id": "20260828-091500-bbbb",
            "round": 3,
            "finding_id": "I-003-A",
        }
    ]


def test_non_partial_export_reports_converged_with_no_escalation(
    tmp_path: Path,
) -> None:
    """Сошедшаяся фаза + `partial=False` — `converged: true`, без эскалации."""
    state = _converged_state()

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    manifest = _manifest(result_dir(tmp_path, _PIPELINE_ID))
    assert manifest["converged"] is True
    assert manifest["escalation_reason"] is None
    assert manifest["open_issues"] == []


@pytest.mark.parametrize(
    ("state_factory", "expected_reason"),
    [
        (_failed_state, TransitionReason.SESSION_FAILED.value),
        (_escalated_state, TransitionReason.SESSION_DEADLOCK.value),
    ],
)
def test_stopped_pipeline_is_never_converged_without_the_flag(
    tmp_path: Path,
    state_factory: Callable[[], PipelineState],
    expected_reason: str,
) -> None:
    """Записанная остановка сильнее отсутствия `--partial` (§8.2, P7).

    `converged` выводится из фазы и истории переходов, а не из одного лишь
    флага: манифест, несущий `"phase": "failed"` рядом с `"converged":
    true`, противоречит сам себе — и делал это ровно тогда, когда оператор
    забыл флаг, то есть в самом вероятном случае.
    """
    export_pipeline(
        state_factory(),
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    manifest = _manifest(result_dir(tmp_path, _PIPELINE_ID))
    assert manifest["converged"] is False
    assert manifest["escalation_reason"] == expected_reason


def test_export_before_a_terminal_phase_is_not_converged(tmp_path: Path) -> None:
    """Пайплайн в середине контура сходимости не достиг — и не заявляет её."""
    export_pipeline(
        _base_state(),
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    manifest = _manifest(result_dir(tmp_path, _PIPELINE_ID))
    assert manifest["phase"] == PipelinePhase.PAIR_LOOP.value
    assert manifest["converged"] is False
    assert manifest["escalation_reason"] is None


def test_partial_flag_only_narrows_honesty(tmp_path: Path) -> None:
    """`--partial` — операторское уточнение: снимает `converged`, но не ставит.

    Обратное направление проверено соседним тестом: флага нет, а
    сошедшимся пайплайн от этого не становится.
    """
    export_pipeline(
        _converged_state(),
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=True,
    )

    manifest = _manifest(result_dir(tmp_path, _PIPELINE_ID))
    assert manifest["converged"] is False
    assert manifest["escalation_reason"] is None, (
        "эскалации не было — причину нельзя выдумывать по флагу оператора"
    )


def test_both_outcomes_share_the_same_manifest_key_set(tmp_path: Path) -> None:
    """Партиал и полный экспорт отличаются значениями, а не набором ключей."""
    escalated_root = tmp_path / "escalated"
    converged_root = tmp_path / "converged"
    escalated_root.mkdir()
    converged_root.mkdir()

    export_pipeline(
        _escalated_state(),
        workspace_root=escalated_root,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=True,
    )
    export_pipeline(
        _base_state(),
        workspace_root=converged_root,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    escalated = _manifest(result_dir(escalated_root, _PIPELINE_ID))
    converged = _manifest(result_dir(converged_root, _PIPELINE_ID))
    assert set(escalated) == set(converged)


def test_publish_txt_uses_a_warning_template_when_remote_or_branch_is_unknown(
    tmp_path: Path,
) -> None:
    """Remote/branch не определены однозначно → шаблон с предупреждением."""
    state = _base_state()

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url=None,
        branch="docs/foo",
        partial=False,
    )

    publish = _read(result_dir(tmp_path, _PIPELINE_ID) / PUBLISH_NAME)
    assert "git push" not in publish or "<REMOTE>" in publish
    assert "<REMOTE>" in publish
    assert "<BRANCH>" in publish
    lowered = publish.casefold()
    assert "внимание" in lowered or "warning" in lowered


def test_publish_txt_uses_the_real_remote_and_branch_when_both_are_known(
    tmp_path: Path,
) -> None:
    """Remote и branch известны → настоящая команда `git push` + `gh pr create`."""
    state = _base_state()

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    publish = _read(result_dir(tmp_path, _PIPELINE_ID) / PUBLISH_NAME)
    assert "<REMOTE>" not in publish
    assert "<BRANCH>" not in publish
    assert shlex.quote("git@github.com:acme/repo.git") in publish
    assert shlex.quote("docs/foo") in publish
    assert "git push" in publish
    assert "gh pr create" in publish
    assert "--draft" in publish


def test_publish_txt_references_pr_files_relative_to_the_workspace_root(
    tmp_path: Path,
) -> None:
    """`--body-file`/`--title` адресуют `result/` от корня, а не голым именем.

    `publish.txt` рассчитан на запуск из корня рабочего дерева — там же, где
    выполняются остальные git-команды сессии, — а `pr_title.txt`/
    `pr_body.md` лежат внутри `.disputatio/pipelines/<id>/result/`; голое имя
    файла без этого префикса не нашлось бы с той рабочей директории.
    """
    state = _base_state()

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    directory = result_dir(tmp_path, _PIPELINE_ID)
    relative = directory.relative_to(tmp_path).as_posix()
    publish = _read(directory / PUBLISH_NAME)
    assert f"{relative}/{PR_BODY_NAME}" in publish
    assert f"{relative}/{PR_TITLE_NAME}" in publish
    assert f" {PR_BODY_NAME}" not in publish
    assert f"cat {PR_TITLE_NAME}" not in publish


def test_publish_txt_quotes_a_branch_name_with_shell_metacharacters(
    tmp_path: Path,
) -> None:
    """Ветка со спецсимволом заквочена через `shlex.quote`, а не вклеена сырой."""
    state = _base_state()
    hostile_branch = "docs/foo; rm -rf /"

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch=hostile_branch,
        partial=False,
    )

    publish = _read(result_dir(tmp_path, _PIPELINE_ID) / PUBLISH_NAME)
    assert shlex.quote(hostile_branch) in publish
    assert f" {hostile_branch}\n" not in publish
    assert f" {hostile_branch} " not in publish
    parsed_lines = [shlex.split(line) for line in publish.splitlines() if line.strip()]
    for tokens in parsed_lines:
        if "push" in tokens:
            assert hostile_branch in tokens


def test_publish_txt_quotes_a_remote_url_with_shell_metacharacters(
    tmp_path: Path,
) -> None:
    """Remote с пробелом/спецсимволом тоже проходит через `shlex.quote`."""
    state = _base_state()
    hostile_remote = "git@github.com:acme/repo with spaces.git"

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url=hostile_remote,
        branch="docs/foo",
        partial=False,
    )

    publish = _read(result_dir(tmp_path, _PIPELINE_ID) / PUBLISH_NAME)
    assert shlex.quote(hostile_remote) in publish
    parsed_lines = [shlex.split(line) for line in publish.splitlines() if line.strip()]
    for tokens in parsed_lines:
        if "push" in tokens:
            assert hostile_remote in tokens


def test_pr_title_and_body_are_non_empty_and_reference_the_documents(
    tmp_path: Path,
) -> None:
    """`pr_title.txt`/`pr_body.md` называют реальные пути пары, не заглушку."""
    state = _base_state()

    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url="git@github.com:acme/repo.git",
        branch="docs/foo",
        partial=False,
    )

    directory = result_dir(tmp_path, _PIPELINE_ID)
    title = _read(directory / PR_TITLE_NAME)
    body = _read(directory / PR_BODY_NAME)
    assert title.strip()
    assert "docs/specs/foo.md" in title or "docs/specs/foo.md" in body
    assert "docs/plans/foo.md" in body
    assert _PIPELINE_ID in body


def test_pair_pr_body_labels_are_untouched(tmp_path: Path) -> None:
    """Регрессия: `pr_body.md` пары не меняется редакцией v0.2.

    Тест выше (`..._pr_files`) проверяет присутствие ПУТЕЙ, но не подписей,
    и потому пропустил бы замену «Спека:»/«План:» общим списком документов
    вида. Ограничение плана называет у пары ровно три допустимых отличия, и
    все три — в сериализации манифеста, не в пользовательском артефакте.
    """
    state = _base_state()
    export_pipeline(
        state,
        workspace_root=tmp_path,
        remote_url=None,
        branch="docs/foo",
        partial=False,
    )

    body = _read(result_dir(tmp_path, _PIPELINE_ID) / PR_BODY_NAME)
    assert "Спека: `docs/specs/foo.md`" in body
    assert "План: `docs/plans/foo.md`" in body


def test_pair_pr_title_is_byte_identical_to_v01(tmp_path: Path) -> None:
    """Заголовок пары после перехода на `documents.paths()` тот же байт-в-байт."""
    export_pipeline(
        _base_state(),
        workspace_root=tmp_path,
        remote_url=None,
        branch="docs/foo",
        partial=False,
    )

    title = _read(result_dir(tmp_path, _PIPELINE_ID) / PR_TITLE_NAME)
    # `_base_state` стоит в PAIR_LOOP, поэтому сходимости нет и заголовок
    # честно несёт пометку частичного исхода — это прежнее поведение (§8.2).
    assert title == "[partial] docs: docs/specs/foo.md + docs/plans/foo.md\n"
