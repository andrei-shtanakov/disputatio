"""Composition root — единственная точка связывания портов ([DESIGN-001]).

Здесь и только здесь имена конкретных реализаций встречаются вне их
собственных пакетов (INV-11). Результат — `RuntimeDeps`: frozen-контейнер,
все поля которого удовлетворяют `runtime_checkable` Protocol'ам
`disputatio.contracts.ports`. Цикл (`runtime/loop.py`, `runtime/steps.py`)
принимает контейнер и не знает, чем он наполнен — подмена любой зависимости
фейком не требует ни одной правки в коде шагов ([REQ-001]).

Три инварианта сборки, каждый со своим тестом:

1. **Sink создаётся раньше адаптеров** и инжектится в их конструкторы
   (`event_sink=`). Это единственный способ, которым нативный поток CLI
   превращается в события §8: порт `AgentAdapter` стриминга не описывает.
2. **Роли различены** — автор получает `Role.AUTHOR`, ревьюер
   `Role.REVIEWER`; из роли пакет адаптеров сам выводит права §7.
3. **Права ревьюера не переопределяются** (NFR-003): фабрике не передаётся
   ничего, кроме роли, каталога, sink'а и id сессии, поэтому read-only
   остаётся собственностью пакета адаптеров и меняется вместе с ним.

Импорты чужих пакетов идут только через их публичные `__init__` — это
пинится AST-сканом этого файла в `tests/runtime/test_composition.py`.
"""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import anyio

from disputatio.adapters import ClaudeCodeAdapter, CodexAdapter
from disputatio.contracts import (
    AgentAdapter,
    EventSink,
    Mode,
    Role,
    RoundBoundaryPolicy,
    SessionState,
    StateStore,
    Verifier,
)
from disputatio.core import TERMINAL_PHASES
from disputatio.events import (
    FilePipelineStateStore,
    FileStateStore,
    IntegrityAnchor,
    JsonlEventSink,
    PipelineEventSink,
    atomic_write,
    bootstrap_session,
    write_config_snapshot,
)
from disputatio.runtime.config import RuntimeConfig
from disputatio.runtime.errors import ConfigError, UnknownAdapterError
from disputatio.runtime.git import GitOps
from disputatio.runtime.history import load_patch
from disputatio.runtime.layout import adopted_findings_json
from disputatio.runtime.pipeline_adopt import OperatorIntents
from disputatio.runtime.pipeline_config import (
    PipelineConfig,
    SessionProfile,
    toplevel_root,
)
from disputatio.runtime.pipeline_export import ExportFn, export_pipeline
from disputatio.runtime.pipeline_integrity import ControlPlane, PipelineIntegrityPolicy
from disputatio.runtime.pipeline_resume import PipelineResume
from disputatio.runtime.pipeline_runner import (
    CONTOUR_SPEC,
    PipelineRunner,
    SessionCreation,
    load_session_state,
    pipeline_dir_of,
    split_revision,
)
from disputatio.verifier import DocVerifier, VerifierRunner

AdapterFactory = Callable[..., AgentAdapter]
"""Фабрика адаптера: вызывается только именованными аргументами сборки."""

ADAPTER_FACTORIES: Mapping[str, AdapterFactory] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}


@dataclass(frozen=True, slots=True)
class RuntimeDeps:
    """Связка портов с реализациями — результат единственной композиции.

    Корней два, и они названы порознь (SPEC-002 §4.1). `workspace_root` —
    рабочий git-репозиторий: из него запускаются агентские CLI, по нему
    считается `changes.patch` и по нему же прогоняются гейты. `artifact_root`
    — журнал сессии: `.disputatio/` со состоянием, событиями, раундами и
    экспортом. До разделения это был один параметр `root`, и он не
    различался; пайплайн кладёт несколько сессий под ОДИН репозиторий, и
    единственное поле свело бы их в общий `session.json`. Имени `root` здесь
    больше нет намеренно: молча перепутать два корня можно было только пока
    один из них назывался как оба.

    **Предусловие обоих корней: они уже нормализованы** (`resolve()`) и
    журнал лежит ВНУТРИ рабочего корня. Держит это `build_runtime` через
    `_normalized_roots`, а не тип: контейнер конструируется напрямую в
    тестах, и валидация в `__post_init__` отвергала бы законный фейковый
    вход, а нормализация на месте — переписывала бы то, что вызывающий подал
    сознательно. Цена предусловия конкретна: шаг `review` считает путь
    артефакта от рабочего корня (`relative_to`), и пара корней в разных
    формах падает там — то есть после `reset --hard`, работы автора и
    прогона гейтов.
    """

    workspace_root: Path
    artifact_root: Path
    store: StateStore
    sink: EventSink
    author: AgentAdapter
    reviewer: AgentAdapter
    verifier: Verifier
    git: GitOps
    now: Callable[[], datetime]
    monotonic: Callable[[], float]


