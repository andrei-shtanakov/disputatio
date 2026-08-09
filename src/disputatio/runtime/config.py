"""Конфиг сессии — `RuntimeConfig` ([DESIGN-014], [REQ-001], [REQ-014]).

Снапшот, из которого собирается вся сессия: адаптеры и модели агентов,
лимиты §5.2, список gates и `base_commit` — цель `git reset` первого раунда.
Frozen: resume обязан прочитать ровно то, что было записано на старте, и
никакой шаг не вправе подкрутить лимит на ходу.

Рядом со структурой живёт её текстовая форма — снапшот `.disputatio/
config.toml` (`render_toml`/`from_toml`/`load_config`). Писателя TOML в
зависимостях нет: `tomllib` из stdlib только читает, а тянуть пакет ради
пяти таблиц значило бы платить зависимостью за сериализацию, которую
целиком описывает [DESIGN-014] (ADR-003). Поэтому писатель здесь свой,
минимальный — строки, целые, булевы, вложенные таблицы и массив таблиц, —
и его корректность держится не на объёме кода, а на round-trip: `render →
from_toml → render` побайтово стабилен, а разбирается результат ещё и
сторонним `tomllib`.
"""

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from disputatio.contracts import (
    AgentRef,
    BudgetUsed,
    Limits,
    Mode,
    Role,
    SessionPhase,
    SessionState,
    TaskSpec,
)
from disputatio.runtime.errors import ConfigError
from disputatio.runtime.layout import config_toml
from disputatio.verifier import GateSpec

