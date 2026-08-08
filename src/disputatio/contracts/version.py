"""Единственная точка версии схемы артефактов disputatio (REQ-001).

Несовместимые изменения схем — только через bump до "disputatio/v2":
новая константа добавляется здесь, старая не переиспользуется.
"""

from typing import Final

SCHEMA_V1: Final = "disputatio/v1"
