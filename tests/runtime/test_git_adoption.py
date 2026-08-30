"""Шесть операций `GitOps` под adoption и reconciliation (SPEC-002).

[TASK-012]. Операторские решения §3.1 (`--adopt-external`, `--discard-round`),
cleanup возврата §7.3 и сверка worktree §8.1 — единственные потребители этих
операций, и каждая из них закрывает дыру, которую иначе пришлось бы латать
`subprocess`'ом мимо порта (INV-11). Тест пинит ровно те свойства, на которых
держатся нормы спеки:

* `head_sha` и `current_branch` — идентичность состояния и предусловие
  protected-ветки §3.1; операции определения ветки в порту до сих пор не было
  вовсе, а `--abbrev-ref` отвечает `HEAD` в detached-состоянии, и без
  перевода этого ответа в `None` «ветка HEAD» прошла бы проверку списка
  `protected_branches` как обычное имя;
* `status_entries` отдаёт статус ЦЕЛИКОМ, включая пути под `.disputatio/`, и
  различает tracked/untracked: узкое правило §3.1 требует отличить
  собственные untracked control-файлы пайплайна (легальны) от
  tracked-изменённых там же (adoption отклоняется), и порт, вырезающий
  каталог сам, эту информацию уничтожил бы;
* `commit_paths` коммитит РОВНО перечисленное: adoption фиксирует диф пары
  документов, а посторонняя грязь дерева обязана остаться вне чекпоинта и в
  дереве — иначе операторский чекпоинт молча присвоил бы чужую правку;
* `find_commit_by_trailer` ищет по трейлеру, а не по заголовку: заголовок
  `disputatio: operator adopt <slug>` одинаков у всех adoption'ов пайплайна,
  и повторный запуск нашёл бы чужой чекпоинт вместо своего — идемпотентность
  §3.1 держится именно на трейлере;
* `diff_readonly` даёт байт-в-байт тот же патч, что `diff_head`, но НЕ
  трогает индекс: шаг 3 §8.1 объявлен немутирующим, а `add --intent-to-add`
  внутри `diff_head` оставил бы новый файл в индексе и изменил вывод
  последующего `git status` у пользователя.

Обращение к методам идёт через `_op`, а к `StatusEntry` — через `_attr`:
до реализации отсутствующее имя обязано падать `AssertionError`, а не
`AttributeError`/`ImportError` на коллекции.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from disputatio.events.paths import SESSION_DIR_NAME
from disputatio.runtime import GitCli
from disputatio.runtime import git as git_module

# Заголовок операторского чекпоинта §3.1. Одинаков у всех adoption'ов
# пайплайна — именно поэтому поиск идёт по трейлеру, а не по нему.
_ADOPT_SUBJECT = "disputatio: operator adopt demo"

_OPERATION_ONE = "9f1c2d3e4a5b"
_OPERATION_TWO = "0a1b2c3d4e5f"

# Трейлер, отличающийся от `_OPERATION_ONE` только хвостом: поиск по
# вхождению подстроки принял бы за него чужой чекпоинт.
_OPERATION_PREFIXED = f"{_OPERATION_ONE}-suffix"

_ADOPTED_FILE = "spec/pair.md"


def _attr(name: str) -> Any:
    """Публичное имя `disputatio.runtime.git`; отсутствие — `AssertionError`."""
    value = getattr(git_module, name, None)
    assert value is not None, (
        f"{name} не объявлен в `disputatio.runtime.git` ([TASK-012])"
    )
    return value


def _op(git: GitCli, name: str) -> Callable[..., Any]:
    """Метод порта на `GitCli`; отсутствие — `AssertionError`, не `AttributeError`."""
    method = getattr(git, name, None)
    assert callable(method), (
        f"GitCli.{name} не реализован — порт `GitOps` не расширен ([TASK-012])"
    )
    return method


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


def _porcelain(root: Path) -> str:
    """`git status --porcelain -uall` глазами пользователя, а не порта."""
    return _git(root, "status", "--porcelain", "-uall")


def _commit_file(root: Path, relative: str, content: str, subject: str) -> None:
    """Кладёт файл в историю репозитория обычным git'ом, минуя порт."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(root, "add", "--", relative)
    _git(root, "commit", "--quiet", "-m", subject)


