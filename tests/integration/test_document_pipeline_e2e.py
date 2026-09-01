"""Сквозные сценарии вида `document`: SPEC-002 v0.2 §10; задача 8 плана.

Тот же принцип, что у парного набора: подменён ровно один шов — реестр
адаптеров, то есть граница, за которой начинается чужой процесс. Всё
остальное настоящее — git, пять baseline-гейтов §6, хранилище манифеста,
анкер P9, runner, resume, экспорт, CLI.

Четыре сценария, и три из них проверяют свойства, которых у пары нет:
единственный контур терминален; анти-сикофантия раунда 1 — ЕДИНСТВЕННАЯ
защита от «написал и сразу approve», потому что второго контура, который
перепроверил бы результат, у вида нет; и манифест, записанный до редакции
v0.2, продолжается новой реализацией (К2 §4.2).
"""

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from disputatio.cli import main
from disputatio.contracts import (
    SCHEMA_V2,
    AgentTurn,
    ArtifactEvidence,
    ChecklistItem,
    Decision,
    Outcome,
    PipelinePhase,
    Review,
    Role,
    SessionPhase,
    SessionState,
    Verdict,
)
from disputatio.runtime import composition
from disputatio.runtime.pipeline_runner import artifact_root_of, pipeline_dir_of

SLUG: Final = "charter"
DOCUMENT_PATH: Final = "docs/charter.md"
WORK_BRANCH: Final = "docs/charter"
TASK_TEXT: Final = "Написать чартер поведенческого конвейера"

REPO_DIR_NAME: Final = "repo"
ANCHOR_DIR_NAME: Final = "anchors"

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

#: Операторский чеклист контура `doc` (§5.3): состав и роль объявляет конфиг.
DOC_IDS: Final[tuple[str, ...]] = ("B1", "B3")
FINDINGS_ITEM: Final = "B3"


# ----------------------------------------------------------------------
# Скриптованный агент
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Turn:
    """Один ответ агента: правки рабочего дерева плюс текст ответа."""

    text: str
    edits: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Script:
    """Очереди ответов по ключу `(session_id, role)` и журнал промптов."""

    turns: dict[tuple[str, str], list[Turn]]
    prompts: list[tuple[str, str, str]] = field(default_factory=list)

    def take(self, session_id: str, role: str) -> Turn:
        queue = self.turns.get((session_id, role))
        assert queue, f"скрипт исчерпан для {session_id}/{role}"
        return queue.pop(0)

    def prompts_of(self, session_id: str, role: str) -> list[str]:
        return [
            prompt
            for recorded_id, recorded_role, prompt in self.prompts
            if (recorded_id, recorded_role) == (session_id, role)
        ]


class ScriptedAgent:
    """`AgentAdapter`-фейк, правящий настоящее рабочее дерево."""

    def __init__(
        self,
        *,
        role: Role,
        session_dir: Path,
        event_sink: Any,
        session: str,
        script: Script,
    ) -> None:
        self._role = role
        self._workspace = session_dir
        self._session = session
        self._script = script
        del event_sink

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        self._script.prompts.append((self._session, self._role.value, prompt))
        turn = self._script.take(self._session, self._role.value)
        for relative, text in turn.edits.items():
            path = self._workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return AgentTurn(text=turn.text, session_ref=session_ref, tokens_used=7)


# ----------------------------------------------------------------------
# Артефакты агента
# ----------------------------------------------------------------------


def proposal(round_no: int) -> str:
    """`proposal.md` doc-раунда: фронтматтер §4.2 плюс короткое тело."""
    responds = "null" if round_no == 1 else f'"rounds/{round_no - 1:03d}/review.json"'
    return (
        "---\n"
        f'schema: "{SCHEMA_V2}"\n'
        f"round: {round_no}\n"
        'role: "author"\n'
        f"responds_to: {responds}\n"
        f'files_touched: ["{DOCUMENT_PATH}"]\n'
        'self_declared_status: "complete"\n'
        "---\n"
        f"Раунд {round_no}: документ обновлён.\n"
    )


def review_json(
    round_no: int,
    verdict: Verdict,
    *,
    failed: Mapping[str, Sequence[str]] = {},
    issues: Sequence[Any] = (),
) -> str:
    """`review.json` doc-ревьюера контура `doc` — набор id объявил оператор."""
    items = [
        ChecklistItem(
            id=item_id,
            status="fail" if item_id in failed else "pass",
            evidence=[
                ArtifactEvidence(kind="artifact", ref=DOCUMENT_PATH, lines="1-3")
            ],
            issue_ids=list(failed.get(item_id, ())),
        )
        for item_id in DOC_IDS
    ]
    model = Review(
        schema=SCHEMA_V2,
        round=round_no,
        role=Role.REVIEWER,
        verdict=verdict,
        confidence=0.9,
        issues=list(issues),
        checked=[DOCUMENT_PATH],
        summary="скриптованное ревью чартера",
        checklist=items,
    )
    return model.model_dump_json(by_alias=True)


