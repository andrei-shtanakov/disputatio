"""Конфиг сессии — `RuntimeConfig` ([DESIGN-014], [REQ-001], [REQ-014]).

Снапшот, из которого собирается вся сессия: адаптеры и модели агентов,
лимиты §5.2, список gates и `base_commit` — цель `git reset` первого раунда.
Frozen: resume обязан прочитать ровно то, что было записано на старте, и
никакой шаг не вправе подкрутить лимит на ходу.

Снапшот живёт на диске текстом (`config.toml`), и текст этот пишется руками
(ADR-003): `tomllib` в stdlib только читает, писателя TOML в зависимостях
нет. Отсюда два несимметричных куска — `render_toml` (структура → текст) и
`from_toml` (текст → структура), — и главное их свойство проверяемо, а не
декларировано: `render → from_toml → render` побайтово стабилен. Разъедься
пара, resume поднял бы сессию не с тем конфигом, которым она была запущена,
и заметить это было бы негде.

Читатель строг ко всему, что не сходится: битый TOML, отсутствующий ключ,
целое, записанное строкой, неизвестный режим — всё это одна и та же
`ConfigError` ([DESIGN-020]). Технические `TOMLDecodeError`/`KeyError`
наружу не выходят: пользователю нужно услышать «конфиг сессии не читается»,
а не repr ключа в кавычках.
"""