_ESCAPES: Final[Mapping[str, str]] = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}
"""Экранирование basic-строки TOML: обратный слэш и кавычка + управляющие.

Таблица посимвольная, а не цепочка `replace`: цепочка обязана начинаться с
обратного слэша, иначе она экранирует то, что сама же и вставила, — и эта
зависимость от порядка ломается при добавлении любого нового правила.
"""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Один агент из `[agents.author]`/`[agents.reviewer]`: имя CLI и модель."""

    adapter: str
    model: str


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    """Лимиты сессии из `[limits]` (§5.2 SPEC-001)."""

    max_rounds: int
    max_total_tokens: int
    max_wall_seconds: int
    schema_retries: int


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Снапшот конфига сессии — вход composition root'а ([DESIGN-001])."""

    session_id: str
    mode: Mode
    base_commit: str
    task_prompt: str
    author: AgentConfig
    reviewer: AgentConfig
    limits: LimitsConfig
    gates: tuple[GateSpec, ...] = ()
    attachments: tuple[str, ...] = ()

    @classmethod
    def from_toml(cls, text: str) -> "RuntimeConfig":
        """Разбирает снапшот `config.toml` ([DESIGN-014], [REQ-014]).

        Любой отказ приходит одним классом `ConfigError`: и синтаксис
        (`TOMLDecodeError`), и нехватка поля (`KeyError`), и поле не того
        типа, и неизвестный режим (`ValueError`). Для пользователя это одно
        событие — «конфиг сессии не читается», — и техническое исключение из
        stdlib на его месте не назвало бы ни файла, ни поля ([DESIGN-020]).
        Причина при этом сохраняется в `__cause__`: диагноз нужен в
        `events.jsonl`, даже когда в stderr уходит одна строка.
        """
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"снапшот config.toml не разбирается как TOML: {exc}"
            ) from exc

        session = _table(data, "session", label="session")
        task = _table(data, "task", label="task")
        limits = _table(data, "limits", label="limits")
        agents = _table(data, "agents", label="agents")
        return cls(
            session_id=_string(session, "id", where="session"),
            mode=_mode(_string(session, "mode", where="session")),
            base_commit=_string(session, "base_commit", where="session"),
            task_prompt=_string(task, "prompt", where="task"),
            author=_agent(agents, "author"),
            reviewer=_agent(agents, "reviewer"),
            limits=LimitsConfig(
                max_rounds=_integer(limits, "max_rounds", where="limits"),
                max_total_tokens=_integer(limits, "max_total_tokens", where="limits"),
                max_wall_seconds=_integer(limits, "max_wall_seconds", where="limits"),
                schema_retries=_integer(limits, "schema_retries", where="limits"),
            ),
            gates=_gates(data),
            attachments=_strings(task, "attachments", where="task"),
        )

    def render_toml(self) -> str:
        """Печатает снапшот в раскладке [DESIGN-014].

        Раскладка — часть контракта, а не вкус: `config.toml` читают глазами
        при разборе инцидента, и «валидный TOML» с перетасованными таблицами
        эту его работу отменяет. Блоки разделены пустой строкой, файл
        заканчивается ровно одним переводом строки — round-trip обязан быть
        побайтовым, а «лишняя пустая строка в хвосте» ломает именно его.

        `attachments` печатается всегда, в том числе пустым массивом: поле,
        которое писатель молча пропускает, читатель молча заменит дефолтом —
        и потеря вложений всплывёт только через раунд, в промпте автора.
        """
        blocks: list[Sequence[str]] = [
            (
                "[session]",
                f"id = {_toml_string(self.session_id)}",
                f"mode = {_toml_string(self.mode.value)}",
                f"base_commit = {_toml_string(self.base_commit)}",
            ),
            (
                "[task]",
                f"prompt = {_toml_string(self.task_prompt)}",
                f"attachments = {_toml_array(self.attachments)}",
            ),
            (
                "[limits]",
                f"max_rounds = {self.limits.max_rounds:d}",
                f"max_total_tokens = {self.limits.max_total_tokens:d}",
                f"max_wall_seconds = {self.limits.max_wall_seconds:d}",
                f"schema_retries = {self.limits.schema_retries:d}",
            ),
            _agent_block("author", self.author),
            _agent_block("reviewer", self.reviewer),
            *(_gate_block(gate) for gate in self.gates),
        ]
        body = "\n\n".join("\n".join(block) for block in blocks)
        return f"{body}\n"

    def to_session_state(self, *, created_at: datetime) -> SessionState:
        """Начальное состояние §4.1: `IDLE`, нулевой раунд, пустой бюджет.

        `created_at` передаётся, а не берётся у `datetime.now`: часы сессии
        инжектируются в `RuntimeDeps.now`, и второй источник времени сделал
        бы `session.json` недетерминированным в тестах ([REQ-001]).
        """
        return SessionState(
            session_id=self.session_id,
            created_at=created_at,
            state=SessionPhase.IDLE,
            current_round=0,
            task=TaskSpec(
                prompt=self.task_prompt,
                attachments=list(self.attachments),
                mode=self.mode,
            ),
            agents={
                Role.AUTHOR: AgentRef(
                    adapter=self.author.adapter, model=self.author.model
                ),
                Role.REVIEWER: AgentRef(
                    adapter=self.reviewer.adapter, model=self.reviewer.model
                ),
            },
            limits=Limits(
                max_rounds=self.limits.max_rounds,
                max_total_tokens=self.limits.max_total_tokens,
                max_wall_seconds=self.limits.max_wall_seconds,
                schema_retries=self.limits.schema_retries,
            ),
            budget_used=BudgetUsed(),
        )


def load_config(root: Path) -> RuntimeConfig:
    """Читает снапшот `root/.disputatio/config.toml` ([REQ-014]).

    Конфиг сессии берётся из снапшота, а не из текущего окружения: файл
    записан один раз на старте и пережил перезапуск процесса, тогда как
    внешний конфиг между запусками вправе поменяться — и подставил бы
    resume другие лимиты, другие гейты и другую цель `git reset`.

    Отсутствие файла — тот же `ConfigError`, что и битое содержимое: с
    точки зрения пользователя это одно «конфига сессии нет», а
    `FileNotFoundError` увёл бы CLI мимо ветки [DESIGN-020].
    """
    path = config_toml(root)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"снапшот конфига {path} не читается: {exc}") from exc
    return RuntimeConfig.from_toml(text)


def _agent_block(role: str, agent: AgentConfig) -> Sequence[str]:
    """Строки вложенной таблицы `[agents.<role>]`."""
    return (
        f"[agents.{role}]",
        f"adapter = {_toml_string(agent.adapter)}",
        f"model = {_toml_string(agent.model)}",
    )


