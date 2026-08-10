"""Сброс дерева перед PROPOSING ([REQ-012], [REQ-015], [DESIGN-012]).

[TASK-007]. Идемпотентность шага PROPOSING держится не на аккуратности
автора, а на том, что каждая попытка стартует из одного и того же
состояния. Тест пинит свойства, без которых это ломается:

* цель сброса **вычисляется**: `base_rev(1)` — `base_commit` из конфига,
  `base_rev(N>1)` — коммит раунда N−1. Не `HEAD`: на входе в раунд N
  `HEAD` — это либо коммит предыдущего принятого раунда (тогда совпадение
  случайно), либо что угодно ещё после прерывания;
* коммит раунда ищется по **точному** сообщению: `fixup! disputatio: round
  003` и `disputatio: round 003 (wip)` в тесте моложе настоящего коммита
  раунда, поэтому поиск «по вхождению подстроки, берём свежайший» вернёт
  именно их и будет пойман;
* отсутствие целевого коммита — доменная ошибка, а не молчаливый откат к
  `HEAD`: сброс не туда стёр бы принятую работу прошлых раундов;
* `reset_hard` — именно `--hard`: `--mixed`/`--soft` оставили бы правку
  прерванной попытки в дереве, и она утекла бы в `changes.patch` следующей;
* `clean` убирает **новые** untracked-файлы: `reset` их не видит вовсе,
  а `git diff HEAD` (через intent-to-add) видит — без уборки чужой
  черновик стал бы работой автора следующей попытки;
* `.disputatio/` переживает и reset, и clean, причём **до** первого
  принятого раунда, когда правила в `.git/info/exclude` ещё нет: журнал
  сессии — единственный источник resume, и стереть его значит потерять
  сессию;
* итоговый критерий — `diff_head()` пуст после reset+clean, и повторный
  прогон пары даёт то же состояние ([REQ-015]).

`base_rev` и класс доменной ошибки до реализации не существуют, а
`GitCli.reset_hard`/`clean` — `NotImplementedError`-заглушки [TASK-004].
Обращение идёт через `_base_rev`/`_domain_error`/`_reset_hard`/`_clean`:
и отсутствующее имя, и заглушка переводятся в `AssertionError` —
red-чекпоинт обязан быть падением assertion'ом, а не ошибкой импорта на
коллекции или всплывшим наружу исключением.
"""

import subprocess
from pathlib import Path

import pytest

from disputatio import runtime
from disputatio.runtime import DisputatioError, GitCli, GitCommandError
from disputatio.runtime.git import ROUND_COMMIT_TEMPLATE

_SESSION_DIR = ".disputatio"

# 40 hex-символов, которых нет в объектной базе: и `rev-parse`, и `reset`
# обязаны ответить отказом, а не взять что-нибудь похожее.
_MISSING_SHA = "deadbeef" * 5

# Сообщения-двойники коммита раунда 003. Коммитятся ПОСЛЕ настоящего
# раунда 003 и ДО раунда 004: поиск подстрокой отдаст самый свежий из них.
_LOOKALIKE_SUBJECTS = (
    "fixup! disputatio: round 003",
    "disputatio: round 003 (wip)",
)

_ABORTED_LINE = "правка прерванной попытки\n"
_SESSION_JSON = '{"state": "PROPOSING"}\n'
_PROPOSAL = "# proposal раунда 004\n"


def _base_rev(root: Path, round_no: int, *, base_commit: str) -> str:
    """`runtime.base_rev(...)`; отсутствие имени — `AssertionError`."""
    func = getattr(runtime, "base_rev", None)
    assert callable(func), (
        f"disputatio.runtime не экспортирует base_rev ([DESIGN-012]): {func!r}"
    )
    target = func(root, round_no, base_commit=base_commit)
    assert isinstance(target, str), (
        f"base_rev вернул не ревизию, а {target!r} — `git reset` такое не примет"
    )
    return target


def _domain_error(name: str) -> type[DisputatioError]:
    """Класс доменной ошибки из публичного API; отсутствие — `AssertionError`."""
    error = getattr(runtime, name, None)
    assert isinstance(error, type) and issubclass(error, DisputatioError), (
        f"disputatio.runtime не экспортирует доменную ошибку {name!r} "
        f"([DESIGN-012], [DESIGN-020]): {error!r}"
    )
    return error


def _reset_hard(root: Path, rev: str) -> None:
    """`GitCli(root).reset_hard(rev)`; заглушка [TASK-004] — `AssertionError`."""
    try:
        GitCli(root).reset_hard(rev)
    except NotImplementedError as exc:
        raise AssertionError(
            f"GitCli.reset_hard — всё ещё заглушка [DESIGN-012]: {exc}"
        ) from exc


