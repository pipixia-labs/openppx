"""Tests for routing capability metadata declarations."""

from __future__ import annotations

import unittest

from openheron.runtime.route_capabilities import channel_supports_scope_metadata


class RouteCapabilitiesTests(unittest.TestCase):
    def test_channel_supports_scope_metadata(self) -> None:
        self.assertTrue(channel_supports_scope_metadata("discord"))
        self.assertTrue(channel_supports_scope_metadata("DISCORD"))
        self.assertFalse(channel_supports_scope_metadata("telegram"))


if __name__ == "__main__":
    unittest.main()
