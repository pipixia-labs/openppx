"""openpipixia package."""

from __future__ import annotations

import importlib

__all__ = ["root_agent", "agent", "cli", "gateway"]


def __getattr__(name: str):
    if name == "root_agent":
        from .app.agent import root_agent

        return root_agent
    if name == "agent":
        return importlib.import_module(".app.agent", __name__)
    if name == "cli":
        return importlib.import_module(".app.cli", __name__)
    if name == "gateway":
        return importlib.import_module(".app.gateway", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
