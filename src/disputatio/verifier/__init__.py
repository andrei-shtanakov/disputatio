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
    resolve_inside,
)
from disputatio.verifier.doc_verifier import BASELINE_GATE_NAMES, DocVerifier
from disputatio.verifier.runner_impl import VerifierRunner
from disputatio.verifier.wiring import check_wiring, render_report
from disputatio.verifier.wiring_snapshot import (
    Snapshot,
    WiringInputError,
    dirty_src_paths,
    read_snapshot,
)

__all__ = [
    "BASELINE_GATE_NAMES",
    "DocVerifier",
    "GateSpec",
    "Snapshot",
    "VerifierRunner",
    "WiringInputError",
    "check_wiring",
    "dirty_src_paths",
    "gate_doc_anchors",
    "gate_doc_line_refs",
    "gate_doc_links",
    "gate_doc_paths",
    "gate_doc_scope",
    "read_snapshot",
    "render_report",
    "resolve_inside",
]