def charter_text(revision: str) -> str:
    """Чартер, проходящий baseline-гейты §6."""
    return f"# Чартер\n\n## Границы\n\nРедакция {revision}.\n"


def converging_author() -> list[Turn]:
    return [
        Turn(text=proposal(1), edits={DOCUMENT_PATH: charter_text("doc-r1")}),
        Turn(text=proposal(2), edits={DOCUMENT_PATH: charter_text("doc-r2")}),
    ]


def converging_reviews() -> list[Turn]:
    """Раунд 1 — замечание, раунд 2 — approve с чистым чеклистом."""
    from disputatio.contracts import Issue, Severity

    finding = Issue(
        id="R1-1",
        severity=Severity.MAJOR,
        file=DOCUMENT_PATH,
        claim="R1-1: границы описаны неполно",
        evidence="строки 1-3",
    )
    return [
        Turn(
            text=review_json(
                1,
                Verdict.REQUEST_CHANGES,
                failed={FINDINGS_ITEM: ["R1-1"]},
                issues=[finding],
            )
        ),
        Turn(text=review_json(2, Verdict.APPROVE)),
    ]


def happy_path_turns() -> dict[tuple[str, str], list[Turn]]:
    return {
        ("doc-r1", "author"): converging_author(),
        ("doc-r1", "reviewer"): converging_reviews(),
    }


# ----------------------------------------------------------------------
# Стенд
# ----------------------------------------------------------------------


def git(workdir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=workdir, check=False, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} упал с кодом {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )
    return completed.stdout


CONFIG_TEMPLATE: Final = """\
[pipeline]
document_path = "{document}"
anchor_path = "{anchors}"
protected_branches = ["master", "main"]

[pipeline.checklists.doc]
findings_item = "{findings_item}"

[pipeline.checklists.doc.items]
B1 = "границы и не-цели названы явно"
B3 = "нет blocker/major-находок"

[agents.author]
adapter = "fake"
model = "m"

[agents.reviewer]
adapter = "fake"
model = "m"

[limits]
max_rounds = {max_rounds}
max_total_tokens = 10000000
max_wall_seconds = 36000
schema_retries = 2
"""


@dataclass(frozen=True, slots=True)
class Stand:
    workspace: Path
    anchor_root: Path
    config_path: Path
    script: Script

    def argv(self, command: str, *extra: str) -> list[str]:
        return [
            "pipeline",
            command,
            "--slug",
            SLUG,
            "--root",
            str(self.workspace),
            "--config",
            str(self.config_path),
            *extra,
        ]

    def pipeline_dir(self) -> Path:
        return pipeline_dir_of(self.workspace, SLUG)

    def manifest(self) -> dict[str, Any]:
        payload = (self.pipeline_dir() / "pipeline.json").read_text(encoding="utf-8")
        loaded = json.loads(payload)
        assert isinstance(loaded, dict)
        return loaded

    def artifact_root(self, session_id: str) -> Path:
        return artifact_root_of(self.workspace, SLUG, session_id)

    def session_state(self, session_id: str) -> SessionState:
        path = self.artifact_root(session_id) / ".disputatio" / "session.json"
        return SessionState.model_validate_json(path.read_text(encoding="utf-8"))

    def decision(self, session_id: str, round_no: int) -> Decision:
        path = (
            self.artifact_root(session_id)
            / ".disputatio"
            / "rounds"
            / f"{round_no:03d}"
            / "decision.json"
        )
        return Decision.model_validate_json(path.read_text(encoding="utf-8"))

    def result(self) -> dict[str, Any]:
        payload = (self.pipeline_dir() / "result" / "manifest.json").read_text(
            encoding="utf-8"
        )
        loaded = json.loads(payload)
        assert isinstance(loaded, dict)
        return loaded