import tomllib
from collections.abc import Iterable, Mapping, Sequence
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
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}
"""Символы, которые basic-строка TOML обязана нести экранированными.

Таблица применяется посимвольно к ИСХОДНОЙ строке, а не цепочкой `replace`:
цепочка обработала бы собственный вывод, и обратный слэш, поставленный
первой заменой, был бы экранирован второй ещё раз.
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
        """Разбирает снапшот `config.toml` ([REQ-014], [DESIGN-014]).

        Все три вида негодного снапшота — синтаксически битый TOML,
        отсутствующий обязательный ключ и значение не того типа — сводятся к
        одной `ConfigError`: для пользователя это один факт «конфигом сессии
        пользоваться нельзя», и различать их технической иерархией stdlib
        значило бы уводить CLI мимо ветки [DESIGN-020]. Исходная ошибка
        остаётся причиной (`__cause__`) — диагноз не теряется, он просто не
        всплывает наружу сам.

        Проверка типов здесь не дублирует чужую валидацию, а заменяет
        отсутствующую: `LimitsConfig` и `GateSpec` — обычные dataclass'ы, и
        `max_rounds = "5"` доехал бы до §5 строкой, где сравнение с числом
        упало бы много позже и совсем в другом месте.
        """
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"config.toml не разбирается как TOML: {exc}") from exc
        try:
            return cls._from_tables(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"config.toml непригоден: {exc}") from exc

    @classmethod
    def _from_tables(cls, raw: Mapping[str, Any]) -> "RuntimeConfig":
        """Собирает конфиг из разобранных таблиц; ошибки уходят вызывающему.

        Отдельный метод, а не тело `from_toml`: перевод в `ConfigError`
        обязан накрывать ВЕСЬ разбор одним `except`, и смешение его с
        разбором в одной функции рано или поздно оставило бы новую строку
        снаружи блока.
        """
        session = _table(raw, "session")
        task = _table(raw, "task")
        limits = _table(raw, "limits")
        agents = _table(raw, "agents")
        return cls(
            session_id=_text(session, "id", where="session"),
            mode=Mode(_text(session, "mode", where="session")),
            base_commit=_text(session, "base_commit", where="session"),
            task_prompt=_text(task, "prompt", where="task"),
            author=_agent(agents, "author"),
            reviewer=_agent(agents, "reviewer"),
            limits=LimitsConfig(
                max_rounds=_integer(limits, "max_rounds"),
                max_total_tokens=_integer(limits, "max_total_tokens"),
                max_wall_seconds=_integer(limits, "max_wall_seconds"),
                schema_retries=_integer(limits, "schema_retries"),
            ),
            gates=tuple(_gate(item) for item in _tables(raw, "gates")),
            attachments=_texts(task, "attachments"),
        )

    def render_toml(self) -> str:
        """Рендерит снапшот в текст раскладки [DESIGN-014].

        Порядок таблиц фиксирован и совпадает со спекой: `config.toml`
        читается человеком в разборе инцидента, и «валидный TOML» с
        перетасованными таблицами эту его работу отменяет.

        Целые печатаются целыми, `enabled` — литералом `true`/`false`:
        TOML принял бы и `max_rounds = "5"`, но такой снапшот перестал бы
        читаться собственным `from_toml`, то есть round-trip разошёлся бы
        ровно там, где его никто не смотрит — при resume.
        """
        blocks = [
            _block(
                "[session]",
                (
                    ("id", _quote(self.session_id)),
                    ("mode", _quote(self.mode.value)),
                    ("base_commit", _quote(self.base_commit)),
                ),
            ),
            _block(
                "[task]",
                (
                    ("prompt", _quote(self.task_prompt)),
                    ("attachments", _quoted_array(self.attachments)),
                ),
            ),
            _block(
                "[limits]",
                (
                    ("max_rounds", str(self.limits.max_rounds)),
                    ("max_total_tokens", str(self.limits.max_total_tokens)),
                    ("max_wall_seconds", str(self.limits.max_wall_seconds)),
                    ("schema_retries", str(self.limits.schema_retries)),
                ),
            ),
            _agent_block("author", self.author),
            _agent_block("reviewer", self.reviewer),
            *(_gate_block(gate) for gate in self.gates),
        ]
        return "\n".join(blocks)

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
    """Читает снапшот `.disputatio/config.toml` сессии под `root` ([REQ-014]).

    Отсутствие файла — та же `ConfigError`, что и битое содержимое: для
    resume «снапшота нет» и «снапшот не читается» — один исход, и голый
    `FileNotFoundError` на этом месте увёл бы CLI мимо [DESIGN-020].
    Читается именно снапшот сессии, а не конфиг текущего окружения: сессия
    обязана продолжиться тем, чем была запущена, даже если внешний файл с
    тех пор переписали.

    Байты, не декодируемые как UTF-8, — тоже `ConfigError`, и отдельным
    `except`: `UnicodeDecodeError` наследуется от `ValueError`, а не от
    `OSError`, и один только перехват ошибок ввода-вывода выпустил бы его
    наружу traceback'ом. Случай не выдуманный: `config.toml` — текстовый
    файл ровно затем, чтобы его правили руками, а редактор, сохранивший
    русский `task.prompt` в однобайтовой кодировке, даёт именно эти байты.
    """
    path = config_toml(root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"снапшот конфига {path} не читается: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"снапшот конфига {path} не в UTF-8: {exc}") from exc
    return RuntimeConfig.from_toml(text)


def _block(header: str, pairs: Sequence[tuple[str, str]]) -> str:
    """Одна таблица TOML: заголовок и пары `ключ = значение`, каждая строкой."""
    body = "".join(f"{key} = {value}\n" for key, value in pairs)
    return f"{header}\n{body}"


def _agent_block(role: str, agent: AgentConfig) -> str:
    """Вложенная таблица `[agents.<role>]` одного агента."""
    return _block(
        f"[agents.{role}]",
        (("adapter", _quote(agent.adapter)), ("model", _quote(agent.model))),
    )


def _gate_block(gate: GateSpec) -> str:
    """Элемент массива таблиц `[[gates]]` — один gate конфигурации."""
    return _block(
        "[[gates]]",
        (
            ("name", _quote(gate.name)),
            ("cmd", _quote(gate.cmd)),
            ("enabled", "true" if gate.enabled else "false"),
        ),
    )


def _quoted_array(values: Iterable[str]) -> str:
    """Массив строк в одну строку; пустой печатается `[]`."""
    return "[" + ", ".join(_quote(value) for value in values) + "]"


def _quote(value: str) -> str:
    """Строка как basic-строка TOML, с экранированием по спеке.

    Управляющие символы, которых нет в `_ESCAPES`, уходят `\\uXXXX`: TOML
    запрещает их в basic-строке сырыми, и сырой `\\x01` в `task.prompt`
    сделал бы снапшот неразбираемым — то есть потерял бы сессию из-за
    одного символа, попавшего в текст задачи.
    """
    body = "".join(_ESCAPES.get(char, _escaped_control(char)) for char in value)
    return f'"{body}"'


def _escaped_control(char: str) -> str:
    """Символ как есть либо `\\uXXXX`, если TOML не примет его сырым."""
    if char < "\x20" or char == "\x7f":
        return f"\\u{ord(char):04X}"
    return char


def _table(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Обязательная таблица верхнего уровня; иначе `KeyError`/`TypeError`."""
    if name not in raw:
        raise KeyError(f"нет обязательной таблицы [{name}]")
    value = raw[name]
    if not isinstance(value, Mapping):
        raise TypeError(f"[{name}] обязана быть таблицей, а не {type(value).__name__}")
    return value


