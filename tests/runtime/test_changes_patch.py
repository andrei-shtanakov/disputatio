"""`changes.patch` — дифф автора относительно HEAD ([REQ-013], [DESIGN-013]).

[TASK-005]. Тест пинит не «метод вернул строку», а свойства, без которых
`changes.patch` перестаёт быть однозначным предметом ревью:

* правка tracked-файла приходит в unified-формате — ревьюер читает
  `--- a/… / +++ b/… / @@`, а не сводку «файл изменился»;
* **созданный** автором файл в дифф попадает: `git diff HEAD` untracked-файлов
  не видит вовсе, поэтому перед диффом обязателен `git add -N`
  (intent-to-add) — без него новый модуль автора исчез бы из ревью бесследно;
* дифф считается от `HEAD`, а не от индекса: работа автора, успевшая попасть
  в индекс (`git add`), остаётся в патче — голый `git diff` её потерял бы;
* чистое дерево даёт **пустую строку**, а не исключение: режим `analyze`
  правок не делает, и это не ошибка шага ([REQ-013]);
* невалидные utf-8 байты вызов не роняют — декодирование идёт с
  `errors="replace"`, иначе бинарник автора обрушил бы весь раунд;
* `.disputatio/` в дифф не попадает и в индекс через intent-to-add тоже:
  служебный каталог сессии — не работа автора;
* окружение герметично и на этом вызове: унаследованный `GIT_DIR` увёл бы
  дифф в чужой репозиторий ([DESIGN §4.2]).

`GitCli.diff_head` до реализации — `NotImplementedError`-заглушка
[TASK-004]. Вызов идёт через `_diff_head`, который переводит её в
`AssertionError`: red-чекпоинт обязан быть падением assertion'ом, а не
всплывшим наружу исключением заглушки.
"""

import subprocess
from pathlib import Path

import pytest

from disputatio.runtime import GitCli

_SESSION_DIR = ".disputatio"

_AUTHOR_LINE = "строка, добавленная автором"

# Байты, невалидные в utf-8 и без NUL: git считает такой файл текстовым и
# выводит их в дифф как есть. Настоящий бинарник (`_BINARY_BLOB`, с NUL)
# git сворачивает в строку «Binary files … differ» и до декодирования
# проблемных байтов дело не доходит — поэтому нужны оба.
_LATIN1_BYTES = b"caf\xe9 bez NUL\n"
_BINARY_BLOB = b"PK\x00\x01\xff\xfe\x00\x7f"

# Внешние переменные расположения и конфига: при абсолютном `GIT_DIR`
# команда отработает в ЧУЖОМ репозитории (`cwd` его не перебивает), а
# `GIT_CONFIG_COUNT` подсовывает конфиг мимо `.git/config`.
_HOSTILE_ENV = {
    "GIT_DIR": "/nonexistent/hostile/.git",
    "GIT_WORK_TREE": "/nonexistent/hostile",
    "GIT_INDEX_FILE": "/nonexistent/hostile/index",
    "GIT_OBJECT_DIRECTORY": "/nonexistent/hostile/objects",
    "GIT_CONFIG_COUNT": "1",
}


def _diff_head(root: Path) -> str:
    """`GitCli(root).diff_head()`; заглушка [TASK-004] — `AssertionError`."""
    try:
        return GitCli(root).diff_head()
    except NotImplementedError as exc:
        raise AssertionError(
            f"GitCli.diff_head — всё ещё заглушка [DESIGN-013]: {exc}"
        ) from exc


def _git(workdir: Path, *args: str) -> str:
    """Вспомогательная git-команда теста; ненулевой код — `CalledProcessError`."""
    completed = subprocess.run(
        ["git", *args],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _append_author_line(root: Path) -> None:
    """Дописывает маркерную строку автора в tracked `README.md`."""
    readme = root / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + _AUTHOR_LINE + "\n",
        encoding="utf-8",
    )


def test_modified_tracked_file_appears_in_unified_diff(git_repo: Path) -> None:
    """Правка tracked-файла приходит ревьюеру unified-диффом ([REQ-013])."""
    _append_author_line(git_repo)

    diff = _diff_head(git_repo)

    assert "diff --git a/README.md b/README.md" in diff, (
        f"изменённый tracked-файл не попал в дифф:\n{diff!r}"
    )
    assert "--- a/README.md" in diff, f"нет старой стороны unified-диффа:\n{diff!r}"
    assert "+++ b/README.md" in diff, f"нет новой стороны unified-диффа:\n{diff!r}"
    assert "@@" in diff, f"нет заголовка ханка — формат не unified:\n{diff!r}"
    assert f"+{_AUTHOR_LINE}" in diff, (
        f"добавленная автором строка не видна в ханке:\n{diff!r}"
    )


