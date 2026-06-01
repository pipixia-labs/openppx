"""Shared runner profile type definitions."""

from __future__ import annotations

from typing import Literal

RunnerProfile = Literal["full", "ephemeral"]

__all__ = ["RunnerProfile"]
