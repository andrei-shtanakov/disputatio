"""Тесты экспортёра TDD-evidence (`scripts/tdd_evidence_export.py`).

Инварианты, которые здесь закреплены:

- неполные данные НЕ материализуются в трекаемый артефакт;
- прежний корректный артефакт при отказе остаётся нетронутым;
- повторный экспорт даёт байт-в-байт тот же файл;
- несовместимая схема или версия — отказ с именами недостающего.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_evidence_export as exporter

from .conftest import CANDIDATE_SHA, CFG_HASH, NS, TASK

VERSION_OK = "2.35.0"


def run(project: Path, **kw: object) -> int:
    """Вызов CLI с подставленной версией spec-runner."""
    argv = [
        "--project-root",
        str(project),
        "--task-id",
        str(kw.pop("task_id", TASK)),
        "--spec-runner-version",
        str(kw.pop("version", VERSION_OK)),
    ]
    return exporter.main(argv)


def artifact(project: Path, ns: str = NS, task: str = TASK) -> Path:
    return project / "spec" / "evidence" / ns / f"{task}.json"


def test_full_chain_is_written(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    data = json.loads(artifact(project).read_text(encoding="utf-8"))
    assert data["schema"] == "disputatio/tdd-evidence/v1"
    assert data["complete"] is True
    assert data["task_id"] == TASK
    assert data["namespace"] == NS
    assert data["red"]["outcome"] == "expected_fail"
    assert data["claims"][0]["path"] == "tests/test_x.py"
    assert data["source"]["spec_runner_version"] == VERSION_OK


def test_refactoring_phase_is_carried(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    phases = json.loads(artifact(project).read_text(encoding="utf-8"))["phases"]
    assert [p["phase"] for p in phases][-1] == "refactoring"
    assert phases[-1]["detail"] == "skipped"


def test_export_is_idempotent(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    first = artifact(project).read_bytes()
    assert run(project) == 0
    assert artifact(project).read_bytes() == first


def test_json_is_canonical(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    text = artifact(project).read_text(encoding="utf-8")
    keys = [k for k in json.loads(text)]
    assert keys == sorted(keys)
    assert text.endswith("\n")


def test_no_temp_files_left(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    leftovers = list((project / "spec" / "evidence" / NS).glob("*.tmp*"))
    assert leftovers == []


@pytest.mark.parametrize(
    ("table", "missing_name"),
    [
        ("red_checkpoints", "red-checkpoint"),
        ("tdd_claims", "claims"),
        ("gate_verdicts", "gate-verdict:tdd.red"),
    ],
)
def test_missing_rows_refuse_and_name_the_gap(
    project: Path,
    full_db: Path,
    table: str,
    missing_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(f"DELETE FROM {table}")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()
    err = capsys.readouterr().err
    assert missing_name in err


def test_missing_review_verdict_refuses(project: Path, full_db: Path) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("DELETE FROM gate_verdicts WHERE gate_id = 'review'")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_unconfirmed_red_refuses(project: Path, full_db: Path) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("UPDATE red_checkpoints SET outcome = 'not_red'")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_abandoned_red_is_not_evidence(project: Path, full_db: Path) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("UPDATE red_checkpoints SET status = 'abandoned'")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_previous_artifact_survives_a_refusal(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    good = artifact(project).read_bytes()

    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("DELETE FROM tdd_claims")
    conn.close()

    assert run(project) != 0
    assert artifact(project).read_bytes() == good


def test_schema_drift_is_named(
    project: Path, full_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("DROP TABLE tdd_phases")
    conn.close()

    assert run(project) != 0
    err = capsys.readouterr().err
    assert "tdd_phases" in err


def test_old_spec_runner_is_refused(
    project: Path, full_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(project, version="2.34.0") != 0
    assert not artifact(project).exists()
    err = capsys.readouterr().err
    assert "2.34.0" in err


def test_missing_db_is_refused(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(project) != 0
    err = capsys.readouterr().err
    assert "state db" in err.lower()


def test_ambiguous_db_is_refused(
    project: Path, full_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from .conftest import seed_full

    seed_full(project / "spec" / ".executor-phase2-state.db")
    assert run(project) != 0
    err = capsys.readouterr().err
    assert "state db" in err.lower()


def test_claims_from_a_previous_lineage_are_not_evidence(
    project: Path, full_db: Path
) -> None:
    """`repair` открывает новую линию; claims старой — не доказательство этой.

    `tdd_claims.checkpoint_sha` — это red-коммит (`claims.py:314`), поэтому
    привязка проверяема: claim от другого чекпоинта не закрывает требование.
    """
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("UPDATE tdd_claims SET checkpoint_sha = ?", ("f" * 40,))
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_retired_claim_alone_is_not_evidence(project: Path, full_db: Path) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("UPDATE tdd_claims SET status = 'superseded'")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_verdict_under_another_policy_is_stale(
    project: Path, full_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Вердикт ключуется на политике: сменилась — прежний не наследуется."""
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(
            "UPDATE gate_verdicts SET config_hash = ? WHERE gate_id = 'tdd.red'",
            ("ffffffffffffffff",),
        )
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()
    assert "tdd.red" in capsys.readouterr().err


def test_verdicts_must_judge_one_tree(project: Path, full_db: Path) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(
            "UPDATE gate_verdicts SET checkpoint_sha = ? WHERE gate_id = 'review'",
            ("e" * 40,),
        )
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_unsatisfied_verdict_refuses(project: Path, full_db: Path) -> None:
    """До post_review доходят только пройденные гейты — иначе БД противоречива."""
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(
            "UPDATE gate_verdicts SET status = 'unsatisfied' WHERE gate_id = 'review'"
        )
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_latest_verdict_per_gate_wins(project: Path, full_db: Path) -> None:
    """Более ранний отказ не отменяет более позднего согласия по тому же гейту."""
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(
            "INSERT INTO gate_verdicts (task_id, gate_id, checkpoint_sha, "
            "config_hash, status, detail, timestamp) VALUES (?,?,?,?,?,?,?)",
            (
                TASK,
                "tdd.red",
                CANDIDATE_SHA,
                CFG_HASH,
                "satisfied",
                "повтор после ретрая",
                "2026-08-21T10:12:00+00:00",
            ),
        )
        conn.execute(
            "UPDATE gate_verdicts SET status = 'unsatisfied' "
            "WHERE gate_id = 'tdd.red' AND timestamp = '2026-08-21T10:11:00+00:00'"
        )
    conn.close()

    assert run(project) == 0
    data = json.loads(artifact(project).read_text(encoding="utf-8"))
    assert data["gates"]["tdd.red"]["status"] == "satisfied"