def _clean(root: Path) -> None:
    """`GitCli(root).clean()`; заглушка [TASK-004] — `AssertionError`."""
    try:
        GitCli(root).clean()
    except NotImplementedError as exc:
        raise AssertionError(
            f"GitCli.clean — всё ещё заглушка [DESIGN-012]: {exc}"
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


def _head_sha(root: Path) -> str:
    """Полный SHA текущего `HEAD`."""
    return _git(root, "rev-parse", "HEAD").strip()


def _subject(root: Path, rev: str) -> str:
    """Заголовок коммита `rev`."""
    return _git(root, "log", "-1", "--format=%s", rev).strip()


def _body(subject: str) -> str:
    """Содержимое файла, закоммиченного вместе с сообщением `subject`."""
    return f"# {subject}\n"


def _commit_file(root: Path, name: str, subject: str) -> str:
    """Коммитит новый файл сообщением `subject`; отдаёт SHA коммита."""
    (root / name).write_text(_body(subject), encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "--quiet", "-m", subject)
    return _head_sha(root)


def _round_history(root: Path) -> dict[int, str]:
    """История принятых раундов 1…4 с двойниками между 003 и 004.

    Сообщения строятся из `ROUND_COMMIT_TEMPLATE`, а не из литералов: цель
    сброса ищется по тому же шаблону, и разойдись они — тест бы этого не
    заметил.
    """
    shas = {
        round_no: _commit_file(
            root, f"round_{round_no}.py", ROUND_COMMIT_TEMPLATE.format(round=round_no)
        )
        for round_no in (1, 2, 3)
    }
    for index, subject in enumerate(_LOOKALIKE_SUBJECTS):
        _commit_file(root, f"lookalike_{index}.py", subject)
    shas[4] = _commit_file(root, "round_4.py", ROUND_COMMIT_TEMPLATE.format(round=4))
    return shas


def _write_session_artifacts(root: Path) -> Path:
    """Журнал сессии: `session.json`, `events.jsonl`, артефакт раунда."""
    session = root / _SESSION_DIR
    (session / "rounds" / "001").mkdir(parents=True, exist_ok=True)
    (session / "session.json").write_text(_SESSION_JSON, encoding="utf-8")
    (session / "events.jsonl").write_text(
        '{"type": "state_change"}\n', encoding="utf-8"
    )
    (session / "rounds" / "001" / "proposal.md").write_text(_PROPOSAL, encoding="utf-8")
    return session


def _is_ignored(root: Path, relative: str) -> bool:
    """Игнорирует ли git путь `relative` (`.gitignore`/`info/exclude`)."""
    completed = subprocess.run(
        ["git", "check-ignore", "--quiet", relative],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def test_first_round_targets_the_configured_base_commit(git_repo: Path) -> None:
    """`base_rev(1)` — `base_commit` из конфига, а не текущий `HEAD`."""
    base_commit = _head_sha(git_repo)
    _round_history(git_repo)

    target = _base_rev(git_repo, 1, base_commit=base_commit)

    assert base_commit != _head_sha(git_repo), (
        "проверка вакуумна: base_commit совпал с HEAD, и молчаливый откат к "
        "HEAD прошёл бы незамеченным"
    )
    assert target == base_commit, (
        "раунд 1 сбрасывается не на base_commit сессии ([DESIGN-012]): "
        f"{target!r} вместо {base_commit!r}"
    )


def test_later_round_targets_the_previous_round_commit(git_repo: Path) -> None:
    """`base_rev(4)` — коммит с сообщением `disputatio: round 003`."""
    base_commit = _head_sha(git_repo)
    shas = _round_history(git_repo)
    assert len({base_commit, shas[2], shas[3], shas[4]}) == 4, (
        "проверка вакуумна: коммиты истории совпали, и промах на раунд "
        "остался бы невидимым"
    )

    target = _base_rev(git_repo, 4, base_commit=base_commit)

    assert target == shas[3], (
        "цель сброса раунда 4 — не коммит раунда 003 ([DESIGN-012]); "
        f"{target!r} при round 003 == {shas[3]!r}, round 004 == {shas[4]!r}, "
        f"base_commit == {base_commit!r}"
    )
    assert _subject(git_repo, target) == "disputatio: round 003", (
        "сообщение целевого коммита — не `disputatio: round 003`: сброс идёт "
        f"на посторонний коммит:\n{_subject(git_repo, target)!r}"
    )


def test_missing_round_commit_raises_a_domain_error(git_repo: Path) -> None:
    """Коммита раунда нет — доменная ошибка, а не `HEAD` и не двойник."""
    base_commit = _head_sha(git_repo)
    for index, subject in enumerate(_LOOKALIKE_SUBJECTS):
        _commit_file(git_repo, f"lookalike_{index}.py", subject)

    with pytest.raises(_domain_error("BaseRevisionNotFound")) as exc_info:
        _base_rev(git_repo, 4, base_commit=base_commit)

    assert ROUND_COMMIT_TEMPLATE.format(round=3) in str(exc_info.value), (
        "текст ошибки не называет ненайденный коммит раунда — по отчёту "
        f"причину не восстановить (NFR-003):\n{exc_info.value}"
    )


def test_unknown_base_commit_raises_a_domain_error(git_repo: Path) -> None:
    """`base_commit`, которого нет в репозитории, — тоже доменная ошибка."""
    with pytest.raises(_domain_error("BaseRevisionNotFound")) as exc_info:
        _base_rev(git_repo, 1, base_commit=_MISSING_SHA)

    assert _MISSING_SHA in str(exc_info.value), (
        f"текст ошибки не называет неразрешимую ревизию:\n{exc_info.value}"
    )


def test_round_below_one_has_no_base_rev(git_repo: Path) -> None:
    """Раунды нумеруются с 1: раунд 0 — не `base_commit` молча, а отказ."""
    base_commit = _head_sha(git_repo)

    with pytest.raises(DisputatioError) as exc_info:
        _base_rev(git_repo, 0, base_commit=base_commit)

    assert not isinstance(exc_info.value, GitCommandError), (
        "отказ пришёл от git, а не от проверки номера раунда: `base_rev` "
        f"успел построить ревизию из раунда 0:\n{exc_info.value}"
    )


def test_reset_hard_rolls_the_tree_back_to_the_target(git_repo: Path) -> None:
    """Правка tracked-файла откатывается, `HEAD` встаёт на целевой коммит."""
    shas = _round_history(git_repo)
    readme = git_repo / "README.md"
    readme_before = readme.read_text(encoding="utf-8")
    readme.write_text(_ABORTED_LINE, encoding="utf-8")
    (git_repo / "round_3.py").write_text(_ABORTED_LINE, encoding="utf-8")
    _git(git_repo, "add", "round_3.py")
    assert _git(git_repo, "status", "--porcelain").strip(), (
        "проверка вакуумна: дерево и без сброса чисто"
    )

    _reset_hard(git_repo, shas[3])

    assert _head_sha(git_repo) == shas[3], (
        f"HEAD не встал на цель сброса: {_head_sha(git_repo)!r} вместо {shas[3]!r}"
    )
    assert readme.read_text(encoding="utf-8") == readme_before, (
        "правка прерванной попытки пережила сброс — вызов не `--hard`: она "
        f"утечёт в changes.patch следующей попытки:\n{readme.read_text()!r}"
    )
    assert (git_repo / "round_3.py").read_text(encoding="utf-8") == _body(
        ROUND_COMMIT_TEMPLATE.format(round=3)
    ), (
        "содержимое файла целевого раунда не восстановлено: работа, попавшая "
        "в индекс, пережила сброс"
    )
    assert not (git_repo / "round_4.py").exists(), (
        "файл более позднего коммита остался в дереве — сброс не тронул "
        "историю выше цели"
    )
    assert _git(git_repo, "status", "--porcelain").strip() == "", (
        f"дерево после сброса не чисто:\n{_git(git_repo, 'status', '--porcelain')}"
    )


def test_reset_hard_reports_an_unresolvable_target(git_repo: Path) -> None:
    """Несуществующая цель — `GitCommandError`, а не тихий no-op."""
    head = _head_sha(git_repo)

    with pytest.raises(GitCommandError):
        _reset_hard(git_repo, _MISSING_SHA)

    assert _head_sha(git_repo) == head, (
        "HEAD сдвинулся на неудавшемся сбросе — дерево осталось в неизвестном состоянии"
    )


def test_clean_removes_the_untracked_leftovers(git_repo: Path) -> None:
    """Новый untracked-файл прерванной попытки удаляется; tracked — нет."""
    readme = git_repo / "README.md"
    readme_before = readme.read_text(encoding="utf-8")
    stray = git_repo / "draft.py"
    stray.write_text(_ABORTED_LINE, encoding="utf-8")
    stray_dir = git_repo / "drafts"
    stray_dir.mkdir()
    (stray_dir / "more.py").write_text(_ABORTED_LINE, encoding="utf-8")

    _clean(git_repo)

    assert not stray.exists(), (
        "новый untracked-файл пережил уборку: `git diff HEAD` увидит его через "
        "intent-to-add, и он станет работой автора следующей попытки"
    )
    assert not stray_dir.exists(), (
        "каталог с untracked-файлами пережил уборку — вызову нужен `-d`"
    )
    assert readme.read_text(encoding="utf-8") == readme_before, (
        "уборка тронула tracked-файл: `clean` убирает только незакоммиченное новое"
    )


def test_session_directory_survives_reset_and_clean(git_repo: Path) -> None:
    """`.disputatio/` переживает сброс и уборку до первого принятого раунда."""
    base_commit = _head_sha(git_repo)
    session = _write_session_artifacts(git_repo)
    (git_repo / "README.md").write_text(_ABORTED_LINE, encoding="utf-8")
    assert not _is_ignored(git_repo, f"{_SESSION_DIR}/session.json"), (
        "проверка вакуумна: каталог сессии уже игнорируется git'ом, и уборка "
        "обошла бы его без явного исключения"
    )

    _reset_hard(git_repo, base_commit)
    _clean(git_repo)

    assert (session / "session.json").read_text(encoding="utf-8") == _SESSION_JSON, (
        "session.json не пережил reset+clean — resume потерял состояние сессии"
    )
    assert (session / "events.jsonl").is_file(), (
        "events.jsonl не пережил reset+clean — единственный фид UI и resume стёрт"
    )
    proposal = session / "rounds" / "001" / "proposal.md"
    assert proposal.read_text(encoding="utf-8") == _PROPOSAL, (
        "артефакты принятого раунда стёрты уборкой: история сессии "
        "append-only, а не расходный материал"
    )


def test_clean_sweeps_the_whole_repository(git_repo: Path) -> None:
    """`root` — подкаталог: уборка идёт по всему репозиторию, минуя сессию.

    `diff_head` считает дифф от корня репозитория (`:/`), поэтому untracked
    вне `root` — такая же работа автора; переживи он уборку, следующая
    попытка унаследовала бы его в `changes.patch`.
    """
    root = git_repo / "workdir"
    root.mkdir()
    (root / "tracked.py").write_text("VALUE = 0\n", encoding="utf-8")
    _git(git_repo, "add", "workdir/tracked.py")
    _git(git_repo, "commit", "--quiet", "-m", "workdir")
    session = _write_session_artifacts(root)
    (git_repo / "stray_at_top.py").write_text(_ABORTED_LINE, encoding="utf-8")
    (root / "stray_in_root.py").write_text(_ABORTED_LINE, encoding="utf-8")

    _clean(root)

    assert not (root / "stray_in_root.py").exists(), (
        "untracked-файл внутри root пережил уборку"
    )
    assert not (git_repo / "stray_at_top.py").exists(), (
        "untracked-файл вне root пережил уборку: `diff_head` его покажет, и "
        "следующая попытка унаследует чужой черновик"
    )
    assert (session / "session.json").read_text(encoding="utf-8") == _SESSION_JSON, (
        "каталог сессии в подкаталоге не защищён: исключение уборки считает "
        "путь от корня репозитория, а `.disputatio/` лежит в root"
    )


def test_diff_head_is_empty_after_reset_and_clean(git_repo: Path) -> None:
    """Следующая попытка не наследует правок предыдущей ([REQ-012])."""
    base_commit = _head_sha(git_repo)
    shas = _round_history(git_repo)
    _write_session_artifacts(git_repo)
    (git_repo / "README.md").write_text(_ABORTED_LINE, encoding="utf-8")
    (git_repo / "draft.py").write_text(_ABORTED_LINE, encoding="utf-8")
    assert GitCli(git_repo).diff_head() != "", (
        "проверка вакуумна: дифф прерванной попытки пуст ещё до сброса"
    )

    _reset_hard(git_repo, _base_rev(git_repo, 4, base_commit=base_commit))
    _clean(git_repo)

    assert GitCli(git_repo).diff_head() == "", (
        "после сброса дифф не пуст — попытка стартует с чужими правками:\n"
        f"{GitCli(git_repo).diff_head()}"
    )
    assert _head_sha(git_repo) == shas[3], (
        f"дерево сброшено не на коммит раунда 003: {_head_sha(git_repo)!r}"
    )


def test_reset_and_clean_are_idempotent(git_repo: Path) -> None:
    """Второй прогон пары даёт то же состояние, а не новое ([REQ-015])."""
    base_commit = _head_sha(git_repo)
    shas = _round_history(git_repo)
    (git_repo / "README.md").write_text(_ABORTED_LINE, encoding="utf-8")
    (git_repo / "draft.py").write_text(_ABORTED_LINE, encoding="utf-8")

    for _ in range(2):
        _reset_hard(git_repo, _base_rev(git_repo, 4, base_commit=base_commit))
        _clean(git_repo)

    assert _head_sha(git_repo) == shas[3], (
        f"повторный сброс увёл HEAD с цели: {_head_sha(git_repo)!r} вместо {shas[3]!r}"
    )
    assert _git(git_repo, "status", "--porcelain").strip() == "", (
        "повторный прогон оставил дерево грязным:\n"
        f"{_git(git_repo, 'status', '--porcelain')}"
    )
