"""Tests for runtime heartbeat prompt/token helpers."""

from __future__ import annotations

import unittest

from openpipixia.runtime.heartbeat_utils import (
    DEFAULT_HEARTBEAT_PROMPT,
    HEARTBEAT_TOKEN,
    resolve_heartbeat_prompt,
    strip_heartbeat_token,
)


class HeartbeatUtilsTests(unittest.TestCase):
    def test_resolve_heartbeat_prompt_uses_default_for_blank_input(self) -> None:
        self.assertEqual(resolve_heartbeat_prompt(""), DEFAULT_HEARTBEAT_PROMPT)
        self.assertEqual(resolve_heartbeat_prompt("   "), DEFAULT_HEARTBEAT_PROMPT)
        self.assertEqual(resolve_heartbeat_prompt(None), DEFAULT_HEARTBEAT_PROMPT)

    def test_resolve_heartbeat_prompt_keeps_explicit_text(self) -> None:
        self.assertEqual(resolve_heartbeat_prompt("ops check"), "ops check")

    def test_strip_heartbeat_token_skips_empty_text(self) -> None:
        result = strip_heartbeat_token("")
        self.assertTrue(result.should_skip)
        self.assertEqual(result.text, "")
        self.assertFalse(result.did_strip)

    def test_strip_heartbeat_token_skips_token_only_payload(self) -> None:
        result = strip_heartbeat_token(HEARTBEAT_TOKEN, mode="heartbeat")
        self.assertTrue(result.should_skip)
        self.assertEqual(result.text, "")
        self.assertTrue(result.did_strip)

    def test_strip_heartbeat_token_skips_short_tail_in_heartbeat_mode(self) -> None:
        result = strip_heartbeat_token(f"{HEARTBEAT_TOKEN} noted", mode="heartbeat", max_ack_chars=20)
        self.assertTrue(result.should_skip)
        self.assertEqual(result.text, "")
        self.assertTrue(result.did_strip)

    def test_strip_heartbeat_token_keeps_long_tail_in_heartbeat_mode(self) -> None:
        long_text = "please check inbox and summarize outstanding follow-ups before noon"
        result = strip_heartbeat_token(f"{HEARTBEAT_TOKEN} {long_text}", mode="heartbeat", max_ack_chars=10)
        self.assertFalse(result.should_skip)
        self.assertEqual(result.text, long_text)
        self.assertTrue(result.did_strip)

    def test_strip_heartbeat_token_keeps_short_tail_in_message_mode(self) -> None:
        result = strip_heartbeat_token(f"{HEARTBEAT_TOKEN} noted", mode="message")
        self.assertFalse(result.should_skip)
        self.assertEqual(result.text, "noted")
        self.assertTrue(result.did_strip)

    def test_strip_heartbeat_token_keeps_token_when_inside_sentence_middle(self) -> None:
        text = f"do not drop {HEARTBEAT_TOKEN} in the middle"
        result = strip_heartbeat_token(text, mode="heartbeat")
        self.assertFalse(result.should_skip)
        self.assertEqual(result.text, text)
        self.assertFalse(result.did_strip)

    def test_strip_heartbeat_token_strips_markdown_or_html_wrappers(self) -> None:
        md_result = strip_heartbeat_token(f"**{HEARTBEAT_TOKEN}**", mode="heartbeat")
        html_result = strip_heartbeat_token(f"<b>{HEARTBEAT_TOKEN}</b>", mode="heartbeat")
        self.assertTrue(md_result.should_skip)
        self.assertTrue(md_result.did_strip)
        self.assertTrue(html_result.should_skip)
        self.assertTrue(html_result.did_strip)

    def test_strip_heartbeat_token_does_not_strip_word_prefix_variants(self) -> None:
        text = "HEARTBEAT_OKAY keep this"
        result = strip_heartbeat_token(text, mode="heartbeat")
        self.assertFalse(result.should_skip)
        self.assertEqual(result.text, text)
        self.assertFalse(result.did_strip)

    def test_strip_heartbeat_token_clamps_negative_threshold(self) -> None:
        result = strip_heartbeat_token(f"{HEARTBEAT_TOKEN} alert", mode="heartbeat", max_ack_chars=-1)
        self.assertFalse(result.should_skip)
        self.assertEqual(result.text, "alert")
        self.assertTrue(result.did_strip)


if __name__ == "__main__":
    unittest.main()

