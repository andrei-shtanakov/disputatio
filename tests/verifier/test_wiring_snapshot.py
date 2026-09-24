"""Снимок кода гейта wiring: отпечаток, чистота, чтение дерева (design §4).

`src_fingerprint` не зависит от диска — только от `HEAD:<src>`, поэтому
раунды, трогающие лишь документы, отпечаток не меняют. `dirty_src_paths`
покрывает три независимых источника грязи (`unstaged`, `staged`,
`untracked`) и подтверждает, что правки вне `src` и игнорируемые файлы в
`src` его не пачкают. `read_snapshot` обязан отдавать байты из git, а не с
диска — иначе незакоммиченная правка молча просочилась бы в анализ.
"""

import os
import subprocess
from pathlib import Path

import pytest

from disputatio.verifier.wiring_snapshot import (
    WiringInputError,
    dirty_src_paths,
    read_snapshot,
    src_fingerprint,
)


def _write(repo: Path, relpath: str, content: str) -> None:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def wiring_repo(tmp_git_repo: Path, git_run) -> Path:
    """Репозиторий фикстуры + каталог `src/` с одним закоммиченным модулем."""
    _write(tmp_git_repo, "src/pkg/mod.py", "VALUE = 1\n")
    git_run(tmp_git_repo, "add", "src/pkg/mod.py")
    git_run(
        tmp_git_repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "--quiet",
        "-m",
        "add src",
    )
    return tmp_git_repo


class TestSrcFingerprint:
    def test_equals_rev_parse_head_src(self, wiring_repo: Path, git_run) -> None:
        expected = git_run(wiring_repo, "rev-parse", "HEAD:src").strip()

        assert src_fingerprint(wiring_repo, "src") == expected

    def test_unchanged_by_docs_only_commit(self, wiring_repo: Path, git_run) -> None:
        before = src_fingerprint(wiring_repo, "src")
        _write(wiring_repo, "docs/x.md", "заметка\n")
        git_run(wiring_repo, "add", "docs/x.md")
        git_run(
            wiring_repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "--quiet",
            "-m",
            "docs only",
        )

        after = src_fingerprint(wiring_repo, "src")

        assert after == before

    def test_raises_for_directory_without_git(self, tmp_path: Path) -> None:
        with pytest.raises(WiringInputError):
            src_fingerprint(tmp_path, "src")

    def test_raises_for_repo_without_commits(self, tmp_path: Path, git_run) -> None:
        git_run(tmp_path, "init", "--quiet")

        with pytest.raises(WiringInputError):
            src_fingerprint(tmp_path, "src")

    def test_raises_for_missing_src_in_head(self, tmp_git_repo: Path) -> None:
        with pytest.raises(WiringInputError):
            src_fingerprint(tmp_git_repo, "src")


class TestDirtySrcPaths:
    def test_empty_when_clean(self, wiring_repo: Path) -> None:
        assert dirty_src_paths(wiring_repo, "src") == ()

    def test_nonempty_for_unstaged_edit(self, wiring_repo: Path) -> None:
        _write(wiring_repo, "src/pkg/mod.py", "VALUE = 2\n")

        assert dirty_src_paths(wiring_repo, "src") != ()

    def test_nonempty_for_staged_edit(self, wiring_repo: Path, git_run) -> None:
        _write(wiring_repo, "src/pkg/mod.py", "VALUE = 2\n")
        git_run(wiring_repo, "add", "src/pkg/mod.py")

        assert dirty_src_paths(wiring_repo, "src") != ()

    def test_nonempty_for_untracked_file(self, wiring_repo: Path) -> None:
        _write(wiring_repo, "src/pkg/new_module.py", "X = 1\n")

        assert dirty_src_paths(wiring_repo, "src") != ()

    def test_empty_for_edit_outside_src(self, wiring_repo: Path) -> None:
        _write(wiring_repo, "docs/notes.md", "правка вне src\n")

        assert dirty_src_paths(wiring_repo, "src") == ()

    def test_empty_for_ignored_file_in_src(self, wiring_repo: Path) -> None:
        _write(
            wiring_repo, ".gitignore", "__pycache__/\n.pytest_cache/\nsrc/ignored.py\n"
        )
        _write(wiring_repo, "src/ignored.py", "IGNORED = 1\n")

        assert dirty_src_paths(wiring_repo, "src") == ()


class TestReadSnapshot:
    def test_returns_exactly_py_files_of_head_src_tree(self, wiring_repo: Path) -> None:
        _write(wiring_repo, "src/pkg/readme.txt", "не питон\n")

        snapshot = read_snapshot(wiring_repo, "src")

        assert set(snapshot.files) == {"src/pkg/mod.py"}
        assert snapshot.files["src/pkg/mod.py"] == b"VALUE = 1\n"

    def test_tree_matches_fingerprint(self, wiring_repo: Path) -> None:
        snapshot = read_snapshot(wiring_repo, "src")

        assert snapshot.tree == src_fingerprint(wiring_repo, "src")

    def test_uncommitted_edit_not_reflected_in_bytes(self, wiring_repo: Path) -> None:
        _write(wiring_repo, "src/pkg/mod.py", "VALUE = 999\n")

        snapshot = read_snapshot(wiring_repo, "src")

        assert snapshot.files["src/pkg/mod.py"] == b"VALUE = 1\n"

    def test_raises_for_missing_src_in_head(self, tmp_git_repo: Path) -> None:
        with pytest.raises(WiringInputError):
            read_snapshot(tmp_git_repo, "src")

    def test_raises_for_symlink_py_in_src(self, wiring_repo: Path, git_run) -> None:
        """Символическая ссылка `.py` — не файл: путь вместо байт кода."""
        link = wiring_repo / "src" / "link.py"
        link.symlink_to(Path("../outside.py"))
        git_run(wiring_repo, "add", "src/link.py")
        git_run(
            wiring_repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "--quiet",
            "-m",
            "add py symlink",
        )

        with pytest.raises(WiringInputError, match="src/link.py"):
            read_snapshot(wiring_repo, "src")

    def test_ignores_symlink_non_py_in_src(self, wiring_repo: Path, git_run) -> None:
        """Не-`.py` символическая ссылка — игнорируется, как любой не-`.py` файл."""
        link = wiring_repo / "src" / "link.txt"
        link.symlink_to(Path("../outside.txt"))
        git_run(wiring_repo, "add", "src/link.txt")
        git_run(
            wiring_repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "--quiet",
            "-m",
            "add non-py symlink",
        )

        snapshot = read_snapshot(wiring_repo, "src")

        assert set(snapshot.files) == {"src/pkg/mod.py"}


