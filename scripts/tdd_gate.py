"""tdd_gate — гейт проверяемого TDD для leaf-задач spec-runner.

Standalone-скрипт (только stdlib: он запускается из `test_command` до
установки продуктовых зависимостей и не должен от них зависеть). Агент
фиксирует red-чекпоинт командой `red`, независимая команда `verify`
переигрывает red SHA в отдельном git worktree и выносит типизированный
вердикт. Evidence хранится на диске в
`spec/.tdd-evidence/{claims,verdicts,waivers}`.

Категории вердикта:
    PASS            — тест проходит на текущем HEAD, red подтверждён replay'ем
    EXPECTED_FAIL   — тест падает assertion'ом, как и ожидалось для red
    UNEXPECTED_FAIL — тест падает не так, как ожидалось (не assertion или не
                      тот селектор)
    ERROR           — окружение сломано либо данные evidence неоднозначны
    WAIVED          — оператор одобрил исключение; WAIVED != PASS, ось H3
                      waived-задачей не закрывается

Контракт exit-кодов всего скрипта: `0` — OK/PASS/WAIVED, `1` — FAIL (нет
claim / red не подтверждён), `3` — ERROR (неоднозначность, чужой claim,
сломанное окружение). Код `2` не используется — pytest занял его под
collection error.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

EVIDENCE = Path("spec/.tdd-evidence")

# Команда запуска селектора в шаге 5 `cmd_red`. Вынесена в константу модуля,
# чтобы тесты могли подменить её через monkeypatch (tmp-репо фикстуры — не
# uv-проект, `uv run pytest` внутри него не работает).
PYTEST_CMD: tuple[str, ...] = ("uv", "run", "pytest", "-q")

_TASK_HEADING_RE = re.compile(r"^#{2,6}\s+([A-Z][A-Z0-9]*-\d+)\b")
# Кандидат в meta-строку: list-item (буллет `-`/`*` после отступа),
# содержащий `|`. Строки markdown-таблиц (начинаются с `|`, без буллета) —
# НЕ кандидаты, даже если несут `|` и слово статуса.
_META_CANDIDATE_RE = re.compile(r"^\s*[-*]\s.*\|")
# Чистый статус-токен: опциональный emoji-префикс СТРОГО из пятёрки
# spec-runner (🔄/🔍/⬜/✅/⏸️), сразу за ним слово статуса целиком, без
# хвоста. Отсекает и свободный текст («review нужен от ревьюера»), и
# произвольную пунктуацию-имитацию буллета («- REVIEW»).
_STATUS_TOKEN_RE = re.compile(
    r"^(?:(?:🔄|🔍|⬜|✅|⏸️)\s*)?(IN_PROGRESS|REVIEW)$", re.IGNORECASE
)
# Тот же whitelist эмодзи, но для статуса DONE (используется `cmd_audit`).
_DONE_STATUS_TOKEN_RE = re.compile(r"^(?:(?:🔄|🔍|⬜|✅|⏸️)\s*)?DONE$", re.IGNORECASE)

# Runner-owned пути: правки, которые spec-runner делает сам ДО старта агента
# (пометка задачи in_progress) или которые агент обязан вносить как часть
# claim'а. Не файлы реализации — но и не `tests/`.
_TASKS_MD_RE = re.compile(r"^spec/[^/]*tasks\.md$")
_TASK_HISTORY_RE = re.compile(r"^spec/\.[^/]*task-history\.log$")

CAT_PASS = "PASS"
CAT_EXPECTED_FAIL = "EXPECTED_FAIL"
CAT_UNEXPECTED_FAIL = "UNEXPECTED_FAIL"
CAT_ERROR = "ERROR"
CAT_WAIVED = "WAIVED"

CATEGORIES = {
    CAT_PASS: "тест проходит, вердикт подтверждён",
    CAT_EXPECTED_FAIL: "тест падает assertion'ом — ожидаемо для red-чекпоинта",
    CAT_UNEXPECTED_FAIL: "тест падает не так, как ожидалось",
    CAT_ERROR: "окружение сломано либо данные evidence неоднозначны",
    CAT_WAIVED: "оператор одобрил waiver — задача закрыта без PASS",
}

_CLAIM_SCHEMA = "tdd-claim/v1"
_VERDICT_SCHEMA = "tdd-verdict/v1"
_WAIVER_SCHEMA = "tdd-waiver/v1"

# Максимальная длина `notes` в verdict-файле — хвост вывода pytest, а не
# весь лог; ограничение защищает evidence от разрастания.
_NOTES_MAX_LEN = 4000


class GateError(Exception):
    """Ошибка гейта: неоднозначность данных, чужой claim, сломанное окружение.

    Соответствует exit-коду `3` в контракте скрипта, если не переопределён
    явно вызывающим кодом.
    """

    def __init__(self, message: str, exit_code: int = 3) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _check_schema(data: dict[str, object], expected: str, kind: str) -> None:
    """Проверяет поле `schema` в evidence-словаре; несовпадение → GateError."""
    schema = data.get("schema")
    if schema != expected:
        raise GateError(f"{kind}: неверная схема {schema!r}, ожидается {expected!r}")


@dataclass(frozen=True)
class Claim:
    """Заявка агента: тест написан и подтверждённо падает (red-чекпоинт)."""

    task_id: str
    selector: str
    expected_behavior: str
    baseline_sha: str
    red_sha: str
    created_at: str
    revision: int

    def to_json(self) -> dict[str, object]:
        """Сериализует claim в JSON-совместимый dict со схемой."""
        return {
            "schema": _CLAIM_SCHEMA,
            "task_id": self.task_id,
            "selector": self.selector,
            "expected_behavior": self.expected_behavior,
            "baseline_sha": self.baseline_sha,
            "red_sha": self.red_sha,
            "created_at": self.created_at,
            "revision": self.revision,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "Claim":
        """Восстанавливает claim из dict; несовпадение `schema` → GateError."""
        _check_schema(data, _CLAIM_SCHEMA, "claim")
        return cls(
            task_id=str(data["task_id"]),
            selector=str(data["selector"]),
            expected_behavior=str(data["expected_behavior"]),
            baseline_sha=str(data["baseline_sha"]),
            red_sha=str(data["red_sha"]),
            created_at=str(data["created_at"]),
            revision=int(data["revision"]),  # type: ignore[call-overload]
        )


@dataclass(frozen=True)
class Verdict:
    """Итог независимой перепроверки claim (replay red SHA + текущий тест)."""

    task_id: str
    claim_revision: int
    red_sha: str
    verified_head: str
    red_replay: str
    selector_at_head: str
    verdict: str
    checked_at: str
    notes: str

    def to_json(self) -> dict[str, object]:
        """Сериализует verdict в JSON-совместимый dict со схемой."""
        return {
            "schema": _VERDICT_SCHEMA,
            "task_id": self.task_id,
            "claim_revision": self.claim_revision,
            "red_sha": self.red_sha,
            "verified_head": self.verified_head,
            "red_replay": self.red_replay,
            "selector_at_head": self.selector_at_head,
            "verdict": self.verdict,
            "checked_at": self.checked_at,
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "Verdict":
        """Восстанавливает verdict из dict; несовпадение `schema` → GateError."""
        _check_schema(data, _VERDICT_SCHEMA, "verdict")
        return cls(
            task_id=str(data["task_id"]),
            claim_revision=int(data["claim_revision"]),  # type: ignore[call-overload]
            red_sha=str(data["red_sha"]),
            verified_head=str(data["verified_head"]),
            red_replay=str(data["red_replay"]),
            selector_at_head=str(data["selector_at_head"]),
            verdict=str(data["verdict"]),
            checked_at=str(data["checked_at"]),
            notes=str(data["notes"]),
        )


@dataclass(frozen=True)
class Waiver:
    """Операторское исключение: задача закрывается без PASS-вердикта."""

    task_id: str
    reason: str
    approved_by: str
    baseline_sha: str

    def to_json(self) -> dict[str, object]:
        """Сериализует waiver в JSON-совместимый dict со схемой."""
        return {
            "schema": _WAIVER_SCHEMA,
            "task_id": self.task_id,
            "reason": self.reason,
            "approved_by": self.approved_by,
            "baseline_sha": self.baseline_sha,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "Waiver":
        """Восстанавливает waiver из dict; несовпадение `schema` → GateError."""
        _check_schema(data, _WAIVER_SCHEMA, "waiver")
        return cls(
            task_id=str(data["task_id"]),
            reason=str(data["reason"]),
            approved_by=str(data["approved_by"]),
            baseline_sha=str(data["baseline_sha"]),
        )


def write_json_atomic(path: Path, obj: object) -> None:
    """Атомарно пишет `obj` как JSON в `path`.

    Пишет во временный файл в том же каталоге (`path.with_suffix(".tmp")`) и
    переименовывает его в `path` через `os.replace` — читатель никогда не
    видит частично записанный файл.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp_path, path)


