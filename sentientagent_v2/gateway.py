"""Gateway that bridges bus/channel traffic to ADK Runner."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.genai import types

from .bus.events import InboundMessage, OutboundMessage
from .bus.queue import MessageBus
from .channels.manager import ChannelManager
from .runtime.adk_utils import extract_text, merge_text_stream
from .runtime.runner_factory import create_runner
from .runtime.tool_context import route_context
from .tools import configure_outbound_publisher

logger = logging.getLogger(__name__)


class Gateway:
    """Consumes inbound messages and executes them via ADK Runner."""

    def __init__(
        self,
        *,
        agent: Any,
        app_name: str,
        bus: MessageBus,
        channel_manager: ChannelManager | None = None,
        session_service: Any | None = None,
    ) -> None:
        self.bus = bus
        self.channel_manager = channel_manager
        self.runner, self.session_service = create_runner(
            agent=agent,
            app_name=app_name,
            session_service=session_service,
        )
        self._inbound_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._inbound_task and not self._inbound_task.done():
            return
        # Tools call `message(...)` from inside runner execution; this bridges
        # those tool-level sends back into the outbound queue.
        configure_outbound_publisher(self.bus.publish_outbound)
        if self.channel_manager:
            await self.channel_manager.start_all()
            await self.channel_manager.start_dispatcher()
        self._inbound_task = asyncio.create_task(self._consume_inbound())

    async def stop(self) -> None:
        configure_outbound_publisher(None)
        if self._inbound_task:
            self._inbound_task.cancel()
            try:
                await self._inbound_task
            except asyncio.CancelledError:
                pass
            self._inbound_task = None
        if self.channel_manager:
            await self.channel_manager.stop_dispatcher()
            await self.channel_manager.stop_all()

    async def process_message(self, msg: InboundMessage) -> OutboundMessage:
        request = types.UserContent(parts=[types.Part.from_text(text=msg.content)])
        final = ""
        # Route context lets tools like `message(...)` infer the current target.
        with route_context(msg.channel, msg.chat_id):
            async for event in self.runner.run_async(
                user_id=msg.sender_id,
                session_id=msg.session_key,
                new_message=request,
            ):
                text = extract_text(getattr(event, "content", None))
                final = merge_text_stream(final, text)
        if not final:
            final = "(no response)"
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final,
            metadata=msg.metadata,
        )

    async def _consume_inbound(self) -> None:
        while True:
            # Single worker keeps message order deterministic for this skeleton.
            msg = await self.bus.consume_inbound()
            try:
                response = await self.process_message(msg)
                await self.bus.publish_outbound(response)
            except Exception:
                logger.exception(
                    "Failed processing inbound message (channel=%s chat_id=%s sender_id=%s)",
                    msg.channel,
                    msg.chat_id,
                    msg.sender_id,
                )
