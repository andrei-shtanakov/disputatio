"""A1: `doc-scope` не вправе пропускать путь, которого он не разобрал.

Границу контура (SPEC-002 §6) обходил не хитрый патч, а обычный:
`git diff HEAD` описывает бинарное изменение строкой
``Binary files … differ`` — **без** пары ``--- a/…``/``+++ b/…``. Разбор,
читающий только текстовые заголовки, не видел в такой секции ни одного
пути, и пустой список распознанных путей означал `PASS`. То же у смены
режима файла и у создания пустого файла: git печатает секцию без
текстовых заголовков.

Патчи здесь берутся из НАСТОЯЩЕГО git'а тем же вызовом, каким пайплайн
пишет `changes.patch` (`GitCli.diff_head`), а бинарные файлы — настоящие
байты. Литерал патча в тесте проверял бы разбор той формы, которую тест
сам себе и придумал; ровно так эта дыра и пережила внутренний цикл.
"""

import json
from collections.abc import Callable
from pathlib import Path

from disputatio.contracts.verification import GateStatus
from disputatio.runtime.git import GitCli
from disputatio.verifier import doc_gates

_BINARY_BYTES = b"\x00\x01\x02\xff\x00PNG\x00"
_OTHER_BINARY_BYTES = b"\x00\xfe\xfd\x00PNG\x00\x01"


def _tail_entries(tail: str | None) -> list[dict[str, object]]:
    return [json.loads(line) for line in (tail or "").splitlines() if line]


def _codes(tail: str | None) -> set[str]:
    return {str(entry["code"]) for entry in _tail_entries(tail)}


def _targets(tail: str | None) -> set[str]:
    return {str(entry["target"]) for entry in _tail_entries(tail)}


def _doc_repo(repo: Path, git_run: Callable[..., str]) -> None:
    """Рабочее дерево document-контура: разрешённая `spec.md` + бинарник."""
    (repo / "spec.md").write_text("# спека\n", encoding="utf-8")
    (repo / "asset.bin").write_bytes(_BINARY_BYTES)
    git_run(repo, "add", "spec.md", "asset.bin")
    git_run(repo, "commit", "--quiet", "-m", "doc baseline")


def test_new_binary_outside_allowed_fails(
    tmp_git_repo: Path, git_run: Callable[..., str]
) -> None:
    """Автор правит разрешённую спеку и создаёт бинарник вне `allowed`.

    Текстовый заголовок в патче есть — но только у `spec.md`. Секция
    `other.bin` состоит из ``Binary files /dev/null and b/other.bin differ``,
    и разбор по текстовым заголовкам объявлял весь патч не вышедшим за
    границу контура.
    """
    _doc_repo(tmp_git_repo, git_run)
    (tmp_git_repo / "spec.md").write_text("# спека\nновая строка\n", encoding="utf-8")
    (tmp_git_repo / "other.bin").write_bytes(_OTHER_BINARY_BYTES)

    patch = GitCli(tmp_git_repo).diff_head()

    assert "Binary files" in patch  # форма, ради которой тест написан
    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.FAIL
    assert "other.bin" in _targets(result.tail)


def test_modified_tracked_binary_outside_allowed_fails(
    tmp_git_repo: Path, git_run: Callable[..., str]
) -> None:
    """Правка уже отслеживаемого бинарника — та же секция без заголовков."""
    _doc_repo(tmp_git_repo, git_run)
    (tmp_git_repo / "asset.bin").write_bytes(_OTHER_BINARY_BYTES)

    patch = GitCli(tmp_git_repo).diff_head()

    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.FAIL
    assert "asset.bin" in _targets(result.tail)


def test_binary_rename_outside_allowed_fails(
    tmp_git_repo: Path, git_run: Callable[..., str]
) -> None:
    """`git mv` бинарника: форма `rename from`/`rename to` — и она обязана ловиться."""
    _doc_repo(tmp_git_repo, git_run)
    git_run(tmp_git_repo, "mv", "asset.bin", "moved.bin")

    patch = GitCli(tmp_git_repo).diff_head()

    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.FAIL
    assert "moved.bin" in _targets(result.tail)


