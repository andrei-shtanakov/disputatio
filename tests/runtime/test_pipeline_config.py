"""Конфиг пайплайна и fail-closed предусловия `run` (SPEC-002 §3.1, §3.2).

[TASK-013]. Два независимых поведения одного модуля:

* `load_pipeline_config` разбирает секцию `[pipeline]` тем же принципом, что
  `RuntimeConfig.from_toml` ([DESIGN-020]) — любая негодность одна и та же
  `ConfigError`, а попытка переопределить baseline-гейт §6 отдельным
  диагнозом, поднятым явно, а не полученным из перехвата чужого исключения;
* `check_run_preconditions` — fail-closed предусловия §3.1: чистое дерево
  (tracked-правки и посторонний untracked блокируют одинаково, собственный
  `.disputatio/` — нет), подходящая ветка, отсутствующий каталог пайплайна,
  `anchor_path` вне `workspace_root` (P9). Тест закрепляет свойство, ради
  которого написан §3.1: посторонний untracked-файл обязан блокировать
  старт, потому что первый же `PROPOSING` уничтожил бы его сбросом и
  уборкой без санкции оператора (`runtime/git.py::clean`).

Нормализация путей — тест `test_check_run_preconditions_normalizes_status_
paths_by_toplevel_prefix` закрепляет ловушку из докстринга `StatusEntry`:
`status_entries()` отдаёт пути от toplevel, а не от `root` сессии, и наивный
фильтр `.disputatio/` промахивается, когда сессия — в подкаталоге чужого
репозитория.
"""

import subprocess
from pathlib import Path

import pytest

from disputatio.contracts import CHECKLIST_TEXT
from disputatio.events.paths import SESSION_DIR_NAME
from disputatio.runtime import (
    ConfigError,
    DirtyWorkingTree,
    GitCli,
    PipelineAlreadyExists,
    PipelineConfig,
    ProtectedBranchError,
    check_run_preconditions,
    load_pipeline_config,
    validate_anchor_path,
)
from disputatio.verifier import BASELINE_GATE_NAMES, GateSpec

_MINIMAL_PIPELINE_TABLE = """
[pipeline]
spec_path = "docs/specs/2026-08-28-foo-design.md"
plan_path = "docs/plans/2026-08-28-foo-plan.md"
"""

_SECTION_3_2_EXAMPLE = """
[pipeline]
spec_path = "docs/specs/2026-08-28-foo-design.md"
plan_path = "docs/plans/2026-08-28-foo-plan.md"
max_architectural_returns = 2
soft_max_pipeline_tokens = 0
soft_max_pipeline_wall_seconds = 0
protected_branches = ["master", "main"]

[agents.author]
adapter = "claude_code"
model = "sonnet"

[agents.reviewer]
adapter = "codex"
model = "gpt-5"

[limits]
max_rounds = 20
max_total_tokens = 100000
max_wall_seconds = 3600
schema_retries = 2
"""


