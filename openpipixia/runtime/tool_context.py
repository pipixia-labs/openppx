"""Per-request tool routing context."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator


_ROUTE_CHANNEL: ContextVar[str | None] = ContextVar("route_channel", default=None)
_ROUTE_CHAT_ID: ContextVar[str | None] = ContextVar("route_chat_id", default=None)


def get_route() -> tuple[str | None, str | None]:
    return _ROUTE_CHANNEL.get(), _ROUTE_CHAT_ID.get()


@contextmanager
def route_context(channel: str, chat_id: str) -> Iterator[None]:
    channel_token: Token[str | None] = _ROUTE_CHANNEL.set(channel)
    chat_token: Token[str | None] = _ROUTE_CHAT_ID.set(chat_id)
    try:
        yield
    finally:
        _ROUTE_CHANNEL.reset(channel_token)
        _ROUTE_CHAT_ID.reset(chat_token)
