"""Local stdio channel for minimal gateway testing."""

from __future__ import annotations

import json
import os
from typing import Callable

from ..bus.events import OutboundMessage
from ..bus.queue import MessageBus
from .base import BaseChannel


class LocalChannel(BaseChannel):
    """A local channel that prints outbound messages to stdout."""

    name = "local"

    def __init__(self, bus: MessageBus, writer: Callable[[str], None] | None = None):
        super().__init__(bus)
        self._writer = writer or print
        self._stream_buffers: dict[str, str] = {}

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        if _local_json_output_enabled():
            self._writer(_json_payload(msg))
            return
        self._writer(_render_local_message(msg))

    async def send_delta(self, chat_id: str, delta: str, metadata: dict | None = None) -> None:
        """Render one streaming delta for local interactive mode."""
        if _local_json_output_enabled():
            payload = {
                "channel": self.name,
                "chat_id": chat_id,
                "content": delta,
                "reply_to": None,
                "metadata": metadata or {"_stream_delta": True},
            }
            self._writer(json.dumps(payload, ensure_ascii=False))
            return
        meta = metadata or {}
        if meta.get("_stream_end"):
            self._stream_buffers.pop(chat_id, None)
            return
        if not delta:
            return
        current = self._stream_buffers.get(chat_id, "")
        current += delta
        self._stream_buffers[chat_id] = current
        self._writer(f"[stream] {current}")

    async def ingest_text(
        self,
        text: str,
        *,
        chat_id: str = "terminal",
        sender_id: str = "local-user",
    ) -> None:
        await self.publish_inbound(
            sender_id=sender_id,
            chat_id=chat_id,
            content=text,
            metadata={"_wants_stream": True},
        )


def _local_json_output_enabled() -> bool:
    raw = os.getenv("OPENPIPIXIA_LOCAL_JSON_OUTPUT", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _json_payload(msg: OutboundMessage) -> str:
    payload = {
        "channel": msg.channel,
        "chat_id": msg.chat_id,
        "content": msg.content,
        "reply_to": msg.reply_to,
        "metadata": msg.metadata,
    }
    return json.dumps(payload, ensure_ascii=False)


def _render_local_message(msg: OutboundMessage) -> str:
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    feedback_type = str(metadata.get("_feedback_type", "")).strip().lower()
    if feedback_type:
        return _render_feedback_message(msg.content, metadata)

    content_type = str(metadata.get("content_type", "")).strip().lower()
    if content_type == "image":
        image_path = str(metadata.get("image_path", "")).strip()
        caption = (msg.content or "").strip()
        return f"[image] {image_path}" if not caption else f"[image] {image_path}\n{caption}"
    if content_type == "file":
        file_name = str(metadata.get("file_name", "")).strip() or str(metadata.get("file_path", "")).strip()
        caption = (msg.content or "").strip()
        return f"[file] {file_name}" if not caption else f"[file] {file_name}\n{caption}"

    return msg.content or "[empty message]"


def _render_feedback_message(content: str, metadata: dict[str, object]) -> str:
    feedback_type = str(metadata.get("_feedback_type", "")).strip().lower()
    status = str(metadata.get("_feedback_status", "")).strip().lower()
    tool_name = str(metadata.get("_tool_name", "")).strip()
    step_title = str(metadata.get("_step_title", "")).strip()
    task_id = str(metadata.get("_task_id", "")).strip()
    session_id = str(metadata.get("_session_id", "")).strip()
    pieces = [piece for piece in [step_title, tool_name] if piece]
    label = " - ".join(pieces) if pieces else "Feedback"

    suffix_parts = []
    if task_id:
        suffix_parts.append(f"task={task_id}")
    if session_id:
        suffix_parts.append(f"session={session_id}")
    suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""

    if feedback_type == "tool_output":
        body = (content or "").rstrip() or "(no output)"
        indented = "\n".join(f"    {line}" if line else "    " for line in body.splitlines())
        header = f"[output] {label}{suffix}"
        return f"{header}\n{indented}"

    if feedback_type == "status":
        prefix = f"[status:{status or 'info'}]"
        summary = (content or "").strip()
        return f"{prefix} {label}{suffix}\n{summary}" if summary else f"{prefix} {label}{suffix}"

    if feedback_type == "tool":
        prefix = "[step]"
        summary = (content or "").strip()
        return f"{prefix} {label}{suffix}\n{summary}" if summary else f"{prefix} {label}{suffix}"

    prefix = f"[{feedback_type or 'feedback'}]"
    summary = (content or "").strip()
    return f"{prefix} {label}{suffix}\n{summary}" if summary else f"{prefix} {label}{suffix}"
