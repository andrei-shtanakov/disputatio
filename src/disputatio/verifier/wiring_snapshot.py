"""Снимок кода гейта wiring: отпечаток, чистота, чтение дерева (design §4).

Правила гейта (задачи 2+) привязаны к конкретному коммиту, а не к рабочему
дереву: координаты строк имеют смысл только на зафиксированном коде.
Отпечаток — `git rev-parse HEAD:<src>`, sha дерева каталога `--src` в
`HEAD`; коммиты, меняющие только документы, его не меняют. Чистота —
`git status --porcelain --untracked-files=all -- <src>` обязан быть пуст
(учитывает staged, unstaged и untracked; игнорируемые файлы в анализ не
попадают вовсе). Источник анализа — объекты git этого дерева (`ls-tree`,
`cat-file`), а не файловая система: анализируется ровно то, что
зафиксировано отпечатком, а не то, что лежит на диске рядом.

Гейт read-only: ни один вызов не пишет в рабочее дерево, индекс или
`.git`. Все команды идут через единственный хелпер `_run_git`, который
переопределяет `GIT_OPTIONAL_LOCKS` на `"0"` — без этого `git status`
вправе переписать `.git/index`, освежив в нём stat-данные путей.
"""

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class WiringInputError(Exception):
    """Непригодные входы гейта: код 2 (§6)."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Снимок дерева `HEAD:<src>`: отпечаток и байты `.py`-файлов из git."""

    tree: str
    files: Mapping[str, bytes]


def src_fingerprint(repo_root: Path, src: str) -> str:
    """Отпечаток каталога `src` в `HEAD` — sha его дерева (§4).

    Эквивалент `git rev-parse HEAD:<src>`. Не git-репозиторий, отсутствие
    `HEAD` (репозиторий без коммитов) и отсутствие `src` в `HEAD` дают
    ненулевой код возврата git и здесь одинаково становятся
    `WiringInputError` — вызывающему коду достаточно одного вида отказа
    для всех «непригодных входов» (§6).
    """
    _require_top_level(repo_root)
    result = _run_git(repo_root, ("rev-parse", f"HEAD:{src}"))
    if result.returncode != 0:
        raise WiringInputError(
            f"не удалось получить отпечаток `HEAD:{src}` в {repo_root}: "
            f"{_diagnostic(result)}"
        )
    return _decode(result.stdout).strip()


def dirty_src_paths(repo_root: Path, src: str) -> tuple[str, ...]:
    """Пути `src`, из-за которых он «грязный»: staged, unstaged, untracked (§4).

    Эквивалент `git status --porcelain --untracked-files=all -- <src>`:
    пустой кортеж — дерево чистое. Игнорируемые файлы в вывод не попадают
    (флаг `--ignored` не передаётся), поэтому запись в игнорируемый файл
    внутри `src` чистоту не портит.
    """
    _require_top_level(repo_root)
    result = _run_git(
        repo_root,
        ("status", "--porcelain", "--untracked-files=all", "--", src),
    )
    if result.returncode != 0:
        raise WiringInputError(
            f"не удалось получить статус `{src}` в {repo_root}: {_diagnostic(result)}"
        )
    return tuple(_parse_porcelain_paths(_decode_path(result.stdout, src)))


def read_snapshot(repo_root: Path, src: str) -> Snapshot:
    """Снимок дерева `HEAD:<src>`: отпечаток + байты `.py`-файлов из git (§4).

    Анализируются все `.py`-файлы дерева, без исключений: пропущенный файл
    мог бы скрыть нарушение. Байты берутся из объектов git (`ls-tree` +
    `cat-file --batch`), а не с диска, поэтому незакоммиченная правка файла
    в выдачу не попадает. Отсутствие `src` в `HEAD` (как и любой другой
    непригодный вход) даёт `WiringInputError` тем же путём, что и
    `src_fingerprint`.
    """
    tree = src_fingerprint(repo_root, src)
    normalized_src = src.rstrip("/")
    entries = _list_python_blobs(repo_root, tree, normalized_src)
    contents = _read_blobs(repo_root, {sha for _, sha in entries})
    files = {f"{normalized_src}/{relpath}": contents[sha] for relpath, sha in entries}
    return Snapshot(tree=tree, files=files)


