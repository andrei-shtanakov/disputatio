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

import json
import os
import re
import subprocess
import sys
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


def _running_task_ids(text: str) -> list[str]:
    """Возвращает ID задач со статусом IN_PROGRESS/REVIEW в тексте `text`.

    Заголовок задачи (`### TASK-NNN: ...`, уровень #### тоже допустим)
    начинает секцию задачи. Внутри секции статус читается ТОЛЬКО из ПЕРВОЙ
    строки-кандидата — list-item'а (буллет `-`/`*`) с `|` внутри
    (`_META_CANDIDATE_RE`), это и есть позиция реальной meta-строки
    spec-runner. Все прочие строки секции (проза, markdown-таблицы, вторые
    и далее буллеты с `|`) полностью игнорируются как источник статуса —
    иначе таблица-описание вида `| Модуль A | REVIEW |` или посторонний
    буллет ниже meta-строки могли бы ложно засчитаться.
    """
    running: list[str] = []
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
        if _meta_line_status(line) is not None:
            running.append(current_task_id)
    return running


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