def _gate_block(gate: GateSpec) -> Sequence[str]:
    """Строки одного элемента массива таблиц `[[gates]]`."""
    return (
        "[[gates]]",
        f"name = {_toml_string(gate.name)}",
        f"cmd = {_toml_string(gate.cmd)}",
        f"enabled = {'true' if gate.enabled else 'false'}",
    )


def _toml_string(value: str) -> str:
    r"""Basic-строка TOML: кавычки, слэши и управляющие символы экранированы.

    Многострочная форма (`\"\"\"`) не используется намеренно: перевод строки
    внутри `task.prompt` уезжает escape-последовательностью, и тогда ни одно
    значение снапшота не способно закончиться так, что следующая строка
    файла прочитается как его продолжение. Символы вне таблицы, но ниже
    пробела, печатаются `\uXXXX` — сырой управляющий байт TOML запрещает.
    """
    out: list[str] = []
    for char in value:
        escaped = _ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
        elif char < " " or char == "\x7f":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    body = "".join(out)
    return f'"{body}"'


def _toml_array(values: Sequence[str]) -> str:
    """Однострочный массив строк; пустой печатается как `[]`."""
    items = ", ".join(_toml_string(value) for value in values)
    return f"[{items}]"


def _table(data: Mapping[str, Any], key: str, *, label: str) -> Mapping[str, Any]:
    """Обязательная таблица снапшота; отсутствие или не-таблица — `ConfigError`."""
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(
            f"в снапшоте config.toml нет таблицы [{label}] либо она не таблица"
        )
    return value


def _string(table: Mapping[str, Any], key: str, *, where: str) -> str:
    """Обязательное строковое поле таблицы `where`."""
    value = table.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"[{where}].{key} обязано быть строкой; в снапшоте {value!r}")
    return value


def _integer(table: Mapping[str, Any], key: str, *, where: str) -> int:
    """Обязательное целое поле таблицы `where`.

    `bool` отвергается явно: в Python он подкласс `int`, поэтому
    `max_rounds = true` прошёл бы проверку «это целое» и превратился бы в
    лимит «один раунд» — молча и с виду законно.
    """
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"[{where}].{key} обязано быть целым; в снапшоте {value!r}")
    return value


def _boolean(table: Mapping[str, Any], key: str, *, where: str, default: bool) -> bool:
    """Булево поле таблицы `where`; отсутствие — `default`."""
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"[{where}].{key} обязано быть булевым; в снапшоте {value!r}")
    return value


def _strings(table: Mapping[str, Any], key: str, *, where: str) -> tuple[str, ...]:
    """Массив строк таблицы `where`; отсутствие — пустой кортеж."""
    value = table.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(
            f"[{where}].{key} обязано быть массивом строк; в снапшоте {value!r}"
        )
    return tuple(value)


def _mode(value: str) -> Mode:
    """`[session].mode` в `Mode`; чужое значение — `ConfigError` со списком."""
    try:
        return Mode(value)
    except ValueError as exc:
        known = ", ".join(mode.value for mode in Mode)
        raise ConfigError(
            f"[session].mode = {value!r} — неизвестный режим; известны: {known}"
        ) from exc


def _agent(agents: Mapping[str, Any], role: str) -> AgentConfig:
    """Один агент из вложенной таблицы `[agents.<role>]`."""
    table = _table(agents, role, label=f"agents.{role}")
    return AgentConfig(
        adapter=_string(table, "adapter", where=f"agents.{role}"),
        model=_string(table, "model", where=f"agents.{role}"),
    )


def _gates(data: Mapping[str, Any]) -> tuple[GateSpec, ...]:
    """Массив таблиц `[[gates]]`; отсутствие — пустой кортеж (гейтов нет)."""
    raw = data.get("gates", [])
    if not isinstance(raw, list):
        raise ConfigError(f"[[gates]] обязан быть массивом таблиц; в снапшоте {raw!r}")
    specs: list[GateSpec] = []
    for index, item in enumerate(raw):
        where = f"gates[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} — не таблица: в снапшоте {item!r}")
        specs.append(
            GateSpec(
                name=_string(item, "name", where=where),
                cmd=_string(item, "cmd", where=where),
                enabled=_boolean(item, "enabled", where=where, default=True),
            )
        )
    return tuple(specs)
