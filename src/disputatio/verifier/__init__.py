"""Verifier — детерминированный раннер gates ([DESIGN-001]).

Публичный API пакета: composition root w-runtime импортирует отсюда,
не из подмодулей. Пакет зависит только от stdlib и
`disputatio.contracts.*` — статическая гарантия отсутствия агентских
CLI ([REQ-010]).
"""

from disputatio.verifier.config import GateSpec
from disputatio.verifier.doc_gates import (
    gate_doc_anchors,
    gate_doc_line_refs,
    gate_doc_links,
    gate_doc_paths,
    gate_doc_scope,
)
from disputatio.verifier.doc_verifier import BASELINE_GATE_NAMES, DocVerifier
from disputatio.verifier.runner_impl import VerifierRunner

__all__ = [
    "BASELINE_GATE_NAMES",
    "DocVerifier",
    "GateSpec",
    "VerifierRunner",
    "gate_doc_anchors",
    "gate_doc_line_refs",
    "gate_doc_links",
    "gate_doc_paths",
    "gate_doc_scope",
]
