"""Role-based CLI capability flags ([DESIGN-001]; argv-building lands in TASK-004)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterCapabilities:
    """Per-CLI capability flags read by permissions.py/fallback.py (stub)."""

    supports_granular_permissions: bool
