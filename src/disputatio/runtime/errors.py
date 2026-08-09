"""Иерархия доменных ошибок runtime ([DESIGN-020], [REQ-020], NFR-003).

Одна база на весь workstream: CLI ловит `DisputatioError`, печатает одну
строку `.args[0]` в stderr и возвращает `2`. Голый traceback пользователю не
показывается никогда — но и не проглатывается: он уходит событием `error` в
`events.jsonl`, где ему и место.

Каждый класс здесь заменяет стандартное исключение, которое иначе всплыло бы
наружу техническим мусором: `CalledProcessError` вместо «дерево грязное»,
`KeyError` вместо «нет такого адаптера», `TOMLDecodeError` вместо «битый
config.toml».
"""


class DisputatioError(Exception):
    """База доменных ошибок runtime; CLI печатает `.args[0]`, не traceback."""


class DirtyWorkingTree(DisputatioError):
    """Pre-flight: в рабочем дереве есть незакоммиченные tracked-правки."""


class NotAGitRepository(DisputatioError):
    """Pre-flight: `root` не является git-репозиторием."""


class EmptyRepository(DisputatioError):
    """Pre-flight: в репозитории нет ни одного коммита — `HEAD` не разрешается.

    Отдельный класс, а не `NotAGitRepository`: репозиторий тут настоящий, и
    пользователю нужно услышать «сделайте первый коммит», а не «это не git».
    """


class BaseRevisionNotFound(DisputatioError):
    """Цель `git reset` перед PROPOSING не вычисляется ([DESIGN-012]).

    Либо `base_commit` из конфига не разрешается в коммит, либо в истории
    нет коммита предыдущего раунда. Отдельный класс, а не `GitCommandError`:
    сбоя git тут нет — есть история, которая не совпала с ожиданиями сессии,
    и молчаливый откат к `HEAD` вместо ошибки стёр бы принятую работу.
    """


class GitCommandError(DisputatioError):
    """git-команда не выполнилась: ненулевой код либо git отсутствует в PATH.

    Текст несёт и команду, и stderr.

    Замена `CalledProcessError`, чей `__str__` печатает только код возврата:
    без stderr причина сбоя не восстанавливается ни из отчёта, ни из
    `events.jsonl` (NFR-003).
    """


class SessionNotFound(DisputatioError):
    """Resume: сессии с таким `session_id` в `.disputatio/` нет."""


class UnknownAdapterError(DisputatioError):
    """Композиция: в конфиге назван адаптер, которого нет в реестре.

    Отдельный класс, а не `KeyError`: опечатка в `config.toml` — ошибка
    пользователя, и её текст обязан называть и введённое имя, и список
    доступных, а не быть repr'ом ключа в кавычках.
    """


class ConfigError(DisputatioError):
    """Конфиг сессии не читается или неполон (битый `config.toml`)."""


class ReviewParseError(DisputatioError):
    """Вывод ревьюера не разобрался в `review.json` после всех retry."""
