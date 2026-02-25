"""Feishu channel adapter (inbound WebSocket + outbound message API)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from .base import BaseChannel

logger = logging.getLogger(__name__)

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        GetFileRequest,
        GetMessageResourceRequest,
        P2ImMessageReceiveV1,
    )

    FEISHU_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    lark = None
    CreateFileRequest = None
    CreateFileRequestBody = None
    CreateImageRequest = None
    CreateImageRequestBody = None
    CreateMessageRequest = None
    CreateMessageRequestBody = None
    GetFileRequest = None
    GetMessageResourceRequest = None
    P2ImMessageReceiveV1 = None
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


def _iter_post_lang_payloads(content_json: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    if isinstance(content_json.get("content"), list):
        payloads.append(content_json)
    for lang_key in ("zh_cn", "en_us", "ja_jp"):
        lang = content_json.get(lang_key)
        if isinstance(lang, dict) and isinstance(lang.get("content"), list):
            payloads.append(lang)
    return payloads


def _extract_post_image_keys(content_json: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for lang in _iter_post_lang_payloads(content_json):
        blocks = lang.get("content", [])
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, list):
                continue
            for el in block:
                if not isinstance(el, dict):
                    continue
                if el.get("tag") not in {"img", "image"}:
                    continue
                key = str(el.get("image_key", "")).strip()
                if key:
                    keys.append(key)
    return list(dict.fromkeys(keys))


def _workspace_root() -> Path:
    workspace = os.getenv("OPENHERON_WORKSPACE", "").strip()
    if workspace:
        return Path(workspace).expanduser().resolve()
    return Path.cwd().resolve()


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\- ]+", "_", (name or "").strip()).strip(" .")
    return cleaned or "attachment.bin"


def _suffix_from_content_type(content_type: str, default_suffix: str) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "application/pdf": ".pdf",
    }
    return mapping.get(normalized, default_suffix)


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
        allow_from: list[str] | None = None,
    ) -> None:
        super().__init__(bus, allow_from=allow_from)
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

    @staticmethod
    def _resolve_receive_id_type(chat_id: str) -> str:
        return "chat_id" if chat_id.startswith("oc_") else "open_id"

    def _send_text_sync(self, msg, text: str | None = None) -> None:
        if not self._client:
            return
        receive_id_type = self._resolve_receive_id_type(msg.chat_id)
        payload = json.dumps({"text": text if text is not None else msg.content}, ensure_ascii=False)
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

    def _upload_image_sync(self, image_path: str) -> str:
        if not self._client or CreateImageRequest is None or CreateImageRequestBody is None:
            raise RuntimeError("Feishu image API is unavailable in current SDK/runtime")

        target = Path(image_path).expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(f"Image file not found: {target}")
        if not target.is_file():
            raise ValueError(f"Image path is not a file: {target}")

        with target.open("rb") as image_file:
            request = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(image_file)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.image.create(request)

        success_fn = getattr(response, "success", None)
        if callable(success_fn) and not success_fn():
            code = getattr(response, "code", "")
            message = getattr(response, "msg", "")
            log_id_fn = getattr(response, "get_log_id", None)
            log_id = log_id_fn() if callable(log_id_fn) else ""
            raise RuntimeError(f"Feishu image upload failed: code={code}, msg={message}, log_id={log_id}")

        image_key = getattr(getattr(response, "data", None), "image_key", "")
        if not image_key:
            raise RuntimeError("Feishu image upload returned empty image_key")
        return str(image_key)

    def _upload_file_sync(self, file_path: str) -> str:
        if not self._client or CreateFileRequest is None or CreateFileRequestBody is None:
            raise RuntimeError("Feishu file API is unavailable in current SDK/runtime")

        target = Path(file_path).expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(f"File not found: {target}")
        if not target.is_file():
            raise ValueError(f"File path is not a file: {target}")

        with target.open("rb") as file_obj:
            request = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type("stream")
                    .file_name(target.name)
                    .file(file_obj)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.file.create(request)

        success_fn = getattr(response, "success", None)
        if callable(success_fn) and not success_fn():
            code = getattr(response, "code", "")
            message = getattr(response, "msg", "")
            log_id_fn = getattr(response, "get_log_id", None)
            log_id = log_id_fn() if callable(log_id_fn) else ""
            raise RuntimeError(f"Feishu file upload failed: code={code}, msg={message}, log_id={log_id}")

        file_key = getattr(getattr(response, "data", None), "file_key", "")
        if not file_key:
            raise RuntimeError("Feishu file upload returned empty file_key")
        return str(file_key)

    def _send_image_sync(self, msg, image_path: str) -> None:
        if not self._client:
            return
        receive_id_type = self._resolve_receive_id_type(msg.chat_id)
        image_key = self._upload_image_sync(image_path)
        payload = json.dumps({"image_key": image_key}, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(msg.chat_id)
                .msg_type("image")
                .content(payload)
                .build()
            )
            .build()
        )
        self._client.im.v1.message.create(request)

    def _send_file_sync(self, msg, file_path: str) -> None:
        if not self._client:
            return
        receive_id_type = self._resolve_receive_id_type(msg.chat_id)
        file_key = self._upload_file_sync(file_path)
        payload = json.dumps({"file_key": file_key}, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(msg.chat_id)
                .msg_type("file")
                .content(payload)
                .build()
            )
            .build()
        )
        self._client.im.v1.message.create(request)

    def _send_sync(self, msg) -> None:
        if not self._client:
            return
        metadata = msg.metadata if isinstance(getattr(msg, "metadata", None), dict) else {}
        content_type = str(metadata.get("content_type", "")).strip().lower()
        image_path = str(metadata.get("image_path", "")).strip() if content_type == "image" else ""
        file_path = str(metadata.get("file_path", "")).strip() if content_type == "file" else ""
        if image_path:
            try:
                self._send_image_sync(msg, image_path)
                caption = (msg.content or "").strip()
                if caption:
                    self._send_text_sync(msg, caption)
            except Exception:
                logger.exception("Failed sending Feishu image message; falling back to text")
                fallback = (msg.content or "").strip() or f"[image send failed] {image_path}"
                self._send_text_sync(msg, fallback)
            return
        if file_path:
            try:
                self._send_file_sync(msg, file_path)
                caption = (msg.content or "").strip()
                if caption:
                    self._send_text_sync(msg, caption)
            except Exception:
                logger.exception("Failed sending Feishu file message; falling back to text")
                fallback = (msg.content or "").strip() or f"[file send failed] {file_path}"
                self._send_text_sync(msg, fallback)
            return
        self._send_text_sync(msg)

    def _download_resource_sync(
        self,
        *,
        resource_key: str,
        message_id: str,
        resource_type: str,
        suggested_name: str,
        default_suffix: str,
        allow_legacy_file_api: bool,
    ) -> Path:
        if not self._client:
            raise RuntimeError("Feishu client is unavailable")

        if GetMessageResourceRequest is not None:
            request = (
                GetMessageResourceRequest.builder()
                .type(resource_type)
                .message_id(message_id)
                .file_key(resource_key)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
        elif allow_legacy_file_api and GetFileRequest is not None:
            request = GetFileRequest.builder().file_key(resource_key).build()
            response = self._client.im.v1.file.get(request)
        else:
            raise RuntimeError("Feishu file download APIs are unavailable in current SDK/runtime")

        success_fn = getattr(response, "success", None)
        if callable(success_fn) and not success_fn():
            code = getattr(response, "code", "")
            message = getattr(response, "msg", "")
            log_id_fn = getattr(response, "get_log_id", None)
            log_id = log_id_fn() if callable(log_id_fn) else ""
            raise RuntimeError(f"Feishu resource download failed: code={code}, msg={message}, log_id={log_id}")

        file_obj = getattr(response, "file", None)
        if file_obj is None:
            raise RuntimeError("Feishu file download returned empty payload")
        if hasattr(file_obj, "read"):
            data = file_obj.read()
        else:
            data = file_obj
        if isinstance(data, str):
            payload = data.encode("utf-8")
        elif isinstance(data, bytes):
            payload = data
        else:
            raise RuntimeError(f"Unexpected resource payload type: {type(data)!r}")

        raw_headers = getattr(getattr(response, "raw", None), "headers", None)
        content_type = ""
        if raw_headers is not None:
            content_type = str(raw_headers.get("Content-Type", "")).strip()

        fallback_name = str(getattr(response, "file_name", "") or suggested_name).strip()
        suffix = _suffix_from_content_type(content_type, default_suffix)
        if fallback_name:
            safe_name = _safe_filename(fallback_name)
            if not Path(safe_name).suffix:
                safe_name = f"{safe_name}{suffix}"
        else:
            safe_name = _safe_filename(f"{resource_key}{suffix}")

        save_dir = _workspace_root() / "inbox" / self.name
        save_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(safe_name).stem or "attachment"
        suffix = Path(safe_name).suffix or default_suffix
        target = save_dir / safe_name
        if target.exists():
            token = message_id or resource_key
            target = save_dir / f"{stem}-{token[:8]}{suffix}"
        target.write_bytes(payload)
        return target.resolve()

    def _download_file_sync(self, file_key: str, file_name: str, message_id: str) -> Path:
        # For message attachments uploaded by users, message_resource is the
        # correct endpoint. The legacy file endpoint is only a fallback.
        return self._download_resource_sync(
            resource_key=file_key,
            message_id=message_id,
            resource_type="file",
            suggested_name=file_name or f"{file_key}.bin",
            default_suffix=".bin",
            allow_legacy_file_api=True,
        )

    def _download_image_sync(self, image_key: str, message_id: str) -> Path:
        return self._download_resource_sync(
            resource_key=image_key,
            message_id=message_id,
            resource_type="image",
            suggested_name=f"{image_key}.png",
            default_suffix=".png",
            allow_legacy_file_api=False,
        )

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

    @staticmethod
    def _extract_text_content(raw_content: str) -> str:
        """Extract plain text from Feishu text message payload."""
        try:
            return json.loads(raw_content).get("text", "")
        except json.JSONDecodeError:
            return raw_content

    @staticmethod
    def _parse_json_dict(raw_content: str) -> dict[str, Any]:
        """Parse message content JSON and return dict payload or empty dict."""
        try:
            parsed = json.loads(raw_content)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def _download_image(self, image_key: str, message_id: str) -> Path:
        """Run image download in executor and return local path."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._download_image_sync,
            image_key,
            message_id,
        )

    async def _download_file(self, file_key: str, file_name: str, message_id: str) -> Path:
        """Run file download in executor and return local path."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._download_file_sync,
            file_key,
            file_name,
            message_id,
        )

    async def _handle_post_message(
        self,
        *,
        raw_content: str,
        message_id: str,
        metadata: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """Handle Feishu `post` message payload and return normalized content/media."""
        post_payload = self._parse_json_dict(raw_content)

        text_content = _extract_post_text(post_payload) if post_payload else ""
        image_keys = _extract_post_image_keys(post_payload) if post_payload else []
        image_paths: list[str] = []
        image_errors: list[str] = []
        if image_keys:
            metadata["image_keys"] = image_keys
            for image_key in image_keys:
                try:
                    local_path = await self._download_image(image_key, message_id)
                    image_paths.append(str(local_path))
                except Exception as exc:
                    logger.exception(
                        "Failed downloading Feishu image in post (message_id=%s image_key=%s)",
                        message_id,
                        image_key,
                    )
                    image_errors.append(f"{image_key}: {exc}")
        if image_paths:
            metadata["image_paths"] = image_paths
        if image_errors:
            metadata["image_download_errors"] = image_errors

        parts: list[str] = []
        if text_content:
            parts.append(text_content)
        if image_paths:
            parts.append("Received images:\n" + "\n".join(image_paths))
        if image_errors:
            parts.append("Failed downloading images:\n" + "\n".join(image_errors))
        return "\n\n".join(parts).strip(), image_paths

    async def _handle_image_message(
        self,
        *,
        raw_content: str,
        message_id: str,
        metadata: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """Handle Feishu `image` message payload and return normalized content/media."""
        payload = self._parse_json_dict(raw_content)
        image_key = str(payload.get("image_key", "")).strip()
        metadata["image_key"] = image_key
        if not image_key:
            return "Received an image message without image_key.", []

        try:
            local_path = await self._download_image(image_key, message_id)
            metadata["local_path"] = str(local_path)
            return f"Received image: {local_path}", [str(local_path)]
        except Exception as exc:
            logger.exception(
                "Failed downloading Feishu image (message_id=%s image_key=%s)",
                message_id,
                image_key,
            )
            metadata["download_error"] = str(exc)
            return f"Received image but download failed: {image_key}", []

    async def _handle_file_message(
        self,
        *,
        raw_content: str,
        message_id: str,
        metadata: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """Handle Feishu `file` message payload and return normalized content/media."""
        payload = self._parse_json_dict(raw_content)
        file_key = str(payload.get("file_key", "")).strip()
        file_name = str(payload.get("file_name", "")).strip()
        metadata["file_key"] = file_key
        metadata["file_name"] = file_name
        if not file_key:
            return "Received a file message without file_key.", []

        try:
            local_path = await self._download_file(file_key, file_name, message_id)
            metadata["local_path"] = str(local_path)
            return f"Received file: {local_path}", [str(local_path)]
        except Exception as exc:
            logger.exception(
                "Failed downloading Feishu file (message_id=%s file_key=%s)",
                message_id,
                file_key,
            )
            metadata["download_error"] = str(exc)
            name_hint = file_name or file_key
            return f"Received file but download failed: {name_hint}", []

    async def _handle_supported_message(
        self,
        *,
        msg_type: str,
        raw_content: str,
        message_id: str,
        metadata: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """Handle one supported Feishu message type and return content/media."""
        if msg_type == "text":
            return self._extract_text_content(raw_content), []
        if msg_type == "post":
            return await self._handle_post_message(
                raw_content=raw_content,
                message_id=message_id,
                metadata=metadata,
            )
        if msg_type == "image":
            return await self._handle_image_message(
                raw_content=raw_content,
                message_id=message_id,
                metadata=metadata,
            )
        if msg_type == "file":
            return await self._handle_file_message(
                raw_content=raw_content,
                message_id=message_id,
                metadata=metadata,
            )
        return "", []

    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        try:
            event = data.event
            message = event.message
            sender = event.sender
            sender_type = getattr(sender, "sender_type", "")
            if sender_type == "bot":
                return

            sender_id = getattr(getattr(sender, "sender_id", None), "open_id", "") or "unknown"
            if not self.is_allowed(sender_id):
                return
            message_id = getattr(message, "message_id", "")
            if message_id:
                # Mirror openheron behavior: acknowledge user messages with a thumbs-up reaction.
                await self._add_reaction(message_id, "THUMBSUP")
            chat_id = getattr(message, "chat_id", "")
            chat_type = getattr(message, "chat_type", "")
            msg_type = getattr(message, "message_type", "")
            raw_content = getattr(message, "content", "") or ""
            metadata = {
                "msg_type": msg_type,
                "chat_type": chat_type,
                "message_id": message_id,
            }
            content, media_paths = await self._handle_supported_message(
                msg_type=msg_type,
                raw_content=raw_content,
                message_id=message_id,
                metadata=metadata,
            )

            if not content:
                return

            # Keep the same routing rule as openheron: groups reply to group chat_id,
            # p2p replies to sender open_id.
            target_chat_id = chat_id if chat_type == "group" else sender_id
            await self.publish_inbound(
                sender_id=sender_id,
                chat_id=target_chat_id,
                content=content,
                media=media_paths if media_paths else None,
                metadata=metadata,
            )
        except Exception:
            logger.exception("Failed handling Feishu inbound message")
