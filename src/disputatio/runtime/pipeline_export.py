"""Экспорт готовой к публикации пары «спека + план» (SPEC-002 §8.2, P7).

`export_pipeline` — тот самый «порт», который runner (задача 15) получает
инъекцией как `exporter: ExportFn`; тип объявлен здесь же, рядом с функцией,
которая ему удовлетворяет. Задача идёт до runner'а намеренно (см. бриф): без
готового экспортёра runner либо импортировал бы несуществующий модуль, либо
заводил незапланированную заглушку под intent `export`.

Цель v1 — не «сам открывает draft-PR», а «выдаёт полностью готовую к
публикации пару одним draft-PR» (§8.2): публикация (`git push`, `gh pr
create`) — внешний эффект, который исполняет человек, а не эта функция.

Четыре свойства, вокруг которых построен модуль:

* **Идемпотентность, канонические байты.** Повтор без изменения `state`
  даёт байт-в-байт тот же `result/`: JSON пишется с сортированными ключами
  и без единого вызова часов — единственное время в байтах результата это
  метки самого `state` (`created_at`, `transitions[].at`), уже
  зафиксированные вызывающей стороной.
* **`manifest.json` — commit marker.** Пишется последним и перечисляет
  полный ожидаемый набор файлов `result/` с их sha256 (ключ `"files"`);
  обрыв между записью содержимого и манифеста оставляет набор без
  манифеста — валидным его считать нельзя, а повторный вызов чинит его же
  кодовым путём, каким написал в первый раз.
* **Старт экспорта убирает stale.** Любой файл в `result/`, не входящий в
  канонический набор (`pr_title.txt`, `pr_body.md`, `publish.txt`,
  `manifest.json`), удаляется до записи нового набора — обрубок прежнего
  partial/full экспорта не переживает повтор.
* **`publish.txt` не выдумывает команду.** Когда `remote_url`/`branch`
  переданы, идёт настоящая пара `git push`/`gh pr create --draft` с
  экранированием через `shlex.quote`; когда любой из них `None` —
  параметризованный шаблон с явным предупреждением вместо придуманного
  значения.

Манифест — честная сводка §4.2 плюс три вычисленных здесь ключа:
`converged` (= `not partial`, простое значение параметра, а не отдельная
ветка поведения), `escalation_reason`/`open_issues` (из последнего перехода
`state.transitions`, ПРИВЕДШЕГО в `ESCALATED`/`FAILED`, когда `partial=True`)
и `files` (sha256 трёх содержательных файлов). Ключевой набор манифеста один и тот же независимо
от `partial` — различаются только значения честности (P7).
"""

import hashlib
import json
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from disputatio.contracts import PipelinePhase, PipelineState
from disputatio.events import atomic_write
from disputatio.runtime.git import SESSION_DIR_NAME
from disputatio.runtime.pipeline_config import PIPELINES_DIR_NAME

#: Сигнатура порта экспорта — ровно то, что runner (задача 15) получит
#: инъекцией как `exporter`. Объявлен здесь, рядом с реализацией, которая
#: ему удовлетворяет: другого места, где рождается контракт, нет.
ExportFn = Callable[..., Path]

MANIFEST_NAME: Final = "manifest.json"
PR_TITLE_NAME: Final = "pr_title.txt"
PR_BODY_NAME: Final = "pr_body.md"
PUBLISH_NAME: Final = "publish.txt"

#: Полный ожидаемый набор файлов результата (§8.2) — используется и для
#: уборки stale-остатков на старте, и для перечня содержательных файлов,
#: которым `export_pipeline` считает sha256.
_CONTENT_FILE_NAMES: Final = (PR_TITLE_NAME, PR_BODY_NAME, PUBLISH_NAME)
_ALL_RESULT_NAMES: Final = (*_CONTENT_FILE_NAMES, MANIFEST_NAME)

#: Имя каталога экспорта внутри `pipelines/<slug>/` (§4.1, §8.2).
_RESULT_DIR_NAME: Final = "result"

#: Фазы, приход в которые и есть остановка пайплайна (§2): их переход несёт
#: причину, ради которой пишется честный частичный результат.
_STOPPED_PHASES: Final = (PipelinePhase.ESCALATED, PipelinePhase.FAILED)


def _result_dir(workspace_root: Path, pipeline_id: str) -> Path:
    """`pipelines/<pipeline_id>/result` (§4.1) — считается, не импортируется.

    `events.pipeline_paths` — внутренняя деталь раскладки `.disputatio/` и
    наружу пакетом `events` не экспортируется (см. докстринг
    `events/__init__.py`); `runtime` уже знает оба сегмента пути —
    `SESSION_DIR_NAME` (`runtime/git.py`) и `PIPELINES_DIR_NAME`
    (`runtime/pipeline_config.py`, тот же приём, что там применён к
    `SESSION_DIR_NAME`), и досчитывает путь сам, а не заново дублирует
    константу третьей копией.
    """
    return (
        workspace_root
        / SESSION_DIR_NAME
        / PIPELINES_DIR_NAME
        / pipeline_id
        / _RESULT_DIR_NAME
    )