def _dirty_tree(root: Path) -> None:
    """Грязное дерево из обоих видов правок: modified tracked и untracked."""
    readme = root / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "строка автора\n", encoding="utf-8"
    )
    created = root / _ADOPTED_FILE
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text("новый документ пары\n", encoding="utf-8")


def test_head_sha_matches_rev_parse(git_repo: Path) -> None:
    """`head_sha` — полный SHA `HEAD`, тот же, что видит сам git."""
    expected = _git(git_repo, "rev-parse", "HEAD").strip()

    assert _op(GitCli(git_repo), "head_sha")() == expected


def test_current_branch_matches_rev_parse(git_repo: Path) -> None:
    """На обычной ветке `current_branch` совпадает с `--abbrev-ref HEAD`."""
    expected = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert expected == "main", f"фикстура сменила имя ветки: {expected!r}"

    assert _op(GitCli(git_repo), "current_branch")() == expected


def test_current_branch_is_none_in_detached_head(git_repo: Path) -> None:
    """В detached HEAD ветки нет — порт отвечает `None`, а не строкой `HEAD`.

    Без перевода сентинела в `None` предусловие §3.1 сравнивало бы литерал
    `HEAD` со списком `protected_branches` и пропускало бы старт в состоянии,
    где коммиты раундов не удерживает ни одна ссылка.
    """
    _git(git_repo, "checkout", "--quiet", "--detach")

    assert _op(GitCli(git_repo), "current_branch")() is None


def test_status_entries_report_whole_status_with_tracked_flag(
    git_repo: Path,
) -> None:
    """Статус целиком: `.disputatio/` не вырезан, tracked проставлен верно.

    Четыре записи — по одной на каждый случай, который разбирает §3.1:
    правка tracked-файла, посторонний untracked, собственный untracked
    control-файл пайплайна и tracked-изменённый файл под `.disputatio/` —
    последний как раз тот, ради которого порт не вправе вырезать каталог сам.
    """
    control = f"{SESSION_DIR_NAME}/pipelines/demo/pipeline.json"
    _commit_file(git_repo, control, '{"revision": 1}\n', "control plane")
    (git_repo / control).write_text('{"revision": 2}\n', encoding="utf-8")
    readme = git_repo / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "правка\n", encoding="utf-8")
    (git_repo / "draft.md").write_text("посторонний черновик\n", encoding="utf-8")
    journal = git_repo / SESSION_DIR_NAME / "pipelines" / "demo" / "events.jsonl"
    journal.write_text('{"type": "state_change"}\n', encoding="utf-8")

    entries = _op(GitCli(git_repo), "status_entries")()

    assert {entry.path: entry.tracked for entry in entries} == {
        "README.md": True,
        "draft.md": False,
        control: True,
        f"{SESSION_DIR_NAME}/pipelines/demo/events.jsonl": False,
    }


def test_status_entries_hide_session_dir_once_git_ignores_it(
    git_repo: Path,
) -> None:
    """После первого принятого раунда untracked-файлы сессии из статуса уходят.

    `commit_round` дописывает `.disputatio/` в `.git/info/exclude`
    ([DESIGN-011]), и с этого момента git считает untracked-пути каталога
    игнорируемыми — `--ignored` порт не передаёт, поэтому в выборку они не
    попадают. Свойство пинится не как желаемое, а как фактическое: потребитель
    §3.1 обязан знать, что его фильтр `.disputatio/` бывает избыточен, и не
    выводить из пустого статуса «пайплайн ничего не писал».

    tracked-изменённый файл под тем же каталогом игнор НЕ скрывает — ровно
    тот случай, ради которого порт не вырезает каталог сам, из статуса не
    исчезает ни при каком состоянии `info/exclude`.
    """
    control = f"{SESSION_DIR_NAME}/pipelines/demo/pipeline.json"
    _commit_file(git_repo, control, '{"revision": 1}\n', "control plane")
    (git_repo / control).write_text('{"revision": 2}\n', encoding="utf-8")
    journal = git_repo / SESSION_DIR_NAME / "pipelines" / "demo" / "events.jsonl"
    journal.write_text('{"type": "state_change"}\n', encoding="utf-8")
    exclude = git_repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(f"{SESSION_DIR_NAME}/\n", encoding="utf-8")

    entries = _op(GitCli(git_repo), "status_entries")()

    assert {entry.path: entry.tracked for entry in entries} == {control: True}


