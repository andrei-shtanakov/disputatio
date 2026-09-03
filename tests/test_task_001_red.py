"""RED: TASK-001 — BEH-01 Run фиксирует версионированное proof immutable-модели."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from disputatio.contracts import PipelineKind, ResolvedChecklist
from disputatio.events import FilePipelineStateStore
from disputatio.events.pipeline_events import PipelineEvent
from disputatio.runtime import GitCli, PipelineConfig
from disputatio.runtime.pipeline_runner import PipelineRunner, SessionCreation

SLUG = "charter"
DOCUMENT_PATH = "docs/charter.md"


class _CrashBeforeFirstSession(RuntimeError):
    """Инжектированный крах между commit point и первой сессией."""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _build_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    (workspace / "docs").mkdir(parents=True)
    _git(workspace, "init", "--quiet", "-b", "master")
    _git(workspace, "config", "user.name", "disputatio-tests")
    _git(workspace, "config", "user.email", "tests@disputatio.local")
    (workspace / DOCUMENT_PATH).write_text("# чартер\n", encoding="utf-8")
    _git(workspace, "add", DOCUMENT_PATH)
    _git(workspace, "commit", "--quiet", "-m", "исходный документ")
    _git(workspace, "switch", "--quiet", "-c", "docs/charter")
    return workspace


def test_run_commits_versioned_semantic_proof(tmp_path: Path) -> None:
    """`run` до первой сессии атомарно фиксирует в манифесте ссылку на
    версионированное доказательство итоговой immutable-проекции (BEH-01,
    FR-01): манифест несёт `semantic_proof` — `{path, sha256}`, как уже
    несёт `config` и `checklists`, — и указанный файл существует и называет
    версию канонизации. Крах между этой записью и первой сессией (здесь —
    `session_factory`, которая никогда не вызывается) не мешает: proof уже
    закоммичен той же атомарной записью манифеста, что и первый intent.
    """
    workspace = _build_workspace(tmp_path)
    config = PipelineConfig(
        kind=PipelineKind.DOCUMENT,
        document_path=Path(DOCUMENT_PATH),
        anchor_path=tmp_path / "anchors",
        checklists={
            "doc": ResolvedChecklist(
                order=("B1",),
                texts={"B1": "нет blocker/major-находок"},
                findings_item="B1",
            )
        },
    )

    def _factory(creation: SessionCreation) -> None:
        raise _CrashBeforeFirstSession(
            "процесс убит до первой сессии — proof уже обязан быть на диске"
        )

    def _driver(artifact_root: Path, session_id: str, policy: object) -> None:
        raise AssertionError("драйвер не вызывается раньше первой сессии")

    def _exporter(*args: object, **kwargs: object) -> None:
        raise AssertionError("экспортёр не вызывается раньше первой сессии")

    def _sink_emit(event: PipelineEvent) -> None:
        return None

    runner = PipelineRunner(
        boundary_policies={},
        store=FilePipelineStateStore(workspace),
        sink=type("_Sink", (), {"emit": staticmethod(_sink_emit)})(),
        git=GitCli(workspace),
        session_driver=_driver,  # type: ignore[arg-type]
        session_factory=_factory,  # type: ignore[arg-type]
        exporter=_exporter,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 9, 3, tzinfo=UTC),
        config=config,
        workspace_root=workspace,
    )

    try:
        runner.run(SLUG, "ЗАДАЧА: отполировать чартер")
    except _CrashBeforeFirstSession:
        pass

    manifest = json.loads(
        (workspace / ".disputatio" / "pipelines" / SLUG / "pipeline.json").read_bytes()
    )
    proof_ref = manifest.get("semantic_proof")
    assert proof_ref is not None, (
        "манифест не несёт ссылки на доказательство immutable-проекции — "
        "`run` обязан зафиксировать версионированное proof атомарно, до "
        "первой сессии (BEH-01), одной записью с первым intent'ом"
    )

    proof_path = (
        workspace / ".disputatio" / "pipelines" / SLUG / proof_ref["path"]
    )
    proof = json.loads(proof_path.read_bytes())
    assert "projection_schema_version" in proof, (
        "доказательство обязано нести версию канонизации immutable-проекции "
        "(BEH-01) — без неё нечем отличить совместимую проекцию от несовместимой"
    )
