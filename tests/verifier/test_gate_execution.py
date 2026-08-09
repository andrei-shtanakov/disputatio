"""Тесты TASK-001: каркас `disputatio.verifier` и dataclass `GateSpec`.

Импорты `disputatio.verifier` выполняются внутри тестов: на момент
red-чекпоинта пакета ещё нет, и импорт на уровне модуля сломал бы
collection. Red-селектор (`test_gate_spec_frozen_slots_defaults`)
превращает ImportError в AssertionError — гейт принимает red только при
падении assertion'ом.
"""

import dataclasses

import pytest


def test_gate_spec_frozen_slots_defaults() -> None:
    """`GateSpec` конструируется; frozen, slots, дефолт `enabled=True`."""
    try:
        from disputatio.verifier.config import GateSpec
    except ImportError as exc:  # red-фаза: config.py ещё не создан
        raise AssertionError("src/disputatio/verifier/config.py ещё не создан") from exc

    spec = GateSpec(name="tests", cmd="uv run pytest -q")
    assert spec.name == "tests"
    assert spec.cmd == "uv run pytest -q"
    assert spec.enabled is True

    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "mutated"  # type: ignore[read-only]

    assert spec.__slots__ == ("name", "cmd", "enabled")
    assert not hasattr(spec, "__dict__")


def test_gate_spec_reexported_from_package() -> None:
    """`GateSpec` — публичный реэкспорт из `disputatio.verifier`."""
    try:
        import disputatio.verifier
    except ImportError as exc:  # red-фаза: пакета ещё нет
        raise AssertionError("пакет disputatio.verifier ещё не создан") from exc

    from disputatio.verifier.config import GateSpec

    assert disputatio.verifier.GateSpec is GateSpec
    assert "GateSpec" in disputatio.verifier.__all__
