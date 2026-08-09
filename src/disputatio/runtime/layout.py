"""Read-side зеркало раскладки `.disputatio/` (ADR-002, [REQ-003], [REQ-007]).

Оркестратор обязан **читать** артефакты прошлых раундов, а `disputatio.events`
экспортирует только писателей: `paths` объявлен внутренней деталью раскладки
и в публичный `__init__` не входит, а §4.2 запрещает импорт подмодулей чужих
пакетов. Поэтому здесь — собственный, строго read-only набор функций путей.

Дублирование раскладки не декларативно, а проверяемо: `write_round_artifact`
**возвращает** записанный `Path`, и тест шага требует равенства этого пути
пути отсюда для каждого артефакта, который runtime реально пишет. Разъедься
две раскладки — красным станет шаг, а не только этот модуль.

Модуль чист: только конкатенация путей, никакого I/O. Ничего не создаёт и не
проверяет существование — «нет файла» решается вызывающим, потому что для
`changes.patch` это нормальный раунд без правок, а для `review.json` —
оборванная сессия.
"""

from pathlib import Path
from typing import Final

SESSION_DIR_NAME: Final = ".disputatio"
ROUNDS_DIR_NAME: Final = "rounds"

CONFIG_TOML_NAME: Final = "config.toml"

PROPOSAL_NAME: Final = "proposal.md"
CHANGES_PATCH_NAME: Final = "changes.patch"
VERIFICATION_NAME: Final = "verification.json"
REVIEW_NAME: Final = "review.json"
DECISION_NAME: Final = "decision.json"


def session_dir(root: Path) -> Path:
    """Корневая директория сессии: `root/.disputatio`."""
    return root / SESSION_DIR_NAME


def config_toml(root: Path) -> Path:
    """Снапшот конфига сессии `config.toml` ([DESIGN-014], [REQ-014]).

    Read-side половина `events.write_config_snapshot`: писателю путь строит
    его собственная раскладка, а resume читает файл отсюда — импортировать
    приватный `events.paths` §4.2 запрещает.
    """
    return session_dir(root) / CONFIG_TOML_NAME


def rounds_dir(root: Path) -> Path:
    """Директория всех раундов `rounds/`."""
    return session_dir(root) / ROUNDS_DIR_NAME


def round_dir(root: Path, round_no: int) -> Path:
    """Директория раунда `rounds/NNN` — паддинг тот же, что у писателя."""
    return rounds_dir(root) / f"{round_no:03d}"


def round_artifact(root: Path, round_no: int, name: str) -> Path:
    """Путь артефакта `name` внутри раунда `round_no`.

    Имя не валидируется: проверку «простое имя файла» делает писатель, у
    которого от неё зависит инвариант I3, а read-side дублировать чужой
    инвариант не вправе — разъехавшись, две проверки дали бы два разных
    ответа на один вопрос.
    """
    return round_dir(root, round_no) / name