def test_mode_only_change_outside_allowed_fails(
    tmp_git_repo: Path, git_run: Callable[..., str]
) -> None:
    """Смена режима печатается без `---`/`+++` — та же слепота, не только бинарники."""
    _doc_repo(tmp_git_repo, git_run)
    (tmp_git_repo / "script.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    git_run(tmp_git_repo, "add", "script.sh")
    git_run(tmp_git_repo, "commit", "--quiet", "-m", "script")
    (tmp_git_repo / "script.sh").chmod(0o755)

    patch = GitCli(tmp_git_repo).diff_head()

    assert "new mode" in patch
    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.FAIL
    assert "script.sh" in _targets(result.tail)


def test_binary_patch_body_does_not_blind_the_gate(
    tmp_git_repo: Path, git_run: Callable[..., str]
) -> None:
    """Не-вакуумность: тело `GIT binary patch` не должно съедать остаток патча.

    Форма `--binary` в пайплайне не используется, но проверяется отдельно:
    base85-строки тела не несут пробелов, поэтому под заголовок файла не
    маскируются, и следующий за телом настоящий файл обязан остаться
    видимым.
    """
    _doc_repo(tmp_git_repo, git_run)
    (tmp_git_repo / "asset.bin").write_bytes(_OTHER_BINARY_BYTES)
    (tmp_git_repo / "zz.py").write_text("x = 1\n", encoding="utf-8")
    git_run(tmp_git_repo, "add", "--intent-to-add", "--", ":/")

    patch = git_run(tmp_git_repo, "diff", "--binary", "--no-color", "HEAD")

    assert "GIT binary patch" in patch
    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.FAIL
    assert {"asset.bin", "zz.py"} <= _targets(result.tail)


def test_binary_inside_allowed_passes(
    tmp_git_repo: Path, git_run: Callable[..., str]
) -> None:
    """Обратная сторона: разрешённый путь не должен краснеть из-за бинарности.

    Фикс обязан вывести путь секции, а не объявлять неразобранной всякую
    секцию без текстовых заголовков — иначе `doc-scope` стал бы падать на
    любом легальном изменении такой формы.
    """
    _doc_repo(tmp_git_repo, git_run)
    (tmp_git_repo / "asset.bin").write_bytes(_OTHER_BINARY_BYTES)

    patch = GitCli(tmp_git_repo).diff_head()

    result = doc_gates.gate_doc_scope(patch, ("asset.bin",))

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_patch_without_any_recognised_path_is_not_a_pass() -> None:
    """Непустой патч, из которого не выведен ни один путь, — не успех.

    Общий инвариант поверх частных форм: `doc-scope` отвечает «граница
    контура не нарушена» только про то, что действительно разобрал.
    """
    patch = "какой-то текст, не являющийся unified diff\nвторая строка\n"

    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.FAIL
    assert _codes(result.tail) == {doc_gates.CODE_SCOPE_UNPARSED}


def test_ambiguous_diff_git_header_without_file_headers_is_not_a_pass() -> None:
    """Неоднозначная `diff --git` без заголовков — отказ, а не догадка.

    Пробел-разделитель в общем случае не отличим от пробела в имени файла;
    вывод пути наугад дал бы `doc-scope` право пропустить чужой путь под
    видом разобранного.
    """
    patch = "diff --git a/x b/y b/z b/w\nold mode 100644\nnew mode 100755\n"

    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.FAIL
    assert _codes(result.tail) == {doc_gates.CODE_SCOPE_UNPARSED}


def test_binary_marker_inside_a_hunk_is_still_content(
    tmp_git_repo: Path, git_run: Callable[..., str]
) -> None:
    """Симметрия с B2: `Binary files … differ` в тексте документа — содержимое.

    Спека, описывающая формы патча, законно цитирует эту строку. Разбор,
    ставший внимательнее к бинарным формам, не вправе стать доверчивее к
    строкам внутри hunk'ов.
    """
    _doc_repo(tmp_git_repo, git_run)
    (tmp_git_repo / "spec.md").write_text(
        "# спека\nБинарное изменение печатается так:\n"
        "Binary files a/other.bin and b/other.bin differ\n",
        encoding="utf-8",
    )

    patch = GitCli(tmp_git_repo).diff_head()

    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []
