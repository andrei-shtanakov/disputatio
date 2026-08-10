"""Чтение снапшота с диска и типовые ловушки `from_toml` ([REQ-014]).

[TASK-016], дополнение к `test_resume_dispatch.py` и
`test_config_snapshot_defaults.py`. Те два файла гоняют пару
писатель-читатель на строках: `render_toml` отдаёт `str`, `from_toml`
принимает `str`, и участок «файл на диске → текст» в них не участвует
вовсе. Между тем именно там живёт исход, ради которого снапшот вообще
текстовый: `config.toml` правят руками, и правка возвращается байтами.

Три дыры закрываются здесь, и каждая — не гипотетическая, а с ценой:

* **Снапшот не в UTF-8.** `read_text(encoding="utf-8")` поднимает
  `UnicodeDecodeError`, а он наследуется от `ValueError`, не от `OSError`, —
  то есть проходит мимо перехвата ошибок ввода-вывода и выходит наружу
  traceback'ом вместо строки [DESIGN-020]. Русский `task.prompt`, сохранённый
  редактором в однобайтовой кодировке, даёт ровно эти байты.
* **Управляющие символы в строках.** Их экранирование (`\\uXXXX`) не
  проверяется round-trip'ом собственной пары: сломай его — и `from_toml`
  честно прочтёт то, что написал `render_toml`, а `tomllib` откажется. Здесь
  текст едет через настоящий файл и разбирается заново, поэтому «писатель
  испортил снапшот» становится наблюдаемым: сессия, чей `config.toml` не
  читается, не поднимается уже никогда.
* **Значения правильной формы, но не того типа.** `max_rounds = true`
  (`bool` — подкласс `int` в Python) и `attachments = "док.md"` (строка
  вместо массива) синтаксически валидны, и без проверки типа первый молча
  обрезал бы сессию до одного раунда, а второй так же молча выбросил бы
  вложения.
"""

import tomllib
from pathlib import Path

import pytest

from disputatio.contracts import Mode
from disputatio.events import bootstrap_session, write_config_snapshot
from disputatio.runtime import AgentConfig, ConfigError, LimitsConfig, RuntimeConfig
from disputatio.runtime.config import load_config
from disputatio.verifier import GateSpec

# Управляющие символы, которых нет в таблице коротких escape'ов TOML:
# `\x01` — из середины запрещённого диапазона, `\x7f` — DEL, отдельная
# граница спеки. Оба `tomllib` отвергает сырыми, оба законны в строке Python.
_CONTROL_PROMPT = "начало\x01середина\x7fконец"

_SNAPSHOT = """[session]
id = "s"
mode = "develop"
base_commit = "abc"

[task]
prompt = "Почини экспорт CSV"
attachments = []

[limits]
max_rounds = 5
max_total_tokens = 100000
max_wall_seconds = 600
schema_retries = 1

[agents.author]
adapter = "claude_code"
model = "opus"

[agents.reviewer]
adapter = "claude_code"
model = "sonnet"

[[gates]]
name = "pytest"
cmd = "uv run pytest -q"
enabled = true
"""


def _config(*, prompt: str = "Почини экспорт CSV") -> RuntimeConfig:
    """`RuntimeConfig` сессии; различимое вынесено в аргументы."""
    return RuntimeConfig(
        session_id="20260810-120000-a1b2",
        mode=Mode.DEVELOP,
        base_commit="9f1c2ab",
        task_prompt=prompt,
        author=AgentConfig(adapter="claude_code", model="opus"),
        reviewer=AgentConfig(adapter="claude_code", model="sonnet"),
        limits=LimitsConfig(
            max_rounds=5,
            max_total_tokens=100_000,
            max_wall_seconds=600,
            schema_retries=1,
        ),
        gates=(GateSpec(name="pytest", cmd="uv run pytest -q", enabled=True),),
        attachments=("док.md",),
    )


def _seed(root: Path, text: str) -> None:
    """Кладёт снапшот на диск через штатного писателя `.disputatio/`."""
    bootstrap_session(root)
    write_config_snapshot(root, text)


