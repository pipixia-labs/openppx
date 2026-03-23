"""QQ channel adapter using botpy SDK (minimal C2C support)."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import TYPE_CHECKING

from ..bus.events import OutboundMessage
from .base import BaseChannel
from .polling_utils import cancel_background_task

logger = logging.getLogger(__name__)

try:
    import botpy

    QQ_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    botpy = None
    QQ_AVAILABLE = False

if TYPE_CHECKING:  # pragma: no cover
    from botpy.message import C2CMessage


def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """Create a botpy client class that forwards inbound messages to channel."""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self) -> None:
            super().__init__(intents=intents)

        async def on_c2c_message_create(self, message: "C2CMessage") -> None:
            await channel._on_message(message)

        async def on_direct_message_create(self, message) -> None:
            await channel._on_message(message)

    return _Bot


class QQChannel(BaseChannel):
    """Minimal QQ adapter with outbound send and inbound private message handling."""

    name = "qq"

    def __init__(
        self,
        bus,
        *,
        app_id: str,
        secret: str,
        allow_from: list[str] | None = None,
    ) -> None:
        super().__init__(bus, allow_from=allow_from)
        self.app_id = app_id.strip()
        self.secret = secret.strip()
        self._client = None
        self._bot_task: asyncio.Task[None] | None = None
        self._processed_ids: deque[str] = deque(maxlen=1000)

    async def start(self) -> None:
        if not QQ_AVAILABLE:
            logger.warning("QQ channel unavailable: install qq-botpy to enable runtime connection.")
            return
        if not self.app_id or not self.secret:
            raise RuntimeError("Missing QQ_APP_ID or QQ_SECRET for qq channel.")

        self._running = True
        bot_cls = _make_bot_class(self)
        self._client = bot_cls()
        self._bot_task = asyncio.create_task(self._run_bot(), name="qq-bot")

    async def stop(self) -> None:
        self._running = False
        await cancel_background_task(self._bot_task)
        self._bot_task = None

    async def send(self, msg: OutboundMessage) -> None:
        if not self._client:
            logger.warning("Skip QQ send: client is not running.")
            return
        await self._client.api.post_c2c_message(
            openid=msg.chat_id,
            msg_type=0,
            content=msg.content or "[empty message]",
        )

    async def _run_bot(self) -> None:
        while self._running:
            try:
                await self._client.start(appid=self.app_id, secret=self.secret)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("QQ bot connection failed")
            if self._running:
                await asyncio.sleep(5)

    async def _on_message(self, data) -> None:
        """Normalize QQ C2C message object into bus inbound format."""
        message_id = str(getattr(data, "id", "")).strip()
        if message_id and message_id in self._processed_ids:
            return
        if message_id:
            self._processed_ids.append(message_id)

        author = getattr(data, "author", None)
        sender_id = str(
            getattr(author, "id", None)
            or getattr(author, "user_openid", None)
            or ""
        ).strip()
        content = str(getattr(data, "content", "")).strip()
        if not sender_id or not content:
            return

        await self.publish_inbound(
            sender_id=sender_id,
            chat_id=sender_id,
            content=content,
            metadata={"message_id": message_id},
        )
