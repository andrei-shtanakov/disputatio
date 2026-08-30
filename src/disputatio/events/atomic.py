"""Атомарная запись файла: temp-file + rename ([DESIGN-001], [REQ-001], [ADR-001]).

Единственный примитив атомарности, на котором строятся пишущие операции
`disputatio.events` (`session.json`, `config.toml`, `rounds/NNN/*`,
`result/*`). Исключение — `events.jsonl`: `JsonlEventSink.emit` пишет
чистым `O_APPEND` без перезаписи файла ([DESIGN-004], [ADR-003]), поэтому
через `atomic_write` строки журнала НЕ идут.

Граница обещания [REQ-001] — ОДИН файл. На набор из нескольких файлов
(`result/*` + `manifest.json`) она не распространяется: транзакции на
несколько путей файловая система не даёт, и консистентность набора держит
не этот примитив, а порядок записи вокруг commit marker'а — SPEC-002 §8.2 и
докстринг `export.write_result`.
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

    Побочный эффект `mkstemp` + `os.replace`: итоговый файл наследует режим
    `0600` временного, а не umask и не прежние права `path`.
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