def build_stand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turns: dict[tuple[str, str], list[Turn]],
    *,
    max_rounds: int = 5,
) -> Stand:
    """Репозиторий с одним документом, конфиг вида `document` и агент."""
    workspace = tmp_path / REPO_DIR_NAME
    (workspace / "docs").mkdir(parents=True)
    git(workspace, "init", "--quiet", "-b", "master")
    git(workspace, "config", "user.name", "disputatio-tests")
    git(workspace, "config", "user.email", "tests@disputatio.local")
    (workspace / DOCUMENT_PATH).write_text(charter_text("исходная"), encoding="utf-8")
    git(workspace, "add", DOCUMENT_PATH)
    git(workspace, "commit", "--quiet", "-m", "исходный чартер")
    git(workspace, "switch", "--quiet", "-c", WORK_BRANCH)

    anchors = tmp_path / ANCHOR_DIR_NAME
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(
            document=DOCUMENT_PATH,
            anchors=anchors.as_posix(),
            findings_item=FINDINGS_ITEM,
            max_rounds=max_rounds,
        ),
        encoding="utf-8",
    )

    script = Script(turns=turns)
    monkeypatch.setitem(
        composition.ADAPTER_FACTORIES,
        "fake",
        lambda **kwargs: ScriptedAgent(script=script, **kwargs),
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return Stand(
        workspace=workspace,
        anchor_root=anchors,
        config_path=config_path,
        script=script,
    )


def clock() -> Any:
    moment = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    counter = {"n": 0}

    def now() -> datetime:
        counter["n"] += 1
        return moment + timedelta(seconds=counter["n"])

    return now


def run_cli(stand: Stand, command: str, *extra: str) -> int:
    return main(stand.argv(command, *extra), now=clock())


# ----------------------------------------------------------------------
# Сценарии
# ----------------------------------------------------------------------


def test_document_pipeline_runs_to_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Один контур доходит до `result/` — второго у вида нет (§2, §8.2)."""
    stand = build_stand(tmp_path, monkeypatch, happy_path_turns())

    code = run_cli(stand, "run", "--task", TASK_TEXT)

    assert code == EXIT_OK
    manifest = stand.manifest()
    assert manifest["phase"] == PipelinePhase.DONE.value
    assert manifest["documents"] == {
        "kind": "document",
        "document_path": DOCUMENT_PATH,
    }
    assert [record["session_id"] for record in manifest["doc_sessions"]] == ["doc-r1"]
    assert manifest["doc_sessions"][0]["outcome"] == "converged"
    assert manifest["spec_sessions"] == [] and manifest["pair_sessions"] == []
    assert manifest["schema"] == "disputatio/pipeline/v2"

    result = stand.result()
    assert result["converged"] is True
    assert (stand.pipeline_dir() / "result" / "manifest.json").exists()
    body = (stand.pipeline_dir() / "result" / "pr_body.md").read_text(encoding="utf-8")
    assert f"Документ: `{DOCUMENT_PATH}`" in body
    assert "pair" not in body.lower()


def test_document_pipeline_escalation_exports_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`DEADLOCK` → `ESCALATED` → честный частичный экспорт, код ≠ 0 (P7)."""
    stand = build_stand(tmp_path, monkeypatch, happy_path_turns(), max_rounds=1)

    code = run_cli(stand, "run", "--task", TASK_TEXT)

    assert code == EXIT_FAILED
    manifest = stand.manifest()
    assert manifest["phase"] == PipelinePhase.DONE.value
    assert [transition["to"] for transition in manifest["transitions"]].count(
        PipelinePhase.ESCALATED.value
    ) == 1
    result = stand.result()
    assert result["converged"] is False
    assert result["escalation_reason"] == "session_deadlock"


def test_round_one_approve_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """У вида `document` это ЕДИНСТВЕННАЯ защита: второго контура нет (§5.1).

    Скрипт одобряет документ в первом же раунде чистым чеклистом при зелёных
    гейтах — то есть даёт ядру всё, чего требует критерий сходимости §5.1
    SPEC-001, кроме одного: раунд не первый. У пары «написал и сразу
    approve» перепроверил бы второй контур; здесь перепроверять некому,
    поэтому анти-сикофантия и есть вся защита.
    """
    turns = happy_path_turns()
    turns[("doc-r1", "reviewer")] = [
        Turn(text=review_json(1, Verdict.APPROVE)),
        Turn(text=review_json(2, Verdict.APPROVE)),
    ]
    stand = build_stand(tmp_path, monkeypatch, turns)

    assert run_cli(stand, "run", "--task", TASK_TEXT) == EXIT_OK

    first = stand.decision("doc-r1", 1)
    assert first.outcome is not Outcome.CONVERGED
    assert "sycophancy" in first.reason
    assert stand.decision("doc-r1", 2).outcome is Outcome.CONVERGED
    assert stand.session_state("doc-r1").state is SessionPhase.DONE
    # Содержательный цикл — не формальность: автора спросили дважды.
    assert len(stand.script.prompts_of("doc-r1", "author")) == 2


def test_doc_scope_gate_sees_only_its_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Правка постороннего файла валит `doc-scope` (§6): границу видит гейт.

    У контура `doc` читаемое и правимое совпадают, и это не делает гейт
    бесполезным: посторонних файлов в репозитории все остальные.
    """
    turns = happy_path_turns()
    turns[("doc-r1", "author")] = [
        Turn(
            text=proposal(1),
            edits={
                DOCUMENT_PATH: charter_text("doc-r1"),
                "docs/foreign.md": "# посторонний\n",
            },
        ),
        Turn(text=proposal(2), edits={DOCUMENT_PATH: charter_text("doc-r2")}),
    ]
    stand = build_stand(tmp_path, monkeypatch, turns)

    run_cli(stand, "run", "--task", TASK_TEXT)

    verification = json.loads(
        (
            stand.artifact_root("doc-r1")
            / ".disputatio"
            / "rounds"
            / "001"
            / "verification.json"
        ).read_text(encoding="utf-8")
    )
    scope = [gate for gate in verification["gates"] if gate["name"] == "doc-scope"]
    assert scope and scope[0]["status"] == "fail"
