"""Feishu channel adapter (inbound WebSocket + outbound message API)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..runtime.step_events import classify_outbound_message
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
        PatchMessageRequest,
        PatchMessageRequestBody,
        GetMessageRequest,
        GetFileRequest,
        GetMessageResourceRequest,
        P2ImMessageReceiveV1,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
        UpdateMessageRequest,
        UpdateMessageRequestBody,
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
    PatchMessageRequest = None
    PatchMessageRequestBody = None
    GetMessageRequest = None
    GetFileRequest = None
    GetMessageResourceRequest = None
    P2ImMessageReceiveV1 = None
    ReplyMessageRequest = None
    ReplyMessageRequestBody = None
    UpdateMessageRequest = None
    UpdateMessageRequestBody = None
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
    workspace = os.getenv("OPENPPX_WORKSPACE", "").strip()
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


def _strip_markdown_formatting(text: str) -> str:
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    value = re.sub(r"__(.+?)__", r"\1", value)
    value = re.sub(r"~~(.+?)~~", r"\1", value)
    value = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", value)
    return value


def _parse_md_table(table_text: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in table_text.strip().splitlines() if line.strip()]
    if len(lines) < 3:
        return None

    def split(line: str) -> list[str]:
        return [_strip_markdown_formatting(cell.strip()) for cell in line.strip("|").split("|")]

    headers = split(lines[0])
    rows = [split(line) for line in lines[2:]]
    columns = [
        {"tag": "column", "name": f"c{index}", "display_name": header, "width": "auto"}
        for index, header in enumerate(headers)
    ]
    table_rows = [{f"c{i}": row[i] if i < len(row) else "" for i in range(len(headers))} for row in rows]
    return {
        "tag": "table",
        "page_size": len(table_rows) + 1,
        "columns": columns,
        "rows": table_rows,
    }


_TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
    re.MULTILINE,
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_COMPLEX_MD_RE = re.compile(r"```|^\|.+\|.*\n\s*\|[-:\s|]+\||^#{1,6}\s+|^[\s]*[-*+]\s+|^[\s]*\d+\.\s+", re.MULTILINE)
_SIMPLE_MD_RE = re.compile(r"\*\*.+?\*\*|__.+?__|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|~~.+?~~", re.DOTALL)
_MESSAGE_DEDUP_MAX_ENTRIES = 1000
_REPLY_CONTEXT_MAX_LEN = 200


def _split_headings(content: str) -> list[dict[str, Any]]:
    protected = content
    code_blocks: list[str] = []
    for match in _CODE_BLOCK_RE.finditer(content):
        code_blocks.append(match.group(1))
        protected = protected.replace(match.group(1), f"\x00CODE{len(code_blocks)-1}\x00", 1)

    elements: list[dict[str, Any]] = []
    last_end = 0
    for match in _HEADING_RE.finditer(protected):
        before = protected[last_end:match.start()].strip()
        if before:
            elements.append({"tag": "markdown", "content": before})
        text = _strip_markdown_formatting(match.group(2).strip())
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{text}**"}})
        last_end = match.end()
    remaining = protected[last_end:].strip()
    if remaining:
        elements.append({"tag": "markdown", "content": remaining})

    for index, code_block in enumerate(code_blocks):
        marker = f"\x00CODE{index}\x00"
        for element in elements:
            if element.get("tag") == "markdown":
                element["content"] = str(element.get("content", "")).replace(marker, code_block)
    return elements or [{"tag": "markdown", "content": content}]


def _build_card_elements(content: str) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    last_end = 0
    for match in _TABLE_RE.finditer(content):
        before = content[last_end:match.start()]
        if before.strip():
            elements.extend(_split_headings(before))
        elements.append(_parse_md_table(match.group(1)) or {"tag": "markdown", "content": match.group(1)})
        last_end = match.end()
    remaining = content[last_end:]
    if remaining.strip():
        elements.extend(_split_headings(remaining))
    return elements or [{"tag": "markdown", "content": content}]


def _split_elements_by_table_limit(elements: list[dict[str, Any]], max_tables: int = 1) -> list[list[dict[str, Any]]]:
    """Split card elements into groups with at most ``max_tables`` tables."""

    if not elements:
        return [[]]
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    table_count = 0
    for element in elements:
        if element.get("tag") == "table":
            if table_count >= max_tables:
                if current:
                    groups.append(current)
                current = []
                table_count = 0
            current.append(element)
            table_count += 1
        else:
            current.append(element)
    if current:
        groups.append(current)
    return groups or [[]]


def _detect_msg_format(content: str) -> str:
    stripped = content.strip()
    if not stripped:
        return "text"
    if _COMPLEX_MD_RE.search(stripped) or _SIMPLE_MD_RE.search(stripped):
        return "interactive"
    if len(stripped) > 2000:
        return "interactive"
    if _MD_LINK_RE.search(stripped):
        return "post"
    if len(stripped) <= 200:
        return "text"
    return "post"


def _markdown_to_post(content: str) -> str:
    paragraphs: list[list[dict[str, Any]]] = []
    for line in content.strip().splitlines() or [""]:
        elements: list[dict[str, Any]] = []
        last_end = 0
        for match in _MD_LINK_RE.finditer(line):
            before = line[last_end:match.start()]
            if before:
                elements.append({"tag": "text", "text": before})
            elements.append({"tag": "a", "text": match.group(1), "href": match.group(2)})
            last_end = match.end()
        remaining = line[last_end:]
        if remaining:
            elements.append({"tag": "text", "text": remaining})
        if not elements:
            elements.append({"tag": "text", "text": ""})
        paragraphs.append(elements)
    return json.dumps({"zh_cn": {"content": paragraphs}}, ensure_ascii=False)


def _render_step_markdown(content: str, metadata: dict[str, Any]) -> str:
    phase = str(metadata.get("_step_phase", "")).strip() or "update"
    title = str(metadata.get("_step_title", "")).strip() or str(metadata.get("_tool_name", "")).strip() or "Step"
    body = (content or "").strip()
    markers = {
        "started": "[started]",
        "running": "[running]",
        "waiting": "[waiting]",
        "finished": "[finished]",
        "failed": "[failed]",
        "cancelled": "[cancelled]",
        "queued": "[queued]",
    }
    prefix = markers.get(phase, "[update]")
    lines = [f"{prefix} **{title}**", f"Status: `{phase}`"]
    if body:
        lines.append(body)
    return "\n".join(lines)


def _build_step_card(content: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Build one interactive Feishu card for a structured step event."""

    step_title = str(metadata.get("_step_title", "")).strip() or str(metadata.get("_tool_name", "")).strip() or "Step"
    step_phase = str(metadata.get("_step_phase", "")).strip() or "update"
    step_kind = str(metadata.get("_step_kind", "")).strip() or "system"
    update_kind = str(metadata.get("_step_update_kind", "")).strip() or "status"
    task_id = str(metadata.get("_task_id", "")).strip()
    step_id = str(metadata.get("_step_id", "")).strip()
    event_seq = str(metadata.get("_event_seq", "")).strip()
    step_order = str(metadata.get("_step_order", "")).strip()
    event_class = str(metadata.get("_event_class", "")).strip() or "step_update"

    template = {
        "finished": "green",
        "failed": "red",
        "cancelled": "red",
        "running": "blue",
        "started": "blue",
        "waiting": "orange",
        "queued": "wathet",
    }.get(step_phase, "grey")

    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Status**\n`{step_phase}`"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Kind**\n`{step_kind}`"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Update**\n`{update_kind}`"}},
    ]
    if step_order:
        fields.append({"is_short": True, "text": {"tag": "lark_md", "content": f"**Order**\n`{step_order}`"}})
    if event_seq:
        fields.append({"is_short": True, "text": {"tag": "lark_md", "content": f"**Event**\n`{event_seq}`"}})
    if task_id:
        fields.append({"is_short": False, "text": {"tag": "lark_md", "content": f"**Task**\n`{task_id}`"}})
    elif step_id:
        fields.append({"is_short": False, "text": {"tag": "lark_md", "content": f"**Step**\n`{step_id}`"}})

    body = (content or "").strip()
    body_tag = "markdown" if event_class == "step_output" or _detect_msg_format(body) != "text" else "div"
    body_element: dict[str, Any]
    if body_tag == "markdown":
        body_element = {"tag": "markdown", "content": body or "_No details_"}
    else:
        body_element = {"tag": "div", "text": {"tag": "plain_text", "content": body or "No details"}}

    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": step_title},
        },
        "elements": [
            {"tag": "div", "fields": fields},
            body_element,
        ],
    }


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
        streaming_enabled: bool = False,
        group_policy: str = "mention",
        reply_to_message: bool = False,
        react_emoji: str = "THUMBSUP",
    ) -> None:
        super().__init__(bus, allow_from=allow_from)
        self.app_id = app_id
        self.app_secret = app_secret
        self.encrypt_key = encrypt_key
        self.verification_token = verification_token
        self._streaming_enabled = bool(streaming_enabled)
        self.group_policy = self._normalize_group_policy(group_policy)
        self.reply_to_message = bool(reply_to_message)
        self.react_emoji = (react_emoji or "").strip()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._stream_states: dict[str, dict[str, Any]] = {}
        self._step_states: dict[tuple[str, str], dict[str, Any]] = {}
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._bot_open_id: str | None = None

    @staticmethod
    def _normalize_group_policy(value: str) -> str:
        """Return the supported Feishu group trigger policy."""

        normalized = (value or "").strip().lower()
        if normalized in {"open", "mention"}:
            return normalized
        return "mention"

    @staticmethod
    def _stream_update_interval_seconds() -> float:
        raw = os.getenv("OPENPPX_FEISHU_STREAM_UPDATE_INTERVAL_MS", "200").strip()
        try:
            interval_ms = int(raw)
        except ValueError:
            interval_ms = 200
        return max(0.0, interval_ms / 1000.0)

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
        self._bot_open_id = await self._fetch_bot_open_id()

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

    def _send_content_sync(
        self,
        msg,
        *,
        msg_type: str,
        content: str,
        request_type: str,
        use_reply: bool = False,
    ) -> str:
        if not self._client:
            return ""
        if use_reply:
            reply_message_id = self._resolve_reply_message_id(
                msg.metadata if isinstance(getattr(msg, "metadata", None), dict) else {}
            )
            if reply_message_id:
                try:
                    return self._reply_message_sync(reply_message_id, msg_type, content)
                except Exception as exc:
                    logger.warning("Feishu reply %s failed; falling back to create: %s", request_type, exc)
        receive_id_type = self._resolve_receive_id_type(msg.chat_id)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(msg.chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        return self._send_message_request_sync(request, request_type=request_type)

    def _send_text_sync(self, msg, text: str | None = None, *, use_reply: bool = False) -> str | None:
        payload = json.dumps({"text": text if text is not None else msg.content}, ensure_ascii=False)
        return self._send_content_sync(
            msg,
            msg_type="text",
            content=payload,
            request_type="text",
            use_reply=use_reply,
        )

    def _send_post_sync(self, msg, content: str, *, use_reply: bool = False) -> str:
        return self._send_content_sync(
            msg,
            msg_type="post",
            content=_markdown_to_post(content),
            request_type="post",
            use_reply=use_reply,
        )

    def _send_interactive_sync(self, msg, content: str, *, use_reply: bool = False) -> str:
        if not self._client:
            return ""
        element_groups = _split_elements_by_table_limit(_build_card_elements(content), max_tables=1)
        message_ids: list[str] = []
        for elements in element_groups:
            card = {
                "config": {"wide_screen_mode": True, "enable_forward": True},
                "elements": elements,
            }
            message_ids.append(
                self._send_content_sync(
                    msg,
                    msg_type="interactive",
                    content=json.dumps(card, ensure_ascii=False),
                    request_type="interactive",
                    use_reply=use_reply and not message_ids,
                )
            )
        return next((message_id for message_id in message_ids if message_id), "")

    def _send_step_cards_sync(self, msg, content: str, metadata: dict[str, Any]) -> str:
        """Send a structured step event as an interactive card."""

        if not self._client:
            return ""
        step_id = str(metadata.get("_step_id", "")).strip()
        state_key = (str(msg.chat_id), step_id) if step_id else None
        card_payload = json.dumps(_build_step_card(content, metadata), ensure_ascii=False)
        if state_key is not None:
            existing = self._step_states.get(state_key)
            if existing and str(existing.get("message_id", "")).strip():
                self._patch_message_sync(
                    str(existing["message_id"]),
                    msg_type="interactive",
                    content=card_payload,
                )
                if metadata.get("_done"):
                    self._step_states.pop(state_key, None)
                return str(existing["message_id"])
        receive_id_type = self._resolve_receive_id_type(msg.chat_id)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(msg.chat_id)
                .msg_type("interactive")
                .content(card_payload)
                .build()
            )
            .build()
        )
        message_id = self._send_message_request_sync(request, request_type="interactive")
        if state_key is not None and message_id and not metadata.get("_done"):
            self._step_states[state_key] = {"message_id": message_id}
        return message_id

    def _send_rich_text_sync(
        self,
        msg,
        content: str,
        *,
        preferred_format: str | None = None,
        use_reply: bool = False,
    ) -> str:
        format_name = preferred_format or _detect_msg_format(content)
        if format_name == "interactive":
            return self._send_interactive_sync(msg, content, use_reply=use_reply)
        if format_name == "post":
            return self._send_post_sync(msg, content, use_reply=use_reply)
        return str(self._send_text_sync(msg, content, use_reply=use_reply) or "")

    def _resolve_reply_message_id(self, metadata: dict[str, Any] | None = None) -> str:
        """Return the inbound Feishu message id that should receive a contextual reply."""

        info = metadata or {}
        event_class = str(info.get("_event_class", "") or "").strip()
        if event_class in {"step_update", "step_output", "stream_delta", "stream_end"}:
            return ""
        thread_id = str(info.get("thread_id", "") or "").strip()
        if thread_id:
            return str(info.get("root_id", "") or info.get("message_id", "") or "").strip()
        if not self.reply_to_message:
            return ""
        return str(info.get("message_id", "") or "").strip()

    def _reply_message_sync(self, parent_message_id: str, msg_type: str, content: str) -> str:
        """Reply to an existing Feishu message and return the created message id."""

        if not self._client or ReplyMessageRequest is None or ReplyMessageRequestBody is None:
            raise RuntimeError("Feishu reply API is unavailable in current SDK/runtime")
        request = (
            ReplyMessageRequest.builder()
            .message_id(parent_message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.reply(request)
        success_fn = getattr(response, "success", None)
        if callable(success_fn) and not success_fn():
            code = getattr(response, "code", "")
            message = getattr(response, "msg", "")
            log_id_fn = getattr(response, "get_log_id", None)
            log_id = log_id_fn() if callable(log_id_fn) else ""
            raise RuntimeError(f"Feishu {msg_type} reply failed: code={code}, msg={message}, log_id={log_id}")
        return str(getattr(getattr(response, "data", None), "message_id", "") or "")

    def _patch_message_sync(self, message_id: str, *, msg_type: str, content: str) -> None:
        """Patch one existing Feishu message with refreshed content."""
        if not self._client:
            return
        if PatchMessageRequest is not None and PatchMessageRequestBody is not None:
            request = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.patch(request)
        elif UpdateMessageRequest is not None and UpdateMessageRequestBody is not None:
            request = (
                UpdateMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    UpdateMessageRequestBody.builder()
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.update(request)
        else:
            raise RuntimeError("Feishu message patch/update API is unavailable in current SDK/runtime")

        success_fn = getattr(response, "success", None)
        if callable(success_fn) and not success_fn():
            code = getattr(response, "code", "")
            message = getattr(response, "msg", "")
            log_id_fn = getattr(response, "get_log_id", None)
            log_id = log_id_fn() if callable(log_id_fn) else ""
            raise RuntimeError(f"Feishu patch message failed: code={code}, msg={message}, log_id={log_id}")

    def _patch_text_sync(self, message_id: str, text: str) -> None:
        payload = json.dumps({"text": text}, ensure_ascii=False)
        self._patch_message_sync(message_id, msg_type="text", content=payload)

    def _send_message_request_sync(self, request, *, request_type: str) -> str:
        if not self._client:
            return ""
        response = self._client.im.v1.message.create(request)
        success_fn = getattr(response, "success", None)
        if callable(success_fn) and not success_fn():
            code = getattr(response, "code", "")
            message = getattr(response, "msg", "")
            log_id_fn = getattr(response, "get_log_id", None)
            log_id = log_id_fn() if callable(log_id_fn) else ""
            raise RuntimeError(
                f"Feishu {request_type} message send failed: code={code}, msg={message}, log_id={log_id}"
            )
        return str(getattr(getattr(response, "data", None), "message_id", "") or "")

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

    def _send_image_sync(self, msg, image_path: str) -> str:
        if not self._client:
            return ""
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
        return self._send_message_request_sync(request, request_type="image")

    def _send_file_sync(self, msg, file_path: str) -> str:
        if not self._client:
            return ""
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
        return self._send_message_request_sync(request, request_type="file")

    def _send_sync(self, msg) -> None:
        if not self._client:
            return
        normalized = classify_outbound_message(
            getattr(msg, "content", "") or "",
            msg.metadata if isinstance(getattr(msg, "metadata", None), dict) else {},
        )
        metadata = normalized.metadata
        msg.metadata = metadata
        content_type = str(metadata.get("content_type", "")).strip().lower()
        image_path = str(metadata.get("image_path", "")).strip() if content_type == "image" else ""
        file_path = str(metadata.get("file_path", "")).strip() if content_type == "file" else ""
        if image_path:
            try:
                image_message_id = self._send_image_sync(msg, image_path)
                message_ids = [image_message_id] if image_message_id else []
                caption = (msg.content or "").strip()
                if caption:
                    caption_id = self._send_text_sync(msg, caption)
                    if caption_id:
                        message_ids.append(caption_id)
                metadata["delivery"] = {
                    "status": "sent",
                    "content_type": "image",
                    "message_ids": message_ids,
                }
            except Exception:
                logger.exception("Failed sending Feishu image message; falling back to text")
                fallback = (msg.content or "").strip() or f"[image send failed] {image_path}"
                fallback_id = self._send_text_sync(msg, fallback)
                metadata["delivery"] = {
                    "status": "fallback_text",
                    "content_type": "image",
                    "message_ids": [fallback_id] if fallback_id else [],
                }
            return
        if file_path:
            try:
                file_message_id = self._send_file_sync(msg, file_path)
                message_ids = [file_message_id] if file_message_id else []
                caption = (msg.content or "").strip()
                if caption:
                    caption_id = self._send_text_sync(msg, caption)
                    if caption_id:
                        message_ids.append(caption_id)
                metadata["delivery"] = {
                    "status": "sent",
                    "content_type": "file",
                    "message_ids": message_ids,
                }
            except Exception:
                logger.exception("Failed sending Feishu file message; falling back to text")
                fallback = (msg.content or "").strip() or f"[file send failed] {file_path}"
                fallback_id = self._send_text_sync(msg, fallback)
                metadata["delivery"] = {
                    "status": "fallback_text",
                    "content_type": "file",
                    "message_ids": [fallback_id] if fallback_id else [],
                }
            return
        content = msg.content or ""
        if normalized.event_class in {"step_update", "step_output"}:
            content = _render_step_markdown(content, metadata)
            text_id = self._send_step_cards_sync(msg, content, metadata)
        else:
            detected_format = _detect_msg_format(content)
            if detected_format == "text":
                text_id = str(self._send_text_sync(msg, use_reply=True) or "")
            else:
                text_id = self._send_rich_text_sync(
                    msg,
                    content,
                    preferred_format=detected_format,
                    use_reply=True,
                )
        metadata["delivery"] = {
            "status": "sent",
            "content_type": "text",
            "message_ids": [text_id] if text_id else [],
        }

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

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """Stream text into one Feishu message by patching the latest message."""
        meta = metadata or {}
        state = self._stream_states.get(chat_id)
        now = time.monotonic()

        if meta.get("_stream_end"):
            if state is not None:
                await self._flush_stream_state(chat_id, force=True)
                self._stream_states.pop(chat_id, None)
            return

        if not delta:
            return

        if state is None:
            message_id = await self._send_stream_initial(chat_id, delta)
            self._stream_states[chat_id] = {
                "buffer": delta,
                "sent_text": delta,
                "message_id": message_id,
                "last_flush_at": now,
            }
            return

        state["buffer"] = f"{state.get('buffer', '')}{delta}"
        interval = self._stream_update_interval_seconds()
        last_flush_at = float(state.get("last_flush_at", 0.0) or 0.0)
        if interval <= 0 or now - last_flush_at >= interval:
            await self._flush_stream_state(chat_id, force=True)

    async def _send_stream_initial(self, chat_id: str, text: str) -> str:
        """Send the first streaming frame as a normal text message."""
        msg = type("_FeishuStreamMsg", (), {"chat_id": chat_id, "content": text, "metadata": {}})()
        loop = asyncio.get_running_loop()
        message_id = await loop.run_in_executor(None, self._send_text_sync, msg)
        return str(message_id or "")

    async def _flush_stream_state(self, chat_id: str, *, force: bool = False) -> None:
        """Patch the active Feishu streaming message to the latest buffered text."""
        state = self._stream_states.get(chat_id)
        if state is None:
            return
        buffer = str(state.get("buffer", "") or "")
        sent_text = str(state.get("sent_text", "") or "")
        if not buffer or (not force and buffer == sent_text):
            return
        message_id = str(state.get("message_id", "") or "")
        if not message_id:
            return

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._patch_text_sync, message_id, buffer)
        except Exception:
            logger.exception("Failed updating Feishu streaming message; keeping previous text")
            return

        state["sent_text"] = buffer
        state["last_flush_at"] = time.monotonic()

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> str | None:
        """Best-effort reaction API call executed in thread pool."""
        if (
            not self._client
            or not FEISHU_REACTION_AVAILABLE
            or CreateMessageReactionRequest is None
            or CreateMessageReactionRequestBody is None
            or Emoji is None
        ):
            return None
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
            response = self._client.im.v1.message_reaction.create(request)
            success_fn = getattr(response, "success", None)
            if callable(success_fn) and not success_fn():
                return None
            return str(getattr(getattr(response, "data", None), "reaction_id", "") or "") or None
        except Exception:
            logger.exception("Failed adding Feishu reaction")
            return None

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> str | None:
        if not message_id:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

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

    async def _fetch_bot_open_id(self) -> str | None:
        """Fetch the Feishu bot open id for accurate group mention matching."""

        if not self._client:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_bot_open_id_sync)

    def _fetch_bot_open_id_sync(self) -> str | None:
        """Fetch the bot open id through the Feishu bot info API when available."""

        if not self._client or lark is None:
            return None
        base_request = getattr(lark, "BaseRequest", None)
        http_method = getattr(lark, "HttpMethod", None)
        access_token_type = getattr(lark, "AccessTokenType", None)
        if base_request is None or http_method is None or access_token_type is None:
            return None
        try:
            request = (
                base_request.builder()
                .http_method(http_method.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({access_token_type.APP})
                .build()
            )
            response = self._client.request(request)
            success_fn = getattr(response, "success", None)
            if callable(success_fn) and not success_fn():
                return None
            raw_content = getattr(getattr(response, "raw", None), "content", b"")
            if isinstance(raw_content, bytes | bytearray):
                payload = json.loads(bytes(raw_content).decode("utf-8"))
            else:
                payload = json.loads(str(raw_content or "{}"))
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            bot = data.get("bot", data) if isinstance(data, dict) else {}
            open_id = str(bot.get("open_id", "")).strip() if isinstance(bot, dict) else ""
            return open_id or None
        except Exception as exc:
            logger.warning("Failed fetching Feishu bot open_id: %s", exc)
            return None

    def _mark_message_seen(self, message_id: str) -> bool:
        """Remember one inbound Feishu message id and report whether it was already seen."""

        normalized = str(message_id or "").strip()
        if not normalized:
            return False
        if normalized in self._processed_message_ids:
            self._processed_message_ids.move_to_end(normalized)
            return True
        self._processed_message_ids[normalized] = None
        while len(self._processed_message_ids) > _MESSAGE_DEDUP_MAX_ENTRIES:
            self._processed_message_ids.popitem(last=False)
        return False

    def _is_group_message_for_bot(self, message: Any) -> bool:
        """Return whether a group message should trigger the bot."""

        if self.group_policy == "open":
            return True
        return self._is_bot_mentioned(message)

    def _is_bot_mentioned(self, message: Any) -> bool:
        """Return whether the current Feishu message mentions this bot."""

        raw_content = str(getattr(message, "content", "") or "")
        if "@_all" in raw_content:
            return True
        return any(self._is_bot_mention(mention) for mention in getattr(message, "mentions", None) or [])

    def _is_bot_mention(self, mention: Any) -> bool:
        """Return whether one Feishu mention object points at this bot."""

        mention_id = getattr(mention, "id", None)
        if mention_id is None:
            return False
        mention_open_id = str(getattr(mention_id, "open_id", "") or "").strip()
        if self._bot_open_id:
            return mention_open_id == self._bot_open_id
        mention_user_id = str(getattr(mention_id, "user_id", "") or "").strip()
        return bool(mention_open_id.startswith("ou_") and not mention_user_id)

    def _normalize_mention_text(self, text: str, mentions: list[Any] | None) -> str:
        """Replace Feishu mention placeholders and remove this bot's own mention."""

        normalized = str(text or "")
        if not normalized or not mentions:
            return normalized.strip()
        for mention in mentions:
            key = str(getattr(mention, "key", "") or "").strip()
            if not key:
                continue
            if self._is_bot_mention(mention):
                normalized = normalized.replace(key, "")
                continue
            display_name = str(getattr(mention, "name", "") or "").strip() or "user"
            mention_id = getattr(mention, "id", None)
            open_id = str(getattr(mention_id, "open_id", "") or "").strip() if mention_id else ""
            if open_id:
                normalized = normalized.replace(key, f"@{display_name} ({open_id})")
            else:
                normalized = normalized.replace(key, f"@{display_name}")
        normalized = re.sub(r"[ \t]{2,}", " ", normalized)
        return "\n".join(line.strip() for line in normalized.splitlines()).strip()

    async def _get_reply_context(self, parent_message_id: str) -> str:
        """Return a short textual description of the replied-to Feishu message."""

        if not parent_message_id or not self._client:
            return ""
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._get_reply_context_sync, parent_message_id)
        return result or ""

    def _get_reply_context_sync(self, parent_message_id: str) -> str:
        """Fetch a concise reply context string from one Feishu parent message."""

        if not self._client or GetMessageRequest is None:
            return ""
        try:
            request = GetMessageRequest.builder().message_id(parent_message_id).build()
            response = self._client.im.v1.message.get(request)
            success_fn = getattr(response, "success", None)
            if callable(success_fn) and not success_fn():
                return ""
            items = getattr(getattr(response, "data", None), "items", None) or []
            if not items:
                return ""
            parent_message = items[0]
            msg_type = str(getattr(parent_message, "msg_type", "") or "")
            body = getattr(parent_message, "body", None)
            raw_content = str(getattr(body, "content", "") or "")
            if msg_type == "text":
                text = self._extract_text_content(raw_content)
            elif msg_type == "post":
                text = _extract_post_text(self._parse_json_dict(raw_content))
            else:
                text = ""
            text = " ".join(str(text or "").split()).strip()
            if not text:
                return ""
            if len(text) > _REPLY_CONTEXT_MAX_LEN:
                text = f"{text[: _REPLY_CONTEXT_MAX_LEN - 3].rstrip()}..."
            return f"[Reply to: {text}]"
        except Exception as exc:
            logger.debug("Failed fetching Feishu reply context: parent_message_id=%s error=%s", parent_message_id, exc)
            return ""

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

    def _download_audio_sync(self, file_key: str, file_name: str, message_id: str) -> Path:
        """Download one Feishu audio resource as a local file."""
        return self._download_resource_sync(
            resource_key=file_key,
            message_id=message_id,
            resource_type="file",
            suggested_name=file_name or f"{file_key}.opus",
            default_suffix=".opus",
            allow_legacy_file_api=True,
        )

    async def _download_audio(self, file_key: str, file_name: str, message_id: str) -> Path:
        """Run audio download in executor and return local path."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._download_audio_sync,
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

    async def _handle_audio_message(
        self,
        *,
        raw_content: str,
        message_id: str,
        metadata: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """Handle Feishu `audio`/`voice` payload and return normalized content/media."""
        payload = self._parse_json_dict(raw_content)
        file_key = str(payload.get("file_key", "") or payload.get("audio_key", "")).strip()
        file_name = str(payload.get("file_name", "") or payload.get("name", "")).strip()
        metadata["file_key"] = file_key
        metadata["file_name"] = file_name
        metadata["audio"] = True
        if not file_key:
            return "Received an audio message without file_key.", []

        try:
            local_path = await self._download_audio(file_key, file_name, message_id)
            metadata["local_path"] = str(local_path)
            return f"Received audio: {local_path}", [str(local_path)]
        except Exception as exc:
            logger.exception(
                "Failed downloading Feishu audio (message_id=%s file_key=%s)",
                message_id,
                file_key,
            )
            metadata["download_error"] = str(exc)
            name_hint = file_name or file_key
            return f"Received audio but download failed: {name_hint}", []

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
        if msg_type in {"audio", "voice"}:
            return await self._handle_audio_message(
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
            if message_id and self._mark_message_seen(message_id):
                logger.debug("Feishu inbound duplicate ignored: message_id=%s", message_id)
                return
            chat_id = getattr(message, "chat_id", "")
            chat_type = getattr(message, "chat_type", "")
            msg_type = getattr(message, "message_type", "")
            raw_content = getattr(message, "content", "") or ""
            if chat_type == "group" and not self._is_group_message_for_bot(message):
                logger.debug("Feishu inbound group message ignored because bot was not mentioned: message_id=%s", message_id)
                return
            reaction_id = None
            if message_id and self.react_emoji:
                reaction_id = await self._add_reaction(message_id, self.react_emoji)
            parent_id = str(getattr(message, "parent_id", "") or "")
            root_id = str(getattr(message, "root_id", "") or "")
            thread_id = str(getattr(message, "thread_id", "") or "")
            metadata = {
                "msg_type": msg_type,
                "chat_type": chat_type,
                "message_id": message_id,
                "parent_id": parent_id,
                "root_id": root_id,
                "thread_id": thread_id,
                "_wants_stream": self._streaming_enabled,
            }
            if reaction_id:
                metadata["reaction_id"] = reaction_id
            if self.react_emoji:
                metadata["reaction_emoji"] = self.react_emoji
            content, media_paths = await self._handle_supported_message(
                msg_type=msg_type,
                raw_content=raw_content,
                message_id=message_id,
                metadata=metadata,
            )
            content = self._normalize_mention_text(content, getattr(message, "mentions", None))
            if parent_id:
                reply_context = await self._get_reply_context(parent_id)
                if reply_context:
                    content = f"{reply_context}\n{content}".strip()

            if not content:
                return

            # Keep the same routing rule as openppx: groups reply to group chat_id,
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