def _result_dir_relative(pipeline_id: str) -> str:
    """`result/` относительно `workspace_root`, POSIX-строкой для `publish.txt`.

    `git push`/`gh pr create` из `publish.txt` обязаны выполняться из корня
    рабочего дерева (иначе `gh` не опознает репозиторий, а `git push` — не
    ту ветку), а `pr_title.txt`/`pr_body.md` лежат внутри `result/` — без
    этого префикса `--body-file pr_body.md` не находил бы файл, если человек
    запускает скрипт, как и остальные git-команды, из корня. Строка, а не
    `Path`: содержимое файла не должно зависеть от разделителя пути ОС,
    на которой собирался экспорт.
    """
    return f"{SESSION_DIR_NAME}/{PIPELINES_DIR_NAME}/{pipeline_id}/{_RESULT_DIR_NAME}"


def export_pipeline(
    state: PipelineState,
    *,
    workspace_root: Path,
    remote_url: str | None,
    branch: str | None,
    partial: bool = False,
) -> Path:
    """Пишет `result/{pr_title.txt,pr_body.md,publish.txt,manifest.json}`.

    Возвращает путь к `manifest.json` — commit marker набора. Экспорт
    начинается с удаления из `result/` файлов вне канонического набора
    (stale-остатки прежнего экспорта), затем пишет три содержательных файла
    и в конце — манифест: порядок обеспечивает, что обрыв между записями
    оставляет набор либо ещё не тронутым (манифеста прошлого экспорта нет
    смысла считать валидным без сверки хешей), либо без нового манифеста —
    но никогда не манифест, ссылающийся на ненаписанный файл.

    `partial` управляет только ЗНАЧЕНИЯМИ трёх полей честности манифеста
    (`converged`, `escalation_reason`, `open_issues`) — набор ключей и
    остальные три файла от него не зависят (P7: разный исход — разное
    содержимое одного и того же экспорта, не отдельный кодовый путь).
    """
    directory = _result_dir(workspace_root, state.pipeline_id)
    directory.mkdir(parents=True, exist_ok=True)
    _clear_stale(directory)

    result_relative = _result_dir_relative(state.pipeline_id)
    contents = {
        PR_TITLE_NAME: _pr_title(state, partial=partial),
        PR_BODY_NAME: _pr_body(state, partial=partial),
        PUBLISH_NAME: _publish_script(
            remote_url, branch, result_relative=result_relative
        ),
    }
    checksums: dict[str, str] = {}
    for name, text in contents.items():
        data = text.encode("utf-8")
        atomic_write(directory / name, data)
        checksums[name] = hashlib.sha256(data).hexdigest()

    manifest = _manifest(state, partial=partial, files=checksums)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    manifest_path = directory / MANIFEST_NAME
    atomic_write(manifest_path, payload)
    return manifest_path


def _clear_stale(directory: Path) -> None:
    """Удаляет из `directory` файлы вне канонического набора результата."""
    for entry in directory.iterdir():
        if entry.is_file() and entry.name not in _ALL_RESULT_NAMES:
            entry.unlink()