def _utcnow() -> datetime:
    """Часы сессии по умолчанию: aware-UTC, как требует схема артефактов."""
    return datetime.now(UTC)


def build_runtime(
    config: RuntimeConfig,
    workspace_root: Path,
    *,
    artifact_root: Path | None = None,
    git: GitOps,
    sink: EventSink | None = None,
    store: StateStore | None = None,
    verifier: Verifier | None = None,
    now: Callable[[], datetime] = _utcnow,
    monotonic: Callable[[], float] = time.monotonic,
) -> RuntimeDeps:
    """Собирает `RuntimeDeps` из конфига сессии ([REQ-001]).

    Каждый порт имеет override-параметр: тест подменяет любую зависимость
    фейком, не трогая ни одной строки цикла. `git` пока обязателен —
    реализация `GitCli` приходит с [DESIGN-010]; дефолт по умолчанию,
    указывающий на несуществующий класс, превратился бы в отложенный
    `ImportError` в момент старта сессии.

    `workspace_root` — рабочий git-репозиторий; `artifact_root` — журнал
    сессии (SPEC-002 §4.1). `None` означает `artifact_root = workspace_root`,
    то есть раскладку до разделения байт-в-байт: `disp run`/`disp resume`
    второго корня не знают, и знать им его незачем — одна сессия на
    репозиторий остаётся законным случаем. Разводит корни только пайплайн, у
    которого сессий под одним репозиторием несколько.

    Кто какой корень получает — не деталь сборки, а само разделение:
    `JsonlEventSink` и `FileStateStore` пишут журнал, поэтому им уходит
    `artifact_root`; адаптеры (`session_dir` — их рабочая директория) и
    `VerifierRunner` (гейты идут по коду) работают с репозиторием, поэтому им
    уходит `workspace_root`. Перепутай эти две строки — сессия писала бы
    состояние туда, где его никто не ищет, а гейты гонялись бы по журналу.

    Оба корня нормализуются здесь, один раз, и в `RuntimeDeps` уходят уже
    `resolve()`-нутыми: проверка вложенности и шаг `review`, считающий путь
    артефакта от рабочего корня, обязаны говорить об ОДНИХ путях. Оставь
    корни как переданы — и пара «абсолютный `workspace_root` + относительный
    `artifact_root`» прошла бы гейт (он сравнивает после `resolve`) и упала
    бы в `relative_to` уже на шаге `review`, то есть ровно там, откуда гейт
    её и уводит. Заодно нормализация уравнивает пути через симлинк.

    Вложенность проверяется до разбора имён адаптеров: негодная пара корней —
    отказ сборки, и приходить он обязан раньше любого другого.

    Порядок сборки значим: sink создаётся ПЕРЕД адаптерами, потому что
    попадает в их конструкторы. Соберись адаптеры первыми — они получили бы
    `event_sink=None`, и поток §8 молча исчез бы: адаптер без sink'а
    работает, просто ничего не транслирует.
    """
    workspace, journal = _normalized_roots(
        workspace_root, artifact_root if artifact_root is not None else workspace_root
    )
    event_sink = sink if sink is not None else JsonlEventSink(journal)
    return RuntimeDeps(
        workspace_root=workspace,
        artifact_root=journal,
        store=store if store is not None else FileStateStore(journal),
        sink=event_sink,
        author=_build_adapter(
            config.author.adapter,
            role=Role.AUTHOR,
            workspace_root=workspace,
            sink=event_sink,
            session_id=config.session_id,
        ),
        reviewer=_build_adapter(
            config.reviewer.adapter,
            role=Role.REVIEWER,
            workspace_root=workspace,
            sink=event_sink,
            session_id=config.session_id,
        ),
        verifier=(
            verifier
            if verifier is not None
            else VerifierRunner(list(config.gates), workspace)
        ),
        git=git,
        now=now,
        monotonic=monotonic,
    )


