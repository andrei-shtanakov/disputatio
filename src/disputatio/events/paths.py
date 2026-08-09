"""Единая точка построения путей `.disputatio/` ([DESIGN §3], [REQ-002], [REQ-008]).

Все остальные модули `disputatio.events` строят пути только через функции
этого модуля — `NNN`-паддинг раунда и имя корневой директории сессии не
дублируются по местам.
"""

from pathlib import Path

SESSION_DIR_NAME = ".disputatio"


def session_dir(root: Path) -> Path:
    """Корневая директория сессии: `root/.disputatio`."""
    return root / SESSION_DIR_NAME


def session_json_path(root: Path) -> Path:
    """Путь к `session.json` сессии."""
    return session_dir(root) / "session.json"


def config_toml_path(root: Path) -> Path:
    """Путь к снапшоту конфигурации `config.toml`."""
    return session_dir(root) / "config.toml"


def events_jsonl_path(root: Path) -> Path:
    """Путь к журналу событий `events.jsonl`."""
    return session_dir(root) / "events.jsonl"


def rounds_dir(root: Path) -> Path:
    """Директория всех раундов `rounds/` — родитель `round_dir`."""
    return session_dir(root) / "rounds"


def round_dir(root: Path, round_no: int) -> Path:
    """Директория раунда `rounds/{round_no:03d}`."""
    return rounds_dir(root) / f"{round_no:03d}"


def result_dir(root: Path) -> Path:
    """Директория экспортированного результата `result/`."""
    return session_dir(root) / "result"