def test_judged_tree_is_recorded(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    data = json.loads(artifact(project).read_text(encoding="utf-8"))
    assert data["judged_commit"] == CANDIDATE_SHA
    assert data["red"]["commit_sha"] != data["judged_commit"]


def _git(workdir: Path, *args: str) -> None:
    """Git в тестовом дереве; окружение уже приведено фикстурой `git_env`.

    Env здесь намеренно НЕ подменяется — ровно как в `_git` корневого
    `tests/conftest.py`: герметичность (снятые `GIT_DIR`/`GIT_WORK_TREE`/
    подпись/`GIT_CONFIG_COUNT`, `os.devnull` + `GIT_CONFIG_NOSYSTEM`) —
    ответственность одной фикстуры, а не каждого вызова. Дубль с
    захардкоженным `/dev/null` расходился бы с ней молча.
    """
    try:
        subprocess.run(
            ["git", *args],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        # `CalledProcessError.__str__` печатает только код возврата, поэтому
        # причина падения фикстуры иначе не видна в отчёте pytest — та же
        # пересборка, что у `_git` корневого conftest (Copilot, PR #32).
        raise RuntimeError(
            f"git {' '.join(args)} упал ({exc.returncode}): {exc.stderr or exc.stdout}"
        ) from exc


def workstream_tree(project: Path, branch: str = "ws/w-runtime") -> Path:
    """Дерево, неотличимое от worktree Maestro: spec/maestro-*tasks.md + ветка.

    Репозиторий приносит фикстура `git_repo` (тот же `tmp_path`), поэтому
    здесь остаётся только сигнал maestro-режима и ветка. `maestro-*tasks.md` —
    тот же сигнал, что у INV-16 в `scripts/tdd_gate.py`: наличие файла, а не
    его содержимое.
    """
    # `encoding` явный: строка meta-задачи содержит эмодзи, а `write_text` без
    # него берёт локаль процесса и падает там, где она не UTF-8.
    (project / "spec" / "maestro-tasks.md").write_text(
        "- TASK-001 | ✅ DONE\n", encoding="utf-8"
    )
    _git(project, "checkout", "-q", "-b", branch)
    return project


def test_namespace_comes_from_the_workstream_branch(
    project: Path, full_db: Path, git_repo: Path
) -> None:
    """`ws/<id>` даёт `ws-<id>` и в каталоге, и в поле — INV-16, не хеш пути."""
    workstream_tree(project, "ws/w-runtime")

    assert run(project) == 0

    data = json.loads(artifact(project, ns="ws-w-runtime").read_text(encoding="utf-8"))
    assert data["namespace"] == "ws-w-runtime"
    # Сырой неймспейс БД сохранён как provenance: по нему находятся строки,
    # из которых собран артефакт (в фикстуре он намеренно ДРУГОЙ).
    assert data["state_namespace"] == NS


def test_outside_a_workstream_the_db_namespace_is_used(
    project: Path, full_db: Path
) -> None:
    """Вне workstream'а (ручной прогон, смоук) — fallback на неймспейс БД."""
    assert run(project) == 0

    data = json.loads(artifact(project).read_text(encoding="utf-8"))
    assert data["namespace"] == NS
    assert data["state_namespace"] == NS


@pytest.mark.parametrize(
    ("branch", "fragment"),
    [
        ("feature/x", "ws/"),
        ("ws/a/b", "'/'"),
    ],
)
def test_workstream_tree_with_a_wrong_branch_refuses(
    project: Path, full_db: Path, git_repo: Path, branch: str, fragment: str, capsys
) -> None:
    """В maestro-дереве неймспейс не угадывается: молчаливого fallback нет.

    INV-18: прогон Maestro не имеет права свалиться в другой неймспейс
    из-за неожиданной ветки. `ws/a/b` отдельно — иначе он схлопнулся бы
    с `ws/a-b` (замена '/' на '-' не различает исходный разделитель).
    """
    workstream_tree(project, branch)

    assert run(project) == 1

    assert not (project / "spec" / "evidence").exists()
    assert fragment in capsys.readouterr().err


def test_detached_head_in_a_workstream_tree_refuses(
    project: Path, full_db: Path, git_repo: Path, capsys
) -> None:
    """Detached HEAD — неймспейс неоднозначен, а не «сойдёт и хеш»."""
    workstream_tree(project)
    _git(project, "checkout", "-q", "--detach")

    assert run(project) == 1

    assert not (project / "spec" / "evidence").exists()
    assert "HEAD" in capsys.readouterr().err


def test_inherited_git_dir_does_not_redirect_the_namespace(
    project: Path, full_db: Path, git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """Унаследованный `GIT_DIR` перебивает `cwd` — экспортёр обязан его снять.

    Тот же класс, что описан в `tests/conftest.py`: под обёрткой git-хука или
    в шелле с экспортированным `GIT_DIR` команда отработает успешно, но
    прочитает ЧУЖОЙ репозиторий. Здесь это дало бы evidence, уехавшую в
    неймспейс соседней ветки — молча и с виду корректно.
    """
    workstream_tree(project, "ws/w-runtime")
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    _git(stranger, "init", "-q", "-b", "ws/w-stranger")
    monkeypatch.setenv("GIT_DIR", str(stranger / ".git"))

    assert run(project) == 0

    assert artifact(project, ns="ws-w-runtime").exists()
    assert not artifact(project, ns="ws-w-stranger").exists()


def test_config_from_the_environment_does_not_reach_git(
    project: Path, full_db: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GIT_CONFIG_COUNT` — конфиг поверх локального; вызов обязан его снять.

    Приоритет этих пар выше `.git/config`, и ни `GIT_CONFIG_GLOBAL`, ни
    `GIT_CONFIG_NOSYSTEM` их не отключают — то же соображение, что в
    `tests/conftest.py`. Проверяется поведением: со сломанным счётчиком
    `git symbolic-ref` падает целиком, и без фильтра экспортёр прочитал бы
    это как detached HEAD — отказ на ровном месте.
    """
    workstream_tree(project, "ws/w-runtime")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "bogus")

    assert run(project) == 0

    assert artifact(project, ns="ws-w-runtime").exists()


def test_git_failure_is_not_reported_as_detached_head(
    project: Path, full_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """«git не смог ответить» ≠ «HEAD отцеплён» — причина должна быть настоящей.

    Тот же класс, что у соседа в `_refuse_pre_existing_file`: ненулевой код
    возврата, прочитанный как факт о дереве, превращает отказ инструмента в
    ложное утверждение о состоянии. `symbolic-ref -q` различает их сам:
    detached — код 1 при пустом stderr, всё остальное — код 128 с текстом.
    """
    (project / "spec" / "maestro-tasks.md").write_text(
        "- TASK-001 | ✅ DONE\n", encoding="utf-8"
    )

    assert run(project) == 1

    err = capsys.readouterr().err
    assert "not a git repository" in err
    assert "detached" not in err.lower()
