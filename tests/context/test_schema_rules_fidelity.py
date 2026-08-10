"""Верность блока §4.4 поведению валидатора: ревью-фикс TASK-003.

`test_schema_rules.py` пинит НАЛИЧИЕ четырёх правил, но не их
последствия: мутант, где отсутствие `evidence` «отвергает ревью и
повторяет шаг» вместо понижения до `minor`, проходит весь тот файл
зелёным — токены `evidence|blocker|major|minor` и слово «непуст» на
месте. А это ровно та ложь, ради предотвращения которой модуль и
существует: ревьюер, поверивший в отклонение, не станет писать
неподкреплённое замечание вовсе, хотя §4.4 (REQ-009) его сохраняет.

Здесь пинится соответствие текста конвейеру `validate_review`:
последствие — деградация, и она выполняется ДО проверки
«негативный вердикт требует blocker|major», поэтому необоснованное
замечание негативный вердикт не удерживает.
"""

import importlib

CONSTANT_NAME = "REVIEW_SCHEMA_REQUIREMENTS"


def _evidence_rule_line() -> str:
    """Единственная строка блока, где сформулировано правило об `evidence`."""
    module = importlib.import_module("disputatio.context.schema_rules")
    text: str = getattr(module, CONSTANT_NAME)
    tokens = ("evidence", "blocker", "major", "minor")
    matches = [
        line
        for line in text.splitlines()
        if all(token in line.lower() for token in tokens)
    ]
    assert len(matches) == 1, (
        f"ожидалась ровно одна строка с токенами {tokens}, найдено "
        f"{len(matches)}. Текст блока:\n{text}"
    )
    return matches[0].lower()


def test_missing_evidence_consequence_is_degradation_not_rejection() -> None:
    """REQ-009: последствие пустого `evidence` — понижение, не отклонение."""
    line = _evidence_rule_line()

    assert "обязан" in line, (
        f"правило не формулирует требование как обязанность: {line}"
    )
    assert "понижа" in line, (
        "правило не называет последствием понижение замечания — ревьюер "
        f"ждёт отклонения ревью там, где §4.4 его сохраняет: {line}"
    )


def test_degraded_issue_does_not_hold_a_negative_verdict() -> None:
    """ADR-003: деградация идёт до проверки «негативный вердикт ⇒ blocker|major».

    Порядок конвейера наблюдаем для ревьюера: `request_changes` с
    единственным неподкреплённым блокером отвергается — не за «нет
    evidence», а за «нет существенного замечания». Не сказать этого —
    оставить ревьюеру ретрай в качестве способа узнать.
    """
    line = _evidence_rule_line()

    assert "понижен" in line, (
        f"не сказано, что понижение влияет на судьбу вердикта: {line}"
    )
    assert "вердикт" in line, (
        f"правило не связывает понижение с негативным вердиктом: {line}"
    )
