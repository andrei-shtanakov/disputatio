"""Сквозные сценарии `disp pipeline` на fake-адаптерах (SPEC-002 §3.1, §10).

Единственный набор, который проверяет пайплайн ЦЕЛИКОМ: от argv до `result/`.
Всё, что ниже CLI, — настоящее. Подменён ровно один шов — реестр адаптеров
`composition.ADAPTER_FACTORIES`, то есть граница, за которой начинается чужой
процесс. За ней стоит скриптованный агент: он получает промпт, правит файлы
рабочего дерева и возвращает готовый текст ответа. Всё остальное —
`GitCli` над настоящим репозиторием, `DocVerifier` с пятью baseline-гейтами
§6, `FilePipelineStateStore`, `PipelineEventSink`, `IntegrityAnchor`,
`PipelineRunner`, `PipelineResume`, `export_pipeline` — работает как в
продакшене.

Четыре решения набора, каждое куплено конкретным способом ошибиться:

* **Репозиторий вложен в `tmp_path`, анкер лежит рядом с ним.** P9 требует
  журнал целостности ВНЕ рабочего дерева, и `tmp_path/anchors` рядом с
  `tmp_path/repo` — единственная раскладка, где это верно без записи за
  пределы каталога теста.
* **`XDG_STATE_HOME` подменяется на каждом тесте.** Дефолтный `anchor_root`
  читается из окружения (`pipeline_config._default_anchor_root`), и тест,
  забывший его подменить, писал бы в домашний каталог разработчика — а
  сценарий «resume без `--config` смотрит не туда» иначе доказывал бы
  свойство машины, а не кода.
* **Сессия сходится не раньше раунда 2.** Анти-сикофантия §5 SPEC-001
  засчитывает approve раунда 1 только для `analyze` без правок, поэтому
  скрипт «одобрил сразу» — это тест анти-сикофантии, а не happy path.
* **Скрипт агента ключуется парой `(session_id, role)`.** Ревизии пайплайна
  называются детерминированно (`spec-r1`, `pair-r2`), и ключ по ревизии —
  единственный способ утверждать «pair-r2 стартовал БЕЗ наследства»: очередь
  ответов одной ревизии не может незаметно достаться другой.
"""

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import pytest

from disputatio.cli import main
from disputatio.contracts import (
    SCHEMA_V2,
    AgentTurn,
    ArtifactEvidence,
    ChecklistItem,
    Decision,
    Issue,
    Outcome,
    PipelinePhase,
    Review,
    Role,
    SessionPhase,
    SessionState,
    Severity,
    Verdict,
)
from disputatio.runtime import composition
from disputatio.runtime.pipeline_runner import artifact_root_of, pipeline_dir_of

SLUG: Final = "pair-docs"
SPEC_PATH: Final = "docs/spec.md"
PLAN_PATH: Final = "docs/plan.md"
WORK_BRANCH: Final = "docs/pair"
TASK_TEXT: Final = "Отполировать пару «спека + план» до сходимости"

#: Репозиторий обязан быть ВЛОЖЕННЫМ в `tmp_path`, а не самим `tmp_path`:
#: анкер P9 живёт вне рабочего дерева.
REPO_DIR_NAME: Final = "repo"
ANCHOR_DIR_NAME: Final = "anchors"

#: Коды возврата CLI ([DESIGN-019]); дублируются здесь намеренно —
#: утверждение «0 при сходимости» обязано быть проверяемым из теста, а не
#: сверкой константы с самой собой.
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_ERROR: Final = 2


class Boom(RuntimeError):
    """Крах, инжектированный тестом: процесс убит посреди хода автора."""


# ----------------------------------------------------------------------
# Скрипт агента
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Turn:
    """Один ответ агента: правки рабочего дерева плюс текст ответа."""

    text: str
    edits: Mapping[str, str] = field(default_factory=dict)
    boom: bool = False


@dataclass(slots=True)
class Script:
    """Очереди ответов по ключу `(session_id, role)` и журнал промптов."""

    turns: dict[tuple[str, str], list[Turn]]
    prompts: list[tuple[str, str, str]] = field(default_factory=list)

    def take(self, session_id: str, role: str) -> Turn:
        """Следующий ответ очереди; исчерпанная очередь — провал теста."""
        queue = self.turns.get((session_id, role))
        assert queue, f"скрипт исчерпан для {session_id}/{role}"
        return queue.pop(0)

    def prompts_of(self, session_id: str, role: str) -> list[str]:
        """Промпты, доехавшие до конкретной роли конкретной ревизии."""
        return [
            prompt
            for recorded_id, recorded_role, prompt in self.prompts
            if (recorded_id, recorded_role) == (session_id, role)
        ]


