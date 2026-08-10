"""Коммит принятого раунда ([REQ-011], [REQ-015], [DESIGN-011]).

[TASK-006]. Коммит раунда — не «сохранение на всякий случай», а цель
`git reset` следующего раунда (DESIGN-012) и единственная воспроизводимая
история сессии. Поэтому тест пинит свойства, без которых эта цель
перестаёт находиться или начинает врать:

* ровно ОДИН новый коммит на принятый раунд — второй коммит сдвинул бы
  `base_rev` следующего раунда на артефакт, работы автора не содержащий;
* `NNN` трёхзначный с ведущими нулями и совпадающий с именем `rounds/NNN/`
  — сверка идёт с `events.paths.round_dir`, а не с литералом теста: обе
  стороны обязаны padding'ом совпадать, иначе раунд 3 в истории и раунд
  `003` на диске — разные сущности;
* `ROUND_COMMIT_PATTERN` матчит ровно то, что породил
  `ROUND_COMMIT_TEMPLATE`, и не матчит ни `disputatio: round 3`, ни
  `disputatio: round 0031` — иначе `base_rev(N)` найдёт чужой коммит;
* `.disputatio/` в коммит не попадает: артефакты сессии — журнал
  оркестратора, а не работа автора;
* пустой дифф коммита не создаёт и не падает: analyze-раунд правок не
  делает, а повторный вызов после сбоя обязан давать то же состояние
  ([REQ-015]);
* идентичность коммита — `disputatio`, а не `user.name` пользовательского
  репозитория, и локальный `.gitignore` пользователя не мутируется.

`GitCli.commit_round` до реализации — `NotImplementedError`-заглушка
[TASK-004], а констант формата ещё нет вовсе. Вызов идёт через
`_commit_round`, а константы читаются через `_attr`: и заглушка, и
отсутствующее имя переводятся в `AssertionError` — red-чекпоинт обязан
быть падением assertion'ом, а не всплывшим наружу исключением или
ошибкой импорта на коллекции.
"""

import re
import subprocess
from pathlib import Path

from disputatio import runtime
from disputatio.events.paths import SESSION_DIR_NAME, round_dir
from disputatio.runtime import GitCli
from disputatio.runtime.git import GIT_USER_NAME

# Раунд из чек-листа задачи: цифра `3` и её запись `003` различаются, поэтому
# сверка с `round_dir(...).name` не вакуумна — при формате без ведущих нулей
# она падает.
_ROUND = 3

_AUTHOR_LINE = "строка, добавленная автором"
_CREATED_FILE = "pkg/created.py"

# Сообщения, которые `ROUND_COMMIT_PATTERN` матчить не должен: без ведущих
# нулей, с лишней цифрой и с текстом по краям — `base_rev(N)` ищет коммит
# раунда по этому шаблону, и любое из них увело бы сброс не туда.
_FOREIGN_SUBJECTS = (
    "disputatio: round 3",
    "disputatio: round 0031",
    "fixup! disputatio: round 003",
    "disputatio: round 003 (wip)",
)


def _attr(name: str) -> str:
    """Строковая константа публичного API `disputatio.runtime`.

    Отсутствие имени — `AssertionError`, а не `ImportError` на коллекции:
    гейт red принимает только падение assertion'ом.
    """
    value = getattr(runtime, name, None)
    assert isinstance(value, str), (
        f"{name} не объявлена строкой в публичном API disputatio.runtime "
        f"([DESIGN-011]): {value!r}"
    )
    return value


def _commit_round(root: Path, round_no: int) -> None:
    """`GitCli(root).commit_round(n)`; заглушка [TASK-004] — `AssertionError`."""
    try:
        GitCli(root).commit_round(round_no)
    except NotImplementedError as exc:
        raise AssertionError(
            f"GitCli.commit_round — всё ещё заглушка [DESIGN-011]: {exc}"
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


def _commit_count(root: Path) -> int:
    """Число коммитов в текущей ветке."""
    return int(_git(root, "rev-list", "--count", "HEAD").strip())


def _subject(root: Path) -> str:
    """Заголовок последнего коммита."""
    return _git(root, "log", "-1", "--format=%s").strip()


def _write_author_work(root: Path) -> None:
    """Работа автора раунда: правка tracked-файла и созданный модуль."""
    readme = root / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + _AUTHOR_LINE + "\n",
        encoding="utf-8",
    )
    created = root / _CREATED_FILE
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text(f'VALUE = "{_AUTHOR_LINE}"\n', encoding="utf-8")


