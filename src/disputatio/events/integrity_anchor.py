"""`integrity_anchor` — append-only журнал целостности control plane (P9).

Живёт **вне рабочего дерева автора**, и это его единственная опора. Право
записи автора распространяется на рабочую директорию, а `.disputatio`
намеренно исключён из диффа раунда — запись автора туда невидима и для
`changes.patch`, и для `doc-scope`. Adapter-level запрет записи по путям
якорем быть не может: автору нужен Bash, а deny на инструменты правки файлов
не покрывает запись через shell. Якорь доверия — файловая граница.

Модуль живёт в `events`, а не в `runtime`: §9 SPEC-002 и инварианты D1 отдают
файловые append-only writer'ы этому пакету. `runtime` держит только политику,
использующую анкер (`SessionLifecyclePolicy`, §7.1).

**Путь несёт отпечаток рабочего корня.** `anchor_root` по умолчанию общий на
пользователя, а `anchor_id` равен слагу — два репозитория со слагом `docs`
делили бы один журнал, записи смешались бы, и `last_record` одного пайплайна
описывал бы чужой control plane. Поэтому файл —
`<anchor_root>/<workspace_fingerprint>/<anchor_id>.jsonl`. Все три входа
доступны до чтения рабочего дерева: `anchor_root` из конфига, `workspace_root`
из cwd, `anchor_id` из `--slug`.

**Записи двух видов, и это обязательно.** `pre_turn` — снапшот перед ходом
автора; `turn_completed` — отметка после успешной сверки, с той же identity.
Без второго вида штатно завершённый ход оставлял бы последней запись
`pre_turn`, а runtime сразу после сверки законно пишет артефакты раунда и
двигает `session.json` — следующий `resume` прочитал бы эти штатные изменения
как подмену и уронил пайплайн в `FAILED` на ровном месте. Сверка на resume
применяется, только если последняя запись — `pre_turn`.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from disputatio.contracts.base import ArtifactChild
from disputatio.contracts.pipeline import AppendOnlyEntry, IntegritySnapshot
from disputatio.events.file_lock import exclusive_lock
from disputatio.events.pipeline_paths import validate_slug

FINGERPRINT_LENGTH: Final = 16

AnchorKind = Literal["pre_turn", "turn_completed"]


class AnchorCorrupted(Exception):
    """Строка журнала не читается, и это не оборванный хвост (P9).

    Собственный класс пакета `events` — как `RoundImmutableError`: зависеть
    от `runtime.errors` файловому writer'у нельзя, слои идут в другую
    сторону. Перевод в диагноз пользователя — забота `runtime`
    (`pipeline_resume`), а на живой сессии отказ хука политики и так
    закрывает сессию fail-closed (`retry._run_lifecycle_hook`).
    """


class AnchorRecord(ArtifactChild):
    """Одна строка анкера: вид записи + полная identity хода (P9).

    Identity полная намеренно: §8.1 требует сверку ДО чтения манифеста, и
    `session_id`/`round` иначе пришлось бы взять из того самого манифеста,
    который и мог быть подменён. У `turn_completed` хеш-поля пусты — он несёт
    только identity.
    """

    kind: AnchorKind
    session_id: str
    round: int
    operation_id: str
    immutable: dict[str, str] = Field(default_factory=dict)
    append_only: dict[str, AppendOnlyEntry] = Field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, int, str]:
        """Ключ идемпотентности записи: `{kind, session_id, round, operation_id}`."""
        return (self.kind, self.session_id, self.round, self.operation_id)


def _decode(line: str) -> AnchorRecord:
    """Разбирает строку журнала; негодная — `ValueError` (`json` или модель).

    Один разбор на оба берега: читатель решает по нему, доверять ли строке, а
    `_seal_tail` — стирать ли хвост перед дописыванием. Два разных разбора
    означали бы два ответа на один вопрос, и хвост, годный для чтения, мог бы
    оказаться негодным для записи.
    """
    return AnchorRecord.model_validate(json.loads(line))


def workspace_fingerprint(workspace_root: Path) -> str:
    """Короткий sha256 канонического пути рабочего корня (P9).

    Канонизация полная — `expanduser` + `resolve` (раскрытие `..` и
    symlink'ов): два написания одного каталога обязаны дать один отпечаток,
    иначе `resume` из другого cwd не нашёл бы собственный журнал.
    """
    canonical = str(Path(workspace_root).expanduser().resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


class IntegrityAnchor:
    """Append-only журнал pre-turn снапшотов и отметок о завершении хода."""

    def __init__(self, anchor_root: Path, workspace_root: Path, anchor_id: str) -> None:
        # `anchor_id` равен слагу (§4.2, §8.1) и попадает прямо в имя файла,
        # поэтому проходит ту же грамматику §4.1: без неё `--slug ../чужой`
        # увёл бы журнал за пределы `anchor_root`, то есть мимо отпечатка
        # рабочего корня — ровно к тому смешению, от которого он и защищает.
        self._path = (
            Path(anchor_root).expanduser()
            / workspace_fingerprint(workspace_root)
            / f"{validate_slug(anchor_id)}.jsonl"
        )

    @property
    def path(self) -> Path:
        """Физический путь журнала — машинно-зависим, в манифест не пишется (§4.2)."""
        return self._path

    def create_empty(self) -> None:
        """Создаёт пустой журнал; существующий файл — ошибка, а не усечение.

        Fail-closed по построению (`O_EXCL`): затирать уже накопленную
        доверенную историю нельзя ни при коллизии слага, ни при повторном
        запуске. Отказ здесь громкий и на старте; молчаливое усечение
        обнаружилось бы только тем, что сверять стало нечего.

        Пустой журнал создаётся первым действием `run` именно для того, чтобы
        отсутствие файла на `resume` означало неверное расположение, а не
        раннюю стадию (§8.1 шаг 0).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))

    def append_pre_turn(self, snapshot: IntegritySnapshot) -> None:
        """Дописывает снапшот перед ходом автора (§4.2, P9)."""
        self._append(
            AnchorRecord(
                kind="pre_turn",
                session_id=snapshot.session_id,
                round=snapshot.round,
                operation_id=snapshot.operation_id,
                immutable=dict(snapshot.immutable),
                append_only=dict(snapshot.append_only),
            )
        )

    def append_completion(self, identity: IntegritySnapshot) -> None:
        """Отмечает ход завершённым после успешной сверки.

        Берутся только поля identity: хеши в этой записи бессмысленны — она
        говорит «сверка прошла», а не «состояние было таким».
        """
        self._append(
            AnchorRecord(
                kind="turn_completed",
                session_id=identity.session_id,
                round=identity.round,
                operation_id=identity.operation_id,
            )
        )

    def last_record(self) -> AnchorRecord | None:
        """Последняя запись журнала — без аргументов, identity приходит из неё.

        Вариант `last(session_id, round)` был бы циклическим: §8.1 требует
        сверку ДО чтения манифеста, а эти аргументы брались бы из него же.

        Пустой существующий журнал — `None` («сверять нечего»); отсутствие
        файла — `FileNotFoundError`. Различие обязательное: §8.1 отказывает
        при отсутствии файла и пропускает сверку при пустом журнале, и
        свести оба случая к `None` значило бы дать пайплайну с нестандартным
        `anchor_path` молча пропускать сверку.

        Чтение идёт под той же блокировкой, что и запись: `_seal_tail`
        укорачивает файл, а `read_text` добирает содержимое не одним
        системным вызовом — читатель, попавший в это окно, склеил бы хвост
        старого файла с началом новой строки и объявил бы `AnchorCorrupted`
        на журнале, с которым всё в порядке.
        """
        with exclusive_lock(self._path):
            records = self._read()
        return records[-1] if records else None

    def _append(self, record: AnchorRecord) -> None:
        """Дописывает запись, если её ключа ещё нет; fsync перед возвратом.

        Идемпотентность по `{kind, session_id, round, operation_id}`: повтор
        после краха даёт ту же строку, а не вторую. Запись о ходе, который не
        начался, сверку не ломает — она описывает состояние, которое никто не
        менял.

        Перед дописыванием журнал приводится к границе строки (`_seal_tail`):
        терпимость чтения к оборванному хвосту иначе не пережила бы первую же
        запись — `O_APPEND` положил бы новый JSON вплотную к недописанным
        байтам, и восстановимый крах превратился бы в `AnchorCorrupted`
        навсегда.

        Чтение, решение о хвосте, усечение и сама запись идут под одной
        эксклюзивной блокировкой (`events.file_lock`) — иначе это не одна
        операция. Пока усечения не было, разъехавшиеся писатели теряли разве
        что дедупликацию (`O_APPEND` чужих байтов не затирает), но усечение
        режет файл по смещению, посчитанному ДО чужой записи: два `disp
        pipeline resume` над журналом с оборванным хвостом сняли бы один и
        тот же снимок, второй дописал бы и fsync-нул полную `pre_turn`, а
        первый срезал бы её как «хвост». Append-only журнал терял бы
        подтверждённую запись, и ход остался бы без снапшота P9.
        """
        with exclusive_lock(self._path):
            if any(existing.key == record.key for existing in self._read()):
                return

            line = self._seal_tail() + record.model_dump_json() + "\n"
            with self._path.open("ab") as handle:
                handle.write(line.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())

    def _seal_tail(self) -> str:
        """Готовит конец журнала к дописыванию; отдаёт недостающий разделитель.

        Файл без завершающего `\\n` кончается ровно на том, что успел
        записать убитый процесс, и различие здесь то же, что и при чтении, —
        только ответы зеркальные. **Годная запись без разделителя** дописана
        целиком: терять её нельзя (журнал из одной `turn_completed` читается
        как «незавершённого хода не было», то есть отменяет сверку P9), и
        недостающий байт уезжает вместе со следующей строкой — одним
        `write`, без промежуточного состояния. **Негодный хвост** — тот
        самый след краха внутри `_append`, который чтение и пропускает;
        байты усекаются, чтобы пропуск остался правдой и после записи.

        Усечение доверенных байтов не касается: режется ровно то, что
        читатель уже объявил не относящимся к истории. Prefix-property P9
        (§4.2) журнал анкера не сторожит и сторожить не может — он лежит вне
        рабочего корня (`validate_anchor_path`), а под сверкой префикса
        ходят только пути внутри него.

        **Предусловие: вызывается только из `_append`, под блокировкой.**
        Снимок `read_bytes` и `truncate` по посчитанному из него смещению —
        две операции, и без сериализации между ними помещается чужая
        завершённая запись, которую усечение и срежет.
        """
        raw = self._path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return ""
        tail_start = raw.rfind(b"\n") + 1
        try:
            _decode(raw[tail_start:].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            with self._path.open("r+b") as handle:
                handle.truncate(tail_start)
                os.fsync(handle.fileno())
            return ""
        return "\n"

    def _read(self) -> list[AnchorRecord]:
        """Все записи журнала; терпим ровно один случай — оборванный хвост.

        Пропуск незавершённой ПОСЛЕДНЕЙ строки — не послабление: строка без
        завершающего `\\n` означает крах во время `_append`, то есть ход, о
        котором снапшот не дописан. Строки перед ней — по-прежнему
        доверенные, и `FileNotFoundError` наружу не гасится (см.
        `last_record`). Пропущенные байты уходят из файла при следующей
        записи (`_seal_tail`) — иначе терпимость жила бы ровно до неё.

        Всякая ДРУГАЯ нечитаемая строка — `AnchorCorrupted`, и это
        обязательно. Пропуск испорченной полной записи выглядел бы для
        `last_record()` не поломкой, а фактом: испорти последнюю `pre_turn`
        — и ответом станет предыдущая `turn_completed` либо `None`, то есть
        «незавершённого хода не было», то есть resume не сверит control
        plane вовсе (§8.1 шаг 0). Один байт в записи, которая ловит подмену,
        отключал бы саму проверку — поэтому «не разобрал» здесь останавливает
        пайплайн, а не превращается в чистый журнал.
        """
        raw = self._path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        # Индекс единственной терпимой строки: доказательство обрыва —
        # отсутствие завершающего `\n` у файла, а не негодность её текста.
        truncated_tail = len(lines) - 1 if lines and not raw.endswith("\n") else -1
        records: list[AnchorRecord] = []
        for index, line in enumerate(lines):
            try:
                records.append(_decode(line))
            except ValueError as exc:
                if index == truncated_tail:
                    break
                raise AnchorCorrupted(
                    f"журнал целостности {self._path} повреждён в строке "
                    f"{index + 1}: запись дописана целиком, но не читается "
                    f"({exc}). Пропустить её значило бы отменить сверку P9 "
                    "того хода, который она описывает; разберите журнал "
                    "вручную"
                ) from exc
        return records
