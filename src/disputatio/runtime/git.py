"""Порт git-операций и pre-flight ([DESIGN-010]…[DESIGN-013], ADR-005).

`GitOps` — пятый порт оркестратора, объявленный в `runtime`, а не в
замороженных `contracts`: git-дисциплина §3 принадлежит циклу, а не схемам
артефактов. Здесь же живёт единственная реализация порта `GitCli` и
`preflight` — три проверки, без которых `changes.patch` перестаёт быть
диффом автора.

Все команды идут по одному протоколу ([DESIGN §4.2]): `subprocess.run` без
`shell`, argv-списком, с герметичным окружением и явной идентичностью через
`-c user.name=… -c user.email=…`. Идентичность передаётся флагами, а не
`git config`: сессия не вправе править конфиг пользовательского
репозитория, а унаследованные `GIT_AUTHOR_*`/`GIT_CONFIG_COUNT` перебили бы
её молча — поэтому окружение не наследуется, а собирается.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from disputatio.runtime.errors import (
    BaseRevisionNotFound,
    DirtyWorkingTree,
    EmptyRepository,
    GitCommandError,
    NotAGitRepository,
)

GIT_USER_NAME: Final = "disputatio"
GIT_USER_EMAIL: Final = "disputatio@localhost"

# Имя служебного каталога сессии. Дублирует `events.paths.SESSION_DIR_NAME`
# намеренно: `paths` объявлен внутренней деталью раскладки `.disputatio/` и
# наружу пакетом `events` не экспортируется, а git-дисциплина обязана знать,
# что из дерева исключить.
SESSION_DIR_NAME: Final = ".disputatio"

# Pathspec диффа: всё дерево репозитория минус каталог сессии. Исключающий
# pathspec без положительного ничего не отбирает, поэтому пара неразделима.
# `:/` берёт дерево целиком: дерево было чисто на pre-flight, значит правка
# вне `root` — тоже работа автора и обязана попасть в ревью. Исключение,
# наоборот, идёт БЕЗ магии `top`: она считала бы `.disputatio` от корня
# репозитория, а каталог сессии лежит в `root`, и совпадают эти два пути
# только когда `root` и есть toplevel — чего `preflight` не требует.
# `.disputatio/` — журнал оркестратора, а не работа автора: попав в патч,
# он стал бы предметом ревью, а попав в индекс через intent-to-add — частью
# коммита раунда.
_TREE_PATHSPEC: Final = (":/", f":(exclude){SESSION_DIR_NAME}")

# Флаги, отключающие влияние ЛОКАЛЬНОГО `.git/config` на форму патча:
# `_env` гасит системный и глобальный конфиг, но `.git/config` лежит внутри
# рабочей директории и в дифф не попадает — подмена была бы невидима.
# `diff.external` подменяет весь вывод выводом произвольной программы (и
# запускает её), `color.ui = always` вклеивает ANSI-escape'ы, `noprefix`/
# `mnemonicPrefix` ломают заголовок `--- a/… +++ b/…`, `diff.relative`
# вырезает всё вне `cwd`, а textconv отдаёт вместо содержимого пересказ.
_DIFF_FLAGS: Final = (
    "--no-ext-diff",
    "--no-color",
    "--no-textconv",
    "--no-relative",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)

# Формат сообщения коммита раунда — единственная константа, из которой
# выводится и запись, и поиск ([DESIGN-011]). `:03d` держит `NNN` в том же
# виде, что и имя каталога `rounds/NNN/`: разойдись padding — история и диск
# назвали бы раунд по-разному, а `base_rev(N)` ([DESIGN-012]) искал бы
# несуществующий коммит. Якоря в шаблоне поиска обязательны: без них
# `disputatio: round 0031` и `fixup! disputatio: round 003` считались бы
# коммитом раунда.
ROUND_COMMIT_TEMPLATE: Final = "disputatio: round {round:03d}"
ROUND_COMMIT_PATTERN: Final = r"^disputatio: round [0-9]{3}$"

# Правило, скрывающее каталог сессии от git. Пишется в `.git/info/exclude` —
# файл локальный, в дерево не входит и в чужой `.gitignore` не лезет: сессия
# не вправе править версионируемые файлы пользователя. Без ведущего слэша
# правило не привязано к toplevel: `root` не обязан быть корнем репозитория.
_EXCLUDE_ENTRY: Final = f"{SESSION_DIR_NAME}/"

_IDENTITY_ARGS: Final = (
    "-c",
    f"user.name={GIT_USER_NAME}",
    "-c",
    f"user.email={GIT_USER_EMAIL}",
)

# Переменные окружения, каждая из которых перебивает то, что вызов
# настраивает сам. Расположение репозитория: при абсолютном `GIT_DIR`
# команда отработает успешно, но в ЧУЖОМ репозитории — `cwd` его не
# перебивает. Подпись: `GIT_AUTHOR_NAME` сильнее `-c user.name`, и
# идентичность из `_IDENTITY_ARGS` оказалась бы декоративной.
# `GIT_CONFIG_COUNT` — конфиг прямо из окружения, по приоритету он выше
# локального `.git/config` и не отключается ни `GIT_CONFIG_GLOBAL`, ни
# `GIT_CONFIG_NOSYSTEM`; без счётчика git не читает пары `GIT_CONFIG_KEY_n`.
_DROPPED_ENV_VARS: Final = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
    "GIT_CONFIG_COUNT",
)


@runtime_checkable
class GitOps(Protocol):
    """Порт git-операций рабочего репозитория (SPEC-001 §3)."""

    def diff_head(self) -> str:
        """`git diff HEAD` — дифф рабочего дерева; пустая строка валидна."""
        ...

    def commit_round(self, round_no: int) -> None:
        """Коммитит принятый раунд сообщением `disputatio: round NNN`."""
        ...

    def reset_hard(self, rev: str) -> None:
        """`git reset --hard <rev>` — откат к коммиту прошлого раунда."""
        ...

    def clean(self) -> None:
        """Удаляет untracked-файлы прерванной попытки, сохраняя `.disputatio/`."""
        ...


def preflight(root: Path) -> None:
    """Три проверки перед стартом сессии; успех — молча ([REQ-010]).

    Порядок из [DESIGN-010]: `root` — репозиторий, дерево чисто по
    tracked-файлам, `HEAD` существует. Untracked-файлы старт **не**
    блокируют: `.disputatio/` сама untracked, а требование «удалите
    черновики» сделало бы инструмент недружелюбным.

    Функция ничего не создаёт: `bootstrap_session` вызывается строго после
    неё, поэтому отказ не оставляет `.disputatio/` в чужом репозитории.
    """
    if not root.is_dir():
        raise NotAGitRepository(f"{root} — не каталог: стартовать сессию негде")
    if _run(root, "rev-parse", "--git-dir").returncode != 0:
        raise NotAGitRepository(
            f"{root} — не git-репозиторий; disputatio коммитит каждый принятый "
            "раунд, поэтому рабочая директория обязана быть под git"
        )
    status = _checked(root, "status", "--porcelain", "--untracked-files=no")
    if status.strip():
        raise DirtyWorkingTree(
            f"рабочее дерево {root} содержит незакоммиченные изменения "
            "tracked-файлов — закоммитьте или спрячьте их (`git stash`), "
            "иначе они попадут в changes.patch как работа автора:\n"
            f"{status.rstrip()}"
        )
    if _run(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        raise EmptyRepository(
            f"в репозитории {root} нет ни одного коммита — `git diff HEAD` и "
            "`git reset` не к чему привязать; сделайте первый коммит"
        )


def base_rev(root: Path, round_no: int, *, base_commit: str) -> str:
    """Цель `git reset` перед PROPOSING раунда `round_no` ([DESIGN-012]).

    Вычисляется, а не хранится в изменяемом состоянии ([REQ-012]):

        base_rev(N) = коммит «disputatio: round (N-1):03d»   при N > 1
                    = base_commit                            при N == 1

    Оба источника лежат на диске — история git и снапшот `config.toml`, —
    поэтому цель восстанавливается после перезапуска процесса и шаг остаётся
    идемпотентным ([REQ-015]).

    Возвращается полный SHA, а не то, что подали на вход: `base_commit` в
    конфиге вправе быть сокращённым, а `git reset` следующего раунда обязан
    получить однозначную ревизию.

    Ненайденная цель — `BaseRevisionNotFound`. Молчаливый откат к `HEAD`
    был бы худшим из возможных ответов: на входе в раунд `HEAD` — это
    состояние прерванной попытки, и сброс на него сохранил бы ровно то, от
    чего сброс избавляет.
    """
    if round_no < 1:
        raise BaseRevisionNotFound(
            f"раунды нумеруются с 1, а цель сброса запрошена для раунда "
            f"{round_no} — вычислять её не из чего"
        )
    if round_no == 1:
        return _resolve_base_commit(root, base_commit)
    subject = ROUND_COMMIT_TEMPLATE.format(round=round_no - 1)
    found = _find_round_commit(root, subject)
    if found is None:
        raise BaseRevisionNotFound(
            f"в истории {root} нет коммита «{subject}» — раунд {round_no} "
            "сбрасывать не на что; история сессии оборвана либо переписана"
        )
    return found


def _resolve_base_commit(root: Path, base_commit: str) -> str:
    """Цель раунда 1: `base_commit`, но только если он в истории `HEAD`.

    Проверка достижимости — та же, что для коммита раунда, где поиск идёт по
    предкам `HEAD` намеренно. Без неё половины `base_rev` расходятся:
    `disputatio: round NNN` с чужой ветки целью не становится, а
    `base_commit` с чужой ветки — становится, и `git reset --hard` уводит на
    неё ТЕКУЩУЮ ветку, оставляя её коммиты недостижимыми ни из одной ссылки.

    По [DESIGN-014] `base_commit` — это `HEAD` на старте сессии, то есть в
    норме предок `HEAD` всегда. Расхождение означает, что историю под
    сессией подменили (`rebase`, `amend`, смена ветки между запусками), и
    сброс на записанный SHA стёр бы именно эту работу.
    """
    resolved = _resolve_commit(root, base_commit)
    completed = _run(root, "merge-base", "--is-ancestor", resolved, "HEAD")
    if completed.returncode == 0:
        return resolved
    if completed.returncode != 1:
        # Не «нет», а сбой: `--is-ancestor` отвечает 0/1, всё остальное —
        # битая объектная база или отсутствующий `HEAD`, и молчать об этом
        # значит выдать «другая ветка» за диагноз.
        raise GitCommandError(
            f"git merge-base --is-ancestor {resolved} HEAD упал с кодом "
            f"{completed.returncode}: {(completed.stderr or '').strip()}"
        )
    raise BaseRevisionNotFound(
        f"base_commit {base_commit} не лежит в истории HEAD репозитория "
        f"{root}: сессия стартовала на другой ветке либо историю переписали "
        "(rebase/amend), и сброс на него увёл бы текущую ветку с её коммитов"
    )


def _resolve_commit(root: Path, rev: str) -> str:
    """Полный SHA коммита `rev`; неразрешимая ревизия — доменная ошибка.

    `^{commit}` обязателен: без него `rev-parse` подтвердит и тег, и дерево,
    а `git reset --hard` на не-коммит ушёл бы в git-ошибку на шаг позже —
    уже внутри PROPOSING, а не при вычислении цели.
    """
    completed = _run(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if completed.returncode != 0:
        raise BaseRevisionNotFound(
            f"ревизия {rev} не разрешается в коммит репозитория {root}: "
            "сессия стартовала в другом репозитории либо история переписана"
        )
    return completed.stdout.strip()


def _find_round_commit(root: Path, subject: str) -> str | None:
    """SHA ближайшего к `HEAD` коммита с сообщением ровно `subject`.

    `--grep` только сужает выборку (`--fixed-strings` — чтобы шаблон не
    зависел от диалекта регулярок в конкретной сборке git), а решает
    **точное** сравнение заголовка в Python: по вхождению подстроки
    `fixup! disputatio: round 003` и `disputatio: round 003 (wip)` прошли бы
    за коммит раунда, и сброс ушёл бы на чужую работу. Оно же делает разбор
    нечувствительным к лишним строкам, которые подмешивает локальный конфиг
    пользователя (`log.showSignature`): ни одна из них не совпадёт с
    сообщением целиком.

    Поиск идёт по предкам `HEAD`, а не по всем ссылкам: чужая ветка — в том
    числе оставшаяся от прошлой сессии в этом же репозитории — вправе нести
    своё «disputatio: round NNN», и сброс текущей ветки на неё стёр бы
    принятую работу. Коммит раунда, ставший недостижимым после сброса,
    целью по той же причине уже не является.

    Завершающий `--` обязателен по той же причине, что и в `reset_hard`:
    `HEAD` — и ревизия, и допустимое имя файла, и в репозитории, где такой
    файл есть, git отказывается угадывать (`ambiguous argument 'HEAD'`).
    Имя приходит из пользовательского репозитория, а не из сессии, поэтому
    разделитель — единственный способ на него не наткнуться.

    `--encoding=UTF-8` закрывает ту же дыру, что `_DIFF_FLAGS` закрывают для
    патча: `i18n.logOutputEncoding` лежит в ЛОКАЛЬНОМ `.git/config`, который
    `_env` не гасит, и перекодирует сообщение ДО того, как по нему пройдёт
    `--grep`. Итог был бы не диагностируемым: поиск не находит ничего, и
    сессия обвиняет историю пользователя в том, что она переписана.

    Отсутствие — `None`: решение, ошибка это или нет, принимает вызывающий.
    """
    log = _checked(
        root,
        "log",
        "--encoding=UTF-8",
        "--format=%H %s",
        "--fixed-strings",
        f"--grep={subject}",
        "HEAD",
        "--",
    )
    for line in log.splitlines():
        sha, _, found = line.partition(" ")
        if found == subject:
            return sha
    return None


@dataclass(frozen=True, slots=True)
class GitCli:
    """`GitOps` поверх git CLI: единственная реализация порта (ADR-005).

    Остальные методы порта приходят своими задачами ([DESIGN-011],
    [DESIGN-012]) — заглушки здесь нужны, чтобы `RuntimeDeps.git` уже
    удовлетворял `GitOps`, и при этом не отдают поведения, которое эти
    задачи обязаны доказать собственным red-чекпоинтом.
    """

    root: Path

    def diff_head(self) -> str:
        """`git diff HEAD` — unified-дифф рабочего дерева ([DESIGN-013]).

        Диффу предшествует `git add -N` (intent-to-add): `git diff HEAD`
        сравнивает `HEAD` с индексом и рабочим деревом, а untracked-файлов
        не видит ни та, ни другая сторона — без intent-to-add созданный
        автором модуль исчез бы из ревью бесследно. Записи intent-to-add
        содержимого не несут, поэтому коммит раунда они не подменяют.

        Форма патча задаётся флагами, а не конфигом репозитория
        (`_DIFF_FLAGS`): `changes.patch` — предмет ревью, и он обязан быть
        одним и тем же unified-диффом в любом пользовательском репозитории.

        Пустая строка — валидный результат (режим `analyze`): артефакт
        пишется всегда, в том числе пустым, и шаг ошибкой не считается
        ([REQ-013]).

        Каталог сессии снимается с индекса отдельным шагом, а не исключающим
        pathspec на месте `add`: со второго раунда `.disputatio/` уже лежит
        в `.git/info/exclude` ([DESIGN-011]), а `git add` с явно названным
        игнорируемым путём — пусть даже названным в `:(exclude)` — считает
        это ошибкой и возвращает код 1. Дифф исключающий pathspec сохраняет:
        игнор на `git diff` не влияет, а каталог сессии обязан остаться вне
        патча и когда он в репозитории пользователя уже отслеживается.
        """
        _checked(self.root, "add", "--intent-to-add", "--", ":/")
        _unstage_session_dir(self.root)
        return _checked(self.root, "diff", *_DIFF_FLAGS, "HEAD", "--", *_TREE_PATHSPEC)

    def commit_round(self, round_no: int) -> None:
        """Коммитит принятый раунд сообщением шаблона ([REQ-011], [DESIGN-011]).

        Ровно один коммит на принятый раунд: он же — цель `git reset`
        следующего раунда ([DESIGN-012]), поэтому лишний коммит сдвинул бы
        базу на состояние, работы автора не содержащее.

        Пустой дифф коммита не создаёт и ошибкой не считается: analyze-раунд
        правок не делает, а повторный вызов после прерывания обязан давать
        то же состояние ([REQ-015]). Отсюда же порядок: сначала `git add`,
        потом проверка индекса — «нечего коммитить» видно только по индексу,
        а `git commit` в этом случае упал бы кодом 1.

        `.disputatio/` из коммита исключена дважды: правилом в
        `.git/info/exclude` и снятием каталога с индекса сразу после `add`.
        Первого хватило бы, но оно живёт в файле, который пользователь вправе
        отредактировать, а уже отслеживаемые артефакты игнор и вовсе не
        скрывает. Исключающий pathspec на месте `add` не годится: он отбирает
        файлы правильно, но сам факт совпадения `:/` с игнорируемым каталогом
        git считает ошибкой и возвращает код 1.
        """
        _exclude_session_dir(self.root)
        _checked(self.root, "add", "--all")
        _unstage_session_dir(self.root)
        staged = _checked(
            self.root, "diff", "--cached", "--name-only", "HEAD", "--", *_TREE_PATHSPEC
        )
        if not staged.strip():
            return
        _checked(
            self.root,
            "commit",
            "--quiet",
            # Форма коммита не зависит от чужого конфига и чужих скриптов:
            # `commit.gpgsign` в локальном `.git/config` сорвал бы неинтер-
            # активную сессию запросом ключа, а pre-commit-хук пользователя
            # либо отверг бы коммит раунда, либо переписал файлы уже ПОСЛЕ
            # снятого `changes.patch` — ревью читало бы не то, что в истории.
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            ROUND_COMMIT_TEMPLATE.format(round=round_no),
        )

    def reset_hard(self, rev: str) -> None:
        """`git reset --hard <rev>` — сброс дерева к цели ([DESIGN-012]).

        Именно `--hard`: `--mixed` и `--soft` оставили бы правку прерванной
        попытки в рабочем дереве, и следующий `changes.patch` предъявил бы
        ревьюеру чужую работу как работу автора.

        `.disputatio/` сброс не трогает: каталог сессии не отслеживается, а
        `reset` работает по tracked-файлам. Untracked-файлы прерванной
        попытки он по той же причине не убирает — это дело `clean`.

        Завершающий `--` отделяет ревизию от путей: `rev`, совпавший с
        именем файла в дереве, без него неоднозначен, и git отказал бы в
        сбросе (`Cannot do hard reset with paths`) вместо того, чтобы
        сбросить дерево на одноимённый коммит.

        `--end-of-options` закрывает вторую половину той же щели: `rev`,
        начинающийся с дефиса, git разбирает как ОПЦИЮ, и `reset --hard …
        --mixed` тихо становится mixed-сбросом с кодом 0 — метод обещает
        `--hard`, а правка прерванной попытки остаётся в дереве. Отказ
        (`Failed to resolve '--mixed'`) — единственный честный ответ.
        """
        _checked(self.root, "reset", "--hard", "--quiet", "--end-of-options", rev, "--")

    def clean(self) -> None:
        """Убирает untracked-файлы прерванной попытки ([DESIGN-012]).

        Без уборки новые файлы пережили бы `reset_hard` (он их не видит) и
        попали бы в `changes.patch` следующей попытки: `diff_head` тянет
        untracked через intent-to-add, и чужой черновик стал бы работой
        автора — идемпотентность шага ([REQ-015]) на этом кончается.

        Область та же, что у `diff_head`, — `_TREE_PATHSPEC`: всё дерево
        репозитория (`:/`) минус каталог сессии. Правка вне `root` в патч
        попадает, значит и уборкой должна сниматься; разойдись области,
        `changes.patch` показывал бы одно множество файлов, а уборка
        снимала другое.

        Каталог сессии закрыт вдобавок `--exclude`, и это не дубль
        pathspec'а: когда сам `root` — untracked-каталог (сессия в свежем
        подкаталоге чужого репозитория), под `:/` попадает он целиком, а
        исключение пути внутри него уже ничего не решает. Гейт здесь —
        gitignore-шаблон, он же спасает журнал до первого принятого раунда,
        пока правила в `.git/info/exclude` ещё нет ([DESIGN-011]).

        Два исключения совпадают не буквально, и это осознанно: `:(exclude)`
        считается от `cwd` и закрывает только `<root>/.disputatio`, а
        gitignore-шаблон без ведущего слэша — каталог с таким именем на
        любой глубине. Шире — в безопасную сторону: `.disputatio` где угодно
        в репозитории есть журнал оркестратора, а не работа автора, и
        сносить журнал соседней сессии уборка не вправе. Обратная сторона
        известна: такой соседний журнал `diff_head` всё ещё покажет в патче
        (его исключение — узкое), и уборка его не снимет. Закрывается это на
        стороне `_TREE_PATHSPEC`, то есть в области `changes.patch`, а не
        расширением того, что уборка удаляет.

        Untracked-каталог со своим `.git` внутри `-d` пропускает (для его
        удаления нужен `-ff`), поэтому вложенный репозиторий, созданный
        прерванной попыткой, уборку переживает и остаётся виден в следующем
        `changes.patch` как gitlink. Удалять чужой клон уборкой раунда
        опаснее, чем показать его ревьюеру, — `-ff` не передаётся намеренно.

        Игнорируемые файлы не трогаются вовсе: `-x` не передаётся, поэтому
        сборочный мусор пользователя уборка раунда не выносит.
        """
        _checked(
            self.root,
            "clean",
            "--force",
            "-d",
            "--quiet",
            f"--exclude={_EXCLUDE_ENTRY}",
            "--",
            *_TREE_PATHSPEC,
        )


def _unstage_session_dir(root: Path) -> None:
    """Возвращает индексу состояние `HEAD` по `.disputatio/` ([DESIGN-011]).

    Снимает и записи intent-to-add, и содержимое, затянутое `git add --all`:
    `.disputatio/` — журнал оркестратора, а не работа автора, и ни в патч,
    ни в коммит раунда попасть не вправе. Путь передаётся голым, без магии
    `:(exclude)`: он относителен `cwd` (== `root`), а `git add` с явно
    названным игнорируемым путём падает кодом 1. Пустое совпадение ошибкой
    не считается — до первого раунда каталога может ещё не быть.
    """
    _checked(root, "reset", "--quiet", "--", SESSION_DIR_NAME)


def _exclude_session_dir(root: Path) -> None:
    """Прячет `.disputatio/` от git через `.git/info/exclude` ([DESIGN-011]).

    Идемпотентна: правило дописывается только если его там ещё нет — иначе
    каждый принятый раунд наращивал бы файл повторами. Существующее
    содержимое сохраняется целиком: `info/exclude` принадлежит пользователю
    не меньше, чем `.gitignore`, — сессия лишь дописывает свою строку.
    """
    exclude_file = _exclude_file_path(root)
    existing = (
        exclude_file.read_text(encoding="utf-8") if exclude_file.is_file() else ""
    )
    if any(line.strip() == _EXCLUDE_ENTRY for line in existing.splitlines()):
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    exclude_file.write_text(
        f"{existing}{separator}{_EXCLUDE_ENTRY}\n", encoding="utf-8"
    )


def _exclude_file_path(root: Path) -> Path:
    """Путь `info/exclude` репозитория; спрашивается у git, а не собирается.

    Берётся именно `--git-common-dir`: `.git` бывает файлом-ссылкой (submodule,
    `git worktree`), а `info/exclude` git читает из общего каталога, а не из
    приватного каталога worktree. Относительный ответ (`.git` при `root` ==
    toplevel) достраивается от `root`, абсолютный `Path.__truediv__`
    поглощает сам.
    """
    common_dir = _checked(root, "rev-parse", "--git-common-dir").strip()
    return root / common_dir / "info" / "exclude"


def _checked(root: Path, *args: str) -> str:
    """stdout команды; ненулевой код — `GitCommandError` с командой и stderr."""
    completed = _run(root, *args)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise GitCommandError(
            f"git {' '.join(args)} упал с кодом {completed.returncode}: {detail}"
        )
    return completed.stdout


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Запускает git в `root`, не проверяя код возврата ([DESIGN §4.2]).

    Код возврата остаётся вызывающему: для `rev-parse` ненулевой код — это
    ответ «нет» (не репозиторий, нет `HEAD`), а не сбой. Трансляцию сбоя в
    доменную ошибку делает `_checked` — кроме одного случая: отсутствие
    самого клиента кода возврата не даёт, `exec` роняет `FileNotFoundError`
    мимо любой проверки, поэтому он переводится здесь (NFR-003).
    """
    try:
        return subprocess.run(
            ["git", *_IDENTITY_ARGS, *args],
            cwd=root,
            env=_env(),
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise GitCommandError(
            "git не найден в PATH: disputatio ведёт историю сессии коммитами, "
            "поэтому без git-клиента сессия не стартует"
        ) from exc


def _env() -> dict[str, str]:
    """Окружение git-вызова: без унаследованного, с отключённым конфигом."""
    env = dict(os.environ)
    for var in _DROPPED_ENV_VARS:
        env.pop(var, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env
