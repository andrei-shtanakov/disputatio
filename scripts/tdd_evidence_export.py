"""tdd_evidence_export — экспорт TDD-evidence в трекаемый артефакт репо.

Запускается spec-runner'ом из hook-точки `post_review` (spec-runner#307):
после вердикта ревью и успешных пре-терминальных гейтов, до DONE-флипа и
`commit_task_work`. Записанное здесь подметает `stage_all_except_runtime`, и
evidence уезжает в той же доставляемой истории, что и работа.

Standalone-скрипт (только stdlib): он вызывается из плагина как отдельный
процесс и не должен зависеть от продуктовых зависимостей репо.

Контракт полноты (решение владельца, 2026-08-21): полнота — это «все данные,
доступные ПЕРЕД DONE»: подтверждённый red, claims, достигнутые TDD-фазы
включая `refactoring`, вердикт ревью и вердикты пре-терминальных гейтов. Сам
DONE не требуется — в точке `post_review` он ещё не записан.

Неполные данные НЕ материализуются: экспортёр печатает недостающее поимённо в
stderr и завершается ненулевым кодом, а прежний корректный артефакт остаётся
нетронутым. В трекаемый файл попадает только `complete: true`.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

SCHEMA = "disputatio/tdd-evidence/v1"

#: Первый релиз spec-runner с hook-точкой `post_review` (#307). На момент
#: написания она в `[Unreleased]`; прогон-доказательство ждёт публикации.
MIN_SPEC_RUNNER = (2, 35, 0)

#: Гейты, регистрируемые под `execution_mode: tdd` (spec-runner
#: `gates.py:522-523`) плюс ревью (`gates.py:500`). Отсутствие любого —
#: неполная цепочка, а не «гейт не нужен».
REQUIRED_GATES = ("tdd.red", "tdd.claims", "review")

#: Таблицы и колонки, без которых экспорт невозможен. Проверяются до чтения:
#: дрейф схемы должен называться, а не всплывать как пустой результат.
REQUIRED_SCHEMA: dict[str, tuple[str, ...]] = {
    "red_checkpoints": (
        "task_id",
        "namespace",
        "commit_sha",
        "baseline_sha",
        "selector",
        "environment_id",
        "execution_mode",
        "config_hash",
        "outcome",
        "timestamp",
        "status",
    ),
    "tdd_claims": (
        "namespace",
        "task_id",
        "checkpoint_sha",
        "path",
        "blob_sha",
        "created_at",
        "status",
    ),
    "tdd_remedies": (
        "namespace",
        "task_id",
        "operation",
        "reason",
        "actor",
        "timestamp",
    ),
    "tdd_phases": ("task_id", "namespace", "phase", "detail", "timestamp"),
    "gate_verdicts": (
        "task_id",
        "gate_id",
        "checkpoint_sha",
        "config_hash",
        "status",
        "detail",
        "timestamp",
    ),
    "phase_waivers": (
        "task_id",
        "phase",
        "waived_outcome",
        "reason",
        "actor",
        "timestamp",
    ),
}


class Refusal(Exception):
    """Отказ экспорта: причина печатается в stderr, файл не трогается."""


def fail(message: str) -> NoReturn:
    """Поднять отказ с готовым текстом.

    `NoReturn` здесь не украшение: он сообщает типчекеру, что путь после
    вызова недостижим, иначе каждый отказ выглядит как «переменная может
    быть не инициализирована».
    """
    raise Refusal(message)


#: Ветка workstream'а в Mode 2 Maestro; из неё берётся идентичность evidence.
WS_BRANCH_PREFIX = "ws/"


def in_workstream_tree(project_root: Path) -> bool:
    """`True`, если это worktree Maestro (`spec/maestro-*tasks.md` в дереве).

    Сигнал берётся по факту наличия файла, а не по его содержимому — правило
    INV-16, унаследованное от удалённого локального гейта.
    """
    return any((project_root / "spec").glob("maestro-*tasks.md"))


#: Переменные расположения репозитория перебивают `cwd`: с экспортированным
#: `GIT_DIR` (обёртка git-хука, шелл разработчика, CI) команда отработает
#: успешно, но прочитает ЧУЖОЙ репозиторий — и evidence уедет в неймспейс
#: соседней ветки молча и с виду корректно. Тот же класс описан в
#: `tests/conftest.py`. Подпись и конфиг не трогаем: здесь только чтение.
GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
)

#: `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n` — конфиг прямо из окружения, по
#: приоритету выше локального `.git/config` и не отключаемый ни
#: `GIT_CONFIG_GLOBAL`, ни `GIT_CONFIG_NOSYSTEM`. Снимается счётчик: без него
#: git не читает пары (та же конвенция, что в `tests/conftest.py`). Измерено:
#: со сломанным счётчиком `symbolic-ref` падает целиком, и экспортёр прочитал
#: бы это как detached HEAD — отказ на ровном месте.
GIT_CONFIG_INJECTION_VAR = "GIT_CONFIG_COUNT"

#: Всё, что вызов git обязан не унаследовать.
GIT_STRIPPED_VARS = (*GIT_LOCATION_VARS, GIT_CONFIG_INJECTION_VAR)


def current_branch(project_root: Path) -> str | None:
    """Имя текущей ветки `project_root`; `None` ТОЛЬКО при detached HEAD.

    Обещание в первой строке буквальное: любой другой исход — отказ, включая
    успешный код с пустым выводом.

    «git не смог ответить» — не то же самое, что «HEAD отцеплён», и читать
    любой ненулевой код как второе значит превращать отказ инструмента в
    ложное утверждение о состоянии дерева: не-репозиторий, битый `.git` и
    отсутствующий бинарь докладывались бы как detached HEAD. Тот же принцип,
    по которому сосед выбрал `ls-tree` вместо `cat-file -e` (spec-runner
    `_refuse_pre_existing_file`), и та же fail-closed линия, что у остальных
    проверок экспортёра.

    `symbolic-ref -q` различает случаи сам, и это измерено: detached — код 1
    при пустом stderr; всё прочее — код 128 с текстом (`fatal: not a git
    repository`). Поэтому detached опознаётся по паре (код, пустой stderr),
    а не по одному коду.
    """
    env = {k: v for k, v in os.environ.items() if k not in GIT_STRIPPED_VARS}
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        fail(f"git не запустился в {project_root}: {exc}")
    if result.returncode == 0:
        branch = result.stdout.strip()
        if not branch:
            # Успех с пустым выводом — ответ, которого контракт git не
            # предусматривает. Прочитать его как detached HEAD значило бы
            # вернуть ту же подмену причины через другую дверь.
            fail(f"git вернул пустое имя ветки для {project_root} при коде 0")
        return branch
    detail = (result.stderr or result.stdout).strip()
    if result.returncode == 1 and not detail:
        return None
    fail(
        f"git не смог определить ветку {project_root} "
        f"(exit {result.returncode}): {detail[:200] or 'вывода нет'}"
    )


def resolve_export_namespace(project_root: Path, state_namespace: str) -> str:
    """Под каким именем evidence ложится в репо (INV-16), а не в БД.

    Неймспейс spec-runner'а — `sha256(путь worktree + spec_prefix)[:16]`, если
    `tdd_namespace` не объявлен, а объявить его в `project.yaml` нельзя: ключ
    один на весь конфиг, а неймспейс обязан быть свой у каждого workstream'а.
    Хеш изолирует верно, но идентичности не даёт: тот же workstream по другому
    пути получает другой каталог, и история трекаемой evidence разъезжается.
    Поэтому в worktree Maestro имя берётся из ветки `ws/<id>` → `ws-<id>`, а
    хеш остаётся fallback'ом для прогонов вне workstream'а (ручных, смоуковых).

    В maestro-дереве неожиданная ветка — отказ, а не молчаливый fallback
    (INV-18): прогон не имеет права незаметно уехать в другой неймспейс.
    Форма СТРОГО `ws/<id>` без внутренних `/`: иначе `ws/a/b` и `ws/a-b`
    схлопнулись бы в один каталог, поделив evidence двух разных workstream'ов.
    """
    if not in_workstream_tree(project_root):
        return state_namespace
    branch = current_branch(project_root)
    if branch is None:
        fail(
            "detached HEAD в maestro-дереве: неймспейс неоднозначен, "
            f"ожидается ветка {WS_BRANCH_PREFIX}<workstream-id>"
        )
    prefix = WS_BRANCH_PREFIX
    if not branch.startswith(prefix) or branch == prefix:
        fail(
            f"ветка {branch!r} в maestro-дереве не соответствует "
            f"{prefix}<workstream-id> — неймспейс неоднозначен"
        )
    if "/" in branch[len(prefix) :]:
        fail(
            f"ветка {branch!r} содержит лишний '/' после {prefix!r} — "
            "неймспейс схлопнулся бы с другой веткой при замене '/' на '-'"
        )
    return branch.replace("/", "-")


def resolve_state_db(project_root: Path, explicit: str | None) -> Path:
    """Найти живую `.executor-*state.db`; неоднозначность — отказ.

    Имя БД зависит от `spec_prefix`, поэтому ищем по маске и требуем ровно
    одну: две базы в одном `spec/` означают, что мы не знаем, какая относится
    к текущему прогону, и угадывать здесь нельзя.
    """
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            fail(f"state db не найдена: {path}")
        return path

    found = sorted((project_root / "spec").glob(".executor-*state.db"))
    if not found:
        fail(f"state db не найдена под {project_root / 'spec'}")
    if len(found) > 1:
        names = ", ".join(p.name for p in found)
        fail(f"неоднозначная state db: {names} — ожидалась ровно одна")
    return found[0]


def parse_version(raw: str) -> tuple[int, ...]:
    """`"spec-runner 2.35.0"` / `"2.35.0"` → `(2, 35, 0)`."""
    token = raw.strip().split()[-1]
    core = token.split("+")[0].split("-")[0]
    try:
        return tuple(int(part) for part in core.split("."))
    except ValueError:
        fail(f"не удалось разобрать версию spec-runner: {raw!r}")


def installed_version() -> str:
    """Версия из метаданных пакета; пустая строка — пакет не установлен."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("spec-runner")
    except PackageNotFoundError:
        return ""