class ScriptedAgent:
    """`AgentAdapter`-фейк, правящий настоящее рабочее дерево.

    Правки делает именно агент, а не тест: `changes.patch`, `doc-scope` и
    коммит раунда считаются по дереву, и подложенный тестом файл доказывал бы
    работу гейтов на данных, которых сессия не производила.
    """

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
        self._sink = event_sink

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        """Журналирует промпт, применяет правки и отдаёт текст ответа."""
        self._script.prompts.append((self._session, self._role.value, prompt))
        turn = self._script.take(self._session, self._role.value)
        if turn.boom:
            raise Boom(f"процесс убит на ходе {self._role.value} {self._session}")
        for relative, text in turn.edits.items():
            path = self._workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return AgentTurn(text=turn.text, session_ref=session_ref, tokens_used=7)


# ----------------------------------------------------------------------
# Тексты артефактов агента
# ----------------------------------------------------------------------


def proposal(round_no: int, touched: Sequence[str]) -> str:
    """`proposal.md` doc-раунда: фронтматтер §4.2 плюс короткое тело."""
    responds = "null" if round_no == 1 else f'"rounds/{round_no - 1:03d}/review.json"'
    files = ", ".join(f'"{path}"' for path in touched)
    return (
        "---\n"
        f'schema: "{SCHEMA_V2}"\n'
        f"round: {round_no}\n"
        'role: "author"\n'
        f"responds_to: {responds}\n"
        f"files_touched: [{files}]\n"
        'self_declared_status: "complete"\n'
        "---\n"
        f"Раунд {round_no}: документы обновлены.\n"
    )


def evidence(ref: str) -> list[ArtifactEvidence]:
    """Минимальный непустой evidence чеклиста (V2 §5.2)."""
    return [ArtifactEvidence(kind="artifact", ref=ref, lines="1-3")]


def checklist(
    contour: Literal["spec", "pair"],
    *,
    failed: Mapping[str, Sequence[str]] = {},
) -> list[ChecklistItem]:
    """Полный чеклист контура: всё `pass`, кроме перечисленных в `failed`."""
    ids = (
        ("S1", "S2", "S3", "S4", "S5")
        if contour == "spec"
        else ("P1", "P2", "P3", "P4", "P5")
    )
    ref = SPEC_PATH if contour == "spec" else PLAN_PATH
    items: list[ChecklistItem] = []
    for item_id in ids:
        issue_ids = list(failed.get(item_id, ()))
        items.append(
            ChecklistItem(
                id=item_id,
                status="fail" if item_id in failed else "pass",
                evidence=evidence(ref),
                issue_ids=issue_ids,
            )
        )
    return items


def review_json(
    round_no: int,
    contour: Literal["spec", "pair"],
    verdict: Verdict,
    *,
    issues: Sequence[Issue] = (),
    failed: Mapping[str, Sequence[str]] = {},
    summary: str = "скриптованное ревью",
) -> str:
    """`review.json` doc-ревьюера — ровно та форма, которую ждёт §5.2."""
    model = Review(
        schema=SCHEMA_V2,
        round=round_no,
        role=Role.REVIEWER,
        verdict=verdict,
        confidence=0.9,
        issues=list(issues),
        checked=[SPEC_PATH, PLAN_PATH],
        summary=summary,
        checklist=checklist(contour, failed=failed),
    )
    return model.model_dump_json(by_alias=True)


def issue(
    issue_id: str,
    *,
    severity: Severity = Severity.MAJOR,
    defect_class: Literal["architectural", "execution"] | None = None,
    file: str = SPEC_PATH,
) -> Issue:
    """Находка ревьюера с непустым evidence — деградации REQ-009 не подлежит."""
    return Issue(
        id=issue_id,
        severity=severity,
        file=file,
        claim=f"{issue_id}: замечание",
        evidence="строки 1-3",
        defect_class=defect_class,
    )


# ----------------------------------------------------------------------
# Документы рабочего дерева
# ----------------------------------------------------------------------


def spec_text(revision: str, *, broken_link: bool = False) -> str:
    """Спека, проходящая baseline-гейты §6; `broken_link` ломает `doc-links`."""
    tail = "\nСм. [пропавший документ](missing.md).\n" if broken_link else ""
    return f"# Спека\n\n## Требования\n\nРедакция {revision}.\n{tail}"


