"""Contract-слой disputatio: pydantic-модели артефактов, валидация, ports.

Публичный API пакета (DESIGN-009): потребители (w-runtime и другие)
импортируют только из `disputatio.contracts`, не из подмодулей. `__all__`
расширяется по мере добавления моделей волны 1.
"""

from disputatio.contracts.base import ArtifactModel, FrozenModel
from disputatio.contracts.version import SCHEMA_V1

__all__ = [
    "SCHEMA_V1",
    "ArtifactModel",
    "FrozenModel",
]