def test_snapshot_that_is_not_utf8_is_a_config_error(tmp_path: Path) -> None:
    """Байты не в UTF-8 — `ConfigError`, а не `UnicodeDecodeError` наружу.

    `UnicodeDecodeError` — `ValueError`, поэтому перехват `OSError` вокруг
    чтения его не ловит: пользователь получил бы traceback там, где CLI
    обязан напечатать одну строку ([DESIGN-020]). Диагноз при этом
    сохраняется причиной — иначе «конфиг не читается» не сказало бы, чем
    именно он плох.
    """
    bootstrap_session(tmp_path)
    (tmp_path / ".disputatio" / "config.toml").write_bytes(_SNAPSHOT.encode("cp1251"))

    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path)

    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


def test_control_characters_survive_the_trip_through_the_snapshot_file(
    tmp_path: Path,
) -> None:
    """Управляющий символ в `task.prompt` не делает снапшот неразбираемым.

    Проверяется исход, а не приём: текст едет на диск штатным писателем и
    возвращается штатным читателем, а заодно разбирается СТОРОННИМ
    `tomllib` — своя пара писатель-читатель вправе договориться о сыром
    `\\x01`, а `tomllib` такой файл отвергает. Цена ошибки односторонняя:
    снапшот пишется один раз на старте, и сессия с непрочитываемым
    `config.toml` не поднимается уже никогда.
    """
    config = _config(prompt=_CONTROL_PROMPT)
    _seed(tmp_path, config.render_toml())

    text = (tmp_path / ".disputatio" / "config.toml").read_text(encoding="utf-8")

    assert tomllib.loads(text)["task"]["prompt"] == _CONTROL_PROMPT
    assert load_config(tmp_path) == config


def test_load_config_reads_the_snapshot_written_by_the_renderer(
    tmp_path: Path,
) -> None:
    """Читатель с диска берёт именно `.disputatio/config.toml` целиком.

    Без этого теста позитивная половина `load_config` держится на честном
    слове: негативные случаи прошли бы и у читателя, который всегда падает.
    """
    config = _config()
    _seed(tmp_path, config.render_toml())

    assert load_config(tmp_path) == config


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("max_rounds = 5", "max_rounds = true"),
        ("schema_retries = 1", "schema_retries = false"),
        ("attachments = []", 'attachments = "док.md"'),
        ("enabled = true", "enabled = 1"),
    ],
    ids=["bool-limit", "bool-retries", "attachments-not-a-list", "gate-enabled-int"],
)
def test_values_of_the_wrong_type_are_a_config_error(old: str, new: str) -> None:
    """Форма верна, тип нет — `ConfigError`, а не молчаливое приведение.

    `bool` — подкласс `int`, поэтому `max_rounds = true` прошёл бы проверку
    «это целое» и обрезал бы сессию до одного раунда; строка вместо массива
    так же тихо оставила бы задачу без вложений. Оба исхода хуже отказа:
    сессия идёт дальше, но не той, какой её запустили.
    """
    broken = _SNAPSHOT.replace(old, new, 1)
    assert broken != _SNAPSHOT, "мутация снапшота ничего не изменила"

    with pytest.raises(ConfigError):
        RuntimeConfig.from_toml(broken)


def test_gates_that_are_not_an_array_of_tables_is_a_config_error() -> None:
    """`gates = "pytest"` — `ConfigError`, а не итерация по символам строки.

    Строка итерируема, и без проверки типа читатель принял бы её за набор
    гейтов, развалив каждый символ в отдельный элемент. Ключ ставится ПЕРЕД
    первой таблицей: после неё он принадлежал бы ей, а не корню, и тест
    проверял бы отсутствие `[[gates]]` вместо их типа.
    """
    broken = 'gates = "pytest"\n' + _SNAPSHOT.split("[[gates]]")[0]

    with pytest.raises(ConfigError):
        RuntimeConfig.from_toml(broken)