def test_status_entries_paths_are_based_on_toplevel_not_session_root(
    git_repo: Path,
) -> None:
    """Сессия в подкаталоге: пути приходят от toplevel, а не от `root`.

    `preflight` не требует, чтобы корень сессии был корнем репозитория (на
    это прямо опирается комментарий к `_TREE_PATHSPEC`), и когда они не
    совпадают, наивный фильтр `.disputatio/` промахивается: предусловие
    старта §3.1 отвергло бы легальный запуск по собственному журналу
    пайплайна, а scope adoption'а — сам документ пары. Тест закрепляет
    базу путей, чтобы потребители §3.1 приводили свои пути к ней осознанно.

    `status.relativePaths=true` ставится намеренно: локальный `.git/config`
    `_env` не гасит (он лежит внутри рабочей директории), и без этой строки
    оставалось бы неясным, не спасает ли конфиг ситуацию сам. Не спасает —
    на `--porcelain` он не влияет.
    """
    root = git_repo / "proj"
    (root / "spec").mkdir(parents=True)
    (root / "spec" / "pair.md").write_text("документ пары\n", encoding="utf-8")
    journal = root / SESSION_DIR_NAME / "pipelines" / "demo"
    journal.mkdir(parents=True)
    (journal / "events.jsonl").write_text(
        '{"type": "state_change"}\n', encoding="utf-8"
    )
    _git(git_repo, "config", "status.relativePaths", "true")

    entries = _op(GitCli(root), "status_entries")()

    assert {entry.path for entry in entries} == {
        "proj/spec/pair.md",
        f"proj/{SESSION_DIR_NAME}/pipelines/demo/events.jsonl",
    }
    assert not any(
        entry.path.startswith(f"{SESSION_DIR_NAME}/") for entry in entries
    ), (
        "наивный фильтр `.disputatio/` совпал бы — тест перестал показывать "
        "ловушку, ради которой написан"
    )


def test_commit_paths_refuses_session_dir_once_git_ignores_it(
    git_repo: Path,
) -> None:
    """Путь под `.disputatio/` в чекпоинт не берётся — область порта уже.

    Со второго принятого раунда каталог сессии лежит в `.git/info/exclude`
    ([DESIGN-011]), и `git add` с явно названным игнорируемым путём выходит
    кодом 1. Нормам §3.1 это не мешает (чекпоинт фиксирует только документы
    пары), но порт объявлен общим, и ограничение закреплено тестом, а не
    только докстрингом.
    """
    control = f"{SESSION_DIR_NAME}/pipelines/demo/pipeline.json"
    (git_repo / control).parent.mkdir(parents=True, exist_ok=True)
    (git_repo / control).write_text('{"revision": 1}\n', encoding="utf-8")
    exclude = git_repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(f"{SESSION_DIR_NAME}/\n", encoding="utf-8")
    head = _git(git_repo, "rev-parse", "HEAD").strip()

    with pytest.raises(_attr("GitCommandError")):
        _op(GitCli(git_repo), "commit_paths")(
            [control], _ADOPT_SUBJECT, trailer=_OPERATION_ONE
        )

    assert _git(git_repo, "rev-parse", "HEAD").strip() == head


