"""WhatsApp channel adapter using the local Node.js bridge (WebSocket)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..bus.events import OutboundMessage
from .base import BaseChannel
from .polling_utils import cancel_background_task

logger = logging.getLogger(__name__)

try:
    import websockets

    WHATSAPP_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    websockets = None
    WHATSAPP_AVAILABLE = False


class WhatsAppChannel(BaseChannel):
    """Minimal WhatsApp adapter that talks to openpipixia's bridge protocol."""

    name = "whatsapp"

    def __init__(
        self,
        bus,
        *,
        bridge_url: str,
        bridge_token: str = "",
        allow_from: list[str] | None = None,
        reconnect_delay_seconds: int = 5,
    ) -> None:
        super().__init__(bus, allow_from=allow_from)
        self.bridge_url = bridge_url.strip()
        self.bridge_token = bridge_token.strip()
        self.reconnect_delay_seconds = max(int(reconnect_delay_seconds), 1)

        self._connected = False
        self._ws = None
        self._listen_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not WHATSAPP_AVAILABLE:
            raise RuntimeError("WhatsApp channel requires `websockets` package.")
        if not self.bridge_url:
            raise RuntimeError("Missing WHATSAPP_BRIDGE_URL for whatsapp channel.")
        if self._listen_task and not self._listen_task.done():
            return

        self._running = True
        self._listen_task = asyncio.create_task(self._run_connection_loop(), name="whatsapp-bridge")

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        await cancel_background_task(self._listen_task)
        self._listen_task = None

    async def send(self, msg: OutboundMessage) -> None:
        if not self._ws or not self._connected:
            logger.warning("Skip WhatsApp send: bridge is not connected.")
            return
        payload = {
            "type": "send",
            "to": msg.chat_id,
            "text": msg.content or "[empty message]",
        }
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _run_connection_loop(self) -> None:
        while self._running:
            try:
                async with websockets.connect(self.bridge_url) as ws:
                    self._ws = ws
                    if self.bridge_token:
                        await ws.send(
                            json.dumps(
                                {"type": "auth", "token": self.bridge_token},
                                ensure_ascii=False,
                            )
                        )
                    self._connected = True
                    async for raw in ws:
                        try:
                            await self._handle_bridge_message(raw)
                        except Exception:
                            logger.exception("WhatsApp bridge message handling failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("WhatsApp bridge connection failed")
            finally:
                self._connected = False
                self._ws = None
            if self._running:
                await asyncio.sleep(self.reconnect_delay_seconds)

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle one inbound JSON payload from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON payload from WhatsApp bridge: %s", raw[:120])
            return
        if not isinstance(data, dict):
            return

        msg_type = str(data.get("type", "")).strip()
        if msg_type == "status":
            status = str(data.get("status", "")).strip().lower()
            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False
            return

        if msg_type != "message":
            return

        sender = str(data.get("sender", "")).strip()
        pn = str(data.get("pn", "")).strip()
        content = str(data.get("content", "")).strip()
        if not content:
            return

        user_id = pn or sender
        sender_id = user_id.split("@")[0] if "@" in user_id else user_id
        chat_id = sender or user_id
        if not sender_id or not chat_id:
            return

        metadata: dict[str, Any] = {
            "message_id": str(data.get("id", "")).strip(),
            "timestamp": str(data.get("timestamp", "")),
            "is_group": bool(data.get("isGroup", False)),
        }
        await self.publish_inbound(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            metadata=metadata,
        )
