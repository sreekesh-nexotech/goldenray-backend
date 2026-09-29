"""The public website calculators (``calculators/basic/``, ``calculators/basic-v2/``, ``calculators/advanced/``).

Each runs its :mod:`engines.website_calculators` port on the request body as parsed (the legacy endpoints read raw
``request.data``, and their outputs depend on how they read it) against the cached table snapshot, and turns an
engine error into a ``DomainError`` with the legacy status and message. An input the legacy endpoint crashed on is
400 ``invalid_input`` (logged at INFO with the cause, for the parity reviews).
"""

from __future__ import annotations

import logging

from calculators.services.data import calculator_data
from core.errors import DomainError
from engines import website_calculators as engine

logger = logging.getLogger("flarize.calculators")

CALCULATORS = {"basic": engine.basic, "basic_v2": engine.basic_v2, "advanced": engine.advanced}


def run(kind: str, body) -> dict:
    try:
        return CALCULATORS[kind](body, calculator_data())
    except engine.CalculatorError as exc:
        if exc.code == "invalid_input":
            logger.info("calculator input refused", extra={"calculator": kind, "detail": exc.detail})
        raise DomainError(exc.code, exc.message, status=exc.status) from None
