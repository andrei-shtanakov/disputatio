"""Терминальная запись анкера: чем доказывается фаза остановленного пайплайна.

До неё анкер о состоянии покоя не говорил ничего: `genesis` покрывает
write-once файлы пайплайна, но `pipeline.json` в нём намеренно нет (манифест
легитимно переписывается), полная сверка идёт только против `pre_turn`, то
есть ВНУТРИ оборванного хода, а `turn_completed` несёт одну identity —
«сверка прошла», а не «состояние было таким». Значит у пайплайна, дошедшего
до `DONE`, содержимое манифеста не покрывал никто, и вопрос потребителя
«пайплайн для слага уже завершился?» честного ответа не имел: манифест
читать нельзя (обход защиты), а сверять его было не с чем.

`terminal` закрывает ровно это окно и связывает три вещи, каждая из которых
по отдельности подделывается: идентификатор пайплайна, название терминальной
фазы и хеш манифеста в момент остановки.
"""

from pathlib import Path

import pytest

from disputatio.events.integrity_anchor import AnchorRecord, IntegrityAnchor

SLUG = "pair-docs"
MANIFEST_SHA = "a" * 64


def _anchor(tmp_path: Path) -> IntegrityAnchor:
    anchor = IntegrityAnchor(tmp_path / "anchors", tmp_path / "repo", SLUG)
    anchor.create_empty()
    return anchor


def test_terminal_record_binds_pipeline_phase_and_manifest(tmp_path: Path) -> None:
    """Запись несёт все три поля: без любого из них она ничего не доказывает."""
    anchor = _anchor(tmp_path)

    anchor.append_terminal(pipeline_id=SLUG, phase="DONE", manifest_sha256=MANIFEST_SHA)

    record = anchor.last_record()
    assert record is not None
    assert record.kind == "terminal"
    assert record.pipeline_id == SLUG
    assert record.phase == "DONE"
    assert record.immutable == {"pipeline.json": MANIFEST_SHA}


def test_terminal_record_is_idempotent(tmp_path: Path) -> None:
    """Повтор после краха даёт ту же строку, а не вторую (ключ идемпотентности).

    Терминальный переход пишется один раз, но запись анкера идёт ПОСЛЕ
    манифеста: крах между ними оставляет пайплайн терминальным без отметки, и
    повторная попытка обязана дописать её ровно один раз.
    """
    anchor = _anchor(tmp_path)

    anchor.append_terminal(pipeline_id=SLUG, phase="DONE", manifest_sha256=MANIFEST_SHA)
    anchor.append_terminal(pipeline_id=SLUG, phase="DONE", manifest_sha256=MANIFEST_SHA)

    assert anchor.path.read_text(encoding="utf-8").count('"terminal"') == 1


def test_two_terminal_phases_are_two_records(tmp_path: Path) -> None:
    """`FAILED` после `DONE` — другая запись, а не молчаливое поглощение.

    Ключ идемпотентности включает фазу через `operation_id`: пайплайн из
    `DONE` не уходит (§2), но анкер обязан различать записи, а не сливать их,
    иначе подделка второй пряталась бы за первой.
    """
    anchor = _anchor(tmp_path)

    anchor.append_terminal(pipeline_id=SLUG, phase="DONE", manifest_sha256=MANIFEST_SHA)
    anchor.append_terminal(pipeline_id=SLUG, phase="FAILED", manifest_sha256="b" * 64)

    lines = anchor.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    last = anchor.last_record()
    assert last is not None
    assert last.phase == "FAILED"


@pytest.mark.parametrize("kind", ["pre_turn", "turn_completed", "genesis"])
def test_records_of_other_kinds_carry_no_terminal_fields(kind: str) -> None:
    """Поля терминальной записи необязательны: прежние строки читаются как были.

    Схема анкера расширена добавлением, а не сменой формы: журнал пайплайна,
    начатого до этой правки, обязан читаться той же моделью.
    """
    record = AnchorRecord(
        kind=kind,  # type: ignore[arg-type]
        session_id="pair-r1",
        round=1,
        operation_id="turn-1",
    )

    assert record.pipeline_id is None
    assert record.phase is None


def test_terminal_record_does_not_hide_the_last_turn(tmp_path: Path) -> None:
    """`last_turn_record` смотрит СКВОЗЬ терминальную отметку (P9).

    Отметка дописывается после закрытия пайплайна, в том числе закрытия
    подменой control plane. Если бы сверка хода читала просто последнюю
    запись, отметка накрыла бы собой `pre_turn` — и второй `resume` над
    подменённым деревом промолчал бы там, где первый отказал. Fail-closed
    держится анкером, и запись, добавленная в него позже, ослабить его не
    вправе.
    """
    from disputatio.contracts import IntegritySnapshot

    anchor = _anchor(tmp_path)
    anchor.append_pre_turn(
        IntegritySnapshot(
            session_id="pair-r1",
            round=1,
            operation_id="turn-1",
            immutable={"pipeline.json": MANIFEST_SHA},
        )
    )
    anchor.append_terminal(
        pipeline_id=SLUG, phase="FAILED", manifest_sha256=MANIFEST_SHA
    )

    last = anchor.last_record()
    turn = anchor.last_turn_record()
    assert last is not None and last.kind == "terminal"
    assert turn is not None and turn.kind == "pre_turn"


def test_terminal_record_is_findable_regardless_of_position(tmp_path: Path) -> None:
    """`terminal_record` отвечает отметкой, а не «последней записью»."""
    anchor = _anchor(tmp_path)
    assert anchor.terminal_record() is None

    anchor.append_terminal(pipeline_id=SLUG, phase="DONE", manifest_sha256=MANIFEST_SHA)

    record = anchor.terminal_record()
    assert record is not None
    assert record.phase == "DONE"
