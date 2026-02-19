"""Email channel adapter (IMAP polling inbound + SMTP outbound)."""

from __future__ import annotations

import asyncio
import html
import imaplib
import logging
import re
import smtplib
import ssl
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any

from ..bus.events import OutboundMessage
from .base import BaseChannel
from .polling_utils import cancel_background_task, run_poll_loop

logger = logging.getLogger(__name__)


class EmailChannel(BaseChannel):
    """Minimal email adapter with polling inbound and SMTP outbound."""

    name = "email"

    def __init__(
        self,
        bus,
        *,
        consent_granted: bool = False,
        auto_reply_enabled: bool = True,
        imap_host: str = "",
        imap_port: int = 993,
        imap_username: str = "",
        imap_password: str = "",
        imap_mailbox: str = "INBOX",
        imap_use_ssl: bool = True,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_username: str = "",
        smtp_password: str = "",
        smtp_use_tls: bool = True,
        smtp_use_ssl: bool = False,
        from_address: str = "",
        poll_interval_seconds: int = 30,
        mark_seen: bool = True,
        max_body_chars: int = 12000,
        allow_from: list[str] | None = None,
    ) -> None:
        super().__init__(bus, allow_from=allow_from)
        self.consent_granted = bool(consent_granted)
        self.auto_reply_enabled = bool(auto_reply_enabled)

        self.imap_host = imap_host.strip()
        self.imap_port = int(imap_port)
        self.imap_username = imap_username.strip()
        self.imap_password = imap_password
        self.imap_mailbox = (imap_mailbox or "INBOX").strip() or "INBOX"
        self.imap_use_ssl = bool(imap_use_ssl)

        self.smtp_host = smtp_host.strip()
        self.smtp_port = int(smtp_port)
        self.smtp_username = smtp_username.strip()
        self.smtp_password = smtp_password
        self.smtp_use_tls = bool(smtp_use_tls)
        self.smtp_use_ssl = bool(smtp_use_ssl)
        self.from_address = from_address.strip()

        self.poll_interval_seconds = max(int(poll_interval_seconds), 5)
        self.mark_seen = bool(mark_seen)
        self.max_body_chars = max(int(max_body_chars), 256)

        self._poll_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start inbound polling loop when IMAP config is present."""
        if not self.consent_granted:
            logger.warning("Email channel disabled: consent_granted=false")
            return

        self._running = True
        if self._imap_ready():
            self._poll_task = asyncio.create_task(self._poll_loop(), name="email-poll")
        else:
            logger.info("Email inbound polling disabled: IMAP credentials are incomplete.")

    async def stop(self) -> None:
        self._running = False
        await cancel_background_task(self._poll_task)
        self._poll_task = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send outbound reply email via SMTP."""
        if not self.consent_granted:
            logger.warning("Skip email send: consent_granted=false")
            return

        force_send = bool((msg.metadata or {}).get("force_send")) if isinstance(msg.metadata, dict) else False
        if not self.auto_reply_enabled and not force_send:
            logger.info("Skip email send: auto_reply_enabled=false and force_send not set.")
            return
        if not self._smtp_ready():
            logger.warning("Skip email send: SMTP credentials are incomplete.")
            return

        to_addr = msg.chat_id.strip()
        if not to_addr:
            logger.warning("Skip email send: empty chat_id recipient.")
            return

        subject = "Re: sentientagent_v2"
        if isinstance(msg.metadata, dict) and isinstance(msg.metadata.get("subject"), str):
            override = msg.metadata.get("subject", "").strip()
            if override:
                subject = override

        email_msg = EmailMessage()
        email_msg["From"] = self.from_address or self.smtp_username or self.imap_username
        email_msg["To"] = to_addr
        email_msg["Subject"] = subject
        email_msg.set_content(msg.content or "")
        await asyncio.to_thread(self._smtp_send, email_msg)

    async def _poll_loop(self) -> None:
        await run_poll_loop(
            is_running=lambda: self._running,
            poll_once=self._poll_once,
            interval_seconds=self.poll_interval_seconds,
            logger=logger,
            error_message="Email polling iteration failed",
            retry_delay_seconds=0,
        )

    async def _poll_once(self) -> None:
        """Poll one batch of unread IMAP messages and publish to the bus."""
        if not self._imap_ready():
            return
        items = await asyncio.to_thread(self._fetch_unseen_sync)
        for item in items:
            sender = str(item.get("sender", "")).strip().lower()
            if not sender:
                continue
            await self.publish_inbound(
                sender_id=sender,
                chat_id=sender,
                content=str(item.get("content", "")).strip() or "(empty email body)",
                metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
            )

    def _imap_ready(self) -> bool:
        return bool(self.imap_host and self.imap_username and self.imap_password)

    def _smtp_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_username and self.smtp_password)

    def _smtp_send(self, msg: EmailMessage) -> None:
        timeout = 30
        if self.smtp_use_ssl:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=timeout) as smtp:
                smtp.login(self.smtp_username, self.smtp_password)
                smtp.send_message(msg)
            return

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=timeout) as smtp:
            if self.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.smtp_username, self.smtp_password)
            smtp.send_message(msg)

    def _fetch_unseen_sync(self) -> list[dict[str, Any]]:
        """Fetch unread messages from IMAP inbox."""
        messages: list[dict[str, Any]] = []
        if self.imap_use_ssl:
            client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        else:
            client = imaplib.IMAP4(self.imap_host, self.imap_port)

        try:
            client.login(self.imap_username, self.imap_password)
            status, _ = client.select(self.imap_mailbox)
            if status != "OK":
                return messages
            status, data = client.search(None, "UNSEEN")
            if status != "OK" or not data:
                return messages

            for imap_id in data[0].split():
                status, fetched = client.fetch(imap_id, "(BODY.PEEK[] UID)")
                if status != "OK" or not fetched:
                    continue
                raw_bytes = self._extract_message_bytes(fetched)
                if raw_bytes is None:
                    continue
                parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes)
                sender = parseaddr(parsed.get("From", ""))[1].strip().lower()
                if not sender:
                    continue
                subject = self._decode_header_value(parsed.get("Subject", ""))
                message_id = parsed.get("Message-ID", "").strip()
                body = self._extract_text_body(parsed)
                body = (body or "(empty email body)")[: self.max_body_chars]
                content = (
                    "Email received.\n"
                    f"From: {sender}\n"
                    f"Subject: {subject}\n\n"
                    f"{body}"
                )
                messages.append(
                    {
                        "sender": sender,
                        "content": content,
                        "metadata": {
                            "subject": subject,
                            "message_id": message_id,
                        },
                    }
                )
                if self.mark_seen:
                    client.store(imap_id, "+FLAGS", "\\Seen")
            return messages
        finally:
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass

    @staticmethod
    def _extract_message_bytes(fetched: Any) -> bytes | None:
        for part in fetched:
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                return bytes(part[1])
        return None

    @staticmethod
    def _decode_header_value(value: str) -> str:
        try:
            return str(make_header(decode_header(value or ""))).strip()
        except Exception:
            return (value or "").strip()

    @staticmethod
    def _extract_text_body(parsed: Any) -> str:
        plain_parts: list[str] = []
        html_parts: list[str] = []
        if parsed.is_multipart():
            for part in parsed.walk():
                if part.get_content_disposition() == "attachment":
                    continue
                ctype = part.get_content_type()
                payload = part.get_content()
                text = payload if isinstance(payload, str) else ""
                if not text.strip():
                    continue
                if ctype == "text/plain":
                    plain_parts.append(text.strip())
                elif ctype == "text/html":
                    html_parts.append(text.strip())
        else:
            payload = parsed.get_content()
            text = payload if isinstance(payload, str) else ""
            if parsed.get_content_type() == "text/html":
                html_parts.append(text.strip())
            else:
                plain_parts.append(text.strip())

        if plain_parts:
            return "\n\n".join(p for p in plain_parts if p)
        if html_parts:
            merged = "\n\n".join(p for p in html_parts if p)
            no_tags = re.sub(r"<[^>]+>", "", merged)
            return html.unescape(no_tags).strip()
        return ""
