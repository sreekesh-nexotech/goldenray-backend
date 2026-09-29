"""System check of the inventory settings (``manage.py check``; no database access).

``inventory.E001`` — ``INVENTORY_RECEIVING_LOCATION`` must be blank (receiving off) or a location code
(1-30 letters, digits, ``_``, ``.``, ``-``). Whether that location exists is only known at run time: a missing one
makes the ``procurement.batch_committed`` handler fail visibly (retried, then parked).
"""

from __future__ import annotations

import re

from django.conf import settings
from django.core import checks

from inventory.models.location import CODE_PATTERN


@checks.register(checks.Tags.compatibility)
def check_receiving_location(app_configs=None, **kwargs):
    value = getattr(settings, "INVENTORY_RECEIVING_LOCATION", "")
    if value is None or (isinstance(value, str) and (not value.strip() or re.match(CODE_PATTERN, value.strip()))):
        return []
    return [
        checks.Error(
            f"INVENTORY_RECEIVING_LOCATION={value!r} is not a location code.",
            hint="Leave it blank to keep receiving off, or set the code of an inventory location (e.g. HO-STORE).",
            id="inventory.E001",
        )
    ]
