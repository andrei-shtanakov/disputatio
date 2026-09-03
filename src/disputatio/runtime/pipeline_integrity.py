"""Политика целостности control plane вокруг хода автора (SPEC-002 §2 P9, §7.1).

Здесь только политика: сам журнал — `IntegrityAnchor` пакета `events` (§9),
и границу эту модуль не переходит. Четыре решения определяют его форму.

**Снапшот пишется ровно в один файл — в анкер, и больше никуда.** Дублировать
его в манифест значило бы согласовать две независимые файловые границы одной
атомарной операцией, чего сделать нельзя: падение между записями оставило бы
расхождение, неотличимое от подмены, и штатный крах читался бы как tampering.
Поэтому `before_author_turn` манифест не трогает вовсе.

**Ход — это один вызов адаптера, а не один раунд.** Внутри `PROPOSING` их
несколько, если вывод не прошёл схему и сработал `schema_retries`, и
обрамление шага целиком оставило бы подмену между попытками невидимой
(`runtime/retry.py` зовёт хуки вокруг каждой попытки).

**Успешный ход отмечается `turn_completed`.** Без отметки последняя запись
успешного хода осталась бы `pre_turn`, а runtime сразу после сверки законно
пишет артефакты раунда и двигает `session.json` — следующий `resume` прочитал
бы эти штатные записи как подмену и уронил пайплайн на ровном месте (§8.1
шаг 0 сверяет, только когда последняя запись — `pre_turn`).

**`operation_id` выводится из содержимого снапшота, а не из счётчика
попыток.** Счётчик живёт в памяти процесса и после краха начинается заново,
поэтому повтор прерванного хода получил бы тот же идентификатор при ДРУГОМ
состоянии диска — запись отбросилась бы дедупликацией, и сверка шла бы против
устаревшего снапшота. Содержимое такой ошибки не допускает: повтор хода,
который не начинался, даёт ту же строку (идемпотентность по `{kind,
session_id, round, operation_id}`), а вторая попытка того же раунда — свою.
Идентификатор вдобавок замешивает identity предыдущей записи журнала: без
этого две подряд идущие попытки с байт-в-байт одинаковым control plane
слились бы в одну запись, и последней в журнале осталась бы `turn_completed`
первой — то есть resume пропустил бы сверку второй попытки.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from disputatio.contracts import AppendOnlyEntry, IntegritySnapshot, SessionState
from disputatio.events import AnchorRecord, IntegrityAnchor
from disputatio.runtime.errors import ControlPlaneTampered
from disputatio.runtime.layout import SESSION_DIR_NAME, rounds_dir
from disputatio.runtime.pipeline_config import validate_anchor_path

#: Файлы каталога пайплайна, неизменяемые в пределах хода автора (§4.1).
#: `pipeline.json` — тот самый манифест, ради недостижимости которого анкер и
#: вынесен из дерева; снапшоты task/config/checklists неизменны на весь
#: пайплайн, и их хеши записаны в манифесте.
_PIPELINE_IMMUTABLE: Final = (
    "pipeline.json",
    "task.md",
    "config.toml",
    "checklists.toml",
    # Доказательство immutable-проекции (WS-65 BEH-01, приёмка PR #90,
    # круг 8): durable-артефакт того же окна, что и снапшоты выше, — P9
    # обязан ловить его подмену/исчезновение в пределах хода. Отсутствие
    # (legacy-пайплайны без proof) выражено членством в наборе, как у всех.
    "semantic_proof.json",
)

#: Файлы каталога сессии, неизменяемые в пределах хода: durable-состояние FSM
#: и снапшот конфига. Между `before_author_turn` и `after_author_turn` runtime
#: не пишет ни того, ни другого — там ровно один вызов адаптера.
_SESSION_IMMUTABLE: Final = ("session.json", "config.toml")


@dataclass(frozen=True, slots=True)
class ControlPlane:
    """Управляющие файлы одной ревизии: что именно охраняет P9.

    Область намеренно узкая и перечислимая, а не «всё под `.disputatio/`»:
    §10 называет ровно три вида (манифест, артефакт раунда, event log), а
    обход дерева целиком затянул бы в снапшот и то, что оркестратор
    создаёт/удаляет сам (read-only worktree ревьюера), — сверка ловила бы
    собственные штатные действия.

    **`append_only_paths` подаются извне, а не вычисляются здесь.** Правило
    `runtime/append_only.py` ([DESIGN-016]) запрещает пакету `runtime`
    вычислять путь `events.jsonl` вообще — ни литералом, ни построителем из
    `events`: журнал открывает ровно один писатель, и второй, собравший путь
    сам, усёк бы его молча. P9 требует от журналов только prefix-property, и
    для СВЕРКИ пути не нужны вовсе — их называет сама запись анкера. Нужны
    они лишь при СНЯТИИ снапшота, и там их знает тот, кто журналами владеет:
    `PipelineEventSink.path` отдаёт путь пайплайнового лога, композиция
    сессии — путь её собственного. Пустой набор — законное «журналов не
    сторожим», и такой `ControlPlane` строит resume: ему сторожить их по
    записи, а не по списку.
    """

    workspace_root: Path
    pipeline_dir: Path
    artifact_root: Path
    append_only_paths: tuple[Path, ...] = ()

    def snapshot(
        self, *, session_id: str, round_no: int, operation_id: str
    ) -> IntegritySnapshot:
        """Текущее состояние control plane в форме записи анкера (§4.2)."""
        return IntegritySnapshot(
            session_id=session_id,
            round=round_no,
            operation_id=operation_id,
            immutable=self.immutable_hashes(),
            append_only=self.append_only_entries(),
        )

    def immutable_hashes(self) -> dict[str, str]:
        """`{путь: sha256}` неизменяемых файлов; отсутствующих в наборе нет.

        Существование выражено членством в наборе, а не отдельным маркером:
        и исчезнувший, и появившийся файл ловятся сравнением наборов, а
        значение вроде `absent` пришлось бы отличать от содержимого файла с
        таким текстом.
        """
        return {
            self._name(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self._immutable_paths()
        }

    def append_only_entries(self) -> dict[str, AppendOnlyEntry]:
        """`{путь: {prefix_bytes, prefix_sha256}}` журналов (§4.2)."""
        entries: dict[str, AppendOnlyEntry] = {}
        for path in self._append_only_paths():
            data = path.read_bytes()
            entries[self._name(path)] = AppendOnlyEntry(
                prefix_bytes=len(data),
                prefix_sha256=hashlib.sha256(data).hexdigest(),
            )
        return entries

    def violations(self, record: AnchorRecord) -> tuple[str, ...]:
        """Расхождения текущего состояния с записанным снапшотом (P9).

        Неизменяемые файлы — равенством хешей, журналы — prefix-property:
        старое содержимое обязано остаться неизменённым префиксом нового.
        Пустой кортеж означает «сверка сошлась».
        """
        return (*self._immutable_violations(record), *self._log_violations(record))

    def _immutable_violations(self, record: AnchorRecord) -> tuple[str, ...]:
        """Равенство хешей и совпадение наборов путей."""
        current = self.immutable_hashes()
        problems: list[str] = []
        for name in sorted(set(record.immutable) | set(current)):
            recorded = record.immutable.get(name)
            actual = current.get(name)
            if recorded is None:
                problems.append(f"{name}: файл control plane появился за ход автора")
            elif actual is None:
                problems.append(f"{name}: файл control plane исчез за ход автора")
            elif actual != recorded:
                problems.append(
                    f"{name}: содержимое изменилось за ход автора "
                    f"({recorded[:12]}… → {actual[:12]}…)"
                )
        return tuple(problems)

    def _log_violations(self, record: AnchorRecord) -> tuple[str, ...]:
        """Prefix-property журналов: append легален, усечение и правка — нет.

        Сторожатся ровно те журналы, которые НАЗВАНЫ записью: сверка ничего
        не знает о раскладке логов и знать не должна (см. докстринг класса),
        а появление рядом нового файла с чужим именем P9 нарушением не
        объявляет — журналом от этого он не становится.
        """
        problems: list[str] = []
        for name in sorted(record.append_only):
            recorded = record.append_only[name]
            path = self.workspace_root / name
            if not path.is_file():
                problems.append(f"{name}: журнал исчез за ход автора")
                continue
            data = path.read_bytes()
            if len(data) < recorded.prefix_bytes:
                problems.append(
                    f"{name}: журнал усечён "
                    f"({recorded.prefix_bytes} → {len(data)} байт)"
                )
                continue
            prefix = hashlib.sha256(data[: recorded.prefix_bytes]).hexdigest()
            if prefix != recorded.prefix_sha256:
                problems.append(
                    f"{name}: записанный префикс журнала переписан "
                    f"(первые {recorded.prefix_bytes} байт)"
                )
        return tuple(problems)

    def _immutable_paths(self) -> tuple[Path, ...]:
        """Существующие неизменяемые файлы: пайплайн, сессия, артефакты раундов."""
        session = self.artifact_root / SESSION_DIR_NAME
        candidates: list[Path] = [
            *(self.pipeline_dir / name for name in _PIPELINE_IMMUTABLE),
            *(session / name for name in _SESSION_IMMUTABLE),
            *sorted(rounds_dir(self.artifact_root).rglob("*")),
        ]
        return tuple(path for path in candidates if path.is_file())

    def _append_only_paths(self) -> tuple[Path, ...]:
        """Существующие из названных владельцем журналов (см. докстринг класса)."""
        return tuple(path for path in self.append_only_paths if path.is_file())

    def _name(self, path: Path) -> str:
        """Имя файла в снапшоте — путь относительно рабочего корня.

        Относительный и потому машинно-независимый: анкер переживает
        переезд каталога, а сравнивать снапшоты, снятые из разных cwd, иначе
        было бы нечем.
        """
        return path.relative_to(self.workspace_root).as_posix()


class PipelineIntegrityPolicy:
    """`SessionLifecyclePolicy` пайплайна: запись и сверка снапшота P9 (§7.1).

    Отказ любого из хуков закрывает сессию fail-closed: `runtime/retry.py`
    переводит её в `FAILED` с причиной `invariant_violation` и пробрасывает
    исключение дальше. Поэтому здесь нет ни одной ветки «пожаловаться и
    продолжить»: сверка либо сошлась, либо пайплайн останавливается.
    """

    def __init__(self, *, anchor: IntegrityAnchor, control_plane: ControlPlane) -> None:
        # P9 повторяет ту же проверку, что и предусловие `run`: анкер,
        # лежащий в дереве автора, анкером не является — а собрать политику
        # с таким анкером значило бы получить сверку, которую проверяемая
        # сторона вправе переписать.
        validate_anchor_path(anchor.path, control_plane.workspace_root)
        self._anchor = anchor
        self._plane = control_plane

    def before_author_turn(self, state: SessionState) -> None:
        """Write-ahead снапшот control plane перед вызовом адаптера (P9)."""
        last = self._anchor.last_record()
        probe = self._plane.snapshot(
            session_id=state.session_id,
            round_no=state.current_round,
            operation_id="",
        )
        self._anchor.append_pre_turn(
            probe.model_copy(update={"operation_id": _turn_operation_id(probe, last)})
        )

    def after_author_turn(self, state: SessionState) -> None:
        """Сверяет control plane против записанного снапшота; успех — отметка.

        Сверка идёт против ЗАПИСИ анкера, а не против снапшота в памяти
        политики: ровно ту же сверку по той же записи проводит `resume`
        (§8.1 шаг 0), и второй источник правды здесь означал бы два разных
        ответа на один вопрос после краха.
        """
        record = self._anchor.last_record()
        if record is None or record.kind != "pre_turn":
            raise ControlPlaneTampered(
                f"в анкере {self._anchor.path} нет pre-turn снапшота хода "
                f"{state.session_id} раунда {state.current_round:03d}: сверять "
                "не с чем, а ход автора без снапшота P9 не бывает"
            )
        if (record.session_id, record.round) != (state.session_id, state.current_round):
            raise ControlPlaneTampered(
                f"последняя запись анкера описывает ход {record.session_id} "
                f"раунда {record.round:03d}, а сверяется ход {state.session_id} "
                f"раунда {state.current_round:03d}: журнал целостности ведёт "
                "чужой пайплайн"
            )
        problems = self._plane.violations(record)
        if problems:
            raise ControlPlaneTampered(_tamper_message(self._anchor, record, problems))
        self._anchor.append_completion(
            IntegritySnapshot(
                session_id=record.session_id,
                round=record.round,
                operation_id=record.operation_id,
            )
        )


def verify_or_raise(
    anchor: IntegrityAnchor, record: AnchorRecord, plane: ControlPlane
) -> None:
    """Сверка снапшота вне цикла сессии — вход resume (§8.1 шаг 0).

    Отдельная функция, а не метод политики: на resume сессии нет, а
    identity берётся из самой записи анкера, и конструировать ради сверки
    политику с её жизненным циклом было бы притворством.
    """
    problems = plane.violations(record)
    if problems:
        raise ControlPlaneTampered(_tamper_message(anchor, record, problems))


def _tamper_message(
    anchor: IntegrityAnchor, record: AnchorRecord, problems: Iterable[str]
) -> str:
    """Текст отказа: что именно разошлось и относительно какой записи."""
    listing = "\n".join(f"  - {problem}" for problem in problems)
    return (
        f"целостность control plane нарушена (P9): состояние не сходится со "
        f"снапшотом хода {record.session_id} раунда {record.round:03d} "
        f"(операция {record.operation_id}), записанным в {anchor.path}:\n"
        f"{listing}"
    )


def _turn_operation_id(snapshot: IntegritySnapshot, last: AnchorRecord | None) -> str:
    """Идентификатор хода: повтор того же хода — тот же, новая попытка — свой.

    Повтор распознаётся буквально: последняя запись — `pre_turn` той же
    identity с тем же содержимым снапшота, то есть ход, о котором запись
    сделана, так и не начался (или не изменил ничего). Всё остальное —
    новая попытка, и её идентификатор замешивает identity предыдущей записи,
    чтобы две неотличимые по содержимому попытки не слились в одну строку.
    """
    if (
        last is not None
        and last.kind == "pre_turn"
        and last.session_id == snapshot.session_id
        and last.round == snapshot.round
        and last.immutable == snapshot.immutable
        and last.append_only == snapshot.append_only
    ):
        return last.operation_id
    seed = "" if last is None else f"{last.kind}:{last.operation_id}"
    payload = json.dumps(
        {
            "session_id": snapshot.session_id,
            "round": snapshot.round,
            "seed": seed,
            "immutable": snapshot.immutable,
            "append_only": {
                name: [entry.prefix_bytes, entry.prefix_sha256]
                for name, entry in snapshot.append_only.items()
            },
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return f"turn-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"
