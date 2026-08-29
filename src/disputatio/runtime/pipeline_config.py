"""Конфиг пайплайна и fail-closed предусловия `run` (SPEC-002 §3.1, §3.2).

`PipelineConfig` — снапшот секции `[pipeline]` конфига, отдельной от
`RuntimeConfig` ([DESIGN-014]): `[agents.*]`/`[limits]` описывают ОДНУ
сессию debate loop'а, а `[pipeline]` — контур из нескольких сессий (§2), и
смешение двух снапшотов в один тип означало бы, что резолвер пайплайна
обязан знать формат сессии, а резолвер сессии — формат пайплайна.

`check_run_preconditions` — fail-closed проверки перед стартом нового
пайплайна (§3.1). «Fail-closed» здесь не фигура речи: каждая проверка обязана
отклонить старт при малейшей неопределённости, потому что первый же
`PROPOSING` необратимо мутирует рабочее дерево (`reset --hard` + `clean` по
всему репозиторию, `runtime/steps.py`, `runtime/git.py::clean`) — старт,
пропущенный по ошибке, стирает работу пользователя молча.

Две ловушки нормализации путей (полностью — в докстрингах `StatusEntry` и
`GitOps.toplevel_prefix`, `runtime/git.py`): `StatusEntry.path` приходит от
toplevel репозитория, а `spec_path`/`plan_path`/каталог сессии — от `root`
пайплайна, и наивное сравнение хвостов промахивается в обе стороны. Здесь обе
стороны сравнения приводятся к toplevel явно, через `GitOps.toplevel_prefix()`
— седьмую операцию порта (`runtime/git.py`), добавленную задачей 13 ровно
затем, чтобы эта нормализация не легла на плечи `status_entries()`.
"""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from disputatio.contracts import CHECKLIST_BY_CONTOUR, CHECKLIST_TEXT
from disputatio.runtime import _toml
from disputatio.runtime.errors import (
    ConfigError,
    DirtyWorkingTree,
    PipelineAlreadyExists,
    ProtectedBranchError,
)
from disputatio.runtime.git import SESSION_DIR_NAME, GitOps
from disputatio.verifier import BASELINE_GATE_NAMES, GateSpec

#: Имя подкаталога пайплайнов внутри каталога сессии (§4.1 SPEC-002):
#: `.disputatio/pipelines/<slug>/`.
PIPELINES_DIR_NAME: Final = "pipelines"

DEFAULT_PROTECTED_BRANCHES: Final[tuple[str, ...]] = ("master", "main")
DEFAULT_MAX_ARCHITECTURAL_RETURNS: Final = 2


def _default_checklists() -> dict[str, dict[str, str]]:
    """Вендоренный дефолт §5.3 (задача 2): id → текст, по контурам."""
    return {
        contour: {item_id: CHECKLIST_TEXT[item_id] for item_id in ids}
        for contour, ids in CHECKLIST_BY_CONTOUR.items()
    }


def _default_anchor_root() -> Path:
    """Каталог журналов целостности P9 по умолчанию — без новой зависимости.

    `XDG_STATE_HOME`, а при его отсутствии `~/.local/state` — тот же
    стандарт, которым уже пользуются CLI-инструменты без выделенного пакета
    под XDG base dirs. Читается функцией, а не константой на импорте модуля:
    `PipelineConfig` — frozen dataclass с `default_factory`, и переменная
    окружения обязана быть видна на момент КОНСТРУКЦИИ (тест подменяет её
    через `monkeypatch` перед вызовом), а не на момент импорта пакета.
    """
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / "disputatio" / "anchors"


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Снапшот секции `[pipeline]` (§3.2 SPEC-002).

    `checklists` — `{contour: {item_id: text}}`; дефолт — вендоренная копия
    задачи 2 (`contracts.checklists_catalog`), override — из `[pipeline.
    checklists.<contour>]` конфига, снапшотится и хешируется отдельно (§3.2).

    `extra_gates` — только ДОБАВЛЕННЫЕ гейты: baseline §6 (`doc-paths`,
    `doc-links`, `doc-anchors`, `doc-line-refs`, `doc-scope`) неотключаем, и
    `load_pipeline_config` отказывает, если `[[pipeline.gates]]` называет
    гейт тем же именем — само наличие этого поля не даёт способа выключить
    прогон baseline (`DocVerifier` гоняет его безусловно), но ранний отказ на
    загрузке конфига честнее, чем молчаливо игнорируемая попытка.

    `anchor_path` — КАТАЛОГ (`anchor_root`), не файл: сам журнал целостности
    лежит `<anchor_path>/<fingerprint>/<anchor_id>.jsonl`, где `fingerprint`
    и `anchor_id` (= `pipeline_id`) вычисляются вызывающим кодом момента
    создания пайплайна, не здесь. Обязан канонически резолвиться ВНЕ
    репозитория (P9) — если точнее, вне его TOPLEVEL, а не только вне
    `workspace_root` сессии (фикс-раунд 1, Important-3: `clean()` идёт по
    всему репозиторию, а не по `workspace_root`). `check_run_preconditions`/
    `validate_anchor_path` проверяют это fail-closed на каждом `run` и
    `resume`.
    """

    spec_path: Path
    plan_path: Path
    max_architectural_returns: int = DEFAULT_MAX_ARCHITECTURAL_RETURNS
    soft_max_pipeline_tokens: int = 0
    soft_max_pipeline_wall_seconds: int = 0
    protected_branches: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES
    checklists: Mapping[str, Mapping[str, str]] = field(
        default_factory=_default_checklists
    )
    extra_gates: tuple[GateSpec, ...] = ()
    anchor_path: Path = field(default_factory=_default_anchor_root)


def load_pipeline_config(path: Path) -> PipelineConfig:
    """Читает секцию `[pipeline]` из файла `path` ([DESIGN-020], §3.2).

    Тот же принцип, что у `RuntimeConfig.from_toml` ([DESIGN-020]): битый
    TOML, отсутствующий обязательный ключ и значение не того типа — одна и та
    же `ConfigError`, а не смесь `TOMLDecodeError`/`KeyError`/`TypeError`
    пользователю в лицо. Отсутствие файла — тоже `ConfigError`: для
    вызывающего «файла нет» и «файл не читается» не разные исходы.

    Единственное исключение из этого слияния — попытка переопределить
    baseline-гейт: она тоже `ConfigError`, но с собственным, не техническим
    текстом («baseline не отключается»), поднятым явно, а не полученным из
    перехвата чужого типа исключения.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"конфиг пайплайна {path} не читается: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"конфиг пайплайна {path} не в UTF-8: {exc}") from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} не разбирается как TOML: {exc}") from exc
    try:
        table = _toml.table(raw, "pipeline")
        return _from_pipeline_table(table)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"[pipeline] в {path} непригодна: {exc}") from exc


def check_run_preconditions(
    git: GitOps,
    workspace_root: Path,
    config: PipelineConfig,
    slug: str,
) -> None:
    """Fail-closed предусловия `disp pipeline run` (§3.1 SPEC-002).

    Порядок — тот же, что в §3.1: чистое дерево, подходящая ветка, каталог
    пайплайна отсутствует, `anchor_path` резолвится вне репозитория. Функция
    ничего не создаёт и не мутирует репозиторий: любое внешнее действие
    (создание ветки, каталога, анкера) — решение, принимаемое ПОСЛЕ успешных
    предусловий, отдельным шагом (§3.1: «runner ветку не создаёт»).

    Граница для `anchor_path` — TOPLEVEL репозитория, не `workspace_root`
    (фикс-раунд 1, Important-3, решение team-lead — шире буквы §3.1 в
    защищаемую сторону): `runtime/git.py::clean` идёт по `_TREE_PATHSPEC =
    (":/", …)`, то есть по ВСЕМУ репозиторию, а не по `workspace_root`, и
    анкер, лежащий между корнем сессии и toplevel, прошёл бы containment по
    буквальному `workspace_root` и был бы снесён первым же `PROPOSING`.
    """
    _check_clean_tree(git)
    _check_branch(git, config, slug)
    _check_pipeline_dir_absent(workspace_root, slug)
    validate_anchor_path(config.anchor_path, toplevel_root(git, workspace_root))


def toplevel_root(git: GitOps, workspace_root: Path) -> Path:
    """Абсолютный путь toplevel репозитория — `workspace_root` минус префикс.

    `GitOps.toplevel_prefix()` отдаёт ОТНОСИТЕЛЬНЫЙ путь `workspace_root` от
    toplevel (`"proj/"`, либо `""`, когда они совпадают); порт не даёт
    операции, отвечающей абсолютным путём toplevel напрямую (`--show-
    toplevel` не заведён — задача 13 остановилась на минимально нужном).
    Поднимаемся на каждый сегмент префикса от канонического
    `workspace_root` — то же число `.parent`, каким git развернул бы `cwd`
    обратно к toplevel.
    """
    canonical = workspace_root.expanduser().resolve()
    prefix = git.toplevel_prefix()
    segments = prefix.rstrip("/").split("/") if prefix else []
    for _ in segments:
        canonical = canonical.parent
    return canonical


def validate_anchor_path(anchor_path: Path, containment_root: Path) -> None:
    """`anchor_path` обязан канонически резолвиться вне `containment_root` (P9).

    Общая проверка `run` и `resume` (§3.1: «та же проверка повторяется при
    каждом `resume`») — вынесена отдельной функцией, а не встроена в
    `check_run_preconditions`, ровно затем, чтобы resume мог переиспользовать
    её без дублирования containment-логики. Сама функция не знает про git и
    про toplevel — она проверяет чистое containment одного пути в другом;
    ВЫБОР границы (toplevel репозитория, а не буквальный `workspace_root` —
    см. докстринг `check_run_preconditions`, Important-3) остаётся на
    вызывающем.

    Канонизация — `expanduser` + `resolve`: без неё символическую ссылку,
    ведущую внутрь `containment_root`, либо `..`-путь, возвращающийся туда
    же, проверка пропустила бы буквальным сравнением префиксов. `resolve()`
    не требует существования пути (`strict=False` по умолчанию) —
    anchor_root законно ещё не создан на первом `run`.

    Совпадение с `containment_root` — тоже нарушение, не только вложенность:
    анкер, лежащий вровень с рабочим деревом, точно так же становится
    предметом собственной сверки, которую призван проверять.
    """
    canonical_anchor = anchor_path.expanduser().resolve()
    canonical_root = containment_root.expanduser().resolve()
    if canonical_anchor.is_relative_to(canonical_root):
        raise ConfigError(
            f"anchor_path {anchor_path} резолвится в {canonical_anchor}, "
            f"а он лежит внутри репозитория ({canonical_root}) — P9 требует, "
            "чтобы журнал целостности жил вне репозитория, который он "
            "проверяет; укажите anchor_path вне рабочего дерева"
        )


def _check_clean_tree(git: GitOps) -> None:
    """Дерево чисто: нет tracked-правок и нет untracked вне `.disputatio/`.

    Исключение узкое и не зависит от `slug`: несколько пайплайнов
    сосуществуют под разными `<slug>` в одном `.disputatio/pipelines/` (§4.1),
    и чужой каталог пайплайна — штатное состояние, а не грязь. Любой другой
    untracked-путь блокирует старт: первый же `PROPOSING` делает `reset
    --hard` и `clean()` по всему дереву минус каталог сессии
    (`runtime/steps.py`, `runtime/git.py::clean`), и посторонний untracked-
    файл был бы молча уничтожен без санкции оператора. `tracked=True` под
    `.disputatio/` блокирует всегда — это внешняя правка control plane, а не
    собственный журнал пайплайна.

    Пути `status_entries()` приходят от toplevel репозитория, а не от `root`
    сессии — префикс, под который сравнивается принадлежность к `.disputatio/`,
    берётся из `GitOps.toplevel_prefix()`, а не собирается эвристикой по
    хвосту строки (см. `StatusEntry`, `GitOps.toplevel_prefix`).
    """
    control_prefix = f"{git.toplevel_prefix()}{SESSION_DIR_NAME}/"
    blocking = [
        entry
        for entry in git.status_entries()
        if entry.tracked or not entry.path.startswith(control_prefix)
    ]
    if not blocking:
        return
    listing = "\n".join(
        f"{'M ' if entry.tracked else '??'} {entry.path}" for entry in blocking
    )
    raise DirtyWorkingTree(
        "рабочее дерево не готово к `run`: есть незакоммиченные tracked-"
        "правки либо untracked-пути вне собственного каталога пайплайна "
        f"({SESSION_DIR_NAME}/) — первый же PROPOSING уничтожит их сбросом "
        f"и уборкой без санкции оператора:\n{listing}"
    )


def _check_branch(git: GitOps, config: PipelineConfig, slug: str) -> None:
    """Текущая ветка не protected и не detached HEAD (§3.1).

    Runner ветку не создаёт (внешний эффект — решение оператора): отказ
    несёт подготовительную команду в тексте, но не выполняет её.
    """
    branch = git.current_branch()
    if branch is not None and branch not in config.protected_branches:
        return
    described = "detached HEAD" if branch is None else f"ветка {branch!r}"
    raise ProtectedBranchError(
        f"{described} не подходит для `run`: переключитесь на рабочую ветку "
        f"перед стартом, например `git switch -c docs/{slug}`"
    )


