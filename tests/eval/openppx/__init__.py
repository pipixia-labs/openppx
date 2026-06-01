"""ADK eval entrypoint for openppx.

The official ``adk eval`` CLI infers ``app_name`` from the agent directory
basename. Keep this directory named ``openppx`` so eval sessions use the same
application scope as production.
"""

from __future__ import annotations

from importlib import import_module


agent = import_module("openppx.app.agent")

__all__ = ["agent"]
