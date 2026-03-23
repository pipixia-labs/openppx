"""Channel factory and env-driven bootstrap helpers."""

from __future__ import annotations

import os
from typing import Callable

from ..bus.queue import MessageBus
from .local import LocalChannel
from .manager import ChannelManager
from .registry import get_channel_spec, known_channel_names


def parse_enabled_channels(channels: str | None) -> list[str]:
    """Parse channel list from CLI arg or env."""
    raw = channels if channels is not None else os.getenv("OPENPIPIXIA_CHANNELS", "local")
    names = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not names:
        return ["local"]
    # Keep input order but deduplicate.
    return list(dict.fromkeys(names))


def validate_channel_setup(channel_names: list[str]) -> list[str]:
    issues: list[str] = []
    supported = set(known_channel_names())
    unknown = [name for name in channel_names if name not in supported]
    if unknown:
        issues.append(f"Unsupported channels: {', '.join(unknown)}")

    for name in channel_names:
        spec = get_channel_spec(name)
        if spec is None:
            continue
        issues.extend(spec.validate_setup())
    return issues


def build_channel_manager(
    *,
    bus: MessageBus,
    channel_names: list[str],
    local_writer: Callable[[str], None] | None = None,
) -> tuple[ChannelManager, LocalChannel | None]:
    """Build channel manager and register selected channels."""
    manager = ChannelManager(bus)
    local_channel: LocalChannel | None = None

    for name in channel_names:
        spec = get_channel_spec(name)
        if spec is None:
            continue

        channel = spec.build(bus, local_writer)
        if channel is None:
            continue
        manager.register(channel)
        if isinstance(channel, LocalChannel):
            local_channel = channel

    return manager, local_channel
