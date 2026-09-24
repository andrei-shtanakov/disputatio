"""Harness-периметр в `project.yaml` совпадает с тем, что обещает документация.

`docs/workstream-setup.md` §2 перечисляет пути под `harness_files`, а
`project.yaml` — SSOT, из которого maestro порождает worktree-конфиг.
Control-plane (`spec-runner.config.yaml`, `pyrefly.toml`) из SSOT выпал ещё
до сноса локального гейта, и документация обещала защиту, которой на
живом прогоне не было (`todo://disputatio/control-plane-not-in-ssot`).
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTROL_PLANE = ("spec-runner.config.yaml", "pyrefly.toml")


def _project_harness_files() -> list[str]:
    config = yaml.safe_load((ROOT / "project.yaml").read_text(encoding="utf-8"))
    executor = config["spec_runner"]["extra_executor_config"]["executor"]
    return list(executor["harness_files"])


def _documented_harness_files() -> list[str]:
    text = (ROOT / "docs" / "workstream-setup.md").read_text(encoding="utf-8")
    section = text.split("## 2. Control-plane в harness_files", 1)[1]
    block = section.split("```yaml", 1)[1].split("```", 1)[0]
    return re.findall(r"^\s*-\s+(\S+)", block, flags=re.MULTILINE)


def test_control_plane_is_in_ssot() -> None:
    harness = _project_harness_files()
    for path in CONTROL_PLANE:
        assert path in harness


def test_documented_harness_matches_ssot() -> None:
    assert _documented_harness_files() == _project_harness_files()