def _write_session_artifacts(root: Path, round_no: int) -> None:
    """Журнал оркестратора: `session.json`, `events.jsonl`, артефакт раунда."""
    session = root / SESSION_DIR_NAME
    session.mkdir(parents=True, exist_ok=True)
    (session / "session.json").write_text('{"state": "DECIDING"}\n', encoding="utf-8")
    (session / "events.jsonl").write_text(
        '{"type": "state_change"}\n', encoding="utf-8"
    )
    artifacts = round_dir(root, round_no)
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "proposal.md").write_text("# proposal\n", encoding="utf-8")


def test_accepted_round_yields_exactly_one_commit(git_repo: Path) -> None:
    """Принятый раунд — ровно один коммит с сообщением шаблона ([REQ-011])."""
    _write_author_work(git_repo)
    before = _commit_count(git_repo)

    _commit_round(git_repo, _ROUND)

    assert _commit_count(git_repo) == before + 1, (
        "принятый раунд обязан дать ровно один новый коммит: лишний сдвинул "
        f"бы `base_rev` следующего раунда:\n{_git(git_repo, 'log', '--oneline')}"
    )
    assert _subject(git_repo) == _attr("ROUND_COMMIT_TEMPLATE").format(round=_ROUND), (
        "заголовок коммита не порождён ROUND_COMMIT_TEMPLATE — сообщение "
        f"склеено мимо единственной константы формата:\n{_subject(git_repo)!r}"
    )
    committed = _git(git_repo, "show", "--name-only", "--format=", "HEAD").split()
    assert "README.md" in committed, (
        f"правка автора в коммит раунда не попала:\n{committed}"
    )
    assert _CREATED_FILE in committed, (
        f"созданный автором файл в коммит раунда не попал:\n{committed}"
    )


def test_commit_number_matches_round_directory_name(git_repo: Path) -> None:
    """`NNN` в сообщении и имя `rounds/NNN/` — одно и то же ([REQ-011])."""
    _write_author_work(git_repo)

    _commit_round(git_repo, _ROUND)

    directory = round_dir(git_repo, _ROUND).name
    assert directory != str(_ROUND), (
        "проверка вакуумна: имя каталога раунда совпало с голой цифрой — "
        f"padding сверять нечем:\n{directory!r}"
    )
    assert _subject(git_repo).endswith(directory), (
        "номер в сообщении коммита не совпал с именем каталога раунда: "
        f"история и диск называют раунд по-разному:\n{_subject(git_repo)!r} "
        f"против {directory!r}"
    )


def test_pattern_matches_only_the_generated_message(git_repo: Path) -> None:
    """`ROUND_COMMIT_PATTERN` находит коммит раунда и ничего сверх него."""
    _write_author_work(git_repo)
    _commit_round(git_repo, _ROUND)
    pattern = _attr("ROUND_COMMIT_PATTERN")

    assert re.search(pattern, _subject(git_repo)) is not None, (
        "ROUND_COMMIT_PATTERN не матчит сообщение, порождённое "
        f"ROUND_COMMIT_TEMPLATE — `base_rev(N)` коммит раунда не найдёт:\n"
        f"{pattern!r} против {_subject(git_repo)!r}"
    )
    for foreign in _FOREIGN_SUBJECTS:
        assert re.search(pattern, foreign) is None, (
            "ROUND_COMMIT_PATTERN матчит чужое сообщение — `base_rev(N)` "
            f"сбросит раунд на посторонний коммит:\n{pattern!r} против "
            f"{foreign!r}"
        )


def test_session_artifacts_never_reach_the_commit(git_repo: Path) -> None:
    """`.disputatio/` — журнал оркестратора, а не работа автора ([REQ-011])."""
    _write_author_work(git_repo)
    _write_session_artifacts(git_repo, _ROUND)

    _commit_round(git_repo, _ROUND)

    committed = _git(git_repo, "show", "--name-only", "--format=", "HEAD")
    assert _CREATED_FILE in committed, (
        f"проверка вакуумна: коммит не содержит даже работы автора:\n{committed}"
    )
    assert SESSION_DIR_NAME not in committed, (
        f"артефакты сессии попали в коммит раунда:\n{committed}"
    )
    tracked = _git(git_repo, "ls-files")
    assert SESSION_DIR_NAME not in tracked, (
        f"артефакты сессии оказались под версионным контролем:\n{tracked}"
    )


