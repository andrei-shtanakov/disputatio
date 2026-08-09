"""Дефолты `RuntimeConfig.from_toml` на неполном снапшоте ([DESIGN-014]).

[TASK-016], дополнение к `test_resume_dispatch.py`. Тот файл гоняет
round-trip на снапшоте, который написал сам `render_toml`, — а писатель
печатает КАЖДОЕ необязательное поле явно. Поэтому все ветки «ключа нет,
берём дефолт» там вакуумны: мутация дефолта переживает весь набор, потому
что до дефолта ни один тест не доходит.

Ключи эти необязательны не случайно: `config.toml` в [DESIGN-014] пишется и
руками (пример спеки не содержит ни `attachments`, ни `enabled`), а сессия
без гейтов законна. Значит каждый дефолт — наблюдаемое поведение, и цена
ошибки у них разная:

* `gates[].enabled` — тихо выключенный гейт. §5 сочла бы раунд зелёным по
  проверке, которую никто не прогонял, и это худшее из возможных прочтений
  «поля нет». Дефолт пинится НЕ литералом `True`, а равенством дефолту
  самого `GateSpec`: два ответа на «гейт без флага включён?» обязаны быть
  одним ответом, иначе они разойдутся при первой же правке спеки гейтов.
* `[[gates]]` целиком и `task.attachments` — пустые наборы; здесь опасна
  обратная ошибка, падение на законном снапшоте.
"""

import pytest

from disputatio.contracts import Mode
from disputatio.runtime import ConfigError, RuntimeConfig
from disputatio.verifier import GateSpec

# Снапшот ровно из примера [DESIGN-014]: ни `attachments`, ни `enabled` в
# нём нет. Он и есть предмет теста — так конфиг выглядит, когда его пишет
# человек, а не `render_toml`.
_SPEC_EXAMPLE = """[session]
id = "20260809-143012-a1b2"
mode = "develop"
base_commit = "9f1c2ab"

[task]
prompt = "Почини экспорт CSV"

[limits]
max_rounds = 5
max_total_tokens = 400000
max_wall_seconds = 3600
schema_retries = 2

[agents.author]
adapter = "claude_code"
model = "opus"

[agents.reviewer]
adapter = "claude_code"
model = "sonnet"

[[gates]]
name = "pytest"
cmd = "uv run pytest -q"
"""

_GATELESS = _SPEC_EXAMPLE.split("[[gates]]")[0]


def test_gate_without_enabled_is_enabled_exactly_as_gate_spec_defaults() -> None:
    """`[[gates]]` без `enabled` даёт включённый гейт — как у `GateSpec`.

    Сравнение идёт с дефолтом спеки гейтов, а не с литералом `True`:
    собственное мнение читателя конфига о том, включён ли гейт без флага,
    было бы вторым источником правды — и разошлось бы молча, в сторону
    непрогнанной проверки.
    """
    default = GateSpec(name="pytest", cmd="uv run pytest -q").enabled

    config = RuntimeConfig.from_toml(_SPEC_EXAMPLE)

    assert config.gates == (
        GateSpec(name="pytest", cmd="uv run pytest -q", enabled=default),
    )
    assert config.gates[0].enabled is default


def test_snapshot_without_gates_and_attachments_reads_as_empty_not_as_error() -> None:
    """Снапшота без `[[gates]]` и `attachments` хватает: пустые наборы.

    Сессия без гейтов законна ([REQ-010]) — `overall` складывается из
    пустого набора, и требовать хотя бы один гейт значило бы завести здесь
    второе мнение о §5. Задача без вложений законна тем более.
    """
    config = RuntimeConfig.from_toml(_GATELESS)

    assert config.gates == ()
    assert config.attachments == ()
    assert config.mode is Mode.DEVELOP
    assert config.base_commit == "9f1c2ab"


def test_gate_enabled_of_the_wrong_type_is_a_config_error() -> None:
    """`enabled = "yes"` — `ConfigError`, а не «истинная непустая строка».

    Питоновская истинность приняла бы и `"no"`, и `"false"` за включённый
    гейт: непустая строка истинна. Тип проверяется, а не приводится.
    """
    broken = _SPEC_EXAMPLE + 'enabled = "yes"\n'

    with pytest.raises(ConfigError):
        RuntimeConfig.from_toml(broken)


def test_attachments_of_the_wrong_element_type_is_a_config_error() -> None:
    """Число среди `attachments` — `ConfigError`, а не путь `42` в промпте.

    `TaskSpec.attachments` — пути для §6, и число, доехавшее до сборки
    промпта, упало бы много позже и совсем в другом месте.
    """
    broken = _SPEC_EXAMPLE.replace(
        'prompt = "Почини экспорт CSV"',
        'prompt = "Почини экспорт CSV"\nattachments = ["док.md", 42]',
        1,
    )

    with pytest.raises(ConfigError):
        RuntimeConfig.from_toml(broken)
