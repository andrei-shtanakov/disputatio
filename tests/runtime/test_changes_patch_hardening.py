"""`changes.patch`: то, что дифф не должен уметь ([REQ-013], [DESIGN-013]).

Ревью [TASK-005]. Файл рядом с байт-locked `test_changes_patch.py` и
закрывает дыры, которые пережили полный suite: байт-locked тест проверяет
дифф на репозитории с пустым `.git/config`, а `_env` гасит только
системный и глобальный конфиг — **локальный** остаётся в силе. Между тем
именно он лежит внутри рабочей директории, к которой у автора есть запись,
и в сам патч не попадает: `.git/` не diff'ится, поэтому подмена невидима.

* `diff.external` заменяет весь вывод `git diff` на вывод произвольной
  программы — и запускает её. Ревьюер получил бы вместо патча что угодно,
  вплоть до пустоты, и одобрил бы «пустой» раунд.
* `color.ui = always` вклеивает ANSI-escape'ы в каждую строку: патч
  перестаёт быть применимым и читается как мусор.
* `diff.noprefix`/`diff.mnemonicPrefix` убирают или переименовывают
  `a/`…`b/` — unified-заголовок, на который смотрит ревьюер, ломается.
* `diff.relative` вырезает из диффа всё, что лежит вне `cwd`.

Вторая дыра — привязка pathspec'а. `:(exclude,top)` считает `.disputatio`
от корня **репозитория**, а каталог сессии лежит в `root`. Совпадают они
только когда `root` и есть toplevel; `preflight` этого не требует
(`rev-parse --git-dir` успешен в любом подкаталоге), и для `root`
внутри репозитория служебный каталог утекал и в патч, и в индекс.
"""

import subprocess
from pathlib import Path

import pytest

from disputatio.runtime import GitCli

_SESSION_DIR = ".disputatio"

_AUTHOR_LINE = "строка, добавленная автором"

_EXTERNAL_MARKER = "ВЫВОД ЧУЖОЙ ПРОГРАММЫ ВМЕСТО ПАТЧА"


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