class TestGitOptionalLocks:
    def test_dirty_src_paths_does_not_touch_index(
        self, wiring_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`status` без `GIT_OPTIONAL_LOCKS=0` вправе переписать `.git/index`.

        Замер 2026-09-24 (см. бриф задачи 1): у отслеживаемого файла в
        `src/` устаревает stat без смены содержимого — `git status` в
        таком дереве переписывает индекс, если не передать
        `GIT_OPTIONAL_LOCKS=0`. Родительское окружение намеренно выставляет
        `GIT_OPTIONAL_LOCKS=1`: хелпер обязан ПЕРЕОПРЕДЕЛИТЬ его на `"0"`,
        а не унаследовать случайно совпавшее значение.
        """
        monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
        target = wiring_repo / "src" / "pkg" / "mod.py"
        future = target.stat().st_mtime + 1
        os.utime(target, (future, future))
        index_path = wiring_repo / ".git" / "index"
        before = index_path.read_bytes()

        dirty_src_paths(wiring_repo, "src")

        after = index_path.read_bytes()
        assert after == before


class TestUnfitRepositoryInputs:
    """Непригодный корень, gitlink в `src`, отсутствие git — код `2` (§6)."""

    def test_read_snapshot_raises_for_subdirectory_root(
        self, wiring_repo: Path
    ) -> None:
        """`--root` — подкаталог: `ls-tree`/`status` смотрели бы не туда."""
        with pytest.raises(WiringInputError, match="не корень репозитория"):
            read_snapshot(wiring_repo / "src", "src")

    def test_dirty_src_paths_raises_for_subdirectory_root(
        self, wiring_repo: Path
    ) -> None:
        """Из подкаталога `status -- src` смотрел бы `src/src` — «чисто» молча."""
        _write(wiring_repo, "src/pkg/mod.py", "VALUE = 2\n")

        with pytest.raises(WiringInputError, match="не корень репозитория"):
            dirty_src_paths(wiring_repo / "src", "src")

    def test_raises_for_gitlink_under_src(self, wiring_repo: Path, git_run) -> None:
        """Подмодуль под `src` — непроанализированный код: отказ, а не пропуск."""
        head = git_run(wiring_repo, "rev-parse", "HEAD").strip()
        git_run(
            wiring_repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{head},src/sub",
        )
        git_run(
            wiring_repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "--quiet",
            "-m",
            "add gitlink",
        )

        with pytest.raises(WiringInputError, match="src/sub"):
            read_snapshot(wiring_repo, "src")

    def test_raises_for_undecodable_path_under_src(
        self, wiring_repo: Path, git_run
    ) -> None:
        """Путь не в UTF-8 — код 2, а не тихая склейка ключей снимка.

        С заменой невалидных байт `x\\xfe.py` и `x\\xff.py` дали бы один
        ключ, и один файл молча выпал бы из снимка. Дерево собирается
        plumbing-командами: файловая система (APFS) такое имя может не
        принять, а индекс git — примет.
        """
        blob = subprocess.run(
            ("git", "hash-object", "-w", "--stdin"),
            cwd=wiring_repo,
            input=b"VALUE = 2\n",
            capture_output=True,
            check=True,
        ).stdout.strip()
        records = b"".join(
            b"100644 blob " + blob + b"\tsrc/pkg/x" + tail + b".py\0"
            for tail in (b"\xfe", b"\xff")
        )
        subprocess.run(
            ("git", "update-index", "-z", "--index-info"),
            cwd=wiring_repo,
            input=records,
            check=True,
        )
        git_run(
            wiring_repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "--quiet",
            "-m",
            "add undecodable paths",
        )

        with pytest.raises(WiringInputError, match=r"x\\xf[ef]\.py"):
            read_snapshot(wiring_repo, "src")

    def test_dirty_undecodable_path_raises(self, wiring_repo: Path, git_run) -> None:
        """Сырые байты пути в `status` (`core.quotePath=false`) — код 2."""
        git_run(wiring_repo, "config", "core.quotePath", "false")
        blob = git_run(wiring_repo, "rev-parse", "HEAD:src/pkg/mod.py").strip()
        subprocess.run(
            ("git", "update-index", "-z", "--index-info"),
            cwd=wiring_repo,
            input=f"100644 blob {blob}\t".encode() + b"src/pkg/x\xfe.py\0",
            check=True,
        )

        with pytest.raises(WiringInputError, match=r"x\\xfe\.py"):
            dirty_src_paths(wiring_repo, "src")

    def test_missing_git_binary_raises_input_error(
        self, wiring_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Нет `git` в `PATH` — отказ окружения (код `2`), а не traceback."""
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))

        with pytest.raises(WiringInputError, match="git"):
            read_snapshot(wiring_repo, "src")
