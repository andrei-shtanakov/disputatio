"""`disp` — командный вход оркестратора ([REQ-019], [DESIGN-019], ADR-007).

Один модуль на весь CLI, stdlib `argparse` и ни одной новой зависимости, а
`main(argv) -> int` делает вход обычной функцией — тест вызывает её
напрямую, не порождая процесса и не завися от того, установлен ли пакет.
Команд верхнего уровня три: `run` заводит сессию, `resume` продолжает
прерванную ([REQ-020], [DESIGN-020]), а группа `pipeline` несёт четыре
команды пайплайна полировки пары (SPEC-002 §3.1). Каждая выбирается
`set_defaults(handler=…)`, и каждая объявляет `--root` сама: глобальный флаг
заставлял бы пользователя помнить, что часть аргументов идёт до имени
команды, а часть после.

Пайплайн — отдельная группа, а не четыре имени в общем списке: предмет у
него другой (`--slug` против `session_id`), и плоский список заставлял бы
читателя `--help` угадывать, к какому объекту относится команда.

Единственное, что этот модуль решает сам, — **порядок** и **коды возврата**.
Ни одного правила сессии здесь нет: pre-flight принадлежит `runtime.git`,
сборка портов — composition root'у, стоп-условия — ядру. Порядок же
принадлежит именно CLI, и он обязателен:

1. **Все проверки старта — до первой записи.** `preflight(root)`
   ([DESIGN-010]), чтение профиля и сборка портов идут раньше
   `bootstrap_session`: отказ любой из них не вправе оставить `.disputatio/`
   в чужом репозитории ([REQ-010]), а единственный способ этого добиться —
   не создавать каталог, пока не прошла последняя из проверок. Грязное
   дерево тут не единственный отказ: адаптер, которого нет в реестре,
   отвергается `build_runtime`, и порядок «bootstrap раньше сборки» оставлял
   бы после опечатки в профиле каталог сессии, которой не было.
2. `session_id` печатается **до** первого записанного файла и первой строки
   журнала, но **после** всех проверок: сессия, которую пользователь не
   может назвать, не поддаётся ни `disp resume`, ни разбору инцидента — а
   названная, но не начавшаяся, посылает его искать несуществующее.
3. Цикл крутится `anyio.run`, а не `asyncio.run` — правило проекта и
   требование порта `AgentAdapter.run`.

`config.toml` сессии — это профиль запуска, в котором заменены ровно четыре
поля, принадлежащие ЭТОМУ запуску: `session.id`, `session.mode`,
`session.base_commit` и `task.prompt`. Формат профиля и снапшота один и тот
же ([DESIGN-014]), поэтому конфиг завершённой сессии годится как профиль
следующей, а читатель и писатель остаются в одном экземпляре. Всё остальное
— агенты, лимиты, гейты — приходит из профиля нетронутым: подкрути их CLI, и
снапшот перестал бы описывать сессию, которая по нему пошла.

Диагностика подчинена одному правилу (NFR-003): пользователь получает одну
строку `.args[0]` в stderr и код `2`, а полный traceback уходит событием
`error` в `events.jsonl` — не проглатывается и не показывается. Журнал молчит
ровно до тех пор, пока `.disputatio/` не создан: запись «ошибки старта» в
чужой репозиторий создала бы каталог сессии, которой не было ([REQ-010]).
"""

import argparse
import secrets
import sys
import traceback
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import anyio

from disputatio.contracts import (
    Event,
    EventSource,
    EventType,
    Mode,
    PipelinePhase,
    PipelineState,
    SessionPhase,
    SessionState,
    StateStore,
)
from disputatio.core import SessionFsm
from disputatio.events import (
    FilePipelineStateStore,
    FileStateStore,
    IntegrityAnchor,
    JsonlEventSink,
    bootstrap_session,
    write_config_snapshot,
)
from disputatio.runtime import (
    ConfigError,
    DisputatioError,
    GitCli,
    PipelineConfig,
    PipelineNotResumable,
    base_rev,
    build_runtime,
    load_config_file,
    load_pipeline_config,
    preflight,
)
from disputatio.runtime.composition import PipelineDeps, build_pipeline
from disputatio.runtime.layout import session_dir
from disputatio.runtime.loop import drive, resume_session
from disputatio.runtime.pipeline_config import load_session_profile
from disputatio.runtime.pipeline_export import export_pipeline
from disputatio.runtime.pipeline_resume import missing_manifest_message
from disputatio.runtime.steps import StepContext

