"""Verifier — детерминированный раннер gates ([DESIGN-001]).

Публичный API пакета: composition root w-runtime импортирует отсюда,
не из подмодулей. Пакет зависит только от stdlib и
`disputatio.contracts.*` — статическая гарантия отсутствия агентских
CLI ([REQ-010]).
"""

from disputatio.verifier.config import GateSpec

__all__ = ["GateSpec"]