def _read_evidence_json(
    root: Path, subdir: str, task_id: str
) -> dict[str, object] | None:
    """Читает JSON evidence-файла `root/EVIDENCE/subdir/task_id.json`.

    Возвращает `None`, если файла нет; битый JSON или не-объект в корне →
    `GateError`.
    """
    path = root / EVIDENCE / subdir / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise GateError(f"{path}: битый JSON evidence ({exc})") from exc
    if not isinstance(data, dict):
        raise GateError(f"{path}: ожидался JSON-объект, получено {type(data)}")
    return data


def load_claim(root: Path, task_id: str) -> Claim | None:
    """Читает claim задачи `task_id`; `None`, если файла нет."""
    data = _read_evidence_json(root, "claims", task_id)
    if data is None:
        return None
    return Claim.from_json(data)


def load_verdict(root: Path, task_id: str) -> Verdict | None:
    """Читает verdict задачи `task_id`; `None`, если файла нет."""
    data = _read_evidence_json(root, "verdicts", task_id)
    if data is None:
        return None
    return Verdict.from_json(data)


def load_waiver(root: Path, task_id: str) -> Waiver | None:
    """Читает waiver задачи `task_id`; `None`, если файла нет."""
    data = _read_evidence_json(root, "waivers", task_id)
    if data is None:
        return None
    return Waiver.from_json(data)


def _meta_line_status(line: str) -> str | None:
    """Извлекает статус из meta-строки вида `- Приоритет: P1 | 🔄 IN_PROGRESS`.

    Строка разбивается на сегменты по `|`; нулевой сегмент (до первого `|`)
    не рассматривается, а каждый из оставшихся проверяется на точное
    совпадение с чистым статус-токеном (`_STATUS_TOKEN_RE`) — сегмент с
    посторонними словами вокруг («review нужен от ревьюера») не статус,
    даже если слово `review` в нём встречается. Проверяются ВСЕ сегменты
    после первого, а не только последний: в реальных meta-строках после
    статуса могут идти ещё сегменты (например, исполнитель).
    """
    segments = line.split("|")[1:]
    for segment in segments:
        match = _STATUS_TOKEN_RE.match(segment.strip())
        if match is not None:
            return match.group(1).upper()
    return None


def _first_meta_line_per_task(text: str) -> dict[str, str]:
    """Возвращает {task_id: первая meta-строка секции} по всем задачам текста.

    Общий позиционный проход для `_running_task_ids` и `_done_task_ids`:
    заголовок задачи (`### TASK-NNN: ...`, уровень #### тоже допустим)
    начинает секцию задачи. Внутри секции meta-строка — ПЕРВАЯ
    строка-кандидат: list-item'а (буллет `-`/`*`) с `|` внутри
    (`_META_CANDIDATE_RE`), это и есть позиция реальной meta-строки
    spec-runner. Все прочие строки секции (проза, markdown-таблицы, вторые
    и далее буллеты с `|`) полностью игнорируются — иначе таблица-описание
    вида `| Модуль A | REVIEW |` или посторонний буллет ниже meta-строки
    могли бы ложно засчитаться. Интерпретация статуса (running/done) —
    забота вызывающих функций, здесь только структура.
    """
    result: dict[str, str] = {}
    current_task_id: str | None = None
    meta_line_seen = False
    for line in text.splitlines():
        heading = _TASK_HEADING_RE.match(line)
        if heading is not None:
            current_task_id = heading.group(1)
            meta_line_seen = False
            continue
        if current_task_id is None or meta_line_seen:
            continue
        if _META_CANDIDATE_RE.match(line) is None:
            continue
        meta_line_seen = True
        result[current_task_id] = line
    return result


def _running_task_ids(text: str) -> list[str]:
    """Возвращает ID задач со статусом IN_PROGRESS/REVIEW в тексте `text`."""
    return [
        task_id
        for task_id, line in _first_meta_line_per_task(text).items()
        if _meta_line_status(line) is not None
    ]


def _meta_line_done(line: str) -> bool:
    """`True`, если meta-строка `line` несёт чистый статус-токен DONE.

    Аналог `_meta_line_status` (см. его докстринг про сегменты), но для
    whitelist'а `_DONE_STATUS_TOKEN_RE` (для `cmd_audit`).
    """
    segments = line.split("|")[1:]
    return any(
        _DONE_STATUS_TOKEN_RE.match(segment.strip()) is not None for segment in segments
    )


def _done_task_ids(text: str) -> list[str]:
    """Возвращает ID задач со статусом DONE в тексте `text` (для `cmd_audit`)."""
    return [
        task_id
        for task_id, line in _first_meta_line_per_task(text).items()
        if _meta_line_done(line)
    ]


def resolve_current_task(root: Path) -> str:
    """Определяет ID единственной текущей задачи по всем `root/spec/*tasks.md`.

    Задача «текущая», если её meta-строка содержит `IN_PROGRESS` или
    `REVIEW` (эмодзи 🔄/🔍 или plain-текст). Ровно одна такая задача по всем
    файлам → её ID; ноль или больше одной → `GateError`.
    """
    running: list[str] = []
    for path in sorted((root / "spec").glob("*tasks.md")):
        running.extend(_running_task_ids(path.read_text()))
    if not running:
        raise GateError("нет задачи со статусом IN_PROGRESS/REVIEW в spec/*tasks.md")
    if len(running) > 1:
        raise GateError(f"больше одной текущей задачи: {', '.join(running)}")
    return running[0]