def _normalized_roots(workspace_root: Path, artifact_root: Path) -> tuple[Path, Path]:
    """Нормализует пару корней и отвергает журнал вне репо (SPEC-002 §4.1).

    Проверка и нормализация — одна операция, а не две: разъедься они, вход,
    прошедший проверку в одной форме, потребитель получил бы в другой. Ровно
    так и было, пока функция только проверяла: `resolve()` внутри неё делал
    смесь форм законной, а `_relative_artifact` звал `relative_to` по путям
    как переданы — и падал на шаге `review`. Поэтому нормализованная пара не
    вычисляется где-то ещё, а **возвращается** отсюда.

    Само требование вложенности приходит от того же шага `review`: путь
    артефакта в промпте ревьюера считается ОТ рабочего корня, потому что
    ревьюер запущен оттуда. Журнал снаружи репозитория назвать таким путём
    нечем. Отказ пришёлся бы на середину раунда — уже после `reset --hard`,
    работы автора и прогона гейтов; здесь он стоит одного сравнения путей и
    не стоит ни одного вызова агента.

    `ValueError`, а не доменная ошибка [DESIGN-020]: это не отказ во вводе
    пользователя, а негодный аргумент вызывающего кода — CLI второго корня
    не передаёт вовсе. Тем же `ValueError` отвечают `events` на имя
    артефакта, уводящее запись мимо раунда: вопрос один и тот же — форма
    пути, а не конфигурация сессии.

    `resolve()` решает обе половины формы сразу: относительный корень рядом
    с абсолютным (законный вход вызывающего) и путь через симлинк, который
    `is_relative_to` посчитал бы лежащим снаружи.
    """
    workspace = workspace_root.resolve()
    journal = artifact_root.resolve()
    if not journal.is_relative_to(workspace):
        raise ValueError(
            f"artifact_root {artifact_root} лежит вне рабочего репозитория "
            f"{workspace_root}: путь артефакта в промпте ревьюера считается от "
            "рабочего корня, и журнал снаружи него назвать нечем"
        )
    return workspace, journal


def _build_adapter(
    name: str,
    *,
    role: Role,
    workspace_root: Path,
    sink: EventSink | None,
    session_id: str,
) -> AgentAdapter:
    """Создаёт адаптер `name` для роли `role` с инжектированным sink'ом.

    Фабрике передаются ровно четыре аргумента: всё остальное — дефолты
    пакета адаптеров. Любой пятый (права, флаги CLI) означал бы, что runtime
    завёл второе мнение о §7 (NFR-003).

    `sink=None` бывает ровно у одного вызывающего — пробы `_resolve_adapters`,
    которая собранный адаптер выбрасывает: журнал принадлежит РЕВИЗИИ, а её
    на момент пробы ещё нет. Продакшен-путь (`build_runtime`) подаёт
    настоящий sink всегда — это инвариант 1 сборки, и пинит его тест
    композиции, а не эта сигнатура.

    Фабрика вправе отказаться собирать пару «адаптер + роль»: `codex`
    ревьюером требует read-only worktree (ADR-004), которого на дефолтах нет.
    Имя зарегистрировано в реестре, то есть легально в `config.toml`, —
    значит отказ обязан прийти в иерархии [DESIGN-020], а не голым
    `ValueError` из чужого пакета. Причина не переписывается, а цитируется:
    почему именно роль не собирается, знает пакет адаптеров, не runtime.
    """
    factory = ADAPTER_FACTORIES.get(name)
    if factory is None:
        known = ", ".join(sorted(ADAPTER_FACTORIES))
        raise UnknownAdapterError(
            f"неизвестный адаптер {name!r} для роли {role.value}; известны: {known}"
        )
    try:
        return factory(
            role=role,
            session_dir=workspace_root,
            event_sink=sink,
            session=session_id,
        )
    except ValueError as exc:
        raise ConfigError(
            f"адаптер {name!r} не собирается для роли {role.value}: {exc}"
        ) from exc