def plan_text(revision: str) -> str:
    """План, проходящий baseline-гейты §6."""
    return f"# План\n\n## Задачи\n\nРедакция {revision}.\n"


# ----------------------------------------------------------------------
# Стенд
# ----------------------------------------------------------------------


def git(workdir: Path, *args: str) -> str:
    """git теста; ненулевой код — `RuntimeError` со stderr."""
    completed = subprocess.run(
        ["git", *args], cwd=workdir, check=False, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} упал с кодом {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )
    return completed.stdout


@dataclass(frozen=True, slots=True)
class Stand:
    """Собранный стенд одного сценария: дерево, конфиг, скрипт, часы."""

    workspace: Path
    anchor_root: Path
    config_path: Path
    script: Script

    def argv(self, command: str, *extra: str) -> list[str]:
        """argv подкоманды `disp pipeline <command>` этого стенда."""
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
        """`.disputatio/pipelines/<slug>` рабочего дерева."""
        return pipeline_dir_of(self.workspace, SLUG)

    def manifest(self) -> dict[str, Any]:
        """Манифест пайплайна как он лежит на диске."""
        payload = (self.pipeline_dir() / "pipeline.json").read_text(encoding="utf-8")
        loaded = json.loads(payload)
        assert isinstance(loaded, dict)
        return loaded

    def artifact_root(self, session_id: str) -> Path:
        """`artifact_root` одной ревизии."""
        return artifact_root_of(self.workspace, SLUG, session_id)

    def session_state(self, session_id: str) -> SessionState:
        """`session.json` ревизии."""
        path = self.artifact_root(session_id) / ".disputatio" / "session.json"
        return SessionState.model_validate_json(path.read_text(encoding="utf-8"))

    def round_dir(self, session_id: str, round_no: int) -> Path:
        """`rounds/NNN` ревизии."""
        return (
            self.artifact_root(session_id)
            / ".disputatio"
            / "rounds"
            / f"{round_no:03d}"
        )

    def decision(self, session_id: str, round_no: int) -> Decision:
        """`decision.json` раунда ревизии."""
        path = self.round_dir(session_id, round_no) / "decision.json"
        return Decision.model_validate_json(path.read_text(encoding="utf-8"))

    def result(self) -> dict[str, Any]:
        """`result/manifest.json` экспорта пары."""
        payload = (self.pipeline_dir() / "result" / "manifest.json").read_text(
            encoding="utf-8"
        )
        loaded = json.loads(payload)
        assert isinstance(loaded, dict)
        return loaded


#: Имя конфига, который CLI берёт без `--config` ([DESIGN-019]). Стенд кладёт
#: его в репозиторий ТРЕКАЕМЫМ и БЕЗ `anchor_path`: только так «resume без
#: `--config`» упирается в шаг 0 §8.1 (журнал не там), а не в отсутствие
#: файла конфига — то есть проверяет то свойство, ради которого написан.
DEFAULT_CONFIG_NAME: Final = "disputatio.toml"

CONFIG_TEMPLATE: Final = """\
[pipeline]
spec_path = "{spec}"
plan_path = "{plan}"
{anchor_line}protected_branches = ["master", "main"]

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
schema_retries = {schema_retries}
"""