EXIT_OK: Final = 0
"""Сессия дошла до `DONE` — в том числе эскалацией: она не сбой ([REQ-018])."""

EXIT_FAILED: Final = 1
"""Сессия записала `FAILED`: агент так и не вернул пригодный вывод."""

EXIT_ERROR: Final = 2
"""Сессия не началась: доменная ошибка запуска либо негодные аргументы."""

SESSION_ID_TIME_FORMAT: Final = "%Y%m%d-%H%M%S"
SESSION_ID_SUFFIX_BYTES: Final = 2
"""Два байта — четыре hex-символа суффикса `session_id` ([DESIGN-019])."""

DEFAULT_CONFIG_NAME: Final = "disputatio.toml"
"""Профиль запуска по умолчанию — рядом с рабочим репозиторием."""

SESSION_MODES: Final[tuple[Mode, ...]] = (Mode.DEVELOP, Mode.ANALYZE)
"""Режимы, которые заводит `disp run`, — не весь `Mode` (SPEC-002 §5.1).

`Mode.DOCUMENT` принадлежит пайплайну: его сессию собирает `build_pipeline`
и только он — с контуром, документами и doc-гейтами. `disp run` ничего этого
не передаёт, а отказаться от режима ПОЗЖЕ разбора аргументов уже поздно:
`steps.propose` делает `reset_hard` и `clean()` до сборки промпта, то есть
до fail-closed проверки контура, — и untracked-черновики пользователя, к
которым `preflight` терпим сознательно, до этой проверки не доживают.
Поэтому выбор сужен здесь, где ещё не прочитан ни один файл.
"""

_FIRST_ROUND: Final = 1
_HEAD_REVISION: Final = "HEAD"

_SubParsers = argparse._SubParsersAction
"""Тип контейнера подкоманд: у argparse публичного имени для него нет."""