def test_local_diff_external_does_not_replace_the_patch(
    git_repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """`diff.external` из `.git/config` не подменяет предмет ревью.

    Скрипт лежит ВНЕ репозитория: внутри он сам стал бы новым файлом
    автора, и маркер приехал бы в патч собственным содержимым — проверка
    провалилась бы даже на исправном вызове.
    """
    external = tmp_path_factory.mktemp("external-diff") / "external.sh"
    external.write_text(f'#!/bin/sh\necho "{_EXTERNAL_MARKER}"\n', encoding="utf-8")
    external.chmod(0o755)
    _git(git_repo, "config", "diff.external", str(external))
    _append_author_line(git_repo)

    diff = GitCli(git_repo).diff_head()

    assert _EXTERNAL_MARKER not in diff, (
        "патч собрала чужая программа: `git diff` уважает локальный "
        f"`diff.external`, вызову нужен `--no-ext-diff`:\n{diff!r}"
    )
    assert f"+{_AUTHOR_LINE}" in diff, (
        f"работа автора из патча исчезла вместе с внешним диффом:\n{diff!r}"
    )


def test_local_textconv_does_not_hide_the_authors_change(
    git_repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """textconv не подменяет содержимое: правка автора видна как есть.

    Драйвер из `.gitattributes` + `diff.<driver>.textconv` показывает обеим
    сторонам пересказ вместо содержимого. Когда пересказ совпал, файл из
    диффа исчезает целиком — ревьюер не видит правки вовсе.
    """
    textconv = tmp_path_factory.mktemp("textconv") / "conv.sh"
    textconv.write_text("#!/bin/sh\necho одно и то же\n", encoding="utf-8")
    textconv.chmod(0o755)
    _git(git_repo, "config", "diff.fake.textconv", str(textconv))
    (git_repo / ".gitattributes").write_text("README.md diff=fake\n", encoding="utf-8")
    _append_author_line(git_repo)

    diff = GitCli(git_repo).diff_head()

    assert f"+{_AUTHOR_LINE}" in diff, (
        "правка автора исчезла из патча: обе стороны прошли через textconv "
        f"и совпали, вызову нужен `--no-textconv`:\n{diff!r}"
    )


def test_local_color_config_does_not_leak_ansi_into_the_patch(git_repo: Path) -> None:
    """`color.ui = always` не вклеивает escape-последовательности в патч."""
    _git(git_repo, "config", "color.ui", "always")
    _append_author_line(git_repo)

    diff = GitCli(git_repo).diff_head()

    assert "\x1b[" not in diff, (
        "в патче ANSI-escape'ы: локальный `color.ui = always` сильнее "
        f"автоопределения tty, вызову нужен `--no-color`:\n{diff!r}"
    )
    assert f"+{_AUTHOR_LINE}" in diff, (
        f"проверка вакуумна: дифф не увидел работу автора:\n{diff!r}"
    )


def test_local_prefix_config_does_not_break_unified_headers(git_repo: Path) -> None:
    """`diff.noprefix`/`diff.mnemonicPrefix` не ломают `a/`…`b/`."""
    _git(git_repo, "config", "diff.noprefix", "true")
    _git(git_repo, "config", "diff.mnemonicPrefix", "true")
    _append_author_line(git_repo)

    diff = GitCli(git_repo).diff_head()

    assert "--- a/README.md" in diff, (
        "старая сторона потеряла префикс `a/`: локальный `diff.noprefix` "
        f"переписал unified-заголовок:\n{diff!r}"
    )
    assert "+++ b/README.md" in diff, (
        "новая сторона потеряла префикс `b/`: вызову нужны явные "
        f"`--src-prefix`/`--dst-prefix`:\n{diff!r}"
    )


def test_session_dir_excluded_when_root_is_a_subdirectory(git_repo: Path) -> None:
    """`root` внутри репозитория: `.disputatio/` не в патче и не в индексе.

    `preflight` не требует, чтобы `root` был toplevel'ом, поэтому pathspec
    обязан считать каталог сессии от `root`, а не от корня репозитория.
    Заодно пиньется охват: правка вне `root` — тоже работа автора (дерево
    было чисто на pre-flight), и `diff.relative` не вправе её прятать.
    """
    root = git_repo / "workdir"
    root.mkdir()
    (root / "tracked.py").write_text("VALUE = 0\n", encoding="utf-8")
    _git(git_repo, "add", "workdir/tracked.py")
    _git(git_repo, "commit", "--quiet", "-m", "workdir")
    _git(git_repo, "config", "diff.relative", "true")
    session = root / _SESSION_DIR
    session.mkdir()
    (session / "session.json").write_text('{"state": "PROPOSING"}\n', encoding="utf-8")
    (root / "authored.py").write_text("VALUE = 1\n", encoding="utf-8")
    _append_author_line(git_repo)

    diff = GitCli(root).diff_head()

    assert "b/workdir/authored.py" in diff, (
        f"проверка вакуумна: дифф не увидел даже работу автора:\n{diff!r}"
    )
    assert _SESSION_DIR not in diff, (
        "каталог сессии утёк в патч: `:(exclude,top)` считает путь от корня "
        f"репозитория, а `.disputatio/` лежит в root:\n{diff!r}"
    )
    assert f"+{_AUTHOR_LINE}" in diff, (
        "правка вне root пропала из патча: локальный `diff.relative` вырезал "
        f"всё за пределами cwd, вызову нужен `--no-relative`:\n{diff!r}"
    )
    status = _git(git_repo, "status", "--porcelain")
    assert f"?? workdir/{_SESSION_DIR}/" in status, (
        f"intent-to-add затянул каталог сессии в индекс:\n{status}"
    )