def _resolve_adapters(profile: SessionProfile, *, workspace_root: Path) -> None:
    """Разрешает адаптеры обеих ролей профиля, отбрасывая результат (§3.1).

    Проверка — это САМА сборка, а не сверка имён по словарю: половина отказов
    приходит из конструктора (`codex` ревьюером требует read-only worktree,
    ADR-004), и список имён пропустил бы такой конфиг дальше. Ровно поэтому
    зовётся `_build_adapter`, а не читается `ADAPTER_FACTORIES`: второй способ
    ответить на вопрос «соберётся ли» разошёлся бы с первым молча.

    Результат отбрасывается, и это не расточительство: адаптер живёт внутри
    ревизии, у которой свой `session_id` и свой журнал, а конструктору ни то,
    ни другое для отказа не нужно — отсюда пустая identity пробы. Держать
    собранную пару до первой ревизии значило бы отдать в сессию адаптер,
    собранный под чужой journal и чужое имя.

    Ничего не пишет: конструкторы адаптеров файлов не трогают, поэтому вызов
    законно стоит до всякой durable-мутации пайплайна.
    """
    for role, agent in (
        (Role.AUTHOR, profile.author),
        (Role.REVIEWER, profile.reviewer),
    ):
        _build_adapter(
            agent.adapter,
            role=role,
            workspace_root=workspace_root,
            sink=None,
            session_id="",
        )


@dataclass(frozen=True, slots=True)
class PipelineDeps:
    """Связка пайплайновых портов — результат второй композиции (SPEC-002 §9).

    Двух движков здесь нет: `resume` доводит пайплайн до состояния, из
    которого вправе продолжить `runner`, и передаёт управление ему. Наружу
    отдаются оба плюс хранилище манифеста — ровно то, что нужно четырём
    командам §3.1, и ни одной операции сверх.
    """

    workspace_root: Path
    slug: str
    store: FilePipelineStateStore
    runner: PipelineRunner
    resume: PipelineResume