def _git(workdir: Path, *args: str) -> str:
    """Вспомогательная git-команда теста; ненулевой код — `CalledProcessError`."""
    completed = subprocess.run(
        ["git", *args],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _write_config(tmp_path: Path, text: str, *, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_pipeline_config
# ---------------------------------------------------------------------------


def test_load_pipeline_config_parses_section_3_2_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пример §3.2 разбирается в `PipelineConfig` с ровно этими значениями."""
    anchor_home = tmp_path / "state-home"
    monkeypatch.setenv("XDG_STATE_HOME", str(anchor_home))
    path = _write_config(tmp_path, _SECTION_3_2_EXAMPLE)

    config = load_pipeline_config(path)

    assert config.spec_path == Path("docs/specs/2026-08-28-foo-design.md")
    assert config.plan_path == Path("docs/plans/2026-08-28-foo-plan.md")
    assert config.max_architectural_returns == 2
    assert config.soft_max_pipeline_tokens == 0
    assert config.soft_max_pipeline_wall_seconds == 0
    assert config.protected_branches == ("master", "main")
    assert config.extra_gates == ()
    assert config.anchor_path == anchor_home / "disputatio" / "anchors"


def test_load_pipeline_config_applies_task_2_vendored_checklists_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без override дефолт — вендоренная копия задачи 2, id-в-id."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    path = _write_config(tmp_path, _MINIMAL_PIPELINE_TABLE)

    config = load_pipeline_config(path)

    assert config.checklists["spec"]["S1"] == CHECKLIST_TEXT["S1"]
    assert config.checklists["pair"]["P5"] == CHECKLIST_TEXT["P5"]


def test_load_pipeline_config_default_anchor_path_is_outside_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дефолт `anchor_path` вычисляется из `XDG_STATE_HOME`, вне репозитория."""
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    anchor_home = tmp_path / "elsewhere"
    monkeypatch.setenv("XDG_STATE_HOME", str(anchor_home))
    path = _write_config(workspace_root, _MINIMAL_PIPELINE_TABLE)

    config = load_pipeline_config(path)

    assert config.anchor_path == anchor_home / "disputatio" / "anchors"
    assert validate_anchor_path(config.anchor_path, workspace_root) is None


def test_load_pipeline_config_rejects_missing_file(tmp_path: Path) -> None:
    """Отсутствие файла — `ConfigError`, не `FileNotFoundError` в лицо."""
    with pytest.raises(ConfigError):
        load_pipeline_config(tmp_path / "does-not-exist.toml")


def test_load_pipeline_config_rejects_invalid_toml(tmp_path: Path) -> None:
    """Синтаксически битый TOML — `ConfigError`, не `TOMLDecodeError`."""
    path = _write_config(tmp_path, "[pipeline\nspec_path = ")

    with pytest.raises(ConfigError):
        load_pipeline_config(path)


def test_load_pipeline_config_rejects_missing_pipeline_table(tmp_path: Path) -> None:
    """Файл без `[pipeline]` — `ConfigError`, не голый `KeyError`."""
    path = _write_config(tmp_path, '[agents.author]\nadapter = "x"\nmodel = "y"\n')

    with pytest.raises(ConfigError):
        load_pipeline_config(path)


@pytest.mark.parametrize(
    "bad_line",
    [
        'spec_path = "../outside/spec.md"',
        'plan_path = "../../etc/passwd"',
        'spec_path = "spec/../../outside.md"',
        'plan_path = "/abs/plan.md"',
        'spec_path = ""',
    ],
)
def test_load_pipeline_config_rejects_paths_outside_the_repository(
    tmp_path: Path, bad_line: str
) -> None:
    """Пара документов обязана лежать в репозитории — отказ на загрузке (§4.2).

    Отказ именно здесь, а не на записи манифеста: путь наружу отвергает и
    схема, но она срабатывает уже после того, как `run` создал анкер,
    каталог и снапшоты — тот же дефект, что D1. Загрузка конфига идёт до
    первой мутации, и `ConfigError` называет файл, который надо править.
    """
    table = _MINIMAL_PIPELINE_TABLE.replace(
        'spec_path = "docs/specs/2026-08-28-foo-design.md"'
        if bad_line.startswith("spec_path")
        else 'plan_path = "docs/plans/2026-08-28-foo-plan.md"',
        bad_line,
    )
    path = _write_config(tmp_path, table)

    with pytest.raises(ConfigError):
        load_pipeline_config(path)


def test_load_pipeline_config_rejects_missing_spec_path(tmp_path: Path) -> None:
    """Отсутствующий обязательный ключ — `ConfigError`, не `KeyError`."""
    path = _write_config(tmp_path, '[pipeline]\nplan_path = "docs/plan.md"\n')

    with pytest.raises(ConfigError):
        load_pipeline_config(path)


@pytest.mark.parametrize("baseline_name", BASELINE_GATE_NAMES)
def test_load_pipeline_config_rejects_gate_overriding_baseline_name(
    tmp_path: Path, baseline_name: str
) -> None:
    """`[[pipeline.gates]]`, названный как baseline-гейт §6, — `ConfigError`.

    Baseline неотключаем (§6): конфиг может только добавлять гейты. Разрешить
    переопределение по имени значило бы дать конфигу способ выключить один
    из пяти обязательных прогонов `DocVerifier`, не трогая код verifier'а.
    """
    text = (
        _MINIMAL_PIPELINE_TABLE
        + "\n[[pipeline.gates]]\n"
        + f'name = "{baseline_name}"\n'
        + 'cmd = "false"\n'
    )
    path = _write_config(tmp_path, text)

    with pytest.raises(ConfigError, match="baseline"):
        load_pipeline_config(path)


def test_load_pipeline_config_accepts_extra_gate_with_new_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гейт с именем вне baseline добавляется, а не отвергается."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    text = (
        _MINIMAL_PIPELINE_TABLE
        + "\n[[pipeline.gates]]\n"
        + 'name = "project-lint"\n'
        + 'cmd = "uv run ruff check ."\n'
    )
    path = _write_config(tmp_path, text)

    config = load_pipeline_config(path)

    assert config.extra_gates == (
        GateSpec(name="project-lint", cmd="uv run ruff check .", enabled=True),
    )


def test_load_pipeline_config_checklist_override_merges_not_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[pipeline.checklists.spec]` с одним `S1` не уносит остальные пункты
    `spec` и весь контур `pair` (фикс-раунд 1, Important-1).

    Прежняя реализация заменяла всю карту override'ом: `config.checklists`
    после одной строки `S1 = "..."` терял `S2`–`S5`, а `config.checklists
    ["pair"]` бросал `KeyError` — чеклист сходимости (критерий, по которому
    судят ревьюера, §5.3) молча лишался четырёх из пяти пунктов и целого
    контура.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    text = (
        _MINIMAL_PIPELINE_TABLE
        + '\n[pipeline.checklists.spec]\nS1 = "кастомная формулировка S1"\n'
    )
    path = _write_config(tmp_path, text)

    config = load_pipeline_config(path)

    assert config.checklists["spec"]["S1"] == "кастомная формулировка S1"
    assert config.checklists["spec"]["S2"] == CHECKLIST_TEXT["S2"]
    assert config.checklists["spec"]["S5"] == CHECKLIST_TEXT["S5"]
    assert config.checklists["pair"]["P1"] == CHECKLIST_TEXT["P1"]
    assert config.checklists["pair"]["P5"] == CHECKLIST_TEXT["P5"]


def test_load_pipeline_config_rejects_unknown_checklist_contour(
    tmp_path: Path,
) -> None:
    """Опечатка в имени контура — `ConfigError`, а не тихое игнорирование."""
    text = (
        _MINIMAL_PIPELINE_TABLE
        + '\n[pipeline.checklists.spce]\nS1 = "опечатка в имени контура"\n'
    )
    path = _write_config(tmp_path, text)

    with pytest.raises(ConfigError):
        load_pipeline_config(path)


def test_load_pipeline_config_rejects_unknown_checklist_id(
    tmp_path: Path,
) -> None:
    """Опечатка в id пункта — `ConfigError`, а не молчаливое добавление."""
    text = (
        _MINIMAL_PIPELINE_TABLE
        + '\n[pipeline.checklists.spec]\nS9 = "несуществующий пункт"\n'
    )
    path = _write_config(tmp_path, text)

    with pytest.raises(ConfigError):
        load_pipeline_config(path)


# ---------------------------------------------------------------------------
# check_run_preconditions / validate_anchor_path
# ---------------------------------------------------------------------------


def _config(*, anchor_path: Path) -> PipelineConfig:
    """`PipelineConfig` минимальный для предусловий: дефолтные protected
    branches (`master`, `main`) совпадают с веткой `git_repo`."""
    return PipelineConfig(
        spec_path=Path("spec/pair.md"),
        plan_path=Path("plan/pair.md"),
        anchor_path=anchor_path,
    )


def _switch_to_working_branch(root: Path, slug: str) -> None:
    """Уходит с protected-ветки `git_repo` на рабочую — как это сделал бы
    оператор по подсказке отказа."""
    _git(root, "switch", "--quiet", "-c", f"docs/{slug}")


def test_check_run_preconditions_passes_on_clean_tree_and_branch(
    git_repo: Path,
) -> None:
    """Чистое дерево, рабочая ветка, отсутствующий каталог — старт разрешён."""
    _switch_to_working_branch(git_repo, "demo")
    config = _config(anchor_path=git_repo.parent / "anchors")

    assert check_run_preconditions(GitCli(git_repo), git_repo, config, "demo") is None


def test_check_run_preconditions_rejects_dirty_tracked_tree(git_repo: Path) -> None:
    """Незакоммиченная правка tracked-файла блокирует старт `run`."""
    _switch_to_working_branch(git_repo, "demo")
    (git_repo / "README.md").write_text("правка автора\n", encoding="utf-8")
    config = _config(anchor_path=git_repo.parent / "anchors")

    with pytest.raises(DirtyWorkingTree, match="README.md"):
        check_run_preconditions(GitCli(git_repo), git_repo, config, "demo")


def test_check_run_preconditions_rejects_foreign_untracked_file(
    git_repo: Path,
) -> None:
    """Посторонний untracked-файл вне `.disputatio/` блокирует старт.

    Прецедент `preflight` SPEC-001 здесь не наследуется (§3.1): первый же
    `PROPOSING` уничтожил бы `notes.txt` сбросом и уборкой без санкции
    оператора, поэтому терпимость к untracked, законная для сессии, для
    предусловий пайплайна не годится.
    """
    _switch_to_working_branch(git_repo, "demo")
    (git_repo / "notes.txt").write_text("посторонний черновик\n", encoding="utf-8")
    config = _config(anchor_path=git_repo.parent / "anchors")

    with pytest.raises(DirtyWorkingTree, match="notes.txt"):
        check_run_preconditions(GitCli(git_repo), git_repo, config, "demo")


def test_check_run_preconditions_allows_own_control_plane_untracked_files(
    git_repo: Path,
) -> None:
    """Untracked-файлы под `.disputatio/` (собственный control plane) не
    блокируют старт: он же будет создан ПОСЛЕ успешных предусловий."""
    _switch_to_working_branch(git_repo, "demo")
    scratch = git_repo / SESSION_DIR_NAME / "scratch.txt"
    scratch.parent.mkdir(parents=True)
    scratch.write_text("необязательный служебный файл\n", encoding="utf-8")
    config = _config(anchor_path=git_repo.parent / "anchors")

    assert check_run_preconditions(GitCli(git_repo), git_repo, config, "demo") is None


def test_run_allowed_with_other_pipeline(git_repo: Path) -> None:
    """Untracked-каталог ЧУЖОГО `<slug>` под `.disputatio/pipelines/` не
    блокирует старт своего пайплайна (§4.1: несколько пайплайнов
    сосуществуют под разными `<slug>`)."""
    _switch_to_working_branch(git_repo, "demo")
    other = git_repo / SESSION_DIR_NAME / "pipelines" / "other-slug" / "pipeline.json"
    other.parent.mkdir(parents=True)
    other.write_text('{"revision": 1}\n', encoding="utf-8")
    config = _config(anchor_path=git_repo.parent / "anchors")

    assert check_run_preconditions(GitCli(git_repo), git_repo, config, "demo") is None


def test_check_run_preconditions_rejects_tracked_change_under_control_plane(
    git_repo: Path,
) -> None:
    """Tracked-изменённый путь под `.disputatio/` — внешняя правка control
    plane, а не собственный журнал: блокирует так же, как любой другой
    tracked-диф."""
    _switch_to_working_branch(git_repo, "demo")
    control = git_repo / SESSION_DIR_NAME / "pipelines" / "demo" / "pipeline.json"
    control.parent.mkdir(parents=True)
    control.write_text('{"revision": 1}\n', encoding="utf-8")
    _git(git_repo, "add", "--", str(control.relative_to(git_repo)))
    _git(git_repo, "commit", "--quiet", "-m", "commit control file")
    control.write_text('{"revision": 2}\n', encoding="utf-8")
    config = _config(anchor_path=git_repo.parent / "anchors")

    with pytest.raises(DirtyWorkingTree):
        check_run_preconditions(GitCli(git_repo), git_repo, config, "other-demo")


def test_check_run_preconditions_rejects_protected_branch(git_repo: Path) -> None:
    """Текущая ветка (`main`, по умолчанию protected) отклоняет старт."""
    config = _config(anchor_path=git_repo.parent / "anchors")

    with pytest.raises(ProtectedBranchError, match="git switch -c docs/demo"):
        check_run_preconditions(GitCli(git_repo), git_repo, config, "demo")


def test_check_run_preconditions_rejects_detached_head(git_repo: Path) -> None:
    """Detached HEAD (`current_branch() is None`) отклоняет старт."""
    head = _git(git_repo, "rev-parse", "HEAD").strip()
    _git(git_repo, "checkout", "--quiet", "--detach", head)
    config = _config(anchor_path=git_repo.parent / "anchors")

    with pytest.raises(ProtectedBranchError, match="detached HEAD"):
        check_run_preconditions(GitCli(git_repo), git_repo, config, "demo")


def test_check_run_preconditions_rejects_existing_pipeline_dir(
    git_repo: Path,
) -> None:
    """`.disputatio/pipelines/<slug>/` уже существует — нужен `resume`."""
    _switch_to_working_branch(git_repo, "demo")
    pipeline_dir = git_repo / SESSION_DIR_NAME / "pipelines" / "demo"
    pipeline_dir.mkdir(parents=True)
    config = _config(anchor_path=git_repo.parent / "anchors")

    with pytest.raises(PipelineAlreadyExists, match="resume"):
        check_run_preconditions(GitCli(git_repo), git_repo, config, "demo")


def test_check_run_preconditions_rejects_anchor_path_inside_workspace_root(
    git_repo: Path,
) -> None:
    """`anchor_path` внутри `workspace_root` — отказ по P9."""
    _switch_to_working_branch(git_repo, "demo")
    config = _config(anchor_path=git_repo / SESSION_DIR_NAME / "anchors")

    with pytest.raises(ConfigError):
        check_run_preconditions(GitCli(git_repo), git_repo, config, "demo")


def test_check_run_preconditions_does_not_create_a_branch(git_repo: Path) -> None:
    """Функция не создаёт рабочую ветку сама — это решение оператора (§3.1)."""
    before = set(_git(git_repo, "branch", "--list").splitlines())
    config = _config(anchor_path=git_repo.parent / "anchors")

    with pytest.raises(ProtectedBranchError):
        check_run_preconditions(GitCli(git_repo), git_repo, config, "demo")

    after = set(_git(git_repo, "branch", "--list").splitlines())
    assert before == after, "предусловие само создало ветку — это не его роль"


def test_check_run_preconditions_normalizes_status_paths_by_toplevel_prefix(
    git_repo: Path,
) -> None:
    """Сессия в подкаталоге toplevel-репозитория: `.disputatio/` чужого
    slug'а не блокирует, а посторонний untracked-файл рядом — блокирует.

    Наивный фильтр `path.startswith(".disputatio/")` не совпал бы вовсе с
    `proj/.disputatio/...` (см. докстринг `StatusEntry`), и предусловие
    отвергло бы легальный запуск по собственному журналу другого пайплайна.
    Тест закрепляет, что `check_run_preconditions` приводит обе стороны
    сравнения к toplevel через `GitOps.toplevel_prefix()`.
    """
    _switch_to_working_branch(git_repo, "demo")
    root = git_repo / "proj"
    other = root / SESSION_DIR_NAME / "pipelines" / "other-slug" / "pipeline.json"
    other.parent.mkdir(parents=True)
    other.write_text('{"revision": 1}\n', encoding="utf-8")
    config = _config(anchor_path=git_repo.parent / "anchors")

    assert check_run_preconditions(GitCli(root), root, config, "demo") is None

    (root / "junk.txt").write_text("посторонний файл\n", encoding="utf-8")
    with pytest.raises(DirtyWorkingTree, match="proj/junk.txt"):
        check_run_preconditions(GitCli(root), root, config, "demo")


def test_check_run_preconditions_rejects_anchor_between_session_root_and_toplevel(
    git_repo: Path,
) -> None:
    """`anchor_path` вне `workspace_root`, но внутри TOPLEVEL — тоже отказ
    (фикс-раунд 1, Important-3).

    Сессия в подкаталоге `proj/` toplevel-репозитория: `anchor_path`, лежащий
    рядом с `proj/` (т.е. НЕ внутри `workspace_root == proj`, но внутри
    toplevel), раньше проходил containment буквально по `workspace_root`. Но
    `runtime/git.py::clean` идёт по `_TREE_PATHSPEC = (":/", …)` — по ВСЕМУ
    репозиторию, а не только по `proj/` — и снёс бы такой анкер первым же
    `PROPOSING`.
    """
    _switch_to_working_branch(git_repo, "demo")
    root = git_repo / "proj"
    root.mkdir()
    between = git_repo / "shared-anchors"  # сосед `proj/`, внутри toplevel
    config = _config(anchor_path=between)

    with pytest.raises(ConfigError):
        check_run_preconditions(GitCli(root), root, config, "demo")


def test_toplevel_prefix_in_linked_worktree(
    git_repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """`toplevel_prefix()` в linked worktree — от границ ЭТОГО worktree, а не
    основного репозитория (фикс-раунд 1: тот же класс дыры, что ревьюер нашёл
    в `diff_readonly` в задаче 12 — операция на `rev-parse` обязана быть
    проверена именно в `git worktree`, а не только в обычном репозитории:
    worktree делит объектную базу с основным репозиторием, но имеет
    собственный toplevel).
    """
    worktree_root = tmp_path_factory.mktemp("worktree")
    worktree = worktree_root / "wt"
    _git(git_repo, "worktree", "add", "--quiet", str(worktree), "-b", "wt-branch")

    assert GitCli(worktree).toplevel_prefix() == ""

    nested = worktree / "proj"
    nested.mkdir()
    assert GitCli(nested).toplevel_prefix() == "proj/"


# ---------------------------------------------------------------------------
# validate_anchor_path — статические N7 варианты (run и resume)
# ---------------------------------------------------------------------------


def test_validate_anchor_path_rejects_direct_path_inside_workspace(
    tmp_path: Path,
) -> None:
    """Прямой путь внутрь `workspace_root` — отказ."""
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()

    with pytest.raises(ConfigError):
        validate_anchor_path(workspace_root / "anchors", workspace_root)


def test_validate_anchor_path_rejects_workspace_root_itself(
    tmp_path: Path,
) -> None:
    """`anchor_path == workspace_root` — тоже нарушение, не только вложенность."""
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()

    with pytest.raises(ConfigError):
        validate_anchor_path(workspace_root, workspace_root)


def test_validate_anchor_path_rejects_relative_path_resolved_from_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Относительный путь резолвится от `cwd`; попав внутрь дерева — отказ."""
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    monkeypatch.chdir(workspace_root)

    with pytest.raises(ConfigError):
        validate_anchor_path(Path("state/anchors"), workspace_root)


def test_validate_anchor_path_rejects_dotdot_landing_back_inside(
    tmp_path: Path,
) -> None:
    """`..`, выводящий обратно внутрь дерева, — отказ (канонизация `resolve`).

    Настоящий вектор (фикс-раунд 1, Important-4): путь ЛЕКСИЧЕСКИ начинается
    СНАРУЖИ `workspace_root` (`tmp_path/outside/..`) и только `..` заводит
    его обратно внутрь. `outside/../anchors` внутри самого `workspace_root`
    (прежняя версия теста) лежит внутри дерева и лексически — на нём
    `Path.absolute()` без `resolve()` тоже прошёл бы, и тест не отличил бы
    рабочую канонизацию от сломанной.
    """
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sneaky = outside / ".." / "repo" / "anchors"

    with pytest.raises(ConfigError):
        validate_anchor_path(sneaky, workspace_root)


def test_validate_anchor_path_rejects_symlink_leading_inside(
    tmp_path: Path,
) -> None:
    """Symlink, ведущий внутрь дерева, — отказ (канонизация через `resolve`)."""
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    link = tmp_path / "anchor-link"
    link.symlink_to(workspace_root / "anchors", target_is_directory=True)

    with pytest.raises(ConfigError):
        validate_anchor_path(link, workspace_root)


def test_validate_anchor_path_rejects_tilde_expanding_inside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~`, разворачивающийся внутрь дерева через `HOME`, — отказ.

    Фикс-раунд 1, Important-4: спека называет `expanduser` наравне с `..` и
    symlink среди векторов канонизации P9, и ревью показало, что без
    отдельного теста уборка `expanduser()` из реализации осталась бы
    незамеченной — весь набор оставался бы зелёным.
    """
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    monkeypatch.setenv("HOME", str(workspace_root))

    with pytest.raises(ConfigError):
        validate_anchor_path(Path("~/anchors"), workspace_root)


def test_validate_anchor_path_accepts_path_outside_workspace(
    tmp_path: Path,
) -> None:
    """Путь вне `workspace_root` проходит — это штатный случай, не только N7."""
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()

    assert validate_anchor_path(tmp_path / "anchors", workspace_root) is None


def test_validate_anchor_path_is_reusable_for_run_and_resume(
    tmp_path: Path,
) -> None:
    """Одна и та же проверка вызывается на `run` и повторно на `resume`
    (§3.1) — оба вызова с теми же аргументами дают тот же (отрицательный)
    вердикт, без побочных эффектов, которые сделали бы второй вызов другим."""
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    anchor_path = workspace_root / "anchors"

    with pytest.raises(ConfigError):
        validate_anchor_path(anchor_path, workspace_root)  # эквивалент `run`
    with pytest.raises(ConfigError):
        validate_anchor_path(anchor_path, workspace_root)  # эквивалент `resume`
