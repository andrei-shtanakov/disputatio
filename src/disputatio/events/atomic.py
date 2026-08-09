"""Атомарная запись файла: temp-file + rename ([DESIGN-001], [REQ-001], [ADR-001]).

Единственный примитив атомарности, на котором строятся все пишущие операции
`disputatio.events` (`session.json`, `config.toml`, строки `events.jsonl`,
`rounds/NNN/*`, `result/*`).
"""

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str | bytes, *, encoding: str = "utf-8") -> None:
    """Атомарно записывает `content` в `path`.

    `tempfile.mkstemp` создаёт временный файл в той же директории, что и
    `path` — `os.replace` атомарен только внутри одной файловой системы
    ([DESIGN-001], NFR-001). Порядок: запись всего содержимого → `fsync` →
    `close` → `os.replace`. `path` либо содержит новое содержимое целиком,
    либо прежнее — промежуточного состояния снаружи не видно. При
    исключении между `mkstemp` и `replace` временный файл остаётся рядом
    с `path`; вызывающая сторона его не подчищает ([ADR-001]).
    """
    data = content.encode(encoding) if isinstance(content, str) else content
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        while data:
            written = os.write(fd, data)
            data = data[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_name, path)
