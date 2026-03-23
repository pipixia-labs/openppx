"""Tests for Email channel adapter behavior."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from openpipixia.bus.events import OutboundMessage
from openpipixia.bus.queue import MessageBus
from openpipixia.channels.email import EmailChannel


class EmailChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_uses_smtp_helper(self) -> None:
        bus = MessageBus()
        channel = EmailChannel(
            bus=bus,
            consent_granted=True,
            smtp_host="smtp.example.com",
            smtp_username="bot@example.com",
            smtp_password="pw",
            from_address="bot@example.com",
        )

        with patch.object(channel, "_smtp_send") as smtp_send:
            await channel.send(
                OutboundMessage(
                    channel="email",
                    chat_id="user@example.com",
                    content="hello from email",
                )
            )

        smtp_send.assert_called_once()
        email_msg = smtp_send.call_args.args[0]
        self.assertEqual(email_msg["To"], "user@example.com")
        self.assertEqual(email_msg["From"], "bot@example.com")

    async def test_send_respects_auto_reply_toggle(self) -> None:
        bus = MessageBus()
        channel = EmailChannel(
            bus=bus,
            consent_granted=True,
            auto_reply_enabled=False,
            smtp_host="smtp.example.com",
            smtp_username="bot@example.com",
            smtp_password="pw",
        )

        with patch.object(channel, "_smtp_send") as smtp_send:
            await channel.send(
                OutboundMessage(
                    channel="email",
                    chat_id="user@example.com",
                    content="should be skipped",
                )
            )
            smtp_send.assert_not_called()

            await channel.send(
                OutboundMessage(
                    channel="email",
                    chat_id="user@example.com",
                    content="force send",
                    metadata={"force_send": True},
                )
            )
            smtp_send.assert_called_once()

    async def test_poll_once_publishes_allowed_inbound(self) -> None:
        bus = MessageBus()
        channel = EmailChannel(
            bus=bus,
            consent_granted=True,
            imap_host="imap.example.com",
            imap_username="bot@example.com",
            imap_password="pw",
            smtp_host="smtp.example.com",
            smtp_username="bot@example.com",
            smtp_password="pw",
            allow_from=["allowed@example.com"],
        )

        with patch.object(
            channel,
            "_fetch_unseen_sync",
            return_value=[
                {
                    "sender": "denied@example.com",
                    "content": "denied",
                    "metadata": {"subject": "x"},
                },
                {
                    "sender": "allowed@example.com",
                    "content": "hello inbound",
                    "metadata": {"subject": "y"},
                },
            ],
        ):
            await channel._poll_once()

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.chat_id, "allowed@example.com")
        self.assertEqual(inbound.content, "hello inbound")


if __name__ == "__main__":
    unittest.main()
