"""Job handler registry. Handler modules register with @handler("kind")."""

from __future__ import annotations

import importlib
from collections.abc import Callable

from sqlalchemy.orm import Session

Handler = Callable[[Session, dict], None]
HANDLERS: dict[str, Handler] = {}

# Modules that define handlers; imported by the worker at startup.
HANDLER_MODULES = (
    "repeater.sources.jobs",
    "repeater.engine.jobs",
)


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


def load_handlers() -> dict[str, Handler]:
    for mod in HANDLER_MODULES:
        importlib.import_module(mod)
    return HANDLERS
