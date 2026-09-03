"""RED — TASK-003 (BEH-08, FR-07): неизвестный ключ отвергается закрытой схемой.

`load_pipeline_config` сегодня молча игнорирует незнакомые ключи на всех
уровнях `[pipeline]` вместо `ConfigError`: `_from_pipeline_table` читает
только перечисленные ключи через `table.get`/`_toml.text`, `_operator_
checklist` читает только `items`/`findings_item`, а `_toml.gate` — только
`name`/`cmd`/`enabled`. FR-07 требует закрытую схему: неизвестный ключ в
`[pipeline]`, `pipeline.checklists`, конкретном чеклисте, его `items` или
записи `pipeline.gates` обязан отклоняться `ConfigError` до построения
доказательства или продолжения.
"""

from pathlib import Path

import pytest

from disputatio.runtime import ConfigError, load_pipeline_config

_UNKNOWN_AT_PIPELINE_LEVEL = """
[pipeline]
spec_path = "docs/specs/2026-08-28-foo-design.md"
plan_path = "docs/plans/2026-08-28-foo-plan.md"
unknown_toplevel_key = "x"
"""

_UNKNOWN_AT_CHECKLISTS_LEVEL = """
[pipeline]
document_path = "docs/charter.md"

[pipeline.checklists]
unknown_checklists_key = "x"

[pipeline.checklists.doc]
findings_item = "B3"

[pipeline.checklists.doc.items]
B3 = "нет blocker/major-находок"
"""

_UNKNOWN_AT_SPECIFIC_CHECKLIST_LEVEL = """
[pipeline]
document_path = "docs/charter.md"

[pipeline.checklists.doc]
findings_item = "B3"
unknown_checklist_key = "x"

[pipeline.checklists.doc.items]
B3 = "нет blocker/major-находок"
"""

_UNKNOWN_AT_GATE_ENTRY_LEVEL = """
[pipeline]
spec_path = "docs/specs/2026-08-28-foo-design.md"
plan_path = "docs/plans/2026-08-28-foo-plan.md"

[[pipeline.gates]]
name = "extra-gate"
cmd = "true"
unknown_gate_key = "x"
"""

_CASES = (
    ("pipeline", _UNKNOWN_AT_PIPELINE_LEVEL),
    ("checklists", _UNKNOWN_AT_CHECKLISTS_LEVEL),
    ("specific-checklist", _UNKNOWN_AT_SPECIFIC_CHECKLIST_LEVEL),
    ("gate-entry", _UNKNOWN_AT_GATE_ENTRY_LEVEL),
)


def test_unknown_pipeline_keys_fail_closed_at_every_schema_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))

    for label, text in _CASES:
        path = tmp_path / f"config-{label}.toml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_pipeline_config(path)
