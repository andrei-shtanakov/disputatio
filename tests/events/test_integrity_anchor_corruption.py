"""A3: повреждённая запись анкера не смеет выглядеть отсутствием записи (P9).

Читатель журнала гасил любую ошибку JSON/модели и шёл дальше. Испорченная
ПОЛНАЯ запись `pre_turn` (строка завершена `\\n`, то есть дописана до
конца) поэтому исчезала из выборки, `last_record()` отдавал предыдущую
`turn_completed` или `None`, и resume заключал, что незавершённого хода не
было, — то есть не сверял control plane вовсе. Подмена `pipeline.json`,
состояния сессии или журналов проходила мимо P9, стоило испортить один
байт в записи, которая эту подмену и должна была поймать.

Терпимость сохраняется ровно к одному случаю — доказуемо оборванному
хвосту: последняя строка без завершающего `\\n` есть след краха внутри
`_append`, то есть хода, о котором снапшот не дописан.
"""

import json
from pathlib import Path

import pytest

from disputatio.contracts import IntegritySnapshot
from disputatio.events.integrity_anchor import AnchorCorrupted, IntegrityAnchor

SLUG = "pair-docs"


def _anchor(tmp_path: Path) -> IntegrityAnchor:
    anchor = IntegrityAnchor(tmp_path / "anchors", tmp_path / "repo", SLUG)
    anchor.create_empty()
    return anchor


def _snapshot(operation_id: str = "turn-1") -> IntegritySnapshot:
    return IntegritySnapshot(
        session_id="pair-r1",
        round=1,
        operation_id=operation_id,
        immutable={"pipeline.json": "0" * 64},
    )


def test_corrupted_last_pre_turn_record_raises_instead_of_disappearing(
    tmp_path: Path,
) -> None:
    """Сценарий находки: порча последней ПОЛНОЙ записи `pre_turn`.

    До фикса `last_record()` возвращал предыдущую `turn_completed` —
    ответ «незавершённого хода нет», то есть отмена сверки. Утверждение
    именно про это: не «что-то упало», а что прежней записи в ответе не
    оказалось, потому что ответа нет вовсе.
    """
    anchor = _anchor(tmp_path)
    anchor.append_pre_turn(_snapshot("turn-1"))
    anchor.append_completion(_snapshot("turn-1"))
    anchor.append_pre_turn(_snapshot("turn-2"))
    lines = anchor.path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1].replace('"kind"', '"kin', 1)
    anchor.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AnchorCorrupted) as excinfo:
        anchor.last_record()

    assert str(anchor.path) in str(excinfo.value)


def test_corrupted_sole_record_raises_instead_of_empty_journal(
    tmp_path: Path,
) -> None:
    """Порча единственной записи давала `None` — «журнал пуст, сверять нечего»."""
    anchor = _anchor(tmp_path)
    anchor.append_pre_turn(_snapshot())
    anchor.path.write_text("не json вовсе\n", encoding="utf-8")

    with pytest.raises(AnchorCorrupted):
        anchor.last_record()


def test_valid_json_but_alien_shape_raises(tmp_path: Path) -> None:
    """Схемно негодная строка — тоже порча: JSON разобрался, запись — нет."""
    anchor = _anchor(tmp_path)
    anchor.path.write_text('{"kind": "pre_turn"}\n', encoding="utf-8")

    with pytest.raises(AnchorCorrupted):
        anchor.last_record()


