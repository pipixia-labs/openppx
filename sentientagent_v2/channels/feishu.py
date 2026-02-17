"""Feishu channel adapter (inbound WebSocket + outbound message API)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from .base import BaseChannel

logger = logging.getLogger(__name__)

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, P2ImMessageReceiveV1

    FEISHU_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    lark = None
    FEISHU_AVAILABLE = False

if FEISHU_AVAILABLE:
    try:
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        FEISHU_REACTION_AVAILABLE = True
    except Exception:  # pragma: no cover - sdk version dependent
        CreateMessageReactionRequest = None
        CreateMessageReactionRequestBody = None
        Emoji = None
        FEISHU_REACTION_AVAILABLE = False
else:
    CreateMessageReactionRequest = None
    CreateMessageReactionRequestBody = None
    Emoji = None
    FEISHU_REACTION_AVAILABLE = False


def _extract_post_text(content_json: dict[str, Any]) -> str:
    """Extract text from Feishu rich-text `post` payload."""
    for lang_key in ("zh_cn", "en_us", "ja_jp"):
        lang = content_json.get(lang_key)
        if isinstance(lang, dict) and isinstance(lang.get("content"), list):
            parts: list[str] = []
            title = lang.get("title", "")
            if title:
                parts.append(str(title))
            for block in lang["content"]:
                if isinstance(block, list):
                    for el in block:
                        if isinstance(el, dict) and el.get("tag") in {"text", "a"}:
                            text = str(el.get("text", "")).strip()
                            if text:
                                parts.append(text)
            if parts:
                return " ".join(parts).strip()
    return ""


class FeishuChannel(BaseChannel):
    """Minimal Feishu adapter compatible with the bus/gateway flow."""

    name = "feishu"

    def __init__(
        self,
        bus,
        *,
        app_id: str,
        app_secret: str,
        encrypt_key: str = "",
        verification_token: str = "",
    ) -> None:
        super().__init__(bus)
        self.app_id = app_id
        self.app_secret = app_secret
        self.encrypt_key = encrypt_key
        self.verification_token = verification_token
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None

    async def start(self) -> None:
        if not FEISHU_AVAILABLE:
            raise RuntimeError("Feishu channel requires `lark-oapi`.")
        if not self.app_id or not self.app_secret:
            raise RuntimeError("Missing FEISHU_APP_ID or FEISHU_APP_SECRET.")

        self._running = True
        self._loop = asyncio.get_running_loop()
        self._client = (
            lark.Client.builder()  # type: ignore[union-attr]
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .log_level(lark.LogLevel.INFO)  # type: ignore[union-attr]
            .build()
        )

        handler = (
            lark.EventDispatcherHandler.builder(  # type: ignore[union-attr]
                self.encrypt_key or "",
                self.verification_token or "",
            )
            .register_p2_im_message_receive_v1(self._on_message_sync)
            .build()
        )
        self._ws_client = lark.ws.Client(  # type: ignore[union-attr]
            self.app_id,
            self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,  # type: ignore[union-attr]
        )

        def _run_ws_forever() -> None:
            while self._running:
                try:
                    self._ws_client.start()
                except Exception:
                    logger.exception("Feishu websocket loop failed; retrying")
                    if self._running:
                        import time

                        time.sleep(3)

        self._ws_thread = threading.Thread(target=_run_ws_forever, daemon=True)
        self._ws_thread.start()

    async def stop(self) -> None:
        self._running = False
        if self._ws_client:
            stop_fn = getattr(self._ws_client, "stop", None)
            close_fn = getattr(self._ws_client, "close", None)
            try:
                if callable(stop_fn):
                    stop_fn()
                elif callable(close_fn):
                    close_fn()
                else:
                    logger.debug("Feishu ws client exposes no stop/close; skipping explicit shutdown")
            except Exception:
                logger.exception("Failed stopping Feishu websocket client")

    def _send_sync(self, msg) -> None:
        if not self._client:
            return
        receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
        payload = json.dumps({"text": msg.content}, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(msg.chat_id)
                .msg_type("text")
                .content(payload)
                .build()
            )
            .build()
        )
        self._client.im.v1.message.create(request)

    async def send(self, msg) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._send_sync, msg)

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """Best-effort reaction API call executed in thread pool."""
        if (
            not self._client
            or not FEISHU_REACTION_AVAILABLE
            or CreateMessageReactionRequest is None
            or CreateMessageReactionRequestBody is None
            or Emoji is None
        ):
            return
        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )
            self._client.im.v1.message_reaction.create(request)
        except Exception:
            logger.exception("Failed adding Feishu reaction")

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        if not message_id:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        try:
            event = data.event
            message = event.message
            sender = event.sender
            sender_type = getattr(sender, "sender_type", "")
            if sender_type == "bot":
                return

            message_id = getattr(message, "message_id", "")
            if message_id:
                # Mirror nanobot behavior: acknowledge user messages with a thumbs-up reaction.
                await self._add_reaction(message_id, "THUMBSUP")

            sender_id = getattr(getattr(sender, "sender_id", None), "open_id", "") or "unknown"
            chat_id = getattr(message, "chat_id", "")
            chat_type = getattr(message, "chat_type", "")
            msg_type = getattr(message, "message_type", "")
            raw_content = getattr(message, "content", "") or ""

            if msg_type == "text":
                try:
                    content = json.loads(raw_content).get("text", "")
                except json.JSONDecodeError:
                    content = raw_content
            elif msg_type == "post":
                try:
                    content = _extract_post_text(json.loads(raw_content))
                except Exception:
                    content = ""
            else:
                content = ""

            if not content:
                return

            # Keep the same routing rule as nanobot: groups reply to group chat_id,
            # p2p replies to sender open_id.
            target_chat_id = chat_id if chat_type == "group" else sender_id
            await self.publish_inbound(
                sender_id=sender_id,
                chat_id=target_chat_id,
                content=content,
                metadata={
                    "msg_type": msg_type,
                    "chat_type": chat_type,
                    "message_id": message_id,
                },
            )
        except Exception:
            logger.exception("Failed handling Feishu inbound message")
