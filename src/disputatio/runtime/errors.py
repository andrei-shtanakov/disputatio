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

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:  # pragma: no cover - только для аннотации, импорта нет
    from disputatio.runtime.pipeline_semantic_proof import ProjectionDiff

#: Причины BEH-15 (WS-disputatio-65): пять способов, которыми доказательство
#: immutable-проекции перестаёт быть доказательством. Закрытый набор, а не
#: свободный текст — `contract`-тест BEH-15 обязан различать их программно,
#: а не парсингом сообщения на естественном языке.
SemanticProofReason = Literal[
    "missing", "digest_mismatch", "parse_error", "contradiction", "unsupported_version"
]

_SEMANTIC_PROOF_REASON_TEXT: Final[dict[SemanticProofReason, str]] = {
    "missing": "артефакт отсутствует либо не читается",
    "digest_mismatch": "содержимое не совпадает с удостоверенным digest",
    "parse_error": "содержимое не разбирается либо не соответствует схеме",
    "contradiction": "источники доказательства взаимно противоречивы",
    "unsupported_version": "версия канонизации не поддерживается этой версией кода",
}


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
    """Текст ревьюера не содержит разбираемого JSON-объекта ([DESIGN-005]).

    Отдельный класс, а не `ValueError`/`json.JSONDecodeError`: «агент
    ответил прозой» и «агент ответил не той схемой» — разные диагнозы с
    разными действиями пользователя, и склеивать их в одно техническое
    исключение значит потерять первый.
    """


class ReviewNotAccepted(DisputatioError):
    """Ревью схемно валидно, но отвергнуто правилами §4.4 ([REQ-005]).

    Не ошибка агента и не ошибка пользователя, а штатный исход шага
    REVIEWING: ревью не принято — значит нужен повтор с перечислением
    причин ([DESIGN-006]). Поэтому причины живут отдельным полем
    `reasons` machine-readable кодами `contracts.REASON_*`, а не только
    внутри текста: следующий промпт собирается из них, и разбирать
    собственное сообщение обратно оркестратор не должен.
    """

    def __init__(self, reasons: Sequence[str], *, round_no: int) -> None:
        """Запоминает коды причин и складывает из них строку для CLI."""
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__(
            f"ревью раунда {round_no:03d} не принято правилами §4.4: "
            f"{', '.join(self.reasons)}"
        )


class ProtectedBranchError(DisputatioError):
    """Предусловие `run`: текущая ветка protected либо detached HEAD (§3.1).

    Отдельный класс, а не `ConfigError`: список `protected_branches` — часть
    конфига, но нарушение здесь — состояние РЕПОЗИТОРИЯ (текущая ветка), не
    конфига. Runner ветку не создаёт (внешний эффект — решение оператора),
    поэтому текст отказа несёт подготовительную команду (`git switch -c
    docs/<slug>`), а не выполняет её.
    """


class PipelineAlreadyExists(DisputatioError):
    """Предусловие `run`: `.disputatio/pipelines/<slug>/` уже есть (§3.1).

    `run` стартует новый пайплайн; продолжение существующего — `resume`, и
    смешивать эти два случая в одну ошибку значило бы прятать от
    пользователя, какую команду ему на самом деле нужно было вызвать.
    """


class ControlPlaneTampered(DisputatioError):
    """Сверка P9 не сошлась: control plane изменился мимо оркестратора (§2 P9).

    Отдельный класс, а не `DirtyWorkingTree`: грязным здесь оказывается не
    рабочее дерево автора, а служебные файлы, которых его ход не касается
    вовсе. Исход один и тот же — `FAILED (invariant_violation)`, — но
    диагноз человеку нужен разный.
    """


class ExternalEditError(DisputatioError):
    """Resume: происхождение состояния дерева не доказано (§8.1).

    Не ошибка пользователя и не сбой инструмента, а требование решения:
    `resume` никогда не выполняет destructive reset поверх состояния,
    которое не сводится ни к чистому дереву, ни к записанному
    `changes.patch`. Текст несёт сам диф — без него человеку не на чем
    выбирать между `--discard-round` и `--adopt-external`.
    """