def test_corruption_in_the_middle_raises_even_with_a_valid_tail(
    tmp_path: Path,
) -> None:
    """Испорченная строка В СЕРЕДИНЕ — не «пропустим и почитаем дальше».

    Доверенной остаётся только та история, которая прочитана целиком:
    вырезанная запись меняет и `last_record`, и идемпотентность `_append`.
    """
    anchor = _anchor(tmp_path)
    anchor.append_pre_turn(_snapshot("turn-1"))
    anchor.append_completion(_snapshot("turn-1"))
    lines = anchor.path.read_text(encoding="utf-8").splitlines()
    anchor.path.write_text(
        "\n".join(["{сломано}", *lines]) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(AnchorCorrupted):
        anchor.last_record()


def test_append_refuses_to_extend_a_corrupted_journal(tmp_path: Path) -> None:
    """Дописывать в повреждённый журнал нельзя: дедуп читает ту же историю.

    `_append` пропускает запись с уже существующим ключом; на журнале,
    прочитанном не целиком, этот вывод не обоснован.
    """
    anchor = _anchor(tmp_path)
    anchor.path.write_text('{"kind": "pre_turn", "session_id": 1}\n', encoding="utf-8")

    with pytest.raises(AnchorCorrupted):
        anchor.append_pre_turn(_snapshot())


def test_truncated_tail_without_newline_is_still_tolerated(tmp_path: Path) -> None:
    """Не-вакуумность: оборванный хвост — след краха, а не подмены.

    Строка без завершающего `\\n` дописана не до конца, то есть описывает
    ход, снапшот которого не сохранён. Предыдущие записи остаются
    доверенными, и `last_record()` отдаёт последнюю целую.
    """
    anchor = _anchor(tmp_path)
    anchor.append_pre_turn(_snapshot("turn-1"))
    whole = anchor.path.read_text(encoding="utf-8")
    with anchor.path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "pre_tu')

    record = anchor.last_record()

    assert record is not None
    assert (record.kind, record.operation_id) == ("pre_turn", "turn-1")
    assert anchor.path.read_text(encoding="utf-8").startswith(whole)


def test_appending_after_a_truncated_tail_keeps_the_journal_readable(
    tmp_path: Path,
) -> None:
    """Терпимость к хвосту обязана переживать СЛЕДУЮЩУЮ запись.

    Читатель признаёт оборванный хвост допустимым, но байты его остаются в
    файле, и `_append` в режиме `ab` дописывал новую запись прямо к
    незаконченным — склейка давала одну невалидную, теперь уже завершённую
    `\\n` строку, то есть `AnchorCorrupted` навсегда. Восстановимый по
    построению crash-retry переставал быть восстановимым при первой же
    попытке продолжить, а это ровно та последовательность, которой ходит
    `PipelineIntegrityPolicy.before_author_turn`: `last_record()`, затем
    `append_pre_turn()`.

    Утверждается связка, а не состояние: чтение ПОСЛЕ дописывания.
    """
    anchor = _anchor(tmp_path)
    anchor.append_pre_turn(_snapshot("turn-1"))
    anchor.append_completion(_snapshot("turn-1"))
    trusted = anchor.path.read_text(encoding="utf-8")
    with anchor.path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "pre_tu')

    assert anchor.last_record() is not None
    anchor.append_pre_turn(_snapshot("turn-2"))

    record = anchor.last_record()
    assert record is not None
    assert (record.kind, record.operation_id) == ("pre_turn", "turn-2")
    raw = anchor.path.read_text(encoding="utf-8")
    # Доверенный префикс не тронут — усечён ровно недописанный хвост.
    assert raw.startswith(trusted)
    assert len(raw.splitlines()) == 3


def test_a_record_that_lost_only_its_newline_survives_the_next_append(
    tmp_path: Path,
) -> None:
    """Целая последняя запись без `\\n` — запись, а не хвост на выброс.

    Крах между `write` строки и её завершающим байтом оставляет годную
    запись без разделителя. Усечь её значило бы стереть `pre_turn`, которым
    держится fail-closed: журнал из одной `turn_completed` читается как
    «незавершённого хода не было», и resume пропускает сверку P9 (§8.1
    шаг 0) — тот самый обход, от которого A3 и A5 закрываются.
    """
    anchor = _anchor(tmp_path)
    anchor.append_pre_turn(_snapshot("turn-1"))
    anchor.path.write_text(
        anchor.path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8"
    )

    anchor.append_completion(_snapshot("turn-1"))

    lines = anchor.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "pre_turn" in lines[0]
    assert anchor.last_record() is not None


def test_concurrent_appends_do_not_drop_an_accepted_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Два resume над одним журналом: усечение хвоста не смеет съесть чужую запись.

    Моделируется РЕАЛЬНОЕ чередование, а не два последовательных вызова:
    писатель A входит в `_append`, снимает байты журнала для решения о
    хвосте и застревает ровно там; писатель B в этот момент проделывает
    свой `_append` целиком — усекает тот же хвост и дописывает полную,
    fsync-нутую запись. Затем A досчитывает по СТАРОМУ снимку и режет файл
    до смещения хвоста, стирая уже принятую запись B.

    Утверждение — про итог, а не про механизм: `_append`, который вернулся
    без исключения, обязан остаться на диске. Журнал append-only, и
    «принят и потерян» — не один из его исходов. Последовательные вызовы
    этого не видят: снимок каждого писателя согласован сам с собой.
    """
    import threading

    anchor = _anchor(tmp_path)
    anchor.append_pre_turn(_snapshot("turn-1"))
    anchor.append_completion(_snapshot("turn-1"))
    trusted = anchor.path.read_text(encoding="utf-8")
    with anchor.path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "pre_tu')

    entered = threading.Event()
    released = threading.Event()
    real_read_bytes = Path.read_bytes

    def _slow_read_bytes(self: Path) -> bytes:
        raw = real_read_bytes(self)
        # Тормозится только снимок A и только тот, по которому считается
        # смещение усечения (`_seal_tail`), — окно TOCTOU целиком.
        if self == anchor.path and threading.current_thread().name == "writer-a":
            entered.set()
            released.wait(5)
        return raw

    monkeypatch.setattr(Path, "read_bytes", _slow_read_bytes)

    failures: dict[str, BaseException] = {}

    def _append(name: str, operation_id: str) -> None:
        try:
            anchor.append_pre_turn(_snapshot(operation_id))
        except BaseException as exc:  # noqa: BLE001 — исход писателя, не диагноз
            failures[name] = exc

    writer_a = threading.Thread(target=_append, args=("A", "turn-2"), name="writer-a")
    writer_b = threading.Thread(target=_append, args=("B", "turn-3"), name="writer-b")
    writer_a.start()
    assert entered.wait(5), "писатель A не дошёл до снимка журнала"
    writer_b.start()
    # Под сериализацией B ждёт блокировку и ожидание истекает — это
    # штатный исход, а не сбой: важно лишь, чтобы без неё B успел записать
    # и fsync-нуть свою строку до того, как A продолжит.
    writer_b.join(1)
    released.set()
    writer_a.join(5)
    writer_b.join(5)

    assert not failures, f"писатель отвергнут: {failures!r}"
    raw = anchor.path.read_text(encoding="utf-8")
    assert raw.startswith(trusted), "доверенный префикс журнала повреждён"
    operations = [json.loads(line)["operation_id"] for line in raw.splitlines()]
    assert "turn-3" in operations, (
        "принятая запись B исчезла: усечение хвоста в A сработало по "
        f"снимку, снятому до записи B (журнал: {operations!r})"
    )
    assert "turn-2" in operations


def test_empty_journal_is_still_none(tmp_path: Path) -> None:
    """Не-вакуумность: пустой журнал — по-прежнему `None`, а не ошибка.

    Пустой и отсутствующий журнал §8.1 различает, и строгость к порче не
    вправе стереть это различие.
    """
    anchor = _anchor(tmp_path)

    assert anchor.last_record() is None


def test_missing_journal_still_raises_file_not_found(tmp_path: Path) -> None:
    """Отсутствие файла остаётся `FileNotFoundError` — свой диагноз (§8.1)."""
    anchor = IntegrityAnchor(tmp_path / "anchors", tmp_path / "repo", SLUG)

    with pytest.raises(FileNotFoundError):
        anchor.last_record()


def test_invalid_utf8_tail_is_truncated_fail_closed(tmp_path: Path) -> None:
    """Негодные байты хвоста усекаются как след краха (disputatio#57 К2).

    Хвост, не декодируемый как UTF-8, проходит той же fail-closed дорогой,
    что и оборванный JSON: `UnicodeDecodeError` — подкласс `ValueError`, и
    приведение перехвата к одному классу не меняет поведения.
    """
    anchor = _anchor(tmp_path)
    anchor.append_pre_turn(_snapshot("turn-1"))
    trusted = anchor.path.read_bytes()
    with anchor.path.open("ab") as handle:
        handle.write(b"\xff\xfe broken tail")

    anchor.append_pre_turn(_snapshot("turn-2"))

    raw = anchor.path.read_bytes()
    assert raw.startswith(trusted)
    assert b"\xff\xfe" not in raw
    record = anchor.last_record()
    assert record is not None
    assert record.operation_id == "turn-2"
