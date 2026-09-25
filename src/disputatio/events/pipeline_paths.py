"""Пути `.disputatio/pipelines/<slug>/`, которые пишет `events` (SPEC-002 §4.1).

Здесь — ровно то, что пишет сам `events`: манифест (`pipeline_store`) и
журнал (`pipeline_events`). Остальную раскладку пайплайна (ревизии сессий,
`adoptions/`, `result/`) строит `runtime.layout`, он же держит зеркало
корня; тот же приём, что для сессии (`paths` ↔ `runtime.layout`), а
расхождение двух копий краснит `tests/runtime/test_pipeline_layout_mirror.py`.

Корень тут — `workspace_root`
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
