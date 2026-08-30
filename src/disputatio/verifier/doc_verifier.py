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
from typing import Final

from disputatio.contracts.base import SCHEMA_V2
from disputatio.contracts.verification import (
    GateResult,
    GateStatus,
    OverallStatus,
    VerificationReport,
)
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

#: Имена пяти baseline doc-гейтов §6 — единственный источник истины для
#: конфигурации пайплайна (SPEC-002 §6, задача 13): попытка объявить `extra`
#: gate с любым из этих имён обязана быть отказом валидации конфига, а не
#: тихим переопределением одного из пяти прогонов выше.
BASELINE_GATE_NAMES: Final[tuple[str, ...]] = (
    "doc-paths",
    "doc-links",
    "doc-anchors",
    "doc-line-refs",
    "doc-scope",
)


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

        `doc_paths` обязан быть непустым: пустой кортеж выключил бы четыре
        из пяти baseline-гейтов (`doc-paths`/`doc-links`/`doc-anchors`/
        `doc-line-refs` попросту не итерируются) — тот же запрещённый §6
        "флаг отключения", только выраженный типом входа, а не булем
        конфигурации (фикс-раунд 1, Critical: пустой `doc_paths` — не
        гипотетический кейс, а обязательный вход по аннотации типа, значит
        и вызывающий код мог собрать его пустым по ошибке). Отказ здесь, а
        не молчаливый пропуск гейтов — тот же принцип, что и `tail_lines`
        у `VerifierRunner`.

        `extra` копируется в `list` — тот же довод, что и у
        `VerifierRunner`: список принадлежит вызывающему коду, и его
        последующая правка не должна менять состав уже настроенного
        верификатора.
        """
        if not doc_paths:
            raise ValueError(
                "doc_paths must not be empty: doc-paths/doc-links/"
                "doc-anchors/doc-line-refs would never run"
            )
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
        baseline: list[GateResult] = []
        for doc in self._doc_paths:
            baseline.append(gate_doc_paths(doc, self._repo_root))
            baseline.append(gate_doc_links(doc, self._repo_root))
            baseline.append(gate_doc_anchors(doc, self._repo_root))
            baseline.append(gate_doc_line_refs(doc, self._repo_root))
        baseline.append(gate_doc_scope(self._patch_reader(round_no), self._allowed))
        gates.extend(baseline)
        gates.extend(run_gate(spec, self._repo_root) for spec in self._extra)
        return VerificationReport(
            # `disputatio/v2` безусловно: этот верификатор существует только
            # для doc-контуров, а §5.1 SPEC-002 требует, чтобы артефакты
            # doc-сессии несли тег v2. Развилки по режиму здесь нет и быть не
            # должно — `VerifierRunner` остаётся v1-писателем, и два тега
            # различаются реализацией, а не флагом.
            schema=SCHEMA_V2,
            round=round_no,
            gates=gates,
            overall=_overall_with_baseline(gates, baseline),
            diff_stats=collect_diff_stats(self._repo_root),
        )


def _overall_with_baseline(
    gates: list[GateResult], baseline: list[GateResult]
) -> OverallStatus:
    """`compute_overall`, но невыполненный baseline-гейт — тоже провал.

    Два верных решения давали вместе fail-open. `_read_document` отдаёт
    `skip`, когда документ не существует или не читается, — сбой запуска
    гейта не летит наружу исключением (конвенция `runner.run_gate`).
    `compute_overall` считает `skip` не-провалом: отключённый гейт ничего
    не опровергает ([DESIGN-009]). В doc-контуре второе к первому не
    применимо: baseline §6 **неотключаем** — у конструктора нет и
    параметра, которым его можно было бы выключить, — поэтому «гейт не
    выполнился» здесь означает не «его отключили», а «проверено не было
    ничего».

    Без этой ветки автор spec-контура, удаливший спеку, получал зелёный
    отчёт: `doc-scope` видит разрешённый путь и молчит, а четыре
    content-гейта уходят в `skip`. Раунд, стерший предмет ревью, проходил
    детерминированную половину критерия сходимости.

    `extra`-гейты правилом не затронуты: они добавлены конфигом, их `skip`
    — ровно тот случай, ради которого [DESIGN-009] и написан.
    """
    if any(gate.status == GateStatus.SKIP for gate in baseline):
        return OverallStatus.FAIL
    return compute_overall(gates)