def build_stand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turns: dict[tuple[str, str], list[Turn]],
    *,
    max_rounds: int = 5,
    schema_retries: int = 2,
    anchor_root: Path | None = None,
) -> Stand:
    """Репозиторий с парой документов, конфиг пайплайна и скриптованный агент."""
    workspace = tmp_path / REPO_DIR_NAME
    (workspace / "docs").mkdir(parents=True)
    git(workspace, "init", "--quiet", "-b", "master")
    git(workspace, "config", "user.name", "disputatio-tests")
    git(workspace, "config", "user.email", "tests@disputatio.local")
    (workspace / SPEC_PATH).write_text(spec_text("исходная"), encoding="utf-8")
    (workspace / PLAN_PATH).write_text(plan_text("исходная"), encoding="utf-8")
    anchors = anchor_root if anchor_root is not None else tmp_path / ANCHOR_DIR_NAME
    settings = {
        "spec": SPEC_PATH,
        "plan": PLAN_PATH,
        "max_rounds": max_rounds,
        "schema_retries": schema_retries,
    }
    # Дефолтный конфиг обязан быть ТРЕКАЕМЫМ: untracked-файл вне
    # `.disputatio/` предусловие `run` блокирует, и стенд отказывал бы себе
    # самому по причине, к сценарию отношения не имеющей.
    (workspace / DEFAULT_CONFIG_NAME).write_text(
        CONFIG_TEMPLATE.format(anchor_line="", **settings), encoding="utf-8"
    )
    git(workspace, "add", SPEC_PATH, PLAN_PATH, DEFAULT_CONFIG_NAME)
    git(workspace, "commit", "--quiet", "-m", "исходная пара")
    git(workspace, "switch", "--quiet", "-c", WORK_BRANCH)

    config_path = tmp_path / "pipeline.toml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(
            anchor_line=f'anchor_path = "{anchors.as_posix()}"\n', **settings
        ),
        encoding="utf-8",
    )

    script = Script(turns=turns)
    monkeypatch.setitem(
        composition.ADAPTER_FACTORIES,
        "fake",
        lambda **kwargs: ScriptedAgent(script=script, **kwargs),
    )
    # Дефолтный `anchor_root` читается из окружения на КОНСТРУКЦИИ конфига:
    # без подмены пайплайн без `--config` писал бы в домашний каталог.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return Stand(
        workspace=workspace,
        anchor_root=anchors,
        config_path=config_path,
        script=script,
    )


def clock() -> Callable[[], datetime]:
    """Детерминированные часы: каждый вызов на секунду позже предыдущего."""
    moment = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    counter = {"n": 0}

    def now() -> datetime:
        counter["n"] += 1
        return moment + timedelta(seconds=counter["n"])

    return now


def run_cli(stand: Stand, command: str, *extra: str) -> int:
    """Вызывает `disp pipeline <command>` с детерминированными часами."""
    return main(stand.argv(command, *extra), now=clock())


# ----------------------------------------------------------------------
# Скрипты сходимости
# ----------------------------------------------------------------------


def converging(session_id: str, contour: Literal["spec", "pair"]) -> list[Turn]:
    """Два раунда автора: правка документа своего контура в каждом."""
    document = SPEC_PATH if contour == "spec" else PLAN_PATH
    render = spec_text if contour == "spec" else plan_text
    return [
        Turn(
            text=proposal(1, [document]),
            edits={document: render(f"{session_id}-r1")},
        ),
        Turn(
            text=proposal(2, [document]),
            edits={document: render(f"{session_id}-r2")},
        ),
    ]


def converging_reviews(contour: Literal["spec", "pair"]) -> list[Turn]:
    """Раунд 1 — замечание, раунд 2 — approve с чистым чеклистом."""
    first = "S1" if contour == "spec" else "P1"
    finding = issue(
        "R1-1",
        defect_class=None if contour == "spec" else "execution",
        file=SPEC_PATH if contour == "spec" else PLAN_PATH,
    )
    return [
        Turn(
            text=review_json(
                1,
                contour,
                Verdict.REQUEST_CHANGES,
                issues=[finding],
                failed={first: ["R1-1"]},
            )
        ),
        Turn(text=review_json(2, contour, Verdict.APPROVE)),
    ]


def happy_path_turns() -> dict[tuple[str, str], list[Turn]]:
    """Оба контура сходятся за два раунда каждый."""
    return {
        ("spec-r1", "author"): converging("spec-r1", "spec"),
        ("spec-r1", "reviewer"): converging_reviews("spec"),
        ("pair-r1", "author"): converging("pair-r1", "pair"),
        ("pair-r1", "reviewer"): converging_reviews("pair"),
    }