def build_pipeline(
    config: PipelineConfig,
    profile: SessionProfile,
    workspace_root: Path,
    slug: str,
    *,
    git: GitOps,
    now: Callable[[], datetime] = _utcnow,
    monotonic: Callable[[], float] = time.monotonic,
    exporter: ExportFn = export_pipeline,
) -> PipelineDeps:
    """Собирает пайплайн из реальных реализаций (SPEC-002 §3.1, §7, §9).

    Второй composition root, и он не дублирует первый, а надстраивается над
    ним: каждая ревизия — обычная сессия SPEC-001, и собирает её тот же
    `build_runtime` внутри `resume_session`. Пайплайн добавляет к ней ровно
    четыре вещи, и все четыре живут здесь, а не в runner'е:

    1. **Фабрика ревизии** — `bootstrap` каталога, снапшот `config.toml` с
       `Mode.DOCUMENT` и durable-набор архитектурных находок (§7.3). Находки
       пишутся файлом, а не передаются в память: интент `create_session`
       исполняется один раз, а промпт автора собирается в каждом раунде — в
       том числе в другом процессе после краха.
    2. **Драйвер ревизии** — `resume_session` под `anyio.run`. Именно
       resume, а не `drive`, и для холодного старта тоже: фабрика уже
       положила `session.json` в `IDLE`, поэтому «первый прогон» и
       «продолжение» отличаются только содержимым файла, а не кодовым путём.
    3. **`DocVerifier` вместо `VerifierRunner`** — пять baseline-гейтов §6 по
       документам своего контура. `allowed` (граница `doc-scope`) уже, чем
       `doc_paths`: pair-контур ЧИТАЕТ спеку, но правит только план (§5.1).
    4. **Политика целостности P9** вокруг каждого хода автора. Ей нужны пути
       обоих журналов, и берутся они у их владельцев (`sink.path`), а не
       вычисляются здесь: [DESIGN-016] запрещает `runtime` строить путь
       `events.jsonl` вообще.

    Оба журнала сторожатся вместе, а не только пайплайновый: сузь набор до
    одного — и подмена ленты сессии, из которой UI читает поток §8, перестала
    бы быть нарушением P9.

    **Адаптеры разрешаются здесь, а не в первой ревизии.** Собирает их
    `build_runtime` внутри `resume_session`, то есть по интенту `run_session`
    — а к нему runner приходит, уже создав анкер, каталог, снапшоты,
    `pipeline.json` и doc-сессию. Опечатка в имени адаптера отказывала бы
    ПОСЛЕ них, и штатными командами такой пайплайн уже не запустить: `run`
    упирается в существующий анкер, а `resume` перечитывает снапшот с той же
    опечаткой. §3.1 требует обратного порядка, поэтому оба имени проверяются
    до возврата `PipelineDeps` — тем же `_build_adapter`, которым потом
    собирается ревизия.

    `loop` и `steps` импортируются внутри функции: оба зависят от ЭТОГО
    модуля (`build_runtime`, `RuntimeDeps`), и импорт на уровне модуля дал бы
    цикл. Это единственная причина; ни одного другого отложенного импорта
    здесь нет.
    """
    from disputatio.runtime.loop import resume_session
    from disputatio.runtime.steps import DocSessionSpec

    workspace = workspace_root.resolve()
    _require_toplevel(git, workspace)
    _resolve_adapters(profile, workspace_root=workspace)
    store = FilePipelineStateStore(workspace)
    try:
        sink = PipelineEventSink(workspace, slug)
    except ValueError as exc:
        raise ConfigError(f"негодный слаг пайплайна: {exc}") from exc

    def session_factory(creation: SessionCreation) -> SessionState:
        """Материализует ревизию: каталог, снапшот конфига, находки, состояние.

        `base_commit` берётся из `SessionCreation`, когда его назвал
        операторский adoption (§3.1), и только иначе — из текущего `HEAD`:
        ревизия, открытая принятой правкой, обязана сбрасываться к
        чекпоинту, а не к состоянию до него.
        """
        bootstrap_session(creation.artifact_root)
        session_config = RuntimeConfig(
            session_id=creation.session_id,
            mode=Mode.DOCUMENT,
            base_commit=creation.base_commit or git.head_sha(),
            task_prompt=creation.task_text,
            author=profile.author,
            reviewer=profile.reviewer,
            limits=profile.limits,
            # Baseline-гейты §6 `GateSpec`-ами не описываются (это функции
            # пакета `verifier`), поэтому в снапшот едут только добавленные
            # конфигом: `gate_started`/`gate_finished` §8 сообщают ровно о
            # том, чьи имена оркестратору известны.
            gates=config.extra_gates,
        )
        write_config_snapshot(creation.artifact_root, session_config.render_toml())
        atomic_write(
            adopted_findings_json(creation.artifact_root),
            json.dumps(
                [issue.model_dump(mode="json") for issue in creation.findings],
                ensure_ascii=False,
            ),
        )
        state = session_config.to_session_state(created_at=now())
        FileStateStore(creation.artifact_root).save(state)
        return state

    def session_driver(
        artifact_root: Path, session_id: str, policy: RoundBoundaryPolicy | None
    ) -> SessionState:
        """Гонит одну ревизию тем же циклом, что и `disp run` ([REQ-008]).

        Исход оборвавшейся сессии спрашивается у ДИСКА — тем же приёмом,
        каким `disp run` отличает провал сессии от поломки CLI. Шаг,
        исчерпавший schema-повторы, поднимает ошибку последней попытки
        ([DESIGN-006]), но `FAILED` к этому моменту уже записан ядром, то
        есть исход сессии определён, а исключение лишь называет причину.
        Пропусти его наружу — и runner не дошёл бы до `finish_session`:
        пайплайн остался бы с непроигранным интентом и объявил бы себя
        `FAILED` только на следующем `resume`, хотя всё нужное для этого
        уже лежит на диске (§7.2). Любое ДРУГОЕ исключение уходит выше как
        есть: сессия, оставшаяся нетерминальной, — это обрыв, и выдавать
        его за исход значило бы объявить пайплайн упавшим там, где его
        нужно продолжить.
        """
        contour = _contour_of(session_id)
        session_sink = JsonlEventSink(artifact_root)
        lifecycle = PipelineIntegrityPolicy(
            anchor=IntegrityAnchor(config.anchor_path, workspace, slug),
            control_plane=ControlPlane(
                workspace_root=workspace,
                pipeline_dir=pipeline_dir_of(workspace, slug),
                artifact_root=artifact_root,
                append_only_paths=(sink.path, session_sink.path),
            ),
        )

        async def call() -> SessionState:
            """Тело прогона ревизии; `anyio.run` — правило проекта."""
            return await resume_session(
                workspace,
                session_id,
                artifact_root=artifact_root,
                round_boundary=policy,
                lifecycle=lifecycle,
                documents=DocSessionSpec(
                    contour=contour,
                    doc_paths=config.contour_documents(contour),
                    # Разрешённый чеклист конфига целиком, а не собранный
                    # здесь заново: §5.3 разрешает переопределить формулировки
                    # (а у контура `doc` — и весь состав), манифест хеширует
                    # именно его снапшот, и другого канала до ревьюера нет.
                    checklist=config.checklists[contour],
                ),
                git=git,
                sink=session_sink,
                verifier=_doc_verifier(config, contour, workspace, artifact_root),
                now=now,
                monotonic=monotonic,
            )

        try:
            return anyio.run(call)
        except Exception:
            settled = load_session_state(artifact_root, session_id)
            if settled is not None and settled.state in TERMINAL_PHASES:
                return settled
            raise

    runner = PipelineRunner(
        store=store,
        sink=sink,
        git=git,
        session_driver=session_driver,
        session_factory=session_factory,
        exporter=exporter,
        now=now,
        config=config,
        workspace_root=workspace,
    )
    return PipelineDeps(
        workspace_root=workspace,
        slug=slug,
        store=store,
        runner=runner,
        resume=PipelineResume(
            runner=runner,
            store=store,
            git=git,
            config=config,
            workspace_root=workspace,
            intents=OperatorIntents(
                store=store,
                sink=sink,
                git=git,
                config=config,
                workspace_root=workspace,
                now=now,
            ),
        ),
    )


