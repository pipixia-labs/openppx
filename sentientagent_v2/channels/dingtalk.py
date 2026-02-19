"""DingTalk channel adapter (stream-mode inbound + token API outbound)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..bus.events import OutboundMessage
from .base import BaseChannel

logger = logging.getLogger(__name__)

try:
    from dingtalk_stream import AckMessage, CallbackHandler, Credential, DingTalkStreamClient
    from dingtalk_stream.chatbot import ChatbotMessage

    DINGTALK_AVAILABLE = True
except Exception:  # pragma: no cover - optional runtime dependency
    AckMessage = None
    CallbackHandler = object
    Credential = None
    DingTalkStreamClient = None
    ChatbotMessage = None
    DINGTALK_AVAILABLE = False


def _ack_ok() -> tuple[str, str]:
    status = getattr(AckMessage, "STATUS_OK", "OK")
    return str(status), "OK"


class DingTalkCallbackHandler(CallbackHandler):
    """Forward Stream SDK callbacks to channel normalization logic."""

    def __init__(self, channel: "DingTalkChannel") -> None:
        super().__init__()
        self._channel = channel

    async def process(self, message: Any) -> tuple[str, str]:
        try:
            payload = getattr(message, "data", None)
            if isinstance(payload, dict):
                await self._channel._process_stream_payload(payload)
        except Exception:
            logger.exception("DingTalk stream callback processing failed")
        return _ack_ok()


class DingTalkChannel(BaseChannel):
    """DingTalk adapter with stream-mode inbound and private outbound messaging."""

    name = "dingtalk"

    def __init__(
        self,
        bus,
        *,
        client_id: str,
        client_secret: str,
        allow_from: list[str] | None = None,
        api_base: str = "https://api.dingtalk.com",
        token_margin_seconds: int = 60,
        enable_stream_mode: bool = True,
        stream_reconnect_delay_seconds: int = 5,
    ) -> None:
        super().__init__(bus, allow_from=allow_from)
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.api_base = api_base.rstrip("/")
        self.token_margin_seconds = max(int(token_margin_seconds), 0)
        self.enable_stream_mode = bool(enable_stream_mode)
        self.stream_reconnect_delay_seconds = max(int(stream_reconnect_delay_seconds), 1)

        self._access_token: str | None = None
        self._token_expiry_epoch: float = 0.0
        self._stream_client: Any = None
        self._stream_task: asyncio.Task[None] | None = None

    def _endpoint(self, path: str) -> str:
        return f"{self.api_base}{path}"

    def _api_call_sync(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body: bytes | None = None
        request_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json; charset=utf-8")

        req = Request(
            self._endpoint(path),
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(req, timeout=20) as response:
                raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
        except HTTPError as exc:
            raise RuntimeError(f"DingTalk API HTTP error ({path}): {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"DingTalk API network error ({path}): {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DingTalk API invalid JSON ({path}): {exc}") from exc

        if not isinstance(parsed, dict):
            raise RuntimeError(f"DingTalk API unexpected response ({path})")
        return parsed

    async def _api_call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        call = functools.partial(
            self._api_call_sync,
            method,
            path,
            payload=payload,
            headers=headers,
        )
        return await loop.run_in_executor(None, call)

    async def start(self) -> None:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Missing DINGTALK_CLIENT_ID or DINGTALK_CLIENT_SECRET for dingtalk channel.")
        self._running = True
        if not self.enable_stream_mode:
            logger.info("DingTalk stream mode disabled by configuration.")
            return
        if not DINGTALK_AVAILABLE or Credential is None or DingTalkStreamClient is None:
            logger.info("dingtalk-stream not installed: inbound stream mode disabled.")
            return

        credential = Credential(self.client_id, self.client_secret)
        self._stream_client = DingTalkStreamClient(credential)
        topic = getattr(ChatbotMessage, "TOPIC", "chatbot.message")
        self._stream_client.register_callback_handler(topic, DingTalkCallbackHandler(self))
        self._stream_task = asyncio.create_task(self._run_stream_loop(), name="dingtalk-stream")

    async def stop(self) -> None:
        self._running = False
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None
        self._stream_client = None

    async def _run_stream_loop(self) -> None:
        while self._running and self._stream_client is not None:
            try:
                await self._stream_client.start()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("DingTalk stream loop failed")
            if self._running:
                await asyncio.sleep(self.stream_reconnect_delay_seconds)

    async def send(self, msg: OutboundMessage) -> None:
        token = await self._get_access_token()
        if not token:
            logger.warning("Skip DingTalk send: access token is unavailable.")
            return
        user_id = msg.chat_id.strip()
        if not user_id:
            logger.warning("Skip DingTalk send: empty chat_id.")
            return

        payload = {
            "robotCode": self.client_id,
            "userIds": [user_id],
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps(
                {
                    "text": msg.content or "[empty message]",
                    "title": "sentientagent_v2 reply",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        await self._api_call(
            "POST",
            "/v1.0/robot/oToMessages/batchSend",
            payload=payload,
            headers={"x-acs-dingtalk-access-token": token},
        )

    async def _get_access_token(self) -> str | None:
        if self._access_token and time.time() < self._token_expiry_epoch:
            return self._access_token

        response = await self._api_call(
            "POST",
            "/v1.0/oauth2/accessToken",
            payload={
                "appKey": self.client_id,
                "appSecret": self.client_secret,
            },
            headers=None,
        )
        token = str(response.get("accessToken", "")).strip()
        if not token:
            return None
        expire_in_raw = response.get("expireIn", 7200)
        try:
            expire_in = int(expire_in_raw)
        except Exception:
            expire_in = 7200

        self._access_token = token
        self._token_expiry_epoch = time.time() + max(expire_in - self.token_margin_seconds, 30)
        return token

    async def _on_message(self, *, content: str, sender_id: str, sender_name: str = "") -> None:
        """Normalize one DingTalk inbound message into bus format."""
        text = str(content).strip()
        user_id = str(sender_id).strip()
        if not text or not user_id:
            return
        await self.publish_inbound(
            sender_id=user_id,
            chat_id=user_id,
            content=text,
            metadata={"sender_name": str(sender_name).strip()},
        )

    async def _process_stream_payload(self, payload: dict[str, Any]) -> None:
        """Normalize Stream SDK payload and publish inbound."""
        content, sender_id, sender_name = self._parse_stream_payload(payload)
        if not content or not sender_id:
            return
        await self._on_message(content=content, sender_id=sender_id, sender_name=sender_name)

    def _parse_stream_payload(self, payload: dict[str, Any]) -> tuple[str, str, str]:
        if ChatbotMessage is not None and hasattr(ChatbotMessage, "from_dict"):
            try:
                parsed = ChatbotMessage.from_dict(payload)
                text_obj = getattr(parsed, "text", None)
                text = str(getattr(text_obj, "content", "")).strip()
                sender_id = str(
                    getattr(parsed, "sender_staff_id", "")
                    or getattr(parsed, "sender_id", "")
                ).strip()
                sender_name = str(getattr(parsed, "sender_nick", "")).strip()
                if text and sender_id:
                    return text, sender_id, sender_name
            except Exception:
                logger.debug("DingTalk ChatbotMessage parser failed; fallback to raw payload fields.")

        text = ""
        raw_text = payload.get("text")
        if isinstance(raw_text, dict):
            text = str(raw_text.get("content", "")).strip()
        if not text:
            text = str(payload.get("content", "")).strip()

        sender_id = str(
            payload.get("senderStaffId")
            or payload.get("sender_staff_id")
            or payload.get("senderId")
            or payload.get("sender_id")
            or ""
        ).strip()
        sender_name = str(
            payload.get("senderNick")
            or payload.get("sender_nick")
            or payload.get("senderName")
            or ""
        ).strip()
        return text, sender_id, sender_name