def _require_top_level(repo_root: Path) -> None:
    """`repo_root` обязан быть корнем рабочего дерева, а не его подкаталогом.

    Из подкаталога `ls-tree` отдаёт пути относительно него, а pathspec
    `status -- <src>` читается от него же, тогда как `HEAD:<src>` — от
    корня: снимок вышел бы пустым, а грязь `src` — невидимой. Непустой
    `git rev-parse --show-prefix` — `WiringInputError` (код 2).
    """
    result = _run_git(repo_root, ("rev-parse", "--show-prefix"))
    if result.returncode != 0:
        raise WiringInputError(
            f"--root {repo_root}: не git-репозиторий: {_diagnostic(result)}"
        )
    prefix = _decode(result.stdout).strip()
    if prefix:
        raise WiringInputError(
            f"--root {repo_root} не корень репозитория (подкаталог {prefix!r})"
        )


def _parse_porcelain_paths(output: str) -> list[str]:
    """Пути из `git status --porcelain`: строка вида `XY путь` или `XY a -> b`.

    Первые три символа строки — двухбуквенный статус и разделяющий пробел
    (формат porcelain v1), дальше идёт путь; для переименования вместо
    одного пути идёт `старый -> новый`, и берётся новый.
    """
    paths: list[str] = []
    for line in output.splitlines():
        if not line:
            continue
        path_field = line[3:]
        _, _, renamed_to = path_field.partition(" -> ")
        paths.append(renamed_to or path_field)
    return paths


_SYMLINK_MODE = b"120000"


def _list_python_blobs(repo_root: Path, tree: str, src: str) -> list[tuple[str, str]]:
    """Список (путь-от-`src`, sha) `.py`-блобов дерева `tree` через `ls-tree -r -z`.

    `-z`: записи разделены `NUL`, а не переводом строки, поэтому путь с
    экзотическими символами не режется на части. Разбор — по сырым
    байтам: `\\t` отделяет метаданные от пути и в имени файла появиться не
    может (это зарезервированный разделитель git).

    Символическая ссылка — тоже объект типа `blob` (режим `120000`), но
    её содержимое — строка пути цели, а не код. `.py`-символлинк отдать
    как модуль значило бы подсунуть анализатору путь вместо байт файла,
    поэтому такая запись — непригодный вход (`WiringInputError`), а не
    тихо пропущенный файл: пропуск мог бы скрыть конструктор за
    подменённым путём. `src` нужен только для полного пути в сообщении
    об ошибке — сам список остаётся путями относительно `tree`.
    Не-`.py` символлинк такому правилу не подчиняется и игнорируется, как
    любой другой не-`.py` файл дерева.

    Подмодуль (gitlink, тип `commit`, режим `160000`) `ls-tree -r` не
    раскрывает: его код в снимок не попал бы, и нарушение в нём прошло бы
    молча. Поэтому gitlink под `src` — тоже `WiringInputError`.
    """
    result = _run_git(repo_root, ("ls-tree", "-r", "-z", tree))
    if result.returncode != 0:
        raise WiringInputError(
            f"не удалось прочитать дерево {tree} в {repo_root}: {_diagnostic(result)}"
        )
    entries: list[tuple[str, str]] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        meta, _, path_bytes = record.partition(b"\t")
        mode, obj_type, sha = meta.split()
        path = _decode_path(path_bytes, src)
        if obj_type == b"commit":
            raise WiringInputError(
                f"подмодуль (gitlink) в дереве не анализируется: {src}/{path}"
            )
        if obj_type != b"blob":
            continue
        if not path.endswith(".py"):
            continue
        if mode == _SYMLINK_MODE:
            raise WiringInputError(
                f"символическая ссылка вместо файла `.py` в дереве: {src}/{path}"
            )
        entries.append((path, sha.decode("ascii")))
    return entries