def _require_toplevel(git: GitOps, workspace: Path) -> None:
    """`--root` обязан быть toplevel репозитория — иначе отказ (SPEC-002 §6).

    Нормализация путей в пайплайне сделана наполовину, и это его честная
    граница, а не забытая мелочь. `check_run_preconditions` и `compute_scope`
    приводят обе стороны сравнения к toplevel через
    `GitOps.toplevel_prefix()`; у `doc-scope` этого нет — `allowed` считается
    от `--root` (`config.plan_path`), а пути в `changes.patch` git пишет от
    toplevel. Пока корни совпадают, разницы нет; ниже toplevel гейт границы
    контура сравнивал бы `docs/plan.md` с `sub/docs/plan.md` и не находил
    совпадений — то есть правка спеки автором pair-контура прошла бы МОЛЧА.

    Неподдержанный случай обязан отказывать, а не отрабатывать наполовину, и
    отказывать до первой мутации: `build_pipeline` вызывается раньше и `run`,
    и `resume`, поэтому ни каталога пайплайна, ни анкера после отказа не
    остаётся. Снять ограничение можно, дав `_doc_verifier` тот же
    `toplevel_prefix`, — но §6 про базу `doc-scope` не говорит вовсе, хотя в
    двух других местах спека прямо называет базой корень репозитория (§4.2:
    «все пути — относительные (к корню репозитория или каталогу пайплайна)»;
    §6, containment: цель «обязана остаться внутри корня репозитория»).
    Вывести из этих двух мест третье правило — про базу СРАВНЕНИЯ путей у
    `doc-scope` — значит угадать, и молчаливая догадка здесь была бы той же
    ошибкой, что и половинчатая нормализация.
    """
    toplevel = toplevel_root(git, workspace)
    if toplevel == workspace:
        return
    raise ConfigError(
        f"--root {workspace} не является корнем репозитория ({toplevel}): "
        "пайплайн сравнивает пути пары документов с путями, которые git "
        "пишет от корня, и ниже него гейт doc-scope молча перестал бы "
        "замечать выход за границу контура. Запустите пайплайн из корня "
        "репозитория"
    )


def _contour_of(session_id: str) -> Literal["spec", "pair"]:
    """Контур ревизии по её имени (`spec-r2` → `spec`, §4.1 SPEC-002).

    Имя ревизии — durable-факт манифеста и каталога, а не догадка: его
    строит `revision_id`, и обратная операция `split_revision` живёт рядом с
    ним. Здесь только сужение до `Literal`, которого требуют промпты §5.1/
    §5.2 и `validate_doc_review`.
    """
    contour, _ = split_revision(session_id)
    return "spec" if contour == CONTOUR_SPEC else "pair"


def _doc_verifier(
    config: PipelineConfig,
    contour: str,
    workspace: Path,
    artifact_root: Path,
) -> Verifier:
    """Пять baseline-гейтов §6 по документам контура плюс добавленные конфигом.

    `allowed` — граница `doc-scope`, и она УЖЕ набора проверяемых документов:
    pair-контур читает спеку, но правит только план (§5.1), поэтому правка
    спеки автором пары обязана валить гейт, а не проходить как «документ же
    из моего контура».

    Патч читается тем же `load_patch`, которым его читает всё остальное
    runtime: `doc-scope` судит по `changes.patch` раунда, и второй читатель
    того же файла разошёлся бы с первым на пустом раунде ([REQ-013]).
    """
    documents = tuple(
        workspace / relative for relative in config.contour_documents(contour)
    )
    allowed = config.scope_paths(contour)
    return DocVerifier(
        doc_paths=documents,
        allowed=allowed,
        repo_root=workspace,
        patch_reader=lambda round_no: load_patch(artifact_root, round_no) or "",
        extra=config.extra_gates,
    )