def main(
    argv: Sequence[str] | None = None,
    *,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Точка входа `disp`: код возврата вместо исключения (ADR-007).

    Доменная ошибка печатается одной строкой в stderr и уходит кодом `2`:
    голого traceback пользователь не видит никогда (NFR-003). Недоменное
    исключение, наоборот, не глушится — это баг оркестратора, а не ошибка
    пользователя, и прятать его за тем же кодом значило бы выдать поломку за
    отказ во вводе.

    `now` инжектируется теми же соображениями, что и `RuntimeDeps.now`: часы
    сессии обязаны быть одни на весь запуск, а тест — детерминированным.

    Подкоманду выбирает не `if` по имени, а `handler`, положенный в разбор
    самим подпарсером: второе место, где перечислены команды, разошлось бы с
    первым молча — ровно тогда, когда команду добавят.
    """
    args = _build_parser().parse_args(argv)
    clock = now if now is not None else _utcnow
    journal = _ErrorJournal(
        root=Path(args.root), now=clock, enabled=getattr(args, "journal", True)
    )
    handler: Callable[..., int] = args.handler
    try:
        return handler(args, now=clock, journal=journal)
    except DisputatioError as exc:
        journal.record(exc)
        print(exc.args[0], file=sys.stderr)
        return EXIT_ERROR


def cmd_run(
    args: argparse.Namespace, *, now: Callable[[], datetime], journal: "_ErrorJournal"
) -> int:
    """Новая сессия: pre-flight → bootstrap → drive ([REQ-019], [DESIGN-019]).

    Порядок обязателен: pre-flight git ([DESIGN-010]) и сборка портов
    выполняются ДО `bootstrap_session`, поэтому ни грязное дерево, ни
    негодный профиль не оставляют `.disputatio/` ([REQ-010]). Сборка портов
    ничего не пишет — она лишь разрешает имена адаптеров в реестре, и
    единственный её отказ (`UnknownAdapterError`/`ConfigError`) относится к
    старту, а не к сессии.

    Начальное состояние сохраняется отдельным `store.save` до цикла: сессия,
    названная в stdout, обязана существовать в `session.json` независимо от
    того, дойдёт ли первый шаг до своего перехода. Событие `state_change`
    при этом не выдумывается — его эмитит первый же переход `IDLE →
    PROPOSING` внутри FSM, и второе, сочинённое здесь, разошлось бы с §8.
    """
    root = Path(args.root)
    preflight(root)

    profile = load_config_file(_config_path(args, root))
    started = now()
    config = replace(
        profile,
        session_id=new_session_id(started),
        mode=Mode(args.mode),
        # `base_commit` — цель сброса раунда 1, то есть `HEAD` на старте
        # сессии ([DESIGN-014]). Считается тем же `base_rev`, которым
        # пользуется шаг PROPOSING: собственный `rev-parse` дал бы второй
        # ответ на вопрос «что такое база сессии».
        base_commit=base_rev(root, _FIRST_ROUND, base_commit=_HEAD_REVISION),
        task_prompt=args.task,
    )
    deps = build_runtime(config, root, git=GitCli(root), now=now)
    # Имя сессии известно только теперь: всё, что упадёт дальше, журналируется
    # её именем, а не пустым — иначе событие `error` не привязать к сессии.
    journal.session = config.session_id

    # `flush` обязателен: за печатью идёт цикл, который живёт минутами, а в
    # конвейере stdout буферизуется — имя сессии дошло бы до пользователя
    # только вместе с её концом, то есть ровно тогда, когда оно уже не нужно.
    print(config.session_id, flush=True)

    bootstrap_session(root)
    write_config_snapshot(root, config.render_toml())

    state = config.to_session_state(created_at=started)
    deps.store.save(state)
    context = StepContext(
        deps=deps,
        fsm=SessionFsm(state, store=deps.store, sink=deps.sink, now=deps.now),
        base_commit=config.base_commit,
        gates=config.gates,
    )

    async def call() -> SessionState:
        """Тело запуска: цикл от `IDLE` до терминальной фазы."""
        return await drive(context)

    # Исход оборвавшейся сессии спрашивается у ДИСКА, а не у `context.fsm`:
    # начисление бюджета пересаживает цикл на новый `SessionFsm`
    # ([DESIGN-009]), и собранный здесь остался бы в фазе, из которой сессия
    # ушла, — то есть не назвал бы `FAILED`, уже записанный ядром.
    return _exit_code(
        _drive_to_terminal(
            call, outcome=lambda: _saved_state(deps.store, config.session_id)
        )
    )


def cmd_resume(
    args: argparse.Namespace, *, now: Callable[[], datetime], journal: "_ErrorJournal"
) -> int:
    """Продолжение прерванной сессии ([REQ-020], [DESIGN-020]).

    Своей логики восстановления здесь нет и быть не должно — она вся в
    `resume_session` ([REQ-014]): конфиг берётся из снапшота сессии, а не из
    профиля окружения, состояние — из `session.json`, дальше крутится тот же
    `drive`. CLI добавляет ровно то же, что и к `run`: `anyio.run` и перевод
    терминальной фазы в код возврата.

    Ни pre-flight, ни `bootstrap_session` тут не зовутся, и это не экономия:
    сессия уже начата, а дерево после обрыва законно содержит правки убитого
    автора — раунд сбрасывает их сам, перед тем как звать агента
    ([DESIGN-012]). Отказ «дерево грязное» на этом месте сделал бы
    невозобновляемой ровно ту сессию, ради которой подкоманда и заведена.

    `store` создаётся здесь и передаётся в `resume_session` override'ом,
    чтобы исход сессии, оборвавшей цикл исключением, читался тем же
    хранилищем, которым он записан. Второй экземпляр читал бы то же самое, но
    молчаливо разошёлся бы с первым, стоит хранилищу завести кэш.
    """
    root = Path(args.root)
    session_id: str = args.session_id
    journal.session = session_id
    store = FileStateStore(root)

    async def call() -> SessionState:
        """Тело продолжения: подготовка resume и тот же цикл, что у `run`."""
        return await resume_session(
            root, session_id, git=GitCli(root), store=store, now=now
        )

    return _exit_code(
        _drive_to_terminal(call, outcome=lambda: _saved_state(store, session_id))
    )


def cmd_pipeline_run(
    args: argparse.Namespace, *, now: Callable[[], datetime], journal: "_ErrorJournal"
) -> int:
    """`disp pipeline run` — новый пайплайн полировки пары (SPEC-002 §3.1).

    Своих предусловий CLI не заводит ни одного: чистое дерево, подходящая
    ветка, отсутствие каталога и расположение анкера проверяет
    `check_run_preconditions` внутри `PipelineRunner.run`, и второй их список
    здесь разошёлся бы с первым молча. Сборка портов, наоборот, идёт ДО
    `run` — по той же причине, что и у `disp run`: опечатка в имени адаптера
    не вправе оставить после себя каталог пайплайна. Держит это обещание
    `build_pipeline` (`_resolve_adapters`), а не порядок строк здесь: сами
    адаптеры живут внутри ревизии, и до неё пайплайн успевает создать анкер,
    манифест и сессию.

    Порядок «предусловия → анкер → снапшоты → манифест» принадлежит runner'у
    (§3.1), и CLI его не воспроизводит: он только переводит терминальное
    состояние манифеста в код возврата.
    """
    root = Path(args.root)
    deps = _pipeline_deps(args, root, now=now)
    return _pipeline_exit_code(deps.runner.run(args.slug, _task_text(args)))


def cmd_pipeline_resume(
    args: argparse.Namespace, *, now: Callable[[], datetime], journal: "_ErrorJournal"
) -> int:
    """`disp pipeline resume` — продолжение пайплайна с санкцией или без (§8.1).

    `--config` здесь так же обязателен по смыслу, как у `run`, и это не
    симметрия ради симметрии: §8.1 шаг 0 ищет журнал целостности по
    `anchor_root` из ЖИВОЙ конфигурации, потому что снапшот конфига лежит в
    каталоге пайплайна — то есть в дереве, доверять которому сверка и
    призвана запретить. Пайплайн с нестандартным `anchor_path`, возобновлённый
    без `--config`, смотрел бы в дефолтный журнал; отказывает за это сам
    `PipelineResume`, а CLI лишь доносит его текст.

    `--discard-round` и `--adopt-external` взаимоисключающи (argparse), и
    отсутствие обоих — законный вход: на чистом либо атрибутированном дереве
    решать нечего, а на неатрибутируемом откажет §8.1.
    """
    root = Path(args.root)
    deps = _pipeline_deps(args, root, now=now)
    return _pipeline_exit_code(deps.resume.resume(args.slug, decision=_decision(args)))


def cmd_pipeline_status(
    args: argparse.Namespace, *, now: Callable[[], datetime], journal: "_ErrorJournal"
) -> int:
    """`disp pipeline status` — снимок пайплайна, строго read-only (§3.1).

    Read-only не как обещание, а как форма: команда не собирает ни runner'а,
    ни портов и не выполняет НИ ОДНОЙ git-команды. Причина конкретна: `git
    status` обновляет stat-кэш в `.git/index`, то есть пишет — и «status
    ничего не изменил» перестало бы быть верным утверждением о диске.
    Отсюда же отсутствие проверки `anchor_path` (она требует `toplevel_prefix`,
    то есть `git rev-parse`): существование журнала команда показывает,
    а его расположение судит `run`/`resume`, которым это решать.

    `--config` нужен ровно за одним — за `anchor_root`: где лежит журнал
    целостности, знает только живая конфигурация (§8.1), и снимок без него
    молчал бы о единственном файле пайплайна вне рабочего дерева.

    Код возврата — `0` на любом успешно прочитанном манифесте, включая
    `FAILED`: §3.1 определяет коды для команд, которые пайплайн ДВИГАЮТ, а
    инспекция, отвечающая ненулём на исправно прочитанное состояние, ломала
    бы `disp pipeline status && …` на ровном месте.
    """
    root = Path(args.root)
    config = load_pipeline_config(_config_path(args, root))
    anchor = _pipeline_anchor(config, root, args.slug)
    print(render_status(_manifest(root, args.slug, anchor), anchor.path))
    return EXIT_OK


def cmd_pipeline_export(
    args: argparse.Namespace, *, now: Callable[[], datetime], journal: "_ErrorJournal"
) -> int:
    """`disp pipeline export` — пересобрать `result/` по текущему манифесту (§8.2).

    Манифест не двигается: экспорт идемпотентен по контракту (`manifest.json`
    — commit marker), и повтор чинит частичный набор тем же кодовым путём,
    каким писал его в первый раз. Пайплайн, дошедший до `DONE`, свой экспорт
    уже получил внутри цикла — эта команда нужна там, где набор испортили
    или потеряли.

    `--partial` пользователь называет сам, но называет им только СУЖЕНИЕ:
    честность манифеста (§8.2, P7) — это ЗНАЧЕНИЯ трёх полей, и `converged`
    экспортёр выводит из записанной фазы, а не из флага. Иначе забытый флаг
    на остановленном пайплайне выдавал бы частичный результат за полный —
    ровно там, где скрипт вокруг CLI ему поверит.

    Код возврата — тот же, что у `run`/`resume`: `0` только у сошедшегося
    `DONE`. §8.2 требует ненулевого кода от `ESCALATED` и `FAILED`, и
    команда, отвечающая нулём на пересборку частичного результата, дала бы
    `disp pipeline export && publish` опубликовать его как готовый. От
    `status` это отличается намеренно: тот инспектирует, а этот пишет
    `result/` и отвечает за исход того, что написал.
    """
    root = Path(args.root)
    config = load_pipeline_config(_config_path(args, root))
    anchor = _pipeline_anchor(config, root, args.slug)
    state = _manifest(root, args.slug, anchor)
    manifest = export_pipeline(
        state,
        workspace_root=root,
        remote_url=None,
        branch=GitCli(root).current_branch(),
        partial=args.partial,
    )
    print(manifest, flush=True)
    return _pipeline_exit_code(state)


def render_status(state: PipelineState, anchor_path: Path) -> str:
    """Снимок пайплайна одним текстовым блоком (§3.1).

    Формат плоский и построчный, а не JSON: это ответ человеку на вопрос
    «где пайплайн стоит», а машинно-читаемый источник у него уже есть —
    сам `pipeline.json`, и вторая его сериализация начала бы расходиться с
    первой.
    """
    budget = state.budget_used
    anchor_state = "есть" if anchor_path.is_file() else "нет"
    lines = [
        f"pipeline: {state.pipeline_id}",
        f"phase: {state.phase.value}",
        f"documents: {state.documents.spec_path} + {state.documents.plan_path}",
        f"budget: tokens={budget.tokens} wall={budget.wall_seconds:g}s",
        f"anchor: {anchor_path} ({anchor_state})",
        f"next_action: {_render_action(state)}",
        "sessions:",
    ]
    for label, records in (
        ("spec", state.spec_sessions),
        ("pair", state.pair_sessions),
    ):
        for record in records:
            outcome = "активна" if record.outcome is None else record.outcome.value
            superseded = (
                ""
                if record.superseded_by is None
                else f", перекрыта {record.superseded_by}"
            )
            lines.append(
                f"  {label} r{record.revision} {record.session_id}: "
                f"{outcome}{superseded}"
            )
    lines.append(f"transitions: {len(state.transitions)}")
    return "\n".join(lines)


def _render_action(state: PipelineState) -> str:
    """Незавершённый интент манифеста либо явное «нет» (§4.3)."""
    action = state.next_action
    if action is None:
        return "нет — пайплайн остановлен"
    return f"{action.kind} ({action.operation_id})"


def _pipeline_anchor(config: PipelineConfig, root: Path, slug: str) -> IntegrityAnchor:
    """Журнал целостности пайплайна; негодный слаг — доменная ошибка (§4.1).

    Слаг попадает прямо в путь, поэтому его грамматику проверяет тот, кто
    путь строит (`events.pipeline_paths.validate_slug`) — и отвечает голым
    `ValueError`. Здесь он переводится в иерархию [DESIGN-020]: опечатка в
    `--slug` это ошибка пользователя, и traceback за неё запрещён (NFR-003).
    Перевод стоит на КОНСТРУКЦИИ анкера, а не на чтении манифеста, потому что
    анкер строится первым — и хранилище с тем же негодным слагом до вызова
    уже не доходит.
    """
    try:
        return IntegrityAnchor(config.anchor_path, root, slug)
    except ValueError as exc:
        raise ConfigError(f"негодный слаг пайплайна: {exc}") from exc


def _manifest(root: Path, slug: str, anchor: IntegrityAnchor) -> PipelineState:
    """Манифест пайплайна; его отсутствие — инструкция человеку, не `KeyError`.

    Тот же текст, что у `resume` (§8.1): окно «каталог создан, манифеста
    нет» невосстановимо автоматически, и второй рассказ о нём разошёлся бы с
    первым ровно в перечне ручных шагов.
    """
    try:
        return FilePipelineStateStore(root).load(slug)
    except KeyError as exc:
        raise PipelineNotResumable(
            missing_manifest_message(root, slug, anchor)
        ) from exc


def _pipeline_deps(
    args: argparse.Namespace, root: Path, *, now: Callable[[], datetime]
) -> PipelineDeps:
    """Собирает пайплайн из живой конфигурации, названной `--config` (§3.1)."""
    path = _config_path(args, root)
    return build_pipeline(
        load_pipeline_config(path),
        load_session_profile(path),
        root,
        args.slug,
        git=GitCli(root),
        now=now,
    )


def _decision(
    args: argparse.Namespace,
) -> Literal["discard_round", "adopt_external"] | None:
    """Санкция оператора из взаимоисключающих флагов `resume` (§3.1)."""
    if args.discard_round:
        return "discard_round"
    if args.adopt_external:
        return "adopt_external"
    return None


def _task_text(args: argparse.Namespace) -> str:
    """Текст задачи: содержимое файла, если `--task` называет файл, иначе строка.

    Различие решается существованием файла, а не флагом: §3.1 объявляет
    `--task <файл|строка>` одним аргументом, и заставлять пользователя
    называть вид ввода значило бы завести флаг, которого спека не просит.
    Пустой `--task` — законный вход только как явная пустая строка; отсутствие
    аргумента argparse отвергает сам.
    """
    candidate = Path(args.task)
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    return args.task


def _pipeline_exit_code(state: PipelineState) -> int:
    """`0` только у сошедшегося `DONE` (§3.1).

    `DONE` один на два исхода: пайплайн доходит до него и после сходимости
    пары, и после эскалации — честный частичный результат тоже экспортируется
    (P7). Различает их история переходов: `ESCALATED` в ней означает, что
    результат неполон, а `converged: false` в `result/manifest.json` уже это
    признал. Отвечать нулём на такой прогон значило бы дать скрипту вокруг
    CLI считать эскалацию успехом.
    """
    escalated = any(
        transition.to is PipelinePhase.ESCALATED for transition in state.transitions
    )
    if state.phase is PipelinePhase.DONE and not escalated:
        return EXIT_OK
    return EXIT_FAILED


def new_session_id(moment: datetime) -> str:
    """`{UTC:%Y%m%d-%H%M%S}-{4 hex}` — имя сессии ([DESIGN-019]).

    Штамп переводится в UTC, а не печатается как есть: `session_id` попадает
    в имена артефактов и в journal, и две сессии, запущенные в разных зонах,
    иначе сортировались бы не по времени. Четыре hex-символа отделяют
    сессии, стартовавшие в одну секунду, — без них второй запуск переписал бы
    состояние первого.
    """
    stamp = moment.astimezone(UTC).strftime(SESSION_ID_TIME_FORMAT)
    return f"{stamp}-{secrets.token_hex(SESSION_ID_SUFFIX_BYTES)}"


def _build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов `disp`; подкоманда обязательна ([DESIGN-020]).

    `required=True` — не строгость ради строгости: без него пустой argv
    разбирается молча, и `main` ушёл бы не в usage, а в `AttributeError` на
    несуществующем `handler`. Неизвестное имя команды argparse отвергает сам,
    печатая usage в stderr и завершая процесс кодом `2`, — тем же, каким
    отвечает CLI на негодный ввод.
    """
    parser = argparse.ArgumentParser(
        prog="disp", description="Оркестратор author↔reviewer debate loop"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="запустить новую сессию")
    run.set_defaults(handler=cmd_run)
    run.add_argument("task", help="текст задачи для автора")
    run.add_argument(
        "--mode",
        choices=[mode.value for mode in SESSION_MODES],
        default=Mode.DEVELOP.value,
        help="режим задачи (по умолчанию develop)",
    )
    run.add_argument(
        "--config",
        default=None,
        help=f"профиль запуска (по умолчанию <root>/{DEFAULT_CONFIG_NAME})",
    )
    _add_root(run)

    resume = commands.add_parser("resume", help="продолжить прерванную сессию")
    resume.set_defaults(handler=cmd_resume)
    resume.add_argument("session_id", help="имя сессии, напечатанное `disp run`")
    _add_root(resume)

    _add_pipeline_commands(commands)
    return parser


def _add_pipeline_commands(commands: _SubParsers) -> None:
    """Четыре команды `disp pipeline` (SPEC-002 §3.1).

    Своя группа подкоманд, а не четыре имени верхнего уровня: пайплайн —
    другой объект, чем сессия (`--slug` против `session_id`), и общий
    плоский список заставлял бы читателя `--help` угадывать, у какой команды
    какой предмет.

    `--config` объявлен у ВСЕХ четырёх, включая `status` и `export`. Это не
    единообразие ради единообразия: живая конфигурация — единственный
    источник `anchor_root` (§8.1 шаг 0), а снапшот в каталоге пайплайна для
    этого негоден по построению — он лежит в дереве, доверять которому
    сверка и запрещает.

    Журнал ошибок §8 у пайплайновых команд выключен (`journal=False`): он
    пишет в `events.jsonl` СЕССИИ рабочего корня, а у пайплайна такой сессии
    нет — запись туда завела бы ленту, которую никто не читает, в чужом
    репозитории ([REQ-010]). Отказы этих команд уходят строкой в stderr, как
    и требует NFR-003.
    """
    pipeline = commands.add_parser(
        "pipeline", help="пайплайн полировки пары «спека + план» (SPEC-002)"
    )
    actions = pipeline.add_subparsers(dest="pipeline_command", required=True)

    run = actions.add_parser("run", help="запустить новый пайплайн")
    run.set_defaults(handler=cmd_pipeline_run)
    run.add_argument("--task", required=True, help="задача автору: файл либо строка")
    _add_pipeline_common(run)

    resume = actions.add_parser("resume", help="продолжить пайплайн")
    resume.set_defaults(handler=cmd_pipeline_resume)
    decision = resume.add_mutually_exclusive_group()
    decision.add_argument(
        "--discard-round",
        action="store_true",
        help="санкционировать сброс раунда; ручные правки будут потеряны",
    )
    decision.add_argument(
        "--adopt-external",
        action="store_true",
        help="принять правку как внешнюю и уйти в новую ревизию",
    )
    _add_pipeline_common(resume)

    status = actions.add_parser("status", help="снимок пайплайна (read-only)")
    status.set_defaults(handler=cmd_pipeline_status)
    _add_pipeline_common(status)

    export = actions.add_parser("export", help="пересобрать result/ по манифесту")
    export.set_defaults(handler=cmd_pipeline_export)
    export.add_argument(
        "--partial",
        action="store_true",
        help="объявить результат частичным (converged: false)",
    )
    _add_pipeline_common(export)


def _add_pipeline_common(command: argparse.ArgumentParser) -> None:
    """Общие аргументы всех четырёх команд §3.1: `--slug`, `--config`, `--root`."""
    command.set_defaults(journal=False)
    command.add_argument("--slug", required=True, help="имя пайплайна (§4.1)")
    command.add_argument(
        "--config",
        default=None,
        help=f"конфиг пайплайна (по умолчанию <root>/{DEFAULT_CONFIG_NAME})",
    )
    _add_root(command)


def _add_root(command: argparse.ArgumentParser) -> None:
    """Добавляет команде `--root`: у каждой свой, общего флага нет."""
    command.add_argument(
        "--root", default=".", help="рабочий git-репозиторий (по умолчанию текущий)"
    )


def _config_path(args: argparse.Namespace, root: Path) -> Path:
    """Путь профиля запуска: `--config`, иначе дефолт рядом с репозиторием."""
    if args.config is not None:
        return Path(args.config)
    return root / DEFAULT_CONFIG_NAME


def _drive_to_terminal(
    call: Callable[[], Awaitable[SessionState]],
    *,
    outcome: Callable[[], SessionState | None],
) -> SessionState:
    """Крутит цикл под `anyio.run`, отличая провал сессии от поломки CLI.

    Шаг, исчерпавший schema-повторы, поднимает ошибку последней попытки
    ([DESIGN-006]) — но `FAILED` к этому моменту уже записан в `session.json`
    ядром, то есть исход сессии определён, а исключение лишь называет его
    причину. Любое другое исключение — не исход, а сбой, и оно уходит выше:
    проглоти его CLI, и сломанный оркестратор отчитывался бы «сессия не
    сошлась» вместо падения.

    Исход спрашивается функцией, а не берётся из контекста: у `run` контекст
    собран до цикла, у `resume` он живёт внутри `resume_session`, и
    единственное, что у них общее, — состояние, записанное на диск. `None`
    означает «исхода нет вовсе» (сессия не найдена, цикл не начинался), и
    тогда исключение — единственная правда о запуске.
    """
    try:
        return anyio.run(call)
    except Exception:
        state = outcome()
        if state is not None and state.state is SessionPhase.FAILED:
            return state
        raise


def _saved_state(store: StateStore, session_id: str) -> SessionState | None:
    """Состояние сессии на диске; отсутствие — `None`, а не исключение.

    Спрашивается уже на пути обработки другой ошибки, и `KeyError` отсюда
    подменил бы собой причину, ради которой состояние и понадобилось.
    """
    try:
        return store.load(session_id)
    except KeyError:
        return None


def _exit_code(state: SessionState) -> int:
    """`0` для `DONE`, `1` для всего остального терминального ([REQ-019])."""
    return EXIT_OK if state.state is SessionPhase.DONE else EXIT_FAILED


@dataclass
class _ErrorJournal:
    """Приёмник traceback'ов доменных ошибок ([DESIGN-020], NFR-003).

    Пользователю уходит одна строка, полный traceback — событием `error` в
    `events.jsonl`: «не показывать» и «не сохранять» — разные обещания, и
    выполнено должно быть только первое.

    Имя сессии заполняется тем, кто его узнаёт: `run` — после генерации,
    `resume` — из argv. До этого журнал молчит по другой причине: пока
    `bootstrap_session` не создал `.disputatio/`, любая запись завела бы
    каталог сессии в чужом репозитории ([REQ-010]) — а отказы старта
    случаются именно там.

    `enabled=False` выключает журнал целиком, и выключен он у команд
    `disp pipeline` (SPEC-002 §3.1): их предмет — не сессия рабочего корня, а
    пайплайн, и запись отказа в ленту, которой у него нет, создала бы
    `events.jsonl` сессии, никогда не существовавшей. Пользователь при этом
    ничего не теряет — строка в stderr уходит в любом случае (NFR-003).
    """

    root: Path
    now: Callable[[], datetime]
    session: str = field(default="")
    enabled: bool = field(default=True)

    def record(self, exc: DisputatioError) -> None:
        """Дописывает traceback событием `error`, если сессия уже начата."""
        if not self.enabled or not session_dir(self.root).exists():
            return
        event = Event(
            ts=self.now(),
            session=self.session,
            source=EventSource.ORCHESTRATOR,
            type=EventType.ERROR,
            payload={
                "error": type(exc).__name__,
                "message": str(exc.args[0]),
                "traceback": "".join(traceback.format_exception(exc)),
            },
        )
        try:
            JsonlEventSink(self.root).emit(event)
        except OSError:
            # Журнал — не единственный получатель диагностики: строка в
            # stderr уйдёт в любом случае, и сорвать её сбоем записи значило
            # бы обменять внятный отказ на traceback, запрещённый NFR-003.
            return


def _utcnow() -> datetime:
    """Часы сессии по умолчанию: aware-UTC, как требует схема артефактов."""
    return datetime.now(UTC)
