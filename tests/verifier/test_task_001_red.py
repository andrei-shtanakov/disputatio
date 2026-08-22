"""RED-тест TASK-001: сбой git больше не выглядит как «изменений нет».

Один тест на четыре состояния репозитория (кейсы (а)–(г) [DESIGN-011]):
red-чекпоинт замораживает файл целиком и проверяется одним селектором, так
что разносить состояния по отдельным тест-функциям здесь нельзя.

Числовых кодов возврата в ассертах нет ([REQ-008]): код берётся из
собственного контрольного запуска той же команды в том же состоянии и
проверяется на присутствие в тексте отказа. Измерение 2026-08-22 показало,
почему это обязательно: вне репозитория `git diff --numstat HEAD` даёт 129
(usage-ошибка fallback'а `--no-index`, которому дан один путь), а
репозиторий без коммитов и bare-репозиторий дают ОДИН И ТОТ ЖЕ код 128 при
противоположных требуемых исходах.

Отказ фиксируется явным `assert`, а не `pytest.raises`: последний поднимает
`_pytest.outcomes.Failed`, который не наследует `AssertionError`, а
[REQ-013] требует падения red-фазы именно `AssertionError`.

Подготовка состояний герметична и не наследует чужой `GIT_DIR` (см.
`_prepare_env`); проверяемый код и контрольные запуски, наоборот, окружение
не переопределяют — ровно как `collect_diff_stats`, — поэтому контрольный
код возврата принадлежит тому же репозиторию, что и измеряемый.
"""

import os
import subprocess
from pathlib import Path

from disputatio.contracts.verification import DiffStats
from disputatio.verifier import diffstats

# Герметичный git-конфиг подготовки: `commit.gpgsign`, `core.hooksPath`,
# `includeIf` из конфига разработчика иначе сорвали бы `git commit` по
# причине, не связанной с тестом. Повторяет `_HERMETIC_GIT_ENV` соседних
# файлов осознанно — фикстура `tmp_git_repo` даёт ровно одно состояние, а
# тесту нужны четыре, включая bare и «вне репозитория».
_HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

# Унаследованные переменные перебивают `cwd`: при экспортированном `GIT_DIR`
# подготовка молча положила бы коммиты в ЧУЖОЙ репозиторий. Тот же список
# чистит `conftest._git_env`.
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

_NUMSTAT_ARGV = ("git", "diff", "--numstat", "HEAD")
_WORKTREE_PROBE_ARGV = ("git", "rev-parse", "--is-inside-work-tree")