class AdoptionScopeError(DisputatioError):
    """`--adopt-external`: диф выходит за пару документов (§3.1, fail-closed).

    Отдельный класс, а не `ExternalEditError`: там оркестратор просит
    решения, здесь — отклоняет уже принятое, потому что принять его значило
    бы протащить мимо документной дисциплины правку, которую пайплайн не
    ведёт.
    """


class PipelineNotResumable(DisputatioError):
    """Resume невозможен: терминальный пайплайн, закрытая сессия, нечего решать.

    Одна ошибка на три случая, потому что действие пользователя во всех трёх
    одно — прочитать текст и не повторять команду; различает их сам текст, а
    не тип.
    """


class UnprovableSemantics(DisputatioError):
    """Доказательство immutable-проекции недоказуемо (WS-disputatio-65 BEH-12).

    Отдельный класс, а не `PipelineNotResumable`: тот отвечает за «нечего
    решать» три способами, различимыми только текстом, а здесь `resume`
    обязан различать причину ПРОГРАММНО (`.reason`, BEH-15, `kind: contract`)
    — вызывающий код и будущие тесты не вправе зависеть от формулировки
    сообщения на естественном языке.

    Единственный конструктор для всех пяти причин, а не подкласс на каждую:
    причины делят один и тот же исход — fail-closed без fallback на живой
    конфиг и без автоматической записи/починки доказательства (FR-11) — и
    вариативен только диагноз, который несёт `.reason` и `.artifact`, а не
    поведение вызывающего кода.

    `.artifact` называет ТОЛЬКО идентификатор проблемного файла или записи
    (`semantic_proof.json`, `config`, `checklists`) — никогда его содержимое:
    BEH-15 запрещает диагностике раскрывать чувствительные значения.
    """

    def __init__(self, reason: SemanticProofReason, artifact: str, detail: str) -> None:
        self.reason: SemanticProofReason = reason
        self.artifact = artifact
        super().__init__(
            f"доказательство immutable-проекции недоказуемо "
            f"({_SEMANTIC_PROOF_REASON_TEXT[reason]}) — артефакт {artifact!r}: "
            f"{detail}. Живой конфиг не принимается как новый baseline, а "
            "доказательство не перезаписывается автоматически: восстановите "
            f"удостоверенные данные ({artifact!r}) из контроля версий/бэкапа "
            "либо завершите или пересоздайте пайплайн (`disp pipeline run "
            "--slug <новый-слаг>`)."
        )


class SemanticDrift(DisputatioError):
    """Живая immutable-проекция `[pipeline]` разошлась с удостоверенной (BEH-11).

    Отдельный класс, а не `UnprovableSemantics`: там доказательство нечем
    доверять (файл отсутствует/повреждён/противоречив), здесь оно доверено
    целиком, а расходится ЗНАЧЕНИЕ живого конфига (FR-09, FR-10). `diffs`
    несёт уже отредактированные `ProjectionDiff`
    (`pipeline_semantic_proof.diff_projections`): тексты чеклистов и команды
    gate'ов там всегда `None` независимо от реальных значений (BEH-14/FR-14)
    — редактирование не забота этого класса, он просто перечисляет то, что
    получил.
    """

    def __init__(
        self, *, slug: str, schema_version: str, diffs: Sequence["ProjectionDiff"]
    ) -> None:
        self.diffs = tuple(diffs)
        fields = ", ".join(diff.field for diff in self.diffs)
        lines = "\n".join(
            f"  - {diff.field}"
            + (
                ""
                if diff.old is None and diff.new is None
                else f": {diff.old!r} → {diff.new!r}"
            )
            for diff in self.diffs
        )
        super().__init__(
            f"semantic drift пайплайна {slug!r} (канонизация проекции v"
            f"{schema_version}): живая immutable-проекция [pipeline] "
            f"разошлась с удостоверенной в {len(self.diffs)} поле(ях) "
            f"({fields}):\n{lines}\nresume остановлен ДО запуска или "
            "возобновления сессии, gate'ов и любых мутаций (BEH-11) — "
            "приведите живой конфиг к удостоверенному состоянию либо "
            "заведите пайплайн заново новым слагом (`disp pipeline run "
            "--slug <новый-слаг>`)."
        )