def test_new_untracked_file_appears_in_diff(git_repo: Path) -> None:
    """Созданный автором файл в диффе есть — это работа `git add -N`."""
    package = git_repo / "pkg"
    package.mkdir()
    (package / "created.py").write_text(f'VALUE = "{_AUTHOR_LINE}"\n', encoding="utf-8")

    diff = _diff_head(git_repo)

    assert "b/pkg/created.py" in diff, (
        "созданный автором файл потерян: `git diff HEAD` untracked-файлов не "
        f"видит, перед диффом нужен `git add -N` (intent-to-add):\n{diff!r}"
    )
    assert "new file mode" in diff, f"файл в диффе не помечен как новый:\n{diff!r}"
    assert f'+VALUE = "{_AUTHOR_LINE}"' in diff, (
        f"содержимое нового файла в дифф не попало:\n{diff!r}"
    )


def test_staged_work_is_diffed_against_head(git_repo: Path) -> None:
    """База сравнения — `HEAD`: работа автора из индекса остаётся в патче."""
    _append_author_line(git_repo)
    _git(git_repo, "add", "README.md")

    diff = _diff_head(git_repo)

    assert f"+{_AUTHOR_LINE}" in diff, (
        "правка, попавшая в индекс, исчезла из патча — дифф считается от "
        f"индекса, а не от HEAD:\n{diff!r}"
    )


def test_clean_tree_yields_empty_string(git_repo: Path) -> None:
    """Автор ничего не изменил → пустая строка, а не исключение ([REQ-013])."""
    diff = _diff_head(git_repo)

    assert diff == "", (
        "режим analyze без правок обязан давать ровно пустую строку — "
        f"артефакт пишется всегда, в том числе пустым:\n{diff!r}"
    )


def test_undecodable_bytes_do_not_break_the_call(git_repo: Path) -> None:
    """Невалидные utf-8 байты заменяются, а не роняют вызов и не пропадают."""
    (git_repo / "blob.bin").write_bytes(_BINARY_BLOB)
    (git_repo / "latin1.txt").write_bytes(_LATIN1_BYTES)

    diff = _diff_head(git_repo)

    assert "b/blob.bin" in diff, f"бинарный файл автора в диффе не назван:\n{diff!r}"
    assert "�" in diff, (
        "невалидные utf-8 байты не заменены на U+FFFD: нужен "
        'errors="replace" — со "strict" вызов упал бы UnicodeDecodeError, '
        f'а с "ignore" байты исчезли бы молча:\n{diff!r}'
    )


def test_session_dir_never_reaches_the_diff(git_repo: Path) -> None:
    """`.disputatio/` — не работа автора: ни в диффе, ни в индексе."""
    session = git_repo / _SESSION_DIR
    session.mkdir()
    (session / "session.json").write_text('{"state": "PROPOSING"}\n', encoding="utf-8")
    (session / "events.jsonl").write_text(
        '{"type": "state_change"}\n', encoding="utf-8"
    )
    (git_repo / "authored.py").write_text("VALUE = 1\n", encoding="utf-8")

    diff = _diff_head(git_repo)

    assert "b/authored.py" in diff, (
        f"проверка вакуумна: дифф не увидел даже работу автора:\n{diff!r}"
    )
    assert _SESSION_DIR not in diff, (
        f"служебный каталог сессии попал в предмет ревью:\n{diff!r}"
    )
    status = _git(git_repo, "status", "--porcelain")
    assert f"?? {_SESSION_DIR}/" in status, (
        f"`.disputatio/` больше не untracked — intent-to-add затянул её в "
        f"индекс; исключать каталог нужно и в `git add -N`:\n{status}"
    )


def test_diff_stays_hermetic_under_hostile_env(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Унаследованные `GIT_*` дифф не уводят: окружение вызова собирается."""
    _append_author_line(git_repo)
    for var, value in _HOSTILE_ENV.items():
        monkeypatch.setenv(var, value)

    diff = _diff_head(git_repo)

    assert f"+{_AUTHOR_LINE}" in diff, (
        "враждебные GIT_* перебили расположение репозитория: дифф снят не с "
        f"рабочей директории сессии:\n{diff!r}"
    )
