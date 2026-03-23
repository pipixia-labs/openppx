"""Tests for browser runtime selection/fallback logic."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpipixia.browser.runtime import (
    BrowserRuntimeError,
    InMemoryBrowserRuntime,
    configure_browser_runtime,
    get_browser_runtime,
    resolve_browser_artifact_path,
    validate_browser_upload_paths,
    validate_browser_url,
)


class BrowserRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)
        configure_browser_runtime(InMemoryBrowserRuntime())

    def test_default_runtime_is_in_memory(self) -> None:
        os.environ.pop("OPENPIPIXIA_BROWSER_RUNTIME", None)
        configure_browser_runtime(None)
        runtime = get_browser_runtime()
        self.assertIsInstance(runtime, InMemoryBrowserRuntime)

    def test_playwright_mode_falls_back_to_memory_when_adapter_fails(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_RUNTIME"] = "playwright"
        with patch("openpipixia.browser.runtime._create_playwright_runtime", side_effect=RuntimeError("boom")):
            configure_browser_runtime(None)
            runtime = get_browser_runtime()
        self.assertIsInstance(runtime, InMemoryBrowserRuntime)

    def test_playwright_mode_strict_raises_when_adapter_fails(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_RUNTIME"] = "playwright"
        os.environ["OPENPIPIXIA_BROWSER_RUNTIME_STRICT"] = "1"
        with patch("openpipixia.browser.runtime._create_playwright_runtime", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                configure_browser_runtime(None)

    def test_playwright_mode_uses_adapter_when_available(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_RUNTIME"] = "playwright"
        sentinel = InMemoryBrowserRuntime()
        with patch("openpipixia.browser.runtime._create_playwright_runtime", return_value=sentinel):
            configure_browser_runtime(None)
            runtime = get_browser_runtime()
        self.assertIs(runtime, sentinel)

    def test_validate_browser_url_blocks_private_hosts_by_default(self) -> None:
        with self.assertRaises(BrowserRuntimeError):
            validate_browser_url("http://127.0.0.1:3000")

        with self.assertRaises(BrowserRuntimeError):
            validate_browser_url("http://localhost:8080")

    def test_validate_browser_url_allows_private_hosts_when_policy_disabled(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_BLOCK_PRIVATE_NETWORKS"] = "0"
        validate_browser_url("http://127.0.0.1:3000")

    def test_validate_browser_upload_paths_enforces_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            inside = os.path.join(root_tmp, "a.txt")
            outside = os.path.join(outside_tmp, "b.txt")
            with open(inside, "w", encoding="utf-8") as f:
                f.write("inside")
            with open(outside, "w", encoding="utf-8") as f:
                f.write("outside")

            os.environ["OPENPIPIXIA_BROWSER_UPLOAD_ROOT"] = root_tmp
            resolved = validate_browser_upload_paths([inside])
            self.assertEqual(resolved, [os.path.realpath(inside)])

            with self.assertRaises(BrowserRuntimeError):
                validate_browser_upload_paths([outside])

    def test_in_memory_runtime_rejects_unknown_profile(self) -> None:
        runtime = InMemoryBrowserRuntime()
        with self.assertRaises(BrowserRuntimeError):
            runtime.status(profile="unknown")

    def test_in_memory_runtime_status_exposes_capability(self) -> None:
        runtime = InMemoryBrowserRuntime()
        status = runtime.status(profile="openpipixia")
        self.assertEqual(status["capability"]["backend"], "memory")
        self.assertIn("act", status["capability"]["supportedActions"])
        profiles = runtime.profiles()
        openpipixia = next(entry for entry in profiles["profiles"] if entry["name"] == "openpipixia")
        self.assertEqual(openpipixia["capability"]["backend"], "memory")
        self.assertIn("snapshot", openpipixia["capability"]["supportedActions"])

    def test_resolve_browser_artifact_path_enforces_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            os.environ["OPENPIPIXIA_BROWSER_ARTIFACT_ROOT"] = root_tmp
            inside = resolve_browser_artifact_path(str(Path(root_tmp) / "a.pdf"), default_filename="x.pdf")
            self.assertEqual(inside, str((Path(root_tmp) / "a.pdf").resolve()))
            with self.assertRaises(BrowserRuntimeError):
                resolve_browser_artifact_path(
                    str(Path(outside_tmp) / "b.pdf"),
                    default_filename="x.pdf",
                )


if __name__ == "__main__":
    unittest.main()
