"""Единая точка построения путей `.disputatio/pipelines/<slug>/` (SPEC-002 §4.1).

Тот же принцип, что и у `paths.py` для сессии: раскладка каталога пайплайна
не дублируется по местам, а живёт здесь одна. Корень тут — `workspace_root`
(git-репозиторий), а не `artifact_root`: каталог пайплайна лежит в
репозитории, а `artifact_root` каждой ревизии сессии — уже внутри него
(`sessions/<revision>/`, §4.1).

Валидация слага — тоже здесь, потому что слаг попадает прямо в путь: чужой
символ означал бы выход за каталог пайплайна (`../`) или машинно-зависимое
имя. Грамматика `[a-z0-9][a-z0-9._-]{0,63}` не даёт ни того, ни другого:
первый символ — буква/цифра, точки и слеши-разделители в теле исключены.
"""

import re
from pathlib import Path
from typing import Final

from disputatio.events.paths import SESSION_DIR_NAME

PIPELINES_DIR_NAME: Final = "pipelines"
MANIFEST_FILE_NAME: Final = "pipeline.json"

_SLUG_RE: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def validate_slug(slug: str) -> str:
    """Возвращает `slug`, если он соответствует грамматике §4.1, иначе `ValueError`.

    `fullmatch` — не `match`: без него `pipe/../..` прошёл бы по префиксу.
    """
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"слаг пайплайна обязан соответствовать [a-z0-9][a-z0-9._-]{{0,63}}: "
            f"{slug!r}"
        )
    return slug


def pipelines_dir(workspace_root: Path) -> Path:
    """Общий каталог всех пайплайнов: `workspace_root/.disputatio/pipelines`."""
    return workspace_root / SESSION_DIR_NAME / PIPELINES_DIR_NAME


def pipeline_dir(workspace_root: Path, slug: str) -> Path:
    """Каталог одного пайплайна `pipelines/<slug>` — корень всех остальных путей."""
    return pipelines_dir(workspace_root) / validate_slug(slug)


def manifest_path(workspace_root: Path, slug: str) -> Path:
    """Путь к манифесту `pipeline.json` (§4.2)."""
    return pipeline_dir(workspace_root, slug) / MANIFEST_FILE_NAME


def events_path(workspace_root: Path, slug: str) -> Path:
    """Путь к журналу событий пайплайна `events.jsonl` (§4.1)."""
    return pipeline_dir(workspace_root, slug) / "events.jsonl"


def sessions_dir(workspace_root: Path, slug: str) -> Path:
    """Каталог ревизий сессий `sessions/` — родитель `session_artifact_root`."""
    return pipeline_dir(workspace_root, slug) / "sessions"


def session_artifact_root(workspace_root: Path, slug: str, revision: str) -> Path:
    """`artifact_root` одной ревизии: `sessions/<revision>` (§4.1).

    Существует, чтобы вызывающий не склеивал имя ревизии с `sessions_dir`
    руками: тогда знание о раскладке разъехалось бы по пакетам, и правило
    «пути строятся только здесь» перестало бы держаться.
    """
    return sessions_dir(workspace_root, slug) / revision


def adoptions_dir(workspace_root: Path, slug: str) -> Path:
    """Каталог патчей принятых внешних правок `adoptions/` (§3.1)."""
    return pipeline_dir(workspace_root, slug) / "adoptions"


def result_dir(workspace_root: Path, slug: str) -> Path:
    """Каталог экспорта пайплайна `result/` (§8.2)."""
    return pipeline_dir(workspace_root, slug) / "result"
