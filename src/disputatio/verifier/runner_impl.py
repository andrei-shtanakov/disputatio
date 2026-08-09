"""`VerifierRunner` — реализация порта `Verifier` ([DESIGN-003], [REQ-001]).

Оркестратор верификации внутри пакета и единственное место, где сходятся
`runner.run_gate` ([DESIGN-006]), `diffstats.collect_diff_stats`
([DESIGN-008]) и `aggregate.compute_overall` ([DESIGN-009]). Своей логики
статусов здесь нет — только композиция и упаковка в артефакт.

Зависимости — stdlib и `disputatio.contracts.*` ([REQ-010]); процессы
запускаются исключительно через `capture` в глубине `run_gate`.
"""

from pathlib import Path

from disputatio.contracts.verification import VerificationReport
from disputatio.verifier.aggregate import compute_overall
from disputatio.verifier.capture import DEFAULT_TAIL_LINES
from disputatio.verifier.config import GateSpec
from disputatio.verifier.diffstats import collect_diff_stats
from disputatio.verifier.runner import run_gate


class VerifierRunner:
    """Конкретная реализация порта `Verifier`: прогон gates раунда."""

    def __init__(
        self,
        gates: list[GateSpec],
        workdir: Path,
        *,
        tail_lines: int = DEFAULT_TAIL_LINES,
    ) -> None:
        """Запоминает конфигурацию прогона; ничего не запускает.

        `gates` копируется: список принадлежит вызывающему коду (w-runtime),
        и его последующая правка не должна менять состав уже настроенного
        раннера. Пустой список — валидный вход (режим `analyze`).

        `tail_lines` проверяется здесь, а не при первом запуске: невалидный
        лимит иначе всплыл бы как `ValueError` внутри `run_gate`, который
        обязан гасить ошибки запуска в `skip` ([REQ-011]). Каждый gate стал
        бы «пропущенным» с чужой причиной, а `overall` — зелёным ([REQ-008]):
        ошибка конфигурации превратилась бы в ложный `pass`.
        """
        if tail_lines < 1:
            raise ValueError(f"tail_lines must be >= 1, got {tail_lines}")
        self._gates = list(gates)
        self._workdir = workdir
        self._tail_lines = tail_lines

    def verify(self, round_no: int) -> VerificationReport:
        """Прогоняет все gates и возвращает отчёт раунда `round_no`.

        Gates выполняются последовательно, в порядке конфигурации, и в том
        же порядке попадают в `report.gates` ([REQ-002]).

        `diff_stats` снимается ПОСЛЕ всех gates: gate вправе оставить в
        дереве побочный продукт, и статистика обязана видеть дерево таким,
        каким его увидит ревьюер ([REQ-007]).

        Метод не хранит состояние между вызовами — `round_no` попадает
        только в поле `round` отчёта, запись `verification.json` остаётся
        зоной w-events ([DESIGN-003]).
        """
        gates = [
            run_gate(spec, self._workdir, tail_lines=self._tail_lines)
            for spec in self._gates
        ]
        return VerificationReport(
            round=round_no,
            gates=gates,
            overall=compute_overall(gates),
            diff_stats=collect_diff_stats(self._workdir),
        )