def test_status_entries_name_both_halves_of_a_rename(git_repo: Path) -> None:
    """Переименование названо обоими путями, а не одной записью.

    Scope §3.1 fail-closed: правка допустима только по путям пары, и
    переименование, свёрнутое git'ом в одну запись `R new`, спрятало бы
    исходный путь — adoption принял бы удаление постороннего файла молча.
    """
    _commit_file(git_repo, "docs/old.md", "документ\n", "документ")
    _git(git_repo, "mv", "docs/old.md", "docs/new.md")

    entries = _op(GitCli(git_repo), "status_entries")()

    assert {entry.path for entry in entries} == {"docs/old.md", "docs/new.md"}
    assert all(entry.tracked for entry in entries)


def test_status_entries_is_empty_on_clean_tree(git_repo: Path) -> None:
    """Чистое дерево — пустой кортеж; иначе предусловие `run` не пройдёт никогда."""
    assert _op(GitCli(git_repo), "status_entries")() == ()


def test_status_entry_is_immutable() -> None:
    """`StatusEntry` неизменяем: вердикт §3.1 не переписывается по дороге."""
    entry = _attr("StatusEntry")(path="spec.md", tracked=False)

    with pytest.raises((AttributeError, TypeError)):
        entry.tracked = True  # type: ignore[misc]


def test_commit_paths_commits_only_listed_paths(git_repo: Path) -> None:
    """Чекпоинт несёт РОВНО названные пути; чужая грязь остаётся в дереве.

    Иначе операторский чекпоинт присвоил бы правку, которой оператор не
    санкционировал, и `--adopt-external` перестал бы быть узким решением.
    """
    _dirty_tree(git_repo)
    before = _git(git_repo, "rev-list", "--count", "HEAD").strip()

    sha = _op(GitCli(git_repo), "commit_paths")(
        [_ADOPTED_FILE], _ADOPT_SUBJECT, trailer=_OPERATION_ONE
    )

    assert sha == _git(git_repo, "rev-parse", "HEAD").strip()
    assert _git(git_repo, "rev-list", "--count", "HEAD").strip() == str(int(before) + 1)
    assert _git(git_repo, "show", "--name-only", "--format=", "HEAD").split() == [
        _ADOPTED_FILE
    ]
    assert " M README.md" in _porcelain(git_repo), (
        "посторонняя правка исчезла из дерева — чекпоинт её присвоил"
    )


def test_commit_paths_rejects_empty_path_list(git_repo: Path) -> None:
    """Пустой список путей — отказ, а не «закоммитить всё, что в индексе».

    `git commit --only` без pathspec теряет своё «только» и берёт индекс
    целиком: молчаливое согласие здесь означало бы чекпоинт, содержащий
    ровно то, чего оператор не санкционировал.
    """
    _dirty_tree(git_repo)
    _git(git_repo, "add", "--", "README.md")
    head = _git(git_repo, "rev-parse", "HEAD").strip()

    with pytest.raises(ValueError):
        _op(GitCli(git_repo), "commit_paths")(
            [], _ADOPT_SUBJECT, trailer=_OPERATION_ONE
        )

    assert _git(git_repo, "rev-parse", "HEAD").strip() == head


def test_commit_paths_writes_subject_and_trailer(git_repo: Path) -> None:
    """Тело чекпоинта — заголовок, пустая строка и трейлер операции."""
    _dirty_tree(git_repo)

    _op(GitCli(git_repo), "commit_paths")(
        [_ADOPTED_FILE], _ADOPT_SUBJECT, trailer=_OPERATION_ONE
    )

    body = _git(git_repo, "log", "-1", "--format=%B", "HEAD")
    assert body.splitlines()[:3] == [
        _ADOPT_SUBJECT,
        "",
        f"Disputatio-Operation: {_OPERATION_ONE}",
    ], f"тело чекпоинта не по форме §3.1: {body!r}"


def test_find_commit_by_trailer_finds_own_and_ignores_foreign(
    git_repo: Path,
) -> None:
    """Свой чекпоинт находится по трейлеру, чужая операция даёт `None`."""
    _dirty_tree(git_repo)
    sha = _op(GitCli(git_repo), "commit_paths")(
        [_ADOPTED_FILE], _ADOPT_SUBJECT, trailer=_OPERATION_ONE
    )

    find = _op(GitCli(git_repo), "find_commit_by_trailer")
    assert find(_OPERATION_ONE) == sha
    assert find(_OPERATION_TWO) is None


