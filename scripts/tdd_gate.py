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
from dataclasses import dataclass
from pathlib import Path

EVIDENCE = Path("spec/.tdd-evidence")

_TASK_HEADING_RE = re.compile(r"^#{2,6}\s+([A-Z][A-Z0-9]*-\d+)\b")
_RUNNING_STATUS_RE = re.compile(r"\b(IN_PROGRESS|REVIEW)\b", re.IGNORECASE)

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
        raise GateError(
            f"{kind}: неверная схема {schema!r}, ожидается {expected!r}"
        )


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


def _running_task_ids(text: str) -> list[str]:
    """Возвращает ID задач со статусом IN_PROGRESS/REVIEW в тексте `text`.

    Заголовок задачи (`### TASK-NNN: ...`, уровень #### тоже допустим)
    привязывает статус к последней встреченной строке ниже него, пока не
    встретится следующий заголовок.
    """
    running: list[str] = []
    current_task_id: str | None = None
    for line in text.splitlines():
        heading = _TASK_HEADING_RE.match(line)
        if heading is not None:
            current_task_id = heading.group(1)
            continue
        if current_task_id is None:
            continue
        if _RUNNING_STATUS_RE.search(line):
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
        raise GateError(
            f"больше одной текущей задачи: {', '.join(running)}"
        )
    return running[0]
