"""`DocVerifier` — реализация порта `Verifier` для doc-контуров (SPEC-002 §6).

Baseline — все пять doc-гейтов §6, гоняются на каждом `verify()` без
возможности отключения: конструктор не принимает параметра, которым это
можно было бы сделать. Отключённый `doc-line-refs` позволил бы объявить
сходимость без скриптовой проверки якорей — ровно того требования правила,
ради которого verifier-стадия существует (§6). `extra` добавляет гейты
поверх baseline через существующий `run_gate` ([DESIGN-006]), не заменяет
его.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from disputatio.contracts.verification import VerificationReport
from disputatio.verifier.aggregate import compute_overall
from disputatio.verifier.config import GateSpec
from disputatio.verifier.diffstats import collect_diff_stats
from disputatio.verifier.doc_gates import (
    gate_doc_anchors,
    gate_doc_line_refs,
    gate_doc_links,
    gate_doc_paths,
    gate_doc_scope,
)
from disputatio.verifier.runner import run_gate


class DocVerifier:
    """Верификатор doc-контура: неотключаемый baseline + опциональные extra.

    `doc_paths` — документы, чья ссылочная целостность проверяется на
    каждом раунде: `doc-paths`/`doc-links`/`doc-anchors`/`doc-line-refs`
    гоняются на КАЖДОМ документе из кортежа. `allowed` — пути, которые
    вправе трогать `changes.patch` раунда (`doc-scope`, один прогон на
    раунд): `spec_path` в spec-контуре, `plan_path` в pair-контуре (§6) —
    граница, а не список для галочки. `patch_reader` возвращает текст
    патча раунда по его номеру — тот же источник, что пишет
    `changes.patch`; владение файловым I/O остаётся за `events`, сюда
    попадает уже готовая строка.
    """

    def __init__(
        self,
        *,
        doc_paths: tuple[Path, ...],
        allowed: tuple[str, ...],
        repo_root: Path,
        patch_reader: Callable[[int], str],
        extra: Sequence[GateSpec] = (),
    ) -> None:
        """Запоминает конфигурацию прогона; ничего не запускает.

        `extra` копируется в `list` — тот же довод, что и у
        `VerifierRunner`: список принадлежит вызывающему коду, и его
        последующая правка не должна менять состав уже настроенного
        верификатора.
        """
        self._doc_paths = doc_paths
        self._allowed = allowed
        self._repo_root = repo_root
        self._patch_reader = patch_reader
        self._extra = list(extra)

    def verify(self, round_no: int) -> VerificationReport:
        """Прогоняет baseline (всегда, все пять гейтов) + `extra`.

        Порядок отчёта: содержательные гейты каждого документа из
        `doc_paths` (в порядке кортежа), затем `doc-scope` по патчу раунда,
        затем `extra` — в порядке конфигурации. `diff_stats` снимается по
        `repo_root` тем же способом, что и у `VerifierRunner`
        ([DESIGN-008]): DocVerifier тоже работает над git-репозиторием, и
        второй реализации той же статистики заводить незачем.
        """
        gates = []
        for doc in self._doc_paths:
            gates.append(gate_doc_paths(doc, self._repo_root))
            gates.append(gate_doc_links(doc, self._repo_root))
            gates.append(gate_doc_anchors(doc, self._repo_root))
            gates.append(gate_doc_line_refs(doc, self._repo_root))
        gates.append(gate_doc_scope(self._patch_reader(round_no), self._allowed))
        gates.extend(run_gate(spec, self._repo_root) for spec in self._extra)
        return VerificationReport(
            round=round_no,
            gates=gates,
            overall=compute_overall(gates),
            diff_stats=collect_diff_stats(self._repo_root),
        )