def _tables(raw: Mapping[str, Any], name: str) -> Sequence[Mapping[str, Any]]:
    """Массив таблиц `[[name]]`; отсутствие — пустой список, а не ошибка.

    Сессия без гейтов законна ([REQ-010]): `verification.overall` тогда
    складывается из пустого набора, и требовать хотя бы один гейт значило бы
    завести здесь второе мнение о §5.
    """
    value = raw.get(name, [])
    if not isinstance(value, list):
        raise TypeError(f"[[{name}]] обязан быть массивом таблиц")
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError(f"элемент [[{name}]] обязан быть таблицей")
    return value


def _agent(agents: Mapping[str, Any], role: str) -> AgentConfig:
    """Агент из вложенной таблицы `[agents.<role>]`."""
    table = _table(agents, role)
    where = f"agents.{role}"
    return AgentConfig(
        adapter=_text(table, "adapter", where=where),
        model=_text(table, "model", where=where),
    )


def _gate(item: Mapping[str, Any]) -> GateSpec:
    """Один `GateSpec` из элемента массива таблиц `[[gates]]`.

    `enabled` необязателен и по умолчанию `True` — тем же дефолтом, что у
    самого `GateSpec`: два разных ответа на «гейт без флага включён?»
    разошлись бы молча, и разошлись бы в сторону пропущенной проверки.
    """
    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TypeError("gates.enabled обязан быть true/false")
    return GateSpec(
        name=_text(item, "name", where="gates"),
        cmd=_text(item, "cmd", where="gates"),
        enabled=enabled,
    )


def _text(table: Mapping[str, Any], key: str, *, where: str) -> str:
    """Обязательное строковое значение таблицы `where`."""
    if key not in table:
        raise KeyError(f"нет обязательного ключа {where}.{key}")
    value = table[key]
    if not isinstance(value, str):
        raise TypeError(f"{where}.{key} обязан быть строкой")
    return value


def _texts(table: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Необязательный массив строк; отсутствие — пустой кортеж."""
    value = table.get(key, [])
    if not isinstance(value, list):
        raise TypeError(f"{key} обязан быть массивом строк")
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"элемент {key} обязан быть строкой")
    return tuple(value)


def _integer(table: Mapping[str, Any], key: str) -> int:
    """Обязательное целое из `[limits]`; `true`/`"5"` целым не считаются.

    `bool` — подкласс `int` в Python, поэтому `max_rounds = true` прошёл бы
    проверку «это целое» молча и превратил бы лимит раундов в единицу.
    """
    if key not in table:
        raise KeyError(f"нет обязательного ключа limits.{key}")
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"limits.{key} обязан быть целым числом")
    return value
