"""Инициализация `.disputatio/` и снапшот конфига ([DESIGN-002], [REQ-002], [REQ-003]).

Модуль отвечает только за директории сессии и за атомарную запись готового
TOML-текста. Содержимое `config.toml` для этого слоя opaque: рендеринг TOML
из структуры конфигурации — обязанность вызывающей стороны ([ADR-002]).
"""

from pathlib import Path

from disputatio.events.atomic import atomic_write
from disputatio.events.paths import (
    config_toml_path,
    result_dir,
    rounds_dir,
    session_dir,
)


def bootstrap_session(artifact_root: Path) -> Path:
    """Создаёт `.disputatio/{rounds,result}/` в журнале сессии и возвращает его.

    Корень — `artifact_root` (SPEC-002 §4.1), поэтому каталог сессии
    заводится там, где она живёт, а не в рабочем репозитории: у пайплайна это
    разные места, и `mkdir` по рабочему корню создал бы общий журнал на все
    ревизии.

    Идемпотентна по построению (`mkdir(parents=True, exist_ok=True)`):
    повторный вызов не бросает и не трогает существующие файлы — в частности,
    `session.json` не читается и не пишется этой функцией ([REQ-002]).
    """
    rounds_dir(artifact_root).mkdir(parents=True, exist_ok=True)
    result_dir(artifact_root).mkdir(parents=True, exist_ok=True)
    return session_dir(artifact_root)


def write_config_snapshot(artifact_root: Path, toml_text: str) -> None:
    """Атомарно пишет `.disputatio/config.toml` из готовой TOML-строки.

    Предусловие: `bootstrap_session(artifact_root)` уже вызван —
    `atomic_write` кладёт временный файл рядом с целевым, поэтому без
    `.disputatio/` вызов упадёт `FileNotFoundError` (директорию эта функция не
    создаёт: bootstrap — единственная точка `mkdir`, [REQ-002]).

    Снапшот пишется один раз при старте сессии, чтобы конфигурация не зависела
    от последующих правок внешнего конфиг-файла (SPEC-001 §3). Повторный вызов
    перезаписывает файл целиком — store перезапись не запрещает ([REQ-003]).
    """
    atomic_write(config_toml_path(artifact_root), toml_text)