def detect_spec_runner_version() -> str:
    """Версия установленного spec-runner; не определив — отказ."""
    from_metadata = installed_version()
    if from_metadata:
        return from_metadata
    try:
        out = subprocess.run(
            ["spec-runner", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        fail("версия spec-runner не определяется: пакет не найден и CLI недоступен")
    if out.returncode != 0 or not out.stdout.strip():
        fail("версия spec-runner не определяется: `spec-runner --version` не ответил")
    return out.stdout.strip()


def check_version(raw: str) -> None:
    """Отказать, если версия младше первой с `post_review`."""
    if parse_version(raw) < MIN_SPEC_RUNNER:
        wanted = ".".join(str(n) for n in MIN_SPEC_RUNNER)
        fail(
            f"spec-runner {raw} младше {wanted} — в нём нет hook-точки "
            "post_review (spec-runner#307), и экспорт не может быть доставлен "
            "вместе с работой"
        )


def check_schema(conn: sqlite3.Connection) -> None:
    """Проверить таблицы и колонки до чтения; дрейф — назвать поимённо."""
    present = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    problems: list[str] = []
    for table, columns in REQUIRED_SCHEMA.items():
        if table not in present:
            problems.append(f"нет таблицы {table}")
            continue
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = [c for c in columns if c not in cols]
        if missing:
            problems.append(f"{table}: нет колонок {', '.join(missing)}")
    if problems:
        fail("схема state db несовместима — " + "; ".join(problems))


def active_red(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    """Последний активный red-чекпоинт задачи."""
    return conn.execute(
        "SELECT * FROM red_checkpoints WHERE task_id = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def rows(
    conn: sqlite3.Connection, sql: str, args: tuple[str, ...]
) -> list[sqlite3.Row]:
    return list(conn.execute(sql, args))


def judged_commit(latest_gate: dict[str, sqlite3.Row]) -> str:
    """Дерево, которое судили требуемые гейты; разнобой — пустая строка.

    Вердикты обязаны относиться к одному candidate-коммиту: набор из двух
    деревьев — это не «цепочка с запасом», а два разных утверждения, склеенных
    в одно.
    """
    shas = {
        latest_gate[gate]["checkpoint_sha"]
        for gate in REQUIRED_GATES
        if gate in latest_gate
    }
    return shas.pop() if len(shas) == 1 else ""


def collect(conn: sqlite3.Connection, task_id: str, version: str) -> dict[str, Any]:
    """Собрать модель evidence; полнота проверяется отдельно."""
    red = active_red(conn, task_id)
    namespace = red["namespace"] if red else ""

    # Claims привязаны к red-коммиту (`claims.py:314` пишет
    # `checkpoint_sha=checkpoint.commit_sha`), поэтому линия проверяема:
    # после `repair` claims прежней линии не доказывают эту.
    claims = rows(
        conn,
        "SELECT * FROM tdd_claims WHERE task_id = ? AND namespace = ? "
        "AND checkpoint_sha = ? ORDER BY id",
        (task_id, namespace, red["commit_sha"] if red else ""),
    )
    remedies = rows(
        conn,
        "SELECT * FROM tdd_remedies WHERE task_id = ? AND namespace = ? ORDER BY id",
        (task_id, namespace),
    )
    phases = rows(
        conn,
        "SELECT * FROM tdd_phases WHERE task_id = ? AND namespace = ? ORDER BY id",
        (task_id, namespace),
    )
    # Последний вердикт на гейт, а не все: гейт переигрывается на ретрае, и
    # прежний отказ не отменяет более позднего согласия. Ключ вердикта —
    # (task, gate, checkpoint_sha, config_hash), поэтому и дерево, и политика
    # переносятся в артефакт и проверяются ниже.
    latest_gate: dict[str, sqlite3.Row] = {}
    for row in rows(
        conn, "SELECT * FROM gate_verdicts WHERE task_id = ? ORDER BY id", (task_id,)
    ):
        latest_gate[row["gate_id"]] = row
    waivers = rows(
        conn, "SELECT * FROM phase_waivers WHERE task_id = ? ORDER BY id", (task_id,)
    )

    return {
        "schema": SCHEMA,
        "task_id": task_id,
        "namespace": namespace,
        "red": None
        if red is None
        else {
            "commit_sha": red["commit_sha"],
            "baseline_sha": red["baseline_sha"],
            "selector": red["selector"],
            "environment_id": red["environment_id"],
            "execution_mode": red["execution_mode"],
            "config_hash": red["config_hash"],
            "outcome": red["outcome"],
            "status": red["status"],
            "timestamp": red["timestamp"],
        },
        "claims": [
            {
                "path": r["path"],
                "blob_sha": r["blob_sha"],
                "checkpoint_sha": r["checkpoint_sha"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in claims
        ],
        "remedies": [
            {
                "operation": r["operation"],
                "reason": r["reason"],
                "actor": r["actor"],
                "timestamp": r["timestamp"],
            }
            for r in remedies
        ],
        "phases": [
            {"phase": r["phase"], "detail": r["detail"], "timestamp": r["timestamp"]}
            for r in phases
        ],
        "gates": {
            gate: {
                "status": r["status"],
                "detail": r["detail"],
                "checkpoint_sha": r["checkpoint_sha"],
                "config_hash": r["config_hash"],
                "timestamp": r["timestamp"],
            }
            for gate, r in latest_gate.items()
        },
        "judged_commit": judged_commit(latest_gate),
        "waivers": [
            {
                "phase": r["phase"],
                "waived_outcome": r["waived_outcome"],
                "reason": r["reason"],
                "actor": r["actor"],
                "timestamp": r["timestamp"],
            }
            for r in waivers
        ],
        "source": {"spec_runner_version": version},
    }


def missing_parts(model: dict[str, Any]) -> list[str]:
    """Чего не хватает до полной цепочки, доступной перед DONE."""
    gaps: list[str] = []
    red = model["red"]
    if red is None:
        gaps.append("red-checkpoint")
    elif red["outcome"] != "expected_fail":
        gaps.append(f"red-checkpoint:не подтверждён (outcome={red['outcome']})")
    if not any(c["status"] == "active" for c in model["claims"]):
        gaps.append("claims:нет активного claim текущей линии red")
    reached = {p["phase"] for p in model["phases"]}
    for phase in ("red_verifying", "green_implementing", "refactoring"):
        if phase not in reached:
            gaps.append(f"phase:{phase}")
    policy = red["config_hash"] if red else None
    for gate in REQUIRED_GATES:
        verdict = model["gates"].get(gate)
        if verdict is None:
            gaps.append(f"gate-verdict:{gate}")
            continue
        if policy is not None and verdict["config_hash"] != policy:
            gaps.append(f"gate-verdict:{gate}:вынесен под другой политикой")
        if verdict["status"] != "satisfied":
            gaps.append(f"gate-verdict:{gate}:status={verdict['status']}")
    if not model["judged_commit"] and not [g for g in gaps if g.startswith("gate-")]:
        gaps.append("gate-verdicts:судят разные деревья")
    return gaps


def render(model: dict[str, Any]) -> str:
    """Канонический JSON: сортированные ключи, без времени экспорта.

    Идемпотентность здесь — свойство, а не совпадение: одинаковый вход даёт
    байт-в-байт одинаковый файл, поэтому повтор на ретрае не создаёт диффа.
    """
    return json.dumps(model, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_atomic(path: Path, text: str) -> None:
    """Запись temp + rename в том же каталоге."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def export(project_root: Path, task_id: str, version: str, db: str | None) -> Path:
    """Полный путь экспорта; при неполноте — `Refusal`."""
    check_version(version)
    state_db = resolve_state_db(project_root, db)
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        check_schema(conn)
        model = collect(conn, task_id, version)
    finally:
        conn.close()

    # Идентичность артефакта в репо и идентичность строк в БД — разные вещи:
    # первая читаема и стабильна (INV-16), вторая нужна, чтобы найти те самые
    # строки в живой БД или post-mortem архиве. Обе в артефакте.
    model["state_namespace"] = model["namespace"]
    model["namespace"] = resolve_export_namespace(project_root, model["namespace"])

    gaps = missing_parts(model)
    if gaps:
        fail(
            f"неполная цепочка для {task_id}: {', '.join(gaps)} — трекаемый "
            "артефакт не записан, прежний оставлен как есть"
        )

    model["complete"] = True
    out = project_root / "spec" / "evidence" / model["namespace"] / f"{task_id}.json"
    write_atomic(out, render(model))
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI-точка входа. `0` — экспорт записан, `1` — отказ."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        default=os.environ.get("SR_PROJECT_ROOT", "."),
        help="корень репо (по умолчанию SR_PROJECT_ROOT)",
    )
    parser.add_argument(
        "--task-id",
        default=os.environ.get("SR_TASK_ID", ""),
        help="идентификатор задачи (по умолчанию SR_TASK_ID)",
    )
    parser.add_argument(
        "--spec-runner-version",
        default="",
        help="версия spec-runner (по умолчанию определяется автоматически)",
    )
    parser.add_argument("--db", default=None, help="путь к state db (для тестов)")
    args = parser.parse_args(argv)

    try:
        if not args.task_id:
            fail("не задан task id: ни --task-id, ни SR_TASK_ID")
        version = args.spec_runner_version or detect_spec_runner_version()
        out = export(Path(args.project_root), args.task_id, version, args.db)
    except Refusal as refusal:
        print(f"tdd-evidence: {refusal}", file=sys.stderr)
        return 1
    print(f"tdd-evidence: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