def test_clean_tree_makes_no_commit(git_repo: Path) -> None:
    """Пустой дифф — не ошибка и не коммит: analyze-раунд правок не делает."""
    before = _commit_count(git_repo)

    _commit_round(git_repo, _ROUND)

    assert _commit_count(git_repo) == before, (
        "коммит без изменений создан: пустая история раунда лучше пустого "
        f"коммита ([DESIGN-011]):\n{_git(git_repo, 'log', '--oneline')}"
    )


def test_session_artifacts_alone_make_no_commit(git_repo: Path) -> None:
    """Раунд без правок автора коммита не даёт, даже когда журнал не пуст."""
    _write_session_artifacts(git_repo, _ROUND)
    before = _commit_count(git_repo)

    _commit_round(git_repo, _ROUND)

    assert _commit_count(git_repo) == before, (
        "коммит создан из одних артефактов сессии: `.disputatio/` считается "
        f"работой автора:\n{_git(git_repo, 'log', '--oneline')}"
    )


def test_second_call_in_a_row_adds_no_commit(git_repo: Path) -> None:
    """Повторный вызов даёт эквивалентное состояние, а не второй коммит."""
    _write_author_work(git_repo)
    before = _commit_count(git_repo)

    _commit_round(git_repo, _ROUND)
    _commit_round(git_repo, _ROUND)

    assert _commit_count(git_repo) == before + 1, (
        "повторный commit_round породил второй коммит — шаг не идемпотентен "
        f"([REQ-015]):\n{_git(git_repo, 'log', '--oneline')}"
    )
    assert _subject(git_repo).endswith(round_dir(git_repo, _ROUND).name), (
        f"HEAD после повторного вызова — не коммит раунда:\n{_subject(git_repo)!r}"
    )


def test_session_dir_excluded_locally_without_touching_gitignore(
    git_repo: Path,
) -> None:
    """`.disputatio/` игнорируется через `.git/info/exclude`, а не `.gitignore`."""
    user_gitignore = "*.log\n"
    (git_repo / ".gitignore").write_text(user_gitignore, encoding="utf-8")
    _git(git_repo, "add", ".gitignore")
    _git(git_repo, "commit", "--quiet", "-m", "user gitignore")
    _write_session_artifacts(git_repo, _ROUND)

    _write_author_work(git_repo)
    _commit_round(git_repo, _ROUND)
    (git_repo / "second.py").write_text("VALUE = 2\n", encoding="utf-8")
    _commit_round(git_repo, _ROUND + 1)

    assert (git_repo / ".gitignore").read_text(encoding="utf-8") == user_gitignore, (
        "пользовательский .gitignore изменён: правило сессии обязано жить в "
        "локальном .git/info/exclude"
    )
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", f"{SESSION_DIR_NAME}/session.json"],
        cwd=git_repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0, (
        "`.disputatio/` не игнорируется git'ом — правило в .git/info/exclude "
        f"не записано:\n{ignored.stderr}"
    )
    exclude_lines = (
        (git_repo / ".git" / "info" / "exclude")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    entries = [
        line for line in exclude_lines if line.strip().strip("/") == SESSION_DIR_NAME
    ]
    assert len(entries) == 1, (
        "правило `.disputatio/` в .git/info/exclude записано не один раз — "
        f"каждый коммит раунда дописывает файл заново:\n{exclude_lines}"
    )


def test_commit_is_authored_by_the_orchestrator(git_repo: Path) -> None:
    """Подпись коммита раунда — `disputatio`, а не `user.*` чужого репозитория."""
    _write_author_work(git_repo)

    _commit_round(git_repo, _ROUND)

    author = _git(git_repo, "log", "-1", "--format=%an").strip()
    committer = _git(git_repo, "log", "-1", "--format=%cn").strip()
    assert (author, committer) == (GIT_USER_NAME, GIT_USER_NAME), (
        "коммит раунда подписан не идентичностью сессии: вызов идёт мимо "
        f"`-c user.name=…` ([DESIGN §4.2]):\n{author!r}/{committer!r}"
    )