def _read_blobs(repo_root: Path, shas: set[str]) -> dict[str, bytes]:
    """Байты блобов по их sha одним вызовом `git cat-file --batch`.

    Один процесс на весь снимок вместо одного на файл: у дерева может
    быть много модулей, а `cat-file --batch` читает их все за один обмен
    по stdin/stdout.
    """
    if not shas:
        return {}
    ordered = sorted(shas)
    stdin_payload = ("\n".join(ordered) + "\n").encode("ascii")
    result = _run_git(repo_root, ("cat-file", "--batch"), input_bytes=stdin_payload)
    if result.returncode != 0:
        raise WiringInputError(
            f"не удалось прочитать блобы в {repo_root}: {_diagnostic(result)}"
        )
    return _parse_batch_output(result.stdout, ordered)


def _parse_batch_output(output: bytes, shas: Sequence[str]) -> dict[str, bytes]:
    """Разбирает вывод `git cat-file --batch`: заголовок `<sha> blob <size>` + байты.

    Формат — построчный текстовый заголовок, затем ровно `size` байт
    содержимого и завершающий `\\n`, повторено для каждого запрошенного
    sha в том же порядке, в котором они поданы на вход.
    """
    contents: dict[str, bytes] = {}
    pos = 0
    for sha in shas:
        newline = output.index(b"\n", pos)
        header = output[pos:newline].decode("ascii", errors="replace")
        pos = newline + 1
        fields = header.split()
        if len(fields) != 3:
            raise WiringInputError(
                f"неожиданный заголовок `git cat-file --batch`: {header!r} "
                f"(ожидался sha {sha})"
            )
        header_sha, _obj_type, size_str = fields
        size = int(size_str)
        contents[header_sha] = output[pos : pos + size]
        pos += size + 1  # завершающий '\n' после содержимого блоба
    return contents


def _run_git(
    repo_root: Path,
    args: Sequence[str],
    *,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Единственная точка запуска git пакета: без shell, с `GIT_OPTIONAL_LOCKS=0`.

    Окружение переопределяет `GIT_OPTIONAL_LOCKS`, а не расширяет
    унаследованное вслепую: случайно совпавшее родительское значение (в
    т.ч. уже выставленная `"1"`) не должна маскировать отсутствие
    переопределения. Без `GIT_OPTIONAL_LOCKS=0` `git status` вправе
    переписать `.git/index`, освежив в нём stat-данные путей, — а гейт
    обязан быть read-only (§4).

    `OSError` запуска (нет бинаря `git`, недоступный `cwd`) — отказ
    окружения, код 2 (`WiringInputError`), а не traceback.
    """
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        return subprocess.run(
            ("git", *args),
            shell=False,
            cwd=repo_root,
            check=False,
            capture_output=True,
            input=input_bytes,
            env=env,
        )
    except OSError as exc:
        raise WiringInputError(
            f"не удалось запустить git в {repo_root}: {exc}"
        ) from exc


def _decode_path(data: bytes, src: str) -> str:
    """Строгий UTF-8 для путей: недекодируемый путь — `WiringInputError` (код 2).

    Замена невалидных байт склеила бы разные пути (`x\\xfe.py` и
    `x\\xff.py` — оба `x\\ufffd.py`) в один ключ снимка, и один файл молча
    выпал бы из анализа. В сообщении — `repr` сырых байт: их нечем честно
    напечатать иначе.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WiringInputError(
            f"путь под {src} не декодируется как UTF-8: {data!r}"
        ) from exc


def _decode(data: bytes) -> str:
    """UTF-8 с заменой невалидных байт — для текстового вывода git (sha, статус)."""
    return data.decode("utf-8", errors="replace")


def _diagnostic(result: subprocess.CompletedProcess[bytes]) -> str:
    """Голова диагностики git: stderr, а при пустом stderr — stdout."""
    raw = result.stderr or result.stdout or b""
    return _decode(raw).strip()