def test_find_commit_by_trailer_distinguishes_two_adoptions(
    git_repo: Path,
) -> None:
    """Два adoption'а с общим заголовком различимы только трейлером.

    Заголовок у обоих один и тот же — поиск по нему вернул бы первый
    попавшийся, и повторный `--adopt-external` присвоил бы чужой чекпоинт
    вместо создания своего.
    """
    _dirty_tree(git_repo)
    commit_paths = _op(GitCli(git_repo), "commit_paths")
    first = commit_paths([_ADOPTED_FILE], _ADOPT_SUBJECT, trailer=_OPERATION_ONE)
    (git_repo / _ADOPTED_FILE).write_text("вторая правка\n", encoding="utf-8")
    second = commit_paths([_ADOPTED_FILE], _ADOPT_SUBJECT, trailer=_OPERATION_TWO)

    find = _op(GitCli(git_repo), "find_commit_by_trailer")
    assert first != second
    assert find(_OPERATION_ONE) == first
    assert find(_OPERATION_TWO) == second


def test_find_commit_by_trailer_requires_whole_value(git_repo: Path) -> None:
    """Трейлер сравнивается целиком: чужой операции с общим префиксом — `None`.

    `operation_id` детерминирован из sha256, и поиск по вхождению подстроки
    признал бы своим чекпоинт операции, чей идентификатор лишь начинается
    так же.
    """
    _dirty_tree(git_repo)
    sha = _op(GitCli(git_repo), "commit_paths")(
        [_ADOPTED_FILE], _ADOPT_SUBJECT, trailer=_OPERATION_PREFIXED
    )

    find = _op(GitCli(git_repo), "find_commit_by_trailer")
    assert find(_OPERATION_PREFIXED) == sha
    assert find(_OPERATION_ONE) is None


def test_diff_readonly_does_not_touch_index(git_repo: Path) -> None:
    """Патч тот же, что у `diff_head`, но индекс и вывод `git status` целы.

    Три утверждения в одном тесте намеренно: свойство «немутирующий» имеет
    смысл только вместе с «тот же патч» — иначе его тривиально удовлетворить
    пустой строкой. Контрольное сравнение с `diff_head` в конце показывает,
    что проверка не вакуумна: настоящий `diff_head` статус меняет
    (untracked-файл встаёт в индекс как intent-to-add).
    """
    _dirty_tree(git_repo)
    before = _porcelain(git_repo)
    assert f"?? {_ADOPTED_FILE}" in before, "untracked-файл не попал в setup"

    readonly = _op(GitCli(git_repo), "diff_readonly")()

    assert _porcelain(git_repo) == before, (
        "сверка worktree §8.1 изменила индекс — фаза объявлена немутирующей"
    )
    assert readonly == GitCli(git_repo).diff_head(), (
        "канонический дифф разошёлся с `changes.patch` SPEC-001"
    )
    assert _porcelain(git_repo) != before, (
        "`diff_head` перестал трогать индекс — сравнение стало вакуумным"
    )


def test_diff_readonly_excludes_session_dir(git_repo: Path) -> None:
    """Каталог сессии вне патча — та же область, что у `diff_head`."""
    _dirty_tree(git_repo)
    journal = git_repo / SESSION_DIR_NAME / "events.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text('{"type": "state_change"}\n', encoding="utf-8")

    readonly = _op(GitCli(git_repo), "diff_readonly")()

    assert SESSION_DIR_NAME not in readonly
    assert _ADOPTED_FILE in readonly


def test_diff_readonly_leaves_no_temporary_index_behind(git_repo: Path) -> None:
    """Одноразовый индекс не остаётся в `.git/`: настоящий индекс не подменён."""
    _dirty_tree(git_repo)
    index = git_repo / ".git" / "index"
    before = index.read_bytes()

    _op(GitCli(git_repo), "diff_readonly")()

    assert index.read_bytes() == before, "настоящий индекс переписан"
    assert not (git_repo / ".git" / "index.lock").exists()