def _check_pipeline_dir_absent(workspace_root: Path, slug: str) -> None:
    """`.disputatio/pipelines/<slug>/` не существует — иначе нужен `resume`."""
    pipeline_dir = workspace_root / SESSION_DIR_NAME / PIPELINES_DIR_NAME / slug
    if pipeline_dir.is_dir():
        raise PipelineAlreadyExists(
            f"каталог пайплайна {pipeline_dir} уже существует — "
            "продолжите его через `disp pipeline resume --slug "
            f"{slug}`, а не `run`"
        )


def _from_pipeline_table(table: Mapping[str, Any]) -> PipelineConfig:
    """Собирает `PipelineConfig` из разобранной таблицы `[pipeline]`."""
    where = "pipeline"
    kwargs: dict[str, Any] = {
        "spec_path": Path(_toml.text(table, "spec_path", where=where)),
        "plan_path": Path(_toml.text(table, "plan_path", where=where)),
    }
    if "max_architectural_returns" in table:
        kwargs["max_architectural_returns"] = _toml.integer(
            table, "max_architectural_returns", where=where
        )
    if "soft_max_pipeline_tokens" in table:
        kwargs["soft_max_pipeline_tokens"] = _toml.integer(
            table, "soft_max_pipeline_tokens", where=where
        )
    if "soft_max_pipeline_wall_seconds" in table:
        kwargs["soft_max_pipeline_wall_seconds"] = _toml.integer(
            table, "soft_max_pipeline_wall_seconds", where=where
        )
    if "protected_branches" in table:
        kwargs["protected_branches"] = _toml.texts(table, "protected_branches")
    if "anchor_path" in table:
        kwargs["anchor_path"] = Path(_toml.text(table, "anchor_path", where=where))
    if "checklists" in table:
        kwargs["checklists"] = _checklists(table["checklists"])
    kwargs["extra_gates"] = _extra_gates(table)
    return PipelineConfig(**kwargs)


def _extra_gates(table: Mapping[str, Any]) -> tuple[GateSpec, ...]:
    """`[[pipeline.gates]]` → `GateSpec`; имя baseline-гейта — `ConfigError`.

    Проверка на baseline — единственное место, где `_from_pipeline_table`
    поднимает `ConfigError` напрямую, а не техническое исключение, которое
    затем переводит `load_pipeline_config`: перепутать переопределение
    baseline с опечаткой ключа значило бы дать пользователю неверный совет.
    """
    gates = tuple(
        _toml.gate(item, where="pipeline.gates")
        for item in _toml.table_array(table, "gates")
    )
    for gate in gates:
        if gate.name in BASELINE_GATE_NAMES:
            raise ConfigError(
                f"[[pipeline.gates]] называет {gate.name!r} — это имя "
                "одного из пяти baseline doc-гейтов §6, и конфиг не вправе "
                "его переопределить: baseline гоняется безусловно, конфиг "
                "может только добавлять гейты сверх него"
            )
    return gates


def _checklists(value: Any) -> dict[str, dict[str, str]]:
    """`[pipeline.checklists.<contour>]` — merge поверх вендоренного дефолта
    задачи 2, по контуру и по id (§5.3, фикс-раунд 1, Important-1).

    Merge, а не замена: override одного `S1` не вправе тихо унести остальные
    пункты `spec` и весь контур `pair` — чеклист сходимости определяет
    критерий, по которому судят ревьюера, и «одна строка override молча
    выключает P1–P5» была бы единственным местом §5.3, не fail-closed.
    Неизвестный контур или id — тоже `ConfigError`, а не тихое добавление:
    опечатка в имени контура иначе дала бы конфиг без обоих контуров молча,
    а опечатка в id — чеклист с пунктом, которого ревьюер никогда не увидит
    покрытым ни дефолтом, ни override.
    """
    if not isinstance(value, Mapping):
        raise TypeError("pipeline.checklists обязана быть таблицей")
    merged = _default_checklists()
    for contour, items in value.items():
        if contour not in merged:
            raise ConfigError(
                f"[pipeline.checklists.{contour}] называет неизвестный "
                f"контур {contour!r} — допустимые контуры: "
                f"{sorted(merged)}"
            )
        if not isinstance(items, Mapping):
            raise TypeError(f"pipeline.checklists.{contour} обязана быть таблицей")
        for item_id, text in items.items():
            if item_id not in merged[contour]:
                raise ConfigError(
                    f"[pipeline.checklists.{contour}] называет неизвестный "
                    f"пункт {item_id!r} — допустимые id: "
                    f"{sorted(merged[contour])}"
                )
            if not isinstance(text, str):
                raise TypeError(
                    f"pipeline.checklists.{contour}.{item_id} обязан быть строкой"
                )
            merged[contour][item_id] = text
    return merged