def _prepare_env() -> dict[str, str]:
    """Окружение команд подготовки: герметичный конфиг, без чужого репозитория."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    return {**env, **_HERMETIC_GIT_ENV}


def _git(workdir: Path, *args: str) -> None:
    """Готовит состояние: `git *args` в `workdir`; ненулевой код — ошибка.

    Сбой пересобирается в `RuntimeError` с stderr: `CalledProcessError`
    печатает только код возврата, и причина падения подготовки иначе не
    видна в отчёте.
    """
    try:
        subprocess.run(
            ["git", *args],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
            env=_prepare_env(),
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"подготовка `git {' '.join(args)}` упала с кодом {exc.returncode}: "
            f"{(exc.stderr or exc.stdout or '').strip()}"
        ) from exc


def _observe(argv: tuple[str, ...], workdir: Path) -> subprocess.CompletedProcess[str]:
    """Контрольный запуск git ровно так, как его делает `collect_diff_stats`.

    Окружение НЕ переопределяется — иначе контрольный код возврата
    относился бы к другому репозиторию, чем измеряемый вызов.
    """
    return subprocess.run(
        argv,
        shell=False,
        cwd=workdir,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _outcome(workdir: Path) -> tuple[DiffStats | None, Exception | None]:
    """Исход `collect_diff_stats`: либо статистика, либо пойманное исключение.

    Ловится `Exception`, а не конкретный класс: требования фиксируют факт
    отказа, но не его тип ([REQ-002]), и ассерт на `RuntimeError` в red-тесте
    сузил бы контракт сильнее, чем спека.
    """
    try:
        return diffstats.collect_diff_stats(workdir), None
    except Exception as exc:  # noqa: BLE001 — класс отказа не задан [REQ-002]
        return None, exc


def test_git_failure_is_distinguished_from_empty_diff(tmp_path: Path) -> None:
    """Четыре состояния репозитория дают три разных исхода, а не один.

    Кейсы (г) и (а) сегодня уже зелёные и стоят первыми как негативный
    контроль: падение на них означало бы сломанную подготовку, а не
    отсутствующее поведение. Кейсы (б) и (в) — собственно red: текущая
    реализация гасит stderr и возвращает `DiffStats(0, 0, 0)` на ЛЮБОЙ
    ненулевой код возврата, поэтому сбой git неотличим от «изменений нет»
    ([REQ-002], [REQ-007]).
    """
    # (г) Успешный путь: репо с коммитом и правками → фактические счётчики.
    # Правки подобраны независимыми от эвристик diff-алгоритма: `grown.txt`
    # только дополняется, `shrunk.txt` только усекается ([REQ-006]).
    changed = tmp_path / "with_changes"
    changed.mkdir()
    _git(changed, "init", "--quiet")
    _git(changed, "config", "user.email", "verifier@tests.local")
    _git(changed, "config", "user.name", "verifier-tests")
    grown = changed / "grown.txt"
    shrunk = changed / "shrunk.txt"
    grown.write_text("alpha\n", encoding="utf-8")
    shrunk.write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(changed, "add", "grown.txt", "shrunk.txt")
    _git(changed, "commit", "--quiet", "-m", "baseline")
    grown.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    shrunk.write_text("one\n", encoding="utf-8")

    stats, exc = _outcome(changed)
    assert exc is None, f"успешный путь обязан оставаться нетронутым, но упал: {exc!r}"
    assert stats is not None
    assert (stats.files, stats.insertions, stats.deletions) == (2, 2, 2)

    # (а) Репо без коммитов, файл в индексе → нули без исключения. Файл
    # заведён в индекс намеренно: изменения есть, точки сравнения нет, и
    # нули обязаны быть решением кода, а не следствием пустого дерева.
    no_head = tmp_path / "no_commits"
    no_head.mkdir()
    _git(no_head, "init", "--quiet")
    (no_head / "first.txt").write_text("alpha\n", encoding="utf-8")
    _git(no_head, "add", "first.txt")

    stats, exc = _outcome(no_head)
    assert exc is None, f"репо без HEAD обязан давать нули, а не отказ: {exc!r}"
    assert stats is not None
    assert (stats.files, stats.insertions, stats.deletions) == (0, 0, 0)

    # (б) Каталог вне git-репозитория → отказ с текстом git и измеренным
    # кодом. «Вне репозитория» подтверждается зондом, а не предполагается:
    # на машине с tmp внутри репозитория кейс иначе молча выродился бы.
    outside = tmp_path / "outside"
    outside.mkdir()
    probe = _observe(_WORKTREE_PROBE_ARGV, outside)
    assert probe.returncode != 0 or probe.stdout.strip() != "true", (
        f"{outside} оказался внутри git-репозитория (зонд: rc="
        f"{probe.returncode}, stdout={probe.stdout.strip()!r}) — кейс выродился"
    )
    outside_control = _observe(_NUMSTAT_ARGV, outside)
    assert outside_control.returncode != 0, (
        "контроль: `git diff --numstat HEAD` вне репозитория обязан падать, "
        f"а дал rc=0 и stdout={outside_control.stdout!r}"
    )

    stats, exc = _outcome(outside)
    assert exc is not None, (
        f"каталог вне git-репозитория: collect_diff_stats вернул {stats!r} "
        "вместо отказа — сбой git неотличим от «изменений нет» ([REQ-002])"
    )
    outside_message = str(exc)
    assert "not a git repository" in outside_message.lower(), (
        "в отказе нет диагностики git (ожидалась строка про «not a git "
        f"repository»), сообщение: {outside_message!r} ([REQ-003])"
    )
    assert str(outside_control.returncode) in outside_message, (
        f"в отказе нет измеренного кода возврата {outside_control.returncode}, "
        f"сообщение: {outside_message!r} ([REQ-003])"
    )

    # (в) Bare-репозиторий → отказ. Регрессия на «128 == законные нули»:
    # код возврата здесь тот же 128, что и у репо без коммитов из кейса
    # (а), а требуемый исход — противоположный ([REQ-007]).
    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "--bare", "--quiet", "bare.git")
    bare_control = _observe(_NUMSTAT_ARGV, bare)
    assert bare_control.returncode != 0, (
        "контроль: `git diff --numstat HEAD` в bare-репозитории обязан падать, "
        f"а дал rc=0 и stdout={bare_control.stdout!r}"
    )

    stats, exc = _outcome(bare)
    assert exc is not None, (
        f"bare-репозиторий: collect_diff_stats вернул {stats!r} вместо отказа "
        "— код 128 принят за «изменений нет» ([REQ-002], [REQ-007])"
    )
    bare_message = str(exc)
    assert "must be run in a work tree" in bare_message.lower(), (
        "в отказе нет диагностики git (ожидалась строка про «must be run in "
        f"a work tree»), сообщение: {bare_message!r} ([REQ-003])"
    )
    assert str(bare_control.returncode) in bare_message, (
        f"в отказе нет измеренного кода возврата {bare_control.returncode}, "
        f"сообщение: {bare_message!r} ([REQ-003])"
    )