def _git_stdout(root: Path, *args: str) -> str:
    """Запускает `git *args` в `root`, возвращает stdout как есть (без strip).

    Нужен отдельно от `git()`: в `--porcelain -z`-выводе `git status`
    ведущий пробел первой записи значим (разделяет index/worktree статус) —
    его нельзя терять к общему `.strip()`.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def git(root: Path, *args: str) -> str:
    """Запускает `git *args` в `root`, возвращает stdout без хвостовых пробелов."""
    return _git_stdout(root, *args).strip()


def head_sha(root: Path) -> str:
    """Возвращает SHA текущего HEAD репозитория `root`."""
    return git(root, "rev-parse", "HEAD")


def changed_paths(root: Path) -> list[str]:
    """Возвращает объединение staged/unstaged/untracked путей репозитория.

    Парсит `git status --porcelain -z`: каждая запись — `XY PATH\\0`, а для
    rename/copy (`X` или `Y` равен `R`/`C`) добавляется вторая NUL-секция с
    исходным путём — в результат попадают обе стороны. `--untracked-files=all`
    обязателен: без него `git status` схлопывает полностью неотслеживаемую
    директорию (например, ранее пустой `spec/`) в одну запись `spec/` вместо
    перечисления файлов внутри — такую запись `classify_changes` не может
    сопоставить ни с одним правилом.
    """
    raw = _git_stdout(root, "status", "--porcelain", "-z", "--untracked-files=all")
    tokens = [t for t in raw.split("\x00") if t]
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        xy, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in xy or "C" in xy:
            i += 1
            paths.append(tokens[i])
        i += 1
    return paths


def _is_allowed_path(path: str, task_id: str) -> bool:
    """Решает, разрешён ли путь `path` в рамках задачи `task_id`.

    Разрешены: всё под `tests/`, claims-файл текущей задачи, и правки
    spec-runner'а (`spec/*tasks.md`, `spec/.task-history.log` и вариации
    с иным префиксом после точки).
    """
    if path.startswith("tests/"):
        return True
    if path == f"spec/.tdd-evidence/claims/{task_id}.json":
        return True
    if _TASKS_MD_RE.match(path) is not None:
        return True
    return _TASK_HISTORY_RE.match(path) is not None


def classify_changes(paths: list[str], task_id: str) -> tuple[list[str], list[str]]:
    """Делит `paths` на (allowed, forbidden) относительно задачи `task_id`."""
    allowed: list[str] = []
    forbidden: list[str] = []
    for path in paths:
        (allowed if _is_allowed_path(path, task_id) else forbidden).append(path)
    return allowed, forbidden


def commit_red(root: Path, task_id: str, baseline: str, selector: str) -> str:
    """Коммитит red-чекпоинт: ТОЛЬКО `tests/`, с трейлерами claim'а.

    Пишет исключительно через pathspec `tests/` (`add` + `commit` с явным
    `-- tests/`) — никакого `-A`, посторонние staged-изменения (например,
    `spec/tasks.md`) в коммит не попадают. Возвращает SHA нового коммита.
    """
    git(root, "add", "--", "tests/")
    message = (
        f"tdd-gate: red checkpoint {task_id}\n\n"
        f"TDD-Red-Task: {task_id}\n"
        f"TDD-Baseline: {baseline}\n"
        f"TDD-Selector: {selector}\n"
    )
    git(root, "commit", "-m", message, "--", "tests/")
    return head_sha(root)


def find_red_commit_by_trailer(root: Path, task_id: str) -> str | None:
    """Recovery: ищет последний коммит с трейлером `TDD-Red-Task: task_id`.

    `None`, если такого коммита нет. `git log` по умолчанию отдаёт коммиты
    от новых к старым, поэтому первое совпадение — самое свежее.
    """
    output = git(root, "log", "--format=%H%x00%(trailers:key=TDD-Red-Task,valueonly)")
    if not output:
        return None
    for line in output.split("\n"):
        sha, _, value = line.partition("\x00")
        if value.strip() == task_id:
            return sha
    return None


def _claim_path(root: Path, task_id: str) -> Path:
    """Путь к claim-файлу задачи `task_id`."""
    return root / EVIDENCE / "claims" / f"{task_id}.json"


def _commit_exists(root: Path, sha: str) -> bool:
    """Проверяет, что `sha` — коммит, присутствующий в истории `root`."""
    try:
        git(root, "cat-file", "-e", f"{sha}^{{commit}}")
    except subprocess.CalledProcessError:
        return False
    return True


def _trailer_value(root: Path, sha: str, key: str) -> str:
    """Возвращает значение трейлера `key` коммита `sha` (пусто, если нет)."""
    return git(root, "show", "-s", f"--format=%(trailers:key={key},valueonly)", sha)


def _is_claim_resolved(root: Path, task_id: str) -> bool:
    """`True`, если claim задачи закрыт PASS/WAIVED-вердиктом.

    Закрытый claim не «pending» — supersession новым red поверх него
    запрещена в v1 (см. `_cmd_red`, шаг 7).
    """
    verdict = load_verdict(root, task_id)
    return verdict is not None and verdict.verdict in (CAT_PASS, CAT_WAIVED)


def _foreign_pending_claim(root: Path, task_id: str) -> str | None:
    """ID чужой задачи с pending claim'ом (без PASS/WAIVED), если есть."""
    claims_dir = root / EVIDENCE / "claims"
    if not claims_dir.exists():
        return None
    for path in sorted(claims_dir.glob("*.json")):
        other_id = path.stem
        if other_id == task_id:
            continue
        if load_claim(root, other_id) is not None and not _is_claim_resolved(
            root, other_id
        ):
            return other_id
    return None