def _escalation_summary(
    state: PipelineState,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Причина эскалации и открытые находки — из перехода В `ESCALATED`/`FAILED`.

    Ищется последний переход, ПРИВЕДШИЙ в остановку, а не последний вообще, и
    разница здесь не теоретическая. Runner кладёт эскалацию двумя переходами
    одной атомарной записью (`ESCALATED`, следом `ESCALATED → EXPORTING`,
    `runtime/pipeline_runner.py::_escalate`): `ESCALATED` без немедленного
    интента экспорта был бы состоянием, из которого пайплайн сам не выходит.
    Поэтому «последний элемент» — это всегда `export_partial`, то есть ответ
    «почему пишется частичный результат» вместо «почему пайплайн
    остановился», а честность манифеста (P7) требует второго. Сквозной
    прогон эскалации показал это `escalation_reason: "export_partial"` в
    `result/manifest.json`; на скриптованном `state` одной задачи разойтись
    было негде — переход туда клали руками.

    Отсутствие такого перехода — `None` и пустой список, а не выдуманная
    причина: `--partial` вправе назвать человек (§3.1) и на пайплайне,
    который никуда не эскалировал.
    """
    for transition in reversed(state.transitions):
        if transition.to in _STOPPED_PHASES:
            issues = [
                evidence.model_dump(mode="json") for evidence in transition.evidence
            ]
            return transition.reason.value, issues
    return None, []


def _manifest(
    state: PipelineState, *, partial: bool, files: Mapping[str, str]
) -> dict[str, Any]:
    """Честная сводка §4.2 плюс три вычисленных здесь ключа.

    `state.model_dump` несёт полную историю манифеста пайплайна как есть —
    `created_at` и `transitions[].at` попадают в байты без изменений
    (§8.2: «метки фактов сохраняются»). Тег `schema` исключён: экспортный
    манифест — не документ семейства `disputatio/pipeline/v1`, а отдельная
    сводка поверх него, и унаследованный тег ввёл бы читателя в заблуждение
    о том, чей это артефакт.
    """
    escalation_reason, open_issues = (
        _escalation_summary(state) if partial else (None, [])
    )
    payload = state.model_dump(mode="json", exclude={"schema_"})
    return {
        **payload,
        "converged": not partial,
        "escalation_reason": escalation_reason,
        "open_issues": open_issues,
        "files": dict(files),
    }


def _pr_title(state: PipelineState, *, partial: bool) -> str:
    """Заголовок draft-PR: пара документов, с пометкой частичного исхода."""
    prefix = "[partial] " if partial else ""
    return f"{prefix}docs: {state.documents.spec_path} + {state.documents.plan_path}\n"


def _pr_body(state: PipelineState, *, partial: bool) -> str:
    """Тело draft-PR: история контуров, сессии, при партиале — эскалация."""
    lines = [
        f"# {state.pipeline_id}",
        "",
        f"Спека: `{state.documents.spec_path}`",
        f"План: `{state.documents.plan_path}`",
        "",
        "Итог: " + ("частичный результат (эскалация)" if partial else "сходимость"),
        "",
        "## Бюджет",
        f"- токены: {state.budget_used.tokens}",
        f"- время: {state.budget_used.wall_seconds:g}s",
        "",
        "## История контуров",
    ]
    for transition in state.transitions:
        lines.append(
            f"- {transition.from_.value} -> {transition.to.value} "
            f"({transition.reason.value}, {transition.at.isoformat()})"
        )

    lines += ["", "## Сессии"]
    for label, sessions in (
        ("spec", state.spec_sessions),
        ("pair", state.pair_sessions),
    ):
        for record in sessions:
            outcome = record.outcome.value if record.outcome is not None else "n/a"
            lines.append(
                f"- {label} r{record.revision} `{record.session_id}`: {outcome}"
            )

    if partial:
        reason, issues = _escalation_summary(state)
        lines += ["", "## Эскалация", f"Причина: {reason}", "", "### Открытые находки"]
        if issues:
            for issue in issues:
                lines.append(
                    f"- {issue['session_id']} round {issue['round']}: "
                    f"{issue['finding_id']}"
                )
        else:
            lines.append("- (нет)")

    return "\n".join(lines) + "\n"


def _publish_script(
    remote_url: str | None, branch: str | None, *, result_relative: str
) -> str:
    """`git push` + `gh pr create --draft`; шаблон, когда вход не определён.

    Значения приходят готовыми от вызывающей стороны (runner определяет их
    локально по репозиторию) — эта функция не гадает и не изобретает
    ничего сама: либо честная команда с обоими значениями, либо
    параметризованный шаблон с явным предупреждением. Обе команды рассчитаны
    на запуск из корня рабочего дерева (там же, где выполняются остальные
    git-команды сессии), поэтому `pr_title.txt`/`pr_body.md` адресуются путём
    от корня — `result_relative`, а не голым именем файла.
    """
    title_path = f"{result_relative}/{PR_TITLE_NAME}"
    body_path = f"{result_relative}/{PR_BODY_NAME}"
    if remote_url is None or branch is None:
        return (
            "# ВНИМАНИЕ: remote и/или ветка не определились однозначно "
            "локально — замените плейсхолдеры ниже перед выполнением, "
            "команда не подставлена автоматически. Запускать из корня "
            "рабочего дерева.\n"
            "git push <REMOTE> <BRANCH>\n"
            "gh pr create --draft --head <BRANCH> "
            f'--title "$(cat {shlex.quote(title_path)})" '
            f"--body-file {shlex.quote(body_path)}\n"
        )
    quoted_remote = shlex.quote(remote_url)
    quoted_branch = shlex.quote(branch)
    return (
        f"git push {quoted_remote} {quoted_branch}\n"
        f"gh pr create --draft --head {quoted_branch} "
        f'--title "$(cat {shlex.quote(title_path)})" '
        f"--body-file {shlex.quote(body_path)}\n"
    )
