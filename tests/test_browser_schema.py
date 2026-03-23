"""Tests for browser schema compatibility helpers."""

from __future__ import annotations

import unittest

from openpipixia.browser.schema import (
    apply_status_metadata,
    build_action_guidance,
    make_profile_entry,
    make_runtime_capability,
    normalize_profile_payload_aliases,
    rank_supported_actions,
)


class BrowserSchemaTests(unittest.TestCase):
    def test_build_action_guidance_sorts_and_caps(self) -> None:
        guidance = build_action_guidance({"custom-z", "status", "pdf", "tabs"}, recommendation_limit=2)
        self.assertEqual(guidance["supportedActions"], ["status", "tabs", "pdf", "custom-z"])
        self.assertEqual(guidance["recommendedActions"], ["status", "tabs"])

    def test_build_action_guidance_with_custom_order(self) -> None:
        guidance = build_action_guidance(
            {"status", "pdf", "tabs"},
            recommendation_limit=3,
            preferred_order=["pdf", "tabs", "status"],
        )
        self.assertEqual(guidance["supportedActions"], ["pdf", "tabs", "status"])

    def test_rank_supported_actions_prefers_known_order(self) -> None:
        ranked = rank_supported_actions({"custom-z", "pdf", "status", "tabs"})
        self.assertEqual(ranked, ["status", "tabs", "pdf", "custom-z"])

    def test_make_runtime_capability(self) -> None:
        capability = make_runtime_capability(
            backend="playwright",
            driver="playwright",
            mode="launch",
            attach_mode="launch-or-cdp",
            supported_actions=["status", "snapshot"],
        )
        self.assertEqual(capability["backend"], "playwright")
        self.assertEqual(capability["driver"], "playwright")
        self.assertEqual(capability["mode"], "launch")
        self.assertEqual(capability["attachMode"], "launch-or-cdp")
        self.assertEqual(capability["supportedActions"], ["status", "snapshot"])
        self.assertIn("browser_timeout", capability["errorCodes"])

    def test_make_profile_entry_with_optional_metadata(self) -> None:
        entry = make_profile_entry(
            name="openpipixia",
            driver="playwright",
            description="Runtime profile",
            available=True,
            attach_mode="launch-or-cdp",
            ownership_model={"browser": "owned"},
            requires={"OPENPIPIXIA_BROWSER_CDP_URL": False},
        )
        self.assertEqual(entry["attachMode"], "launch-or-cdp")
        self.assertIn("ownershipModel", entry)
        self.assertIn("requires", entry)

    def test_apply_status_metadata(self) -> None:
        payload = apply_status_metadata(
            {"running": True, "profile": "openpipixia"},
            attach_mode="launch-or-cdp",
            browser_owned=True,
            context_owned=False,
        )
        self.assertEqual(payload["attachMode"], "launch-or-cdp")
        self.assertTrue(payload["browserOwned"])
        self.assertFalse(payload["contextOwned"])

    def test_normalize_status_aliases(self) -> None:
        payload = normalize_profile_payload_aliases(
            {
                "profile": "openpipixia",
                "attachMode": "launch-or-cdp",
                "browserOwned": True,
                "contextOwned": False,
                "capabilityWarnings": ["bad capability json"],
            }
        )
        self.assertEqual(payload["attach_mode"], "launch-or-cdp")
        self.assertTrue(payload["browser_owned"])
        self.assertFalse(payload["context_owned"])
        self.assertEqual(payload["capability_warnings"], ["bad capability json"])

    def test_normalize_profiles_aliases(self) -> None:
        payload = normalize_profile_payload_aliases(
            {
                "profiles": [
                    {
                        "name": "chrome",
                        "attachMode": "cdp-required",
                        "requires": {"OPENPIPIXIA_BROWSER_CHROME_CDP_URL": True},
                        "ownershipModel": {"browser": "borrowed"},
                        "capability": {
                            "backend": "extension-relay",
                            "attachMode": "cdp-required",
                            "supportedActions": ["status", "profiles"],
                            "recommendedOrder": ["status", "profiles"],
                            "errorCodes": ["proxy_timeout"],
                        },
                    }
                ]
            }
        )
        entry = payload["profiles"][0]
        self.assertEqual(entry["attach_mode"], "cdp-required")
        self.assertIn("requirements", entry)
        self.assertIn("ownership_model", entry)
        self.assertEqual(entry["capability"]["attach_mode"], "cdp-required")
        self.assertEqual(entry["capability"]["supported_actions"], ["status", "profiles"])
        self.assertEqual(entry["capability"]["recommended_order"], ["status", "profiles"])
        self.assertEqual(entry["capability"]["error_codes"], ["proxy_timeout"])


if __name__ == "__main__":
    unittest.main()
