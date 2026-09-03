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

from disputatio.contracts import (
    CHECKLIST_BY_CONTOUR,
    CHECKLIST_TEXT,
    FINDINGS_ITEM_BY_CONTOUR,
    PipelineKind,
    ResolvedChecklist,
    validate_relative_path,
)
from disputatio.runtime import _toml
from disputatio.runtime.config import AgentConfig, LimitsConfig
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


def _default_checklists() -> dict[str, ResolvedChecklist]:
    """Вендоренный дефолт §5.3 встроенных контуров — разрешёнными объектами.

    Только `spec` и `pair`: у операторского контура `doc` вендоренного
    набора нет и быть не может — флотского правила «что такое сошедшийся
    чартер» не существует, копировать нечего (§5.3).
    """
    return {
        contour: ResolvedChecklist(
            order=ids,
            texts={item_id: CHECKLIST_TEXT[item_id] for item_id in ids},
            findings_item=FINDINGS_ITEM_BY_CONTOUR[contour],
        )
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

    `kind` обязателен и дефолта не имеет: вид выводится из ФОРМЫ секции и
    объявляется ею (P0), а «вид по умолчанию» сделал бы неполный конфиг
    молча парным. `spec_path`/`plan_path`/`document_path` опциональны
    порознь, но не произвольно: разбор (`_resolve_kind`) допускает ровно две
    комбинации, и наружу поля идут через аксессоры `documents()`,
    `contour_documents()`, `scope_paths()`.

    `checklists` — `{contour: ResolvedChecklist}`; для встроенных контуров
    дефолт вендоренный (`contracts.checklists_catalog`), а override из
    `[pipeline.checklists.<contour>]` переписывает только ТЕКСТЫ; для
    операторского контура `doc` конфиг объявляет весь набор вместе с ролью
    (§5.3). Снапшотится и хешируется отдельно (§3.2).

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

    kind: PipelineKind
    spec_path: Path | None = None
    plan_path: Path | None = None
    document_path: Path | None = None
    max_architectural_returns: int = DEFAULT_MAX_ARCHITECTURAL_RETURNS
    soft_max_pipeline_tokens: int = 0
    soft_max_pipeline_wall_seconds: int = 0
    protected_branches: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES
    checklists: Mapping[str, ResolvedChecklist] = field(
        default_factory=_default_checklists
    )
    extra_gates: tuple[GateSpec, ...] = ()
    anchor_path: Path = field(default_factory=_default_anchor_root)

    def documents(self) -> tuple[str, ...]:
        """Все редактируемые документы пайплайна в каноническом порядке.

        Опциональность трёх полей выше — не отступление от P10:
        `PipelineConfig` это РАЗОБРАННЫЙ конфиг, и он обязан уметь
        представить обе формы. Невыразимость чужой формы держат манифест
        (union `documents`) и fail-closed разбор `_resolve_kind`; сырые поля
        наружу не выходят — их закрывают эти три аксессора.
        """
        if self.kind is PipelineKind.DOCUMENT:
            return (self._require(self.document_path, "document_path"),)
        return (
            self._require(self.spec_path, "spec_path"),
            self._require(self.plan_path, "plan_path"),
        )

    def contour_documents(self, contour: str) -> tuple[str, ...]:
        """Документы, которые ВИДИТ ревизия контура (§5.1).

        У пары: spec-контур смотрит спеку, pair-контур сверяет план со
        спекой. У вида document читаемое и правимое совпадают — документ
        ровно один.
        """
        if contour == "spec":
            return (self._require(self.spec_path, "spec_path"),)
        return self.documents()

    def scope_paths(self, contour: str) -> tuple[str, ...]:
        """Граница `doc-scope`: что ревизия контура вправе ПРАВИТЬ (§6).

        У́же набора читаемых документов и вычисляется от контура, а не от
        вида: pair-контур читает спеку, но правит только план, поэтому
        правка спеки автором пары обязана валить гейт.
        """
        if contour == "pair":
            return (self._require(self.plan_path, "plan_path"),)
        return self.contour_documents(contour)

    @staticmethod
    def _require(value: Path | None, name: str) -> str:
        """POSIX-вид пути, обязательного для этой формы конфига.

        Отсутствие здесь — не пользовательская ошибка, а нарушение
        инварианта `_resolve_kind`: он не выпускает наружу ни пары
        наполовину, ни документа без пути. Поэтому `AssertionError`, а не
        `ConfigError`.
        """
        assert value is not None, (
            f"{name} не задан у вида, которому он обязателен: "
            "форму проверяет `_resolve_kind` до конструирования конфига (§3.2)"
        )
        return value.as_posix()


@dataclass(frozen=True, slots=True)
class SessionProfile:
    """`[agents.*]` + `[limits]` конфига пайплайна — общие на оба контура (§3.2).

    Отдельный тип, а не поля `PipelineConfig`: §3.2 держит эти секции рядом с
    `[pipeline]` в одном файле, но описывают они ОДНУ сессию debate loop'а, а
    не контур из нескольких (см. докстринг модуля). Ими фабрика ревизии
    достраивает `RuntimeConfig` — оставшиеся четыре поля (`session.id`,
    `session.mode`, `session.base_commit`, `task.prompt`) принадлежат
    конкретной ревизии и в общем профиле смысла не имеют.

    Своего `[session]`/`[task]` у конфига пайплайна нет намеренно, поэтому
    `RuntimeConfig.from_toml` на нём не применим: он потребовал бы вписать в
    общий файл идентификатор одной ревизии — то есть значение, которое к
    следующей ревизии уже ложь.
    """

    author: AgentConfig
    reviewer: AgentConfig
    limits: LimitsConfig


def load_session_profile(path: Path) -> SessionProfile:
    """Читает `[agents.*]` и `[limits]` из того же файла, что и `[pipeline]`.

    Отдельная функция, а не второе поле `load_pipeline_config`: у двух
    читателей разные потребители (`check_run_preconditions` и фабрика
    ревизии), и сцепив их, `disp pipeline status` тянул бы за собой разбор
    лимитов сессии ради строки о фазе.

    Иерархия ошибок та же, что у `load_pipeline_config` и
    `RuntimeConfig.from_toml`: любая негодность — `ConfigError`, потому что
    для пользователя это один факт «конфигом пользоваться нельзя».
    """
    raw = _read_toml(path)
    try:
        agents = _toml.table(raw, "agents")
        limits = _toml.table(raw, "limits")
        return SessionProfile(
            author=_agent(agents, "author"),
            reviewer=_agent(agents, "reviewer"),
            limits=LimitsConfig(
                max_rounds=_toml.integer(limits, "max_rounds", where="limits"),
                max_total_tokens=_toml.integer(
                    limits, "max_total_tokens", where="limits"
                ),
                max_wall_seconds=_toml.integer(
                    limits, "max_wall_seconds", where="limits"
                ),
                schema_retries=_toml.integer(limits, "schema_retries", where="limits"),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"[agents]/[limits] в {path} непригодны: {exc}") from exc


def _agent(agents: Mapping[str, Any], role: str) -> AgentConfig:
    """Агент из вложенной таблицы `[agents.<role>]` конфига пайплайна."""
    table = _toml.table(agents, role)
    where = f"agents.{role}"
    return AgentConfig(
        adapter=_toml.text(table, "adapter", where=where),
        model=_toml.text(table, "model", where=where),
    )


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
    raw = _read_toml(path)
    try:
        table = _toml.table(raw, "pipeline")
        return _from_pipeline_table(table)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"[pipeline] в {path} непригодна: {exc}") from exc


def _read_toml(path: Path) -> Mapping[str, Any]:
    """Разобранный TOML конфига пайплайна; любая негодность — `ConfigError`.

    Общий вход обоих читателей файла (`load_pipeline_config`,
    `load_session_profile`): «файла нет», «файл не в UTF-8» и «это не TOML»
    — один и тот же факт для пользователя, и вторая копия этих трёх веток
    рано или поздно ответила бы на него иначе.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"конфиг пайплайна {path} не читается: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"конфиг пайплайна {path} не в UTF-8: {exc}") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} не разбирается как TOML: {exc}") from exc


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
    """Собирает `PipelineConfig` из разобранной таблицы `[pipeline]`.

    Пара документов проверяется тем же `validate_relative_path`, каким
    манифест проверяет свои пути (`contracts.pipeline`), — одна
    формулировка правила на оба слоя. Проверка здесь не дублирующая, а
    ранняя: без неё `spec_path = "../outside/spec.md"` доживал бы до записи
    манифеста, то есть до момента, когда `run` уже создал анкер, каталог
    пайплайна и снапшоты, — тот же дефект, что D1. `ValueError` отсюда
    `load_pipeline_config` переводит в `ConfigError` с именем файла.
    """
    where = "pipeline"
    unknown = set(table) - _KNOWN_PIPELINE_KEYS
    if unknown:
        raise ConfigError(
            f"[pipeline] содержит неизвестные ключи: {sorted(unknown)} — "
            "закрытая схема §3.2 отклоняет их, а не молчаливо игнорирует "
            "(FR-07)"
        )
    kind = _resolve_kind(table)
    if kind is PipelineKind.DOCUMENT and "max_architectural_returns" in table:
        raise ConfigError(
            _both_forms(
                "max_architectural_returns не применим к виду document: "
                "возвратов у него нет. Ключ отвергается, а не игнорируется — "
                "молча проигнорированная настройка оператора хуже отказа"
            )
        )
    kwargs: dict[str, Any] = {"kind": kind}
    for key in _PATH_KEYS_BY_KIND[kind]:
        kwargs[key] = Path(validate_relative_path(_toml.text(table, key, where=where)))
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
    kwargs["checklists"] = _checklists(table.get("checklists"), kind)
    kwargs["extra_gates"] = _extra_gates(table)
    return PipelineConfig(**kwargs)


#: Ключи путей, обязательные для каждой формы (§3.2). Читаются циклом, а не
#: двумя ветками: форма уже установлена `_resolve_kind`, и второе её
#: перечисление разошлось бы с первым.
_PATH_KEYS_BY_KIND: Final[dict[PipelineKind, tuple[str, ...]]] = {
    PipelineKind.PAIR: ("spec_path", "plan_path"),
    PipelineKind.DOCUMENT: ("document_path",),
}

#: Замкнутая схема `[pipeline]` (FR-07): всё, что читает разбор ниже, плюс
#: `checklists`/`gates`, у которых своя вложенная закрытая схема.
_KNOWN_PIPELINE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "spec_path",
        "plan_path",
        "document_path",
        "max_architectural_returns",
        "soft_max_pipeline_tokens",
        "soft_max_pipeline_wall_seconds",
        "protected_branches",
        "anchor_path",
        "checklists",
        "gates",
    }
)

_PAIR_FORM: Final = (
    "  [pipeline]\n"
    '  spec_path = "docs/specs/…-design.md"\n'
    '  plan_path = "docs/plans/…-plan.md"'
)
_DOCUMENT_FORM: Final = (
    "  [pipeline]\n"
    '  document_path = "docs/charter.md"\n'
    "  [pipeline.checklists.doc]\n"
    '  findings_item = "B3"\n'
    "  [pipeline.checklists.doc.items]\n"
    '  B3 = "нет blocker/major-находок"'
)


def _both_forms(problem: str) -> str:
    """Текст отказа обязан назвать ОБЕ схемы, а не только нарушенную (C3).

    Оператор, написавший `document_path` рядом со `spec_path`, обязан из
    текста ошибки узнать, какие две формы существуют: причина отказа без
    альтернативы оставляет его гадать, что именно исправлять.
    """
    return (
        f"{problem}\n\nСекция [pipeline] существует в двух "
        f"взаимоисключающих формах:\n\nпара «спека + план»:\n{_PAIR_FORM}\n\n"
        f"одиночный документ:\n{_DOCUMENT_FORM}"
    )


def _resolve_kind(table: Mapping[str, Any]) -> PipelineKind:
    """Вид пайплайна по форме секции — fail-closed, без «побеждает первый».

    Форма И ЕСТЬ объявление вида (P0), поэтому неполная и смешанная формы
    отвергаются, а не доопределяются: конфиг, из которого вид выводится
    догадкой, объявлял бы механику, которой оператор не просил.
    """
    has_document = "document_path" in table
    has_spec = "spec_path" in table
    has_plan = "plan_path" in table
    if has_document and (has_spec or has_plan):
        raise ConfigError(
            _both_forms(
                "[pipeline] смешивает формы: document_path задан вместе с путями пары"
            )
        )
    if has_document:
        return PipelineKind.DOCUMENT
    if has_spec and has_plan:
        return PipelineKind.PAIR
    if has_spec or has_plan:
        raise ConfigError(
            _both_forms("[pipeline] задаёт пару наполовину: нужны оба пути")
        )
    raise ConfigError(_both_forms("[pipeline] не задаёт ни одной из форм"))


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


def _checklists(value: Any, kind: PipelineKind) -> dict[str, ResolvedChecklist]:
    """Два происхождения набора, две формы таблицы (§5.3 SPEC-002).

    Для встроенных контуров конфиг переписывает ТЕКСТЫ вендоренного набора;
    для операторского `doc` объявляет набор целиком вместе с ролью. Разная
    форма отражает разную природу: критерий сходимости чартера знает автор
    документа, а не это репо.

    Собираются чеклисты ровно своего вида (P10): у документного пайплайна
    `spec`/`pair` не конструируются вовсе, а не лежат неиспользованными.
    """
    if value is not None and not isinstance(value, Mapping):
        raise TypeError("pipeline.checklists обязана быть таблицей")
    table: Mapping[str, Any] = value or {}
    if kind is PipelineKind.DOCUMENT:
        unknown = set(table) - {"doc"}
        if unknown:
            raise ConfigError(
                f"[pipeline.checklists] содержит неизвестные ключи: "
                f"{sorted(unknown)} — вид document допускает только "
                "[pipeline.checklists.doc] (FR-07)"
            )
        return {"doc": _operator_checklist(table.get("doc"))}
    return _builtin_checklists(table)


def _operator_checklist(table: Any) -> ResolvedChecklist:
    """`[pipeline.checklists.doc]` — состав, порядок и роль от оператора (§5.3).

    Вендоренного дефолта у контура нет: флотского правила «что такое
    сошедшийся чартер» не существует, копировать нечего, а навязанные пять
    слотов заставляли бы автора выдумывать условия под чужой состав.
    Поэтому все три отказа явные, включая обязательность самой таблицы.

    Порядок — `tuple(items)`: `tomllib` сохраняет порядок объявления файла,
    и этот же порядок уходит в снапшот `checklists.toml` при `run`.

    **Известное ограничение (issue #65): на `resume` порядок и состав
    берутся из ЖИВОГО конфига, а не из снапшота.** `resume` сверяет с
    манифестом только вид пайплайна (P0, §8.1 шаг 1), поэтому конфиг,
    изменённый между запусками, доедет до ревьюера, хотя манифест
    удостоверяет хеш прежнего снапшота. Детерминизм порядка на всю жизнь
    пайплайна здесь пока НЕ обещается — обещание станет правдой, когда
    `resume` начнёт сверять неизменяемую половину `[pipeline]` fail-closed
    (SPEC-002, TASK-004 очереди WS-disputatio-65). Закрытая immutable-
    классификация, которой это ограничение измеряется, и функция сравнения
    двух её проекций уже существуют —
    `pipeline_semantic_proof.PIPELINE_CONFIG_FIELD_CLASS`,
    `build_projection`, `diff_projections` — но `resume` их пока не
    вызывает, поэтому здесь и остаётся открытым. Ограничение общее для
    видов `pair` и `document`.
    """
    if not isinstance(table, Mapping):
        raise ConfigError(
            "[pipeline.checklists.doc] обязательна для вида document: "
            "вендоренного набора у операторского контура нет"
        )
    unknown = set(table) - {"findings_item", "items"}
    if unknown:
        raise ConfigError(
            f"[pipeline.checklists.doc] содержит неизвестные ключи: "
            f"{sorted(unknown)} — допустимы только findings_item и items "
            "(FR-07)"
        )
    items = table.get("items")
    if not isinstance(items, Mapping) or not items:
        raise ConfigError(
            "[pipeline.checklists.doc.items] пуст: критерий сходимости "
            "документа обязан быть объявлен"
        )
    role = table.get("findings_item")
    if not isinstance(role, str):
        raise ConfigError(
            "[pipeline.checklists.doc] обязана назначить findings_item — "
            "пункт со смыслом «нет blocker/major-находок». Без него правило "
            "V8 стало бы тихим no-op'ом через конфигурацию (§5.3)"
        )
    if role not in items:
        raise ConfigError(
            f"[pipeline.checklists.doc] findings_item = {role!r} не назван "
            f"среди items: {sorted(items)}"
        )
    texts: dict[str, str] = {}
    for item_id, text in items.items():
        if not isinstance(text, str):
            raise TypeError(
                f"pipeline.checklists.doc.items.{item_id} обязан быть строкой"
            )
        texts[item_id] = text
    return ResolvedChecklist(order=tuple(texts), texts=texts, findings_item=role)


def _builtin_checklists(value: Mapping[str, Any]) -> dict[str, ResolvedChecklist]:
    """`[pipeline.checklists.<contour>]` — merge ТЕКСТОВ поверх вендоренного набора.

    Merge, а не замена: override одного `S1` не вправе тихо унести остальные
    пункты `spec` и весь контур `pair` — чеклист сходимости определяет
    критерий, по которому судят ревьюера, и «одна строка override молча
    выключает P1–P5» была бы единственным местом §5.3, не fail-closed.
    Неизвестный контур или id — тоже `ConfigError`, а не тихое добавление:
    опечатка в имени контура иначе дала бы конфиг без обоих контуров молча,
    а опечатка в id — чеклист с пунктом, которого ревьюер никогда не увидит
    покрытым ни дефолтом, ни override. Состав менять нельзя вовсе: он задан
    флотским правилом, и «одна строка override выключает P1–P5» была бы
    дырой ровно там, где критерий и определяется.
    """
    defaults = _default_checklists()
    texts = {contour: dict(item.texts) for contour, item in defaults.items()}
    for contour, items in value.items():
        if contour not in texts:
            raise ConfigError(
                f"[pipeline.checklists.{contour}] называет неизвестный "
                f"контур {contour!r} — допустимые контуры: {sorted(texts)}"
            )
        if not isinstance(items, Mapping):
            raise TypeError(f"pipeline.checklists.{contour} обязана быть таблицей")
        for item_id, text in items.items():
            if item_id not in texts[contour]:
                raise ConfigError(
                    f"[pipeline.checklists.{contour}] называет неизвестный "
                    f"пункт {item_id!r} — допустимые id: "
                    f"{sorted(texts[contour])}"
                )
            if not isinstance(text, str):
                raise TypeError(
                    f"pipeline.checklists.{contour}.{item_id} обязан быть строкой"
                )
            texts[contour][item_id] = text
    return {
        contour: ResolvedChecklist(
            order=default.order,
            texts=texts[contour],
            findings_item=default.findings_item,
        )
        for contour, default in defaults.items()
    }
