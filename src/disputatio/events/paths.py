"""Единая точка построения путей `.disputatio/` ([DESIGN §3], [REQ-002], [REQ-008]).

Все остальные модули `disputatio.events` строят пути только через функции
этого модуля — `NNN`-паддинг раунда и имя корневой директории сессии не
дублируются по местам.

Корень здесь — `artifact_root`, журнал сессии, а не рабочий git-репозиторий
(SPEC-002 §4.1). До разделения это был один и тот же каталог, и имя `root`
не различало их; пайплайн кладёт несколько сессий под ОДИН репозиторий
(`pipelines/<slug>/sessions/<revision>/`), и с общим корнем все ревизии
конфликтовали бы за один `session.json`. Дефолт `artifact_root =
workspace_root` сохраняет прежнюю раскладку байт-в-байт — выбирает его
composition root, а не этот модуль: здесь известен ровно один корень, и
перепутать их негде по построению.
"""

from pathlib import Path

SESSION_DIR_NAME = ".disputatio"


def session_dir(artifact_root: Path) -> Path:
    """Корневая директория сессии: `artifact_root/.disputatio`."""
    return artifact_root / SESSION_DIR_NAME


def session_json_path(artifact_root: Path) -> Path:
    """Путь к `session.json` сессии."""
    return session_dir(artifact_root) / "session.json"


def config_toml_path(artifact_root: Path) -> Path:
    """Путь к снапшоту конфигурации `config.toml`."""
    return session_dir(artifact_root) / "config.toml"


def events_jsonl_path(artifact_root: Path) -> Path:
    """Путь к журналу событий `events.jsonl`."""
    return session_dir(artifact_root) / "events.jsonl"


def rounds_dir(artifact_root: Path) -> Path:
    """Директория всех раундов `rounds/` — родитель `round_dir`."""
    return session_dir(artifact_root) / "rounds"


def round_dir(artifact_root: Path, round_no: int) -> Path:
    """Директория раунда `rounds/{round_no:03d}`."""
    return rounds_dir(artifact_root) / f"{round_no:03d}"


def result_dir(artifact_root: Path) -> Path:
    """Директория экспортированного результата `result/`."""
    return session_dir(artifact_root) / "result"