def tree_snapshot(root: Path) -> dict[str, tuple[float, bytes]]:
    """Снимок дерева: путь → (mtime_ns, содержимое) для каждого файла."""
    snapshot: dict[str, tuple[float, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = (
                path.stat().st_mtime_ns,
                path.read_bytes(),
            )
    return snapshot


# ----------------------------------------------------------------------
# Сценарии
# ----------------------------------------------------------------------


def test_happy_path_two_contours_reach_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Оба контура сходятся, пайплайн доходит до `result/manifest.json` (§10)."""
    stand = build_stand(tmp_path, monkeypatch, happy_path_turns())

    code = run_cli(stand, "run", "--task", TASK_TEXT)

    assert code == EXIT_OK
    manifest = stand.manifest()
    assert manifest["phase"] == PipelinePhase.DONE.value
    assert [record["session_id"] for record in manifest["spec_sessions"]] == ["spec-r1"]
    assert [record["session_id"] for record in manifest["pair_sessions"]] == ["pair-r1"]
    assert manifest["spec_sessions"][0]["outcome"] == "converged"
    assert manifest["pair_sessions"][0]["outcome"] == "converged"

    result = stand.result()
    assert result["converged"] is True
    assert result["escalation_reason"] is None
    assert sorted(result["files"]) == ["pr_body.md", "pr_title.txt", "publish.txt"]

    # Сходимость наступила ровно на раунде 2 обоих контуров: раунд 1 закрыт
    # содержательным циклом, а не approve'ом (§5.1 SPEC-002).
    for session_id in ("spec-r1", "pair-r1"):
        assert stand.decision(session_id, 1).outcome is not Outcome.CONVERGED
        assert stand.decision(session_id, 2).outcome is Outcome.CONVERGED
        assert stand.session_state(session_id).state is SessionPhase.DONE


def test_status_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`disp pipeline status` не пишет на диск ни байта (§3.1).

    Снимок берётся по ВСЕМУ `tmp_path`, включая `.git`, каталог пайплайна и
    журнал целостности за пределами репозитория: «status ничего не изменил»
    — утверждение про диск целиком, а не про один каталог, и любая git-
    команда, тронувшая `.git/index`, обязана быть здесь видна.
    """
    stand = build_stand(tmp_path, monkeypatch, happy_path_turns())
    assert run_cli(stand, "run", "--task", TASK_TEXT) == EXIT_OK

    before = tree_snapshot(tmp_path)
    code = run_cli(stand, "status")
    after = tree_snapshot(tmp_path)

    assert code == EXIT_OK
    assert after == before


def test_status_names_phase_and_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Вывод `status` называет фазу, ревизии и журнал целостности (§3.1)."""
    stand = build_stand(tmp_path, monkeypatch, happy_path_turns())
    assert run_cli(stand, "run", "--task", TASK_TEXT) == EXIT_OK
    capsys.readouterr()

    assert run_cli(stand, "status") == EXIT_OK

    printed = capsys.readouterr().out
    assert SLUG in printed
    assert PipelinePhase.DONE.value in printed
    assert "spec-r1" in printed
    assert "pair-r1" in printed


def test_status_explains_the_directory_without_manifest_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Каталог есть, манифеста нет: внятный текст, а не traceback (§8.1).

    Окно между `mkdir` каталога пайплайна и первой записью манифеста
    невосстановимо автоматически, и CLI обязан назвать все три ручных шага —
    два из которых лежат вне рабочего дерева.
    """
    stand = build_stand(tmp_path, monkeypatch, happy_path_turns())
    assert run_cli(stand, "run", "--task", TASK_TEXT) == EXIT_OK
    (stand.pipeline_dir() / "pipeline.json").unlink()
    capsys.readouterr()

    code = run_cli(stand, "status")

    assert code == EXIT_ERROR
    message = capsys.readouterr().err
    assert str(stand.pipeline_dir()) in message
    assert "pipeline.json" in message


def test_resume_custom_anchor_requires_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`resume` без `--config` ищет анкер не там и отказывает внятно (§8.1).

    Нестандартный `anchor_path` живёт только в живой конфигурации: снапшот в
    каталоге пайплайна для этого не годится (§8.1 шаг 0 — он лежит в
    недоверенном дереве). Значит `resume` без `--config` обязан упереться в
    отсутствующий журнал и сказать об этом, а не молча пропустить сверку P9.
    """
    turns = happy_path_turns()
    turns[("spec-r1", "author")] = [
        Turn(text="", boom=True),
        *converging("spec-r1", "spec"),
    ]
    stand = build_stand(tmp_path, monkeypatch, turns)

    with pytest.raises(Boom):
        run_cli(stand, "run", "--task", TASK_TEXT)
    capsys.readouterr()

    without_config = main(
        [
            "pipeline",
            "resume",
            "--slug",
            SLUG,
            "--root",
            str(stand.workspace),
        ],
        now=clock(),
    )

    assert without_config == EXIT_ERROR
    refusal = capsys.readouterr().err
    # Отказ обязан быть именно про журнал целостности, а не про
    # ненайденный файл конфига: дефолтный конфиг в репозитории есть, и он
    # отличается от поданного `--config` ровно `anchor_path`.
    assert "--config" in refusal
    assert "P9" in refusal
    assert str(tmp_path / "xdg") in refusal
    assert stand.manifest()["phase"] != PipelinePhase.DONE.value

    assert run_cli(stand, "resume") == EXIT_OK
    assert stand.manifest()["phase"] == PipelinePhase.DONE.value
    assert stand.result()["converged"] is True


def test_run_refuses_foreign_untracked_before_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Посторонний untracked блокирует `run` до первой мутации (§3.1, §10).

    Негативный сценарий «файл пережил старт и был уничтожен первым
    `PROPOSING`» невозможен по построению: отказ приходит раньше, чем создан
    анкер и каталог пайплайна. После удаления файла тот же `run` проходит —
    без этой половины утверждение было бы вакуумным.
    """
    stand = build_stand(tmp_path, monkeypatch, happy_path_turns())
    stray = stand.workspace / "notes.txt"
    stray.write_text("личные заметки\n", encoding="utf-8")

    code = run_cli(stand, "run", "--task", TASK_TEXT)

    assert code == EXIT_ERROR
    assert "notes.txt" in capsys.readouterr().err
    assert stray.read_text(encoding="utf-8") == "личные заметки\n"
    assert not stand.pipeline_dir().exists()
    assert not stand.anchor_root.exists()

    stray.unlink()
    assert run_cli(stand, "run", "--task", TASK_TEXT) == EXIT_OK


def test_round_one_approve_does_not_converge_a_document_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Анти-сикофантия §5.1 действует и в `Mode.DOCUMENT` (§5.1 SPEC-002, §10).

    Скрипт одобряет спеку в первом же раунде чистым чеклистом при зелёных
    гейтах — то есть даёт ядру ВСЁ, чего требует критерий сходимости §5.1
    SPEC-001, кроме одного: раунд не первый. Исключение раунда 1 касается
    только `analyze` без правок кода, а doc-сессия документ пишет, значит
    один содержательный цикл ревью она обязана пройти.

    Инвариант ядра здесь не реализуется, а проверяется на doc-контуре: он
    единственное место, где `Mode.DOCUMENT` встречается с `decide()`.
    """
    turns = happy_path_turns()
    turns[("spec-r1", "reviewer")] = [
        Turn(text=review_json(1, "spec", Verdict.APPROVE)),
        Turn(text=review_json(2, "spec", Verdict.APPROVE)),
    ]
    stand = build_stand(tmp_path, monkeypatch, turns)

    assert run_cli(stand, "run", "--task", TASK_TEXT) == EXIT_OK

    first = stand.decision("spec-r1", 1)
    assert first.outcome is not Outcome.CONVERGED
    assert "sycophancy" in first.reason
    assert first.next_round_directive is not None
    assert stand.decision("spec-r1", 2).outcome is Outcome.CONVERGED
    # Содержательный цикл — не формальность: автора спросили дважды.
    assert len(stand.script.prompts_of("spec-r1", "author")) == 2


def test_approve_breaking_the_checklist_never_reaches_decide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V6: approve с fail-пунктом чеклиста гибнет в schema-retry (§5.2, §10).

    Гарантия интеграционная, а не промптовая, и утверждение здесь ровно
    одно: `decide()` не вызывается НИ РАЗУ. Ревьюер отдаёт `approve` с
    `S1: fail`, сославшимся на настоящую major-находку, — это нарушает V3 и
    V7 и обязано быть отвергнуто ДО записи `review.json`. Скрипт повторяет
    тот же ответ, пока не исчерпан `schema_retries`, поэтому раунд 1 до
    `DECIDING` не доходит вовсе и `core/deciding.py` в этой цепочке не
    участвует — правок в нём V6 действительно не потребовала.

    Spy — обёртка вокруг `steps.decide` в composition тестового прогона;
    сам `core/deciding.py` не редактируется и не подменяется.
    """
    from disputatio.runtime import steps

    calls: list[int] = []
    original = steps.decide
    monkeypatch.setattr(
        steps,
        "decide",
        lambda inputs: (calls.append(inputs.round), original(inputs))[1],
    )

    bad = issue("B-1", severity=Severity.MAJOR)
    approve_with_fail = review_json(
        1,
        "spec",
        Verdict.APPROVE,
        issues=[bad],
        failed={"S1": ["B-1"]},
    )
    # Очередь автора длиннее, чем нужно исправному коду (он до раунда 2 не
    # доходит вовсе): реализация, ПРОПУСТИВШАЯ doc-правила, обязана
    # покраснеть на `calls == []`, а не на исчерпанном скрипте — иначе тест
    # доказывал бы длину очереди, а не V6.
    turns = happy_path_turns()
    turns[("spec-r1", "author")] = [
        Turn(text=proposal(round_no, [SPEC_PATH]), edits={SPEC_PATH: spec_text("r1")})
        for round_no in (1, 2, 3)
    ]
    turns[("spec-r1", "reviewer")] = [Turn(text=approve_with_fail) for _ in range(3)]
    stand = build_stand(tmp_path, monkeypatch, turns, schema_retries=2)

    code = run_cli(stand, "run", "--task", TASK_TEXT)

    assert calls == []
    assert code == EXIT_FAILED
    assert stand.session_state("spec-r1").state is SessionPhase.FAILED
    assert stand.manifest()["phase"] == PipelinePhase.FAILED.value
    # Ни ревью, ни решения на диске: отвергнутый ответ не записывается.
    assert not (stand.round_dir("spec-r1", 1) / "review.json").exists()
    assert not (stand.round_dir("spec-r1", 1) / "decision.json").exists()
    # `FAILED` — без автоэкспорта (P7).
    assert not (stand.pipeline_dir() / "result").exists()
    # Ревьюера спросили ровно `schema_retries + 1` раз (I4).
    assert len(stand.script.prompts_of("spec-r1", "reviewer")) == 3


def test_broken_link_blocks_convergence_but_not_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Красный `doc-links` не отменяет ревью, но запрещает `CONVERGED` (§10).

    Три раунда, и каждый проверяет свою половину правила [REQ-004]:

    1. битая ссылка валит `doc-links`, но `VERIFYING → REVIEWING` состоялся —
       `review.json` раунда 1 на диске, ревьюер судил по красному отчёту сам;
    2. `approve` поверх той же красной ссылки отвергается валидацией
       (`approve_on_failed_gates`) и уходит в schema-retry — сходимость
       заблокирована ДО `decide()`, а не после;
    3. ссылка починена, гейты зелёные, тот же `approve` сходится.
    """
    broken = spec_text("r1", broken_link=True)
    still_broken = spec_text("r2", broken_link=True)
    turns = happy_path_turns()
    turns[("spec-r1", "author")] = [
        Turn(text=proposal(1, [SPEC_PATH]), edits={SPEC_PATH: broken}),
        Turn(text=proposal(2, [SPEC_PATH]), edits={SPEC_PATH: still_broken}),
        Turn(text=proposal(3, [SPEC_PATH]), edits={SPEC_PATH: spec_text("r3")}),
    ]
    turns[("spec-r1", "reviewer")] = [
        # `S1` («нет blocker/major-находок») обязан быть `fail` рядом с любой
        # существенной находкой — иначе V8 отвергает ревью раньше, чем до
        # него доберётся правило про красные гейты.
        Turn(
            text=review_json(
                1,
                "spec",
                Verdict.REQUEST_CHANGES,
                issues=[issue("R1-1")],
                failed={"S1": ["R1-1"], "S5": ["R1-1"]},
            )
        ),
        Turn(text=review_json(2, "spec", Verdict.APPROVE)),
        Turn(
            text=review_json(
                2,
                "spec",
                Verdict.REQUEST_CHANGES,
                issues=[issue("R2-1")],
                failed={"S1": ["R2-1"], "S5": ["R2-1"]},
            )
        ),
        Turn(text=review_json(3, "spec", Verdict.APPROVE)),
    ]
    stand = build_stand(tmp_path, monkeypatch, turns)

    assert run_cli(stand, "run", "--task", TASK_TEXT) == EXIT_OK

    report = json.loads(
        (stand.round_dir("spec-r1", 1) / "verification.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["overall"] == "fail"
    failed_gates = {
        gate["name"] for gate in report["gates"] if gate["status"] == "fail"
    }
    assert "doc-links" in failed_gates
    # Ревью раунда 1 состоялось: провал гейта — материал ревьюеру, не приговор.
    assert (stand.round_dir("spec-r1", 1) / "review.json").is_file()

    assert stand.decision("spec-r1", 1).outcome is not Outcome.CONVERGED
    assert stand.decision("spec-r1", 2).outcome is not Outcome.CONVERGED
    assert stand.decision("spec-r1", 3).outcome is Outcome.CONVERGED
    # Раунд 2 стоил ревьюеру двух попыток: approve при красных гейтах отвергнут.
    assert len(stand.script.prompts_of("spec-r1", "reviewer")) == 4


def test_architectural_defect_returns_to_spec_and_replays_the_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Смешанное ревью → spec-r2 → полная pair-r2 без наследства (P5, P6, §7.3).

    Ревью pair-r1 несёт обе находки сразу — архитектурную и исполнительскую.
    P6 объявляет архитектурную приоритетной безусловно: раунд паркуется до
    `decide()`, пайплайн возвращается в spec-контур, а исполнительская
    находка остаётся в ревью и в evidence перехода не участвует.

    Вторая половина — P5: `pair-r2` перепроверяет пару ЦЕЛИКОМ. Ни
    унаследованного approve, ни чеклиста, ни перенесённых находок: её набор
    `adopted_findings` пуст, промпт автора раунда 1 секции находок не несёт,
    и содержательный цикл ревью она проходит заново.
    """
    architectural = issue(
        "F-ARCH",
        severity=Severity.BLOCKER,
        defect_class="architectural",
        file=SPEC_PATH,
    )
    execution = issue(
        "F-EXEC",
        severity=Severity.MAJOR,
        defect_class="execution",
        file=PLAN_PATH,
    )
    turns = {
        ("spec-r1", "author"): converging("spec-r1", "spec"),
        ("spec-r1", "reviewer"): converging_reviews("spec"),
        ("pair-r1", "author"): [
            Turn(
                text=proposal(1, [PLAN_PATH]),
                edits={PLAN_PATH: plan_text("pair-r1-r1")},
            )
        ],
        ("pair-r1", "reviewer"): [
            Turn(
                text=review_json(
                    1,
                    "pair",
                    Verdict.REQUEST_CHANGES,
                    issues=[architectural, execution],
                    failed={"P1": ["F-ARCH"], "P2": ["F-EXEC"]},
                )
            )
        ],
        ("spec-r2", "author"): converging("spec-r2", "spec"),
        ("spec-r2", "reviewer"): converging_reviews("spec"),
        ("pair-r2", "author"): converging("pair-r2", "pair"),
        ("pair-r2", "reviewer"): converging_reviews("pair"),
    }
    stand = build_stand(tmp_path, monkeypatch, turns)

    assert run_cli(stand, "run", "--task", TASK_TEXT) == EXIT_OK

    manifest = stand.manifest()
    assert manifest["phase"] == PipelinePhase.DONE.value
    spec_records = {
        record["session_id"]: record for record in manifest["spec_sessions"]
    }
    pair_records = {
        record["session_id"]: record for record in manifest["pair_sessions"]
    }
    assert spec_records["spec-r1"]["outcome"] == "converged"
    assert spec_records["spec-r1"]["superseded_by"] == "spec-r2"
    assert pair_records["pair-r1"]["outcome"] == "architectural_defect"
    assert pair_records["pair-r1"]["superseded_by"] == "spec-r2"
    assert spec_records["spec-r2"]["outcome"] == "converged"
    assert pair_records["pair-r2"]["outcome"] == "converged"
    assert pair_records["pair-r2"]["superseded_by"] is None

    returns = [
        transition
        for transition in manifest["transitions"]
        if transition["reason"] == "architectural_defect"
    ]
    assert len(returns) == 1
    # Evidence возврата — только архитектурная находка: execution туда не едет.
    assert [link["finding_id"] for link in returns[0]["evidence"]] == ["F-ARCH"]

    # Припаркованный раунд решения не имеет — на этом §8.1 и строит identity.
    assert (stand.round_dir("pair-r1", 1) / "review.json").is_file()
    assert not (stand.round_dir("pair-r1", 1) / "decision.json").exists()

    # spec-r2 открыт находкой, и до автора она доехала — ровно одна.
    findings = json.loads(
        (
            stand.artifact_root("spec-r2") / ".disputatio" / "adopted_findings.json"
        ).read_text(encoding="utf-8")
    )
    assert [item["id"] for item in findings] == ["F-ARCH"]
    first_prompt = stand.script.prompts_of("spec-r2", "author")[0]
    assert "F-ARCH" in first_prompt
    assert "F-EXEC" not in first_prompt

    # P5: pair-r2 не наследует ничего — ни находок, ни approve.
    inherited = json.loads(
        (
            stand.artifact_root("pair-r2") / ".disputatio" / "adopted_findings.json"
        ).read_text(encoding="utf-8")
    )
    assert inherited == []
    assert (
        "Архитектурные находки" not in stand.script.prompts_of("pair-r2", "author")[0]
    )
    assert stand.decision("pair-r2", 1).outcome is not Outcome.CONVERGED
    assert stand.decision("pair-r2", 2).outcome is Outcome.CONVERGED
    assert len(stand.script.prompts_of("pair-r2", "reviewer")) == 2