def _recover_claim_from_commit(
    root: Path, task_id: str, red_sha: str, expected_behavior: str
) -> Claim:
    """Восстанавливает и записывает claim из трейлеров red-коммита `red_sha`.

    Шаг 8 (recovery-ветка): предыдущий запуск `red` создал коммит, но упал
    до записи claim'а. Baseline и selector берутся из трейлеров самого
    коммита (источник истины), а не из аргументов текущего вызова.
    """
    baseline = _trailer_value(root, red_sha, "TDD-Baseline")
    selector = _trailer_value(root, red_sha, "TDD-Selector")
    created_at = git(root, "show", "-s", "--format=%cI", red_sha)
    claim = Claim(
        task_id=task_id,
        selector=selector,
        expected_behavior=expected_behavior,
        baseline_sha=baseline,
        red_sha=red_sha,
        created_at=created_at,
        revision=1,
    )
    write_json_atomic(_claim_path(root, task_id), claim.to_json())
    return claim


def _run_selector(root: Path, selector: str) -> tuple[str, str]:
    """Запускает `PYTEST_CMD selector` в `root`, классифицирует результат.

    `"expected_fail"` — exit 1, `AssertionError` в выводе, падает именно
    селектор; `"green"` — exit 0, тест ничего не доказывает; `"error"` —
    всё прочее (collection/import/окружение). Возвращает пару
    (категория, объединённый stdout+stderr).
    """
    result = subprocess.run(
        [*PYTEST_CMD, selector],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    if result.returncode == 0:
        return "green", output
    if result.returncode == 1 and "AssertionError" in output and selector in output:
        return "expected_fail", output
    return "error", output


def cmd_red(root: Path, selector: str, expected_behavior: str) -> int:
    """Команда `red`: фиксирует red-чекпоинт для текущей задачи.

    Реализует 8-шаговую логику из плана: резолвит текущую задачу,
    учитывает свой и чужие pending claim'ы (идемпотентность и recovery),
    запрещает продуктовые правки до red, запускает селектор и коммитит
    только `tests/` с claim'ом. Возвращает код по контракту скрипта: `0` —
    OK (в т.ч. идемпотентный повтор), `1` — FAIL, `3` — ERROR. `GateError`
    из внутренней логики перехватывается здесь же и превращается в код
    возврата, чтобы вызывающий код (включая будущий CLI) работал с чистым
    `int`, не заботясь об исключениях.
    """
    try:
        return _cmd_red(root, selector, expected_behavior)
    except GateError as exc:
        print(f"red: {exc}", file=sys.stderr)
        return exc.exit_code


def _cmd_red(root: Path, selector: str, expected_behavior: str) -> int:
    """Логика `cmd_red`; `GateError` пробрасывается наружу (перехват — выше)."""
    task_id = resolve_current_task(root)  # шаг 1

    own_claim = load_claim(root, task_id)
    if own_claim is None:
        # Шаг 8 (recovery): claim не записан, но red-коммит уже есть —
        # предыдущий запуск упал между commit_red и записью claim'а.
        recovered_sha = find_red_commit_by_trailer(root, task_id)
        if recovered_sha is not None:
            _recover_claim_from_commit(root, task_id, recovered_sha, expected_behavior)
            return 0
    elif not _is_claim_resolved(root, task_id):
        # Шаг 2: существующий pending claim этой задачи.
        if _commit_exists(root, own_claim.red_sha):
            return 0  # идемпотентный повтор — второй red-коммит не создаётся
        recovered_sha = find_red_commit_by_trailer(root, task_id)
        if recovered_sha is not None:
            # sha в claim устарел (например, история переписана), но коммит
            # с тем же трейлером найден — чиним claim свежими данными из
            # него, а не молча репортим успех с битым файлом на диске.
            _recover_claim_from_commit(root, task_id, recovered_sha, expected_behavior)
            return 0
        raise GateError(
            f"{task_id}: claim ссылается на red-коммит {own_claim.red_sha}, "
            "которого нет в истории, и по трейлеру ничего не найдено — "
            "recovery невозможен"
        )
    else:
        # own_claim закрыт PASS/WAIVED — supersession новым red запрещена в
        # v1. Проверяем ЗДЕСЬ, ДО forbidden-check/селектора/коммита — иначе
        # каждый повторный вызов `red` после PASS создавал бы новый
        # осиротевший red-коммит и только потом падал на записи claim'а.
        raise GateError(
            f"{task_id}: supersession запрещена — claim уже закрыт вердиктом"
        )

    # Шаг 3: чужой pending claim блокирует запуск.
    foreign = _foreign_pending_claim(root, task_id)
    if foreign is not None:
        raise GateError(f"чужой pending claim блокирует red: {foreign}")

    # Шаг 4: продуктовый код менять до red нельзя.
    _, forbidden = classify_changes(changed_paths(root), task_id)
    if forbidden:
        print(
            "red: запрещённые правки до red-чекпоинта: " + ", ".join(forbidden),
            file=sys.stderr,
        )
        return 1

    # Шаг 5: запуск селектора.
    baseline = head_sha(root)
    category, output = _run_selector(root, selector)
    if category == "green":
        print(
            f"red: {selector} не падает — он ничего не доказывает",
            file=sys.stderr,
        )
        return 1
    if category == "error":
        raise GateError(
            f"{selector}: селектор упал не так, как ожидалось "
            f"(ни зелёный, ни AssertionError)\n{output}"
        )

    # Шаг 6: red-коммит.
    red_sha = commit_red(root, task_id, baseline, selector)

    # Шаг 7: запись claim. Supersession (existing PASS/WAIVED) отсечена выше
    # (см. шаг 2/else) — сюда доходим только когда own_claim is None.
    claim = Claim(
        task_id=task_id,
        selector=selector,
        expected_behavior=expected_behavior,
        baseline_sha=baseline,
        red_sha=red_sha,
        created_at=datetime.now(UTC).isoformat(),
        revision=1,
    )
    write_json_atomic(_claim_path(root, task_id), claim.to_json())
    return 0


def _verdict_path(root: Path, task_id: str) -> Path:
    """Путь к verdict-файлу задачи `task_id`."""
    return root / EVIDENCE / "verdicts" / f"{task_id}.json"


def _history_path(root: Path, task_id: str) -> Path:
    """Путь к append-only history-файлу verdict'ов задачи `task_id`."""
    return root / EVIDENCE / "verdicts" / f"{task_id}.history.jsonl"


def _append_verdict_history(root: Path, task_id: str, verdict: Verdict) -> None:
    """Дописывает `verdict` строкой JSON в history ДО перезаписи основного файла.

    Реверификация (HEAD сдвинулся после PASS) перезаписывает
    `verdicts/<TASK>.json` новым результатом — старый вердикт иначе был бы
    потерян. History — append-only: старые записи никогда не переписываются
    и не читаются обратно самим гейтом.
    """
    path = _history_path(root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as history_file:
        history_file.write(json.dumps(verdict.to_json(), ensure_ascii=False) + "\n")


def _write_verdict(
    root: Path,
    *,
    task_id: str,
    claim: Claim,
    verified_head: str,
    red_replay: str,
    selector_at_head: str,
    verdict: str,
    notes: str,
) -> None:
    """Собирает `Verdict` из `claim` и пишет его атомарно в evidence."""
    record = Verdict(
        task_id=task_id,
        claim_revision=claim.revision,
        red_sha=claim.red_sha,
        verified_head=verified_head,
        red_replay=red_replay,
        selector_at_head=selector_at_head,
        verdict=verdict,
        checked_at=datetime.now(UTC).isoformat(),
        notes=notes,
    )
    write_json_atomic(_verdict_path(root, task_id), record.to_json())


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    """`True`, если `ancestor` — предок `descendant` (или совпадает с ним).

    Обёртка над `git merge-base --is-ancestor`; невалидный/недостижимый
    `ancestor` даёт ненулевой exit code — трактуется как `False`, а не
    исключение (вызывающий код сам решает, ошибка это или просто «нет»).
    """
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _diff_paths(root: Path, base: str, head: str) -> list[str]:
    """Возвращает пути, изменённые в диапазоне `base..head` (`git diff --name-only`).

    `CalledProcessError` (например, `base`/`head` — невалидный/недостижимый
    SHA) превращается в `GateError` — контракт скрипта («любая ошибка →
    exit 3») не терпит сырых traceback'ов из subprocess.
    """
    try:
        output = git(root, "diff", "--name-only", f"{base}..{head}")
    except subprocess.CalledProcessError as exc:
        raise GateError(f"git diff {base}..{head} упал: {exc.stderr}") from exc
    return [line for line in output.split("\n") if line]


def _replay_red(root: Path, red_sha: str, selector: str) -> tuple[str, str]:
    """Реиграет `selector` на `red_sha` в отдельном git worktree вне репо.

    Создаёт временный worktree через `tempfile.mkdtemp` (вне `root`),
    запускает `_run_selector` с `cwd=worktree` — категории результата те
    же, что у `_run_selector`: `"green"` (реализация уже была в
    red-коммите — red не был красным), `"expected_fail"` (ожидаемо),
    `"error"`. Сбой самого `git worktree add` (битый `red_sha`, конфликт
    целевой директории) — `CalledProcessError`, превращается в `GateError`,
    а не всплывает сырым traceback'ом. Worktree убирается в `finally`:
    `git worktree remove --force`, а если это не удалось — `git worktree
    prune`, чтобы не оставить репозиторий с висящей worktree-регистрацией.
    """
    tmp_dir = tempfile.mkdtemp(prefix="tdd-gate-replay-")
    try:
        try:
            git(root, "worktree", "add", "--detach", tmp_dir, red_sha)
        except subprocess.CalledProcessError as exc:
            raise GateError(
                f"не удалось создать replay-worktree на {red_sha}: {exc.stderr}"
            ) from exc
        return _run_selector(Path(tmp_dir), selector)
    finally:
        try:
            git(root, "worktree", "remove", "--force", tmp_dir)
        except subprocess.CalledProcessError:
            git(root, "worktree", "prune")
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _is_valid_waiver(root: Path, waiver: Waiver, task_id: str, head: str) -> bool:
    """Waiver валиден: свой `task_id`, одобрен человеком, baseline — предок HEAD."""
    if waiver.task_id != task_id:
        return False
    if waiver.approved_by != "human":
        return False
    return _is_ancestor(root, waiver.baseline_sha, head)


def _verify_without_claim(root: Path, task_id: str, head: str) -> int:
    """Шаг 2 `cmd_verify`: без claim'а единственный путь дальше — валидный waiver.

    Невалидный или отсутствующий waiver — fail-closed, `1`. Валидный —
    пишет verdict `WAIVED` (WAIVED != PASS, ось H3 им не закрывается — см.
    докстринг модуля) и возвращает `0`.
    """
    waiver = load_waiver(root, task_id)
    if waiver is None or not _is_valid_waiver(root, waiver, task_id, head):
        print(
            f"verify: {task_id}: нет claim'а и нет валидного waiver — fail-closed",
            file=sys.stderr,
        )
        return 1
    verdict = Verdict(
        task_id=task_id,
        claim_revision=0,
        red_sha="",
        verified_head=head,
        red_replay="",
        selector_at_head="",
        verdict=CAT_WAIVED,
        checked_at=datetime.now(UTC).isoformat(),
        notes=f"waiver: {waiver.reason}",
    )
    write_json_atomic(_verdict_path(root, task_id), verdict.to_json())
    return 0


def cmd_verify(root: Path) -> int:
    """Команда `verify`: реиграет red-чекпоинт текущей задачи, пишет verdict.

    Реализует 8-шаговую логику из плана: резолвит текущую задачу, при
    отсутствии claim'а падает на waiver (иначе fail-closed), проверяет
    совместимость/идемпотентность существующего verdict'а, валидирует
    red-диапазон (только `tests/`, red_sha — предок HEAD), реиграет red SHA
    в отдельном worktree и требует зелёный селектор на текущем HEAD.
    Возвращает код по контракту скрипта: `0` — PASS/WAIVED (в т.ч.
    идемпотентный повтор), `1` — FAIL, `3` — ERROR. `GateError` из
    внутренней логики перехватывается здесь же, как и в `cmd_red`.
    """
    try:
        return _cmd_verify(root)
    except GateError as exc:
        print(f"verify: {exc}", file=sys.stderr)
        return exc.exit_code


def _cmd_verify(root: Path) -> int:
    """Логика `cmd_verify`; `GateError` пробрасывается наружу (перехват — выше)."""
    task_id = resolve_current_task(root)  # шаг 1
    head = head_sha(root)

    claim = load_claim(root, task_id)
    if claim is None:
        return _verify_without_claim(root, task_id, head)  # шаг 2

    # Шаги 3-4: совместимость и идемпотентность существующего verdict'а.
    existing = load_verdict(root, task_id)
    if existing is not None:
        if existing.verdict == CAT_WAIVED:
            # waived -> claimed: легитимный переход (решение контроллера,
            # fix round 1). У WAIVED-verdict'а red_sha всегда "" (см.
            # `_verify_without_claim`) — сравнивать его с claim.red_sha как
            # forgery бессмысленно. Архивируем и идём в полную
            # верификацию ниже вместо возврата/раннего exit 3.
            _append_verdict_history(root, task_id, existing)
        else:
            if existing.red_sha != claim.red_sha:
                raise GateError(
                    f"{task_id}: verdict.red_sha={existing.red_sha!r} не совпадает "
                    f"с claim.red_sha={claim.red_sha!r} — рассинхрон/подделка"
                )
            if existing.verdict == CAT_PASS:
                if existing.verified_head == head:
                    return 0  # идемпотентный PASS — второй verdict не пишется
                _append_verdict_history(root, task_id, existing)

    # Шаг 5: baseline_sha/red_sha существуют, red_sha предок HEAD, диапазон
    # трогает только tests/. Проверка baseline_sha симметрична проверке
    # red_sha ниже — битый/несуществующий SHA обязан давать чистый exit 3
    # через GateError, а не CalledProcessError из `_diff_paths`.
    if not _commit_exists(root, claim.baseline_sha):
        raise GateError(
            f"{task_id}: baseline_sha {claim.baseline_sha} отсутствует в истории"
        )
    if not _commit_exists(root, claim.red_sha):
        raise GateError(f"{task_id}: red_sha {claim.red_sha} отсутствует в истории")
    if not _is_ancestor(root, claim.red_sha, head):
        raise GateError(f"{task_id}: red_sha {claim.red_sha} не предок HEAD ({head})")
    changed = _diff_paths(root, claim.baseline_sha, claim.red_sha)
    non_tests = [path for path in changed if not path.startswith("tests/")]
    if non_tests:
        raise GateError(
            f"{task_id}: red-диапазон {claim.baseline_sha}..{claim.red_sha} "
            f"трогает не только tests/: {', '.join(non_tests)}"
        )

    # Шаг 6: replay red_sha в отдельном worktree.
    replay_category, replay_output = _replay_red(root, claim.red_sha, claim.selector)
    if replay_category != "expected_fail":
        verdict_category = (
            CAT_UNEXPECTED_FAIL if replay_category == "green" else CAT_ERROR
        )
        _write_verdict(
            root,
            task_id=task_id,
            claim=claim,
            verified_head=head,
            red_replay=verdict_category,
            selector_at_head="",
            verdict=verdict_category,
            notes=replay_output[-_NOTES_MAX_LEN:],
        )
        return 1 if verdict_category == CAT_UNEXPECTED_FAIL else 3

    # Шаг 7: селектор обязан быть зелёным на текущем HEAD.
    head_category, _head_output = _run_selector(root, claim.selector)
    if head_category != "green":
        print(
            f"verify: {claim.selector} красный на HEAD — "
            "реализация не закрывает свой тест",
            file=sys.stderr,
        )
        return 1

    # Шаг 8: verdict PASS.
    _write_verdict(
        root,
        task_id=task_id,
        claim=claim,
        verified_head=head,
        red_replay=CAT_EXPECTED_FAIL,
        selector_at_head=CAT_PASS,
        verdict=CAT_PASS,
        notes="",
    )
    return 0


def cmd_audit(root: Path) -> int:
    """Команда `audit`: у каждой DONE-задачи с claim обязан быть PASS/WAIVED verdict.

    Ничего не переигрывает — только читает evidence с диска (дёшево,
    идемпотентно). Предназначена для post_done-плагина spec-runner.
    `GateError` из внутренней логики (битый evidence) перехватывается
    здесь же, как и в `cmd_red`/`cmd_verify`.
    """
    try:
        return _cmd_audit(root)
    except GateError as exc:
        print(f"audit: {exc}", file=sys.stderr)
        return exc.exit_code


def _cmd_audit(root: Path) -> int:
    """Логика `cmd_audit`; `GateError` пробрасывается наружу (перехват — выше)."""
    violations: list[str] = []
    for path in sorted((root / "spec").glob("*tasks.md")):
        for task_id in _done_task_ids(path.read_text()):
            if load_claim(root, task_id) is None:
                continue  # DONE без claim — гейт для этой задачи не применялся
            verdict = load_verdict(root, task_id)
            if verdict is None or verdict.verdict not in (CAT_PASS, CAT_WAIVED):
                violations.append(task_id)
    if violations:
        print(
            "audit: DONE-задачи с claim без PASS/WAIVED verdict: "
            + ", ".join(violations),
            file=sys.stderr,
        )
        return 3
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    """Строит argparse-парсер для подкоманд `red`/`verify`/`audit`."""
    parser = argparse.ArgumentParser(prog="tdd_gate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    red_parser = subparsers.add_parser("red", help="зафиксировать red-чекпоинт")
    red_parser.add_argument("-k", "--selector", required=True, help="pytest-селектор")
    red_parser.add_argument("-m", "--message", default="", help="ожидаемое поведение")

    subparsers.add_parser("verify", help="реиграть red-чекпоинт текущей задачи")
    subparsers.add_parser("audit", help="проверить verdict для DONE-задач")

    return parser


_EXIT_CODE_LABEL = {0: "OK", 1: "FAIL", 3: "ERROR"}


def main(argv: list[str] | None = None) -> int:
    """CLI-точка входа гейта: подкоманды `red`/`verify`/`audit`.

    Каждая команда уже перехватывает свой `GateError` внутри (см.
    `cmd_red`/`cmd_verify`/`cmd_audit`) — обёртка здесь на случай, если
    исключение всё же прорвётся наружу из будущего кода. По завершении,
    независимо от кода возврата, печатает одну человекочитаемую итоговую
    строку в stdout.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        if args.command == "red":
            code = cmd_red(root, args.selector, args.message)
        elif args.command == "verify":
            code = cmd_verify(root)
        else:
            code = cmd_audit(root)
    except GateError as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        code = exc.exit_code
    label = _EXIT_CODE_LABEL.get(code, f"UNKNOWN({code})")
    print(f"tdd-gate {args.command}: {label}")
    return code


if __name__ == "__main__":
    sys.exit(main())
