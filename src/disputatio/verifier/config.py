"""Входная конфигурация gates Verifier'а ([DESIGN-002], [REQ-002], [REQ-004]).

`GateSpec` — frozen dataclass, а не pydantic-модель: это внутренний вход
раннера, не артефакт со схемой `disputatio/v1` — схемной эволюции и
сериализации у него нет. Разбор `config.toml` в `list[GateSpec]` — зона
другого workstream'а; Verifier принимает готовый список.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GateSpec:
    """Один gate из конфигурации: имя и команда; enabled=False — заявленный skip."""

    name: str
    cmd: str
    enabled: bool = True
