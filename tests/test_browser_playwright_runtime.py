"""Tests for Playwright runtime helper logic without launching a browser."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from openpipixia.browser.playwright_runtime import PlaywrightBrowserRuntime, build_snapshot_refs
from openpipixia.browser.runtime import BrowserRuntimeError


class PlaywrightRuntimeHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_build_snapshot_refs_skips_entries_without_selector(self) -> None:
        refs, selectors = build_snapshot_refs(
            [
                {"role": "button", "name": "Submit", "selector": "#submit"},
                {"role": "input", "name": "Empty", "selector": ""},
                {"role": "link", "name": "Docs", "selector": "a:nth-of-type(1)"},
            ]
        )
        self.assertEqual(set(refs.keys()), {"e1", "e2"})
        self.assertEqual(refs["e1"]["role"], "button")
        self.assertEqual(selectors["e1"], "#submit")
        self.assertEqual(selectors["e2"], "a:nth-of-type(1)")

    def test_build_snapshot_refs_reuses_previous_refs_for_same_selector(self) -> None:
        initial_refs, initial_selectors = build_snapshot_refs(
            [
                {"role": "button", "name": "Save", "selector": "#save"},
                {"role": "input", "name": "Title", "selector": "input[name='title']"},
            ]
        )
        self.assertEqual(set(initial_refs.keys()), {"e1", "e2"})

        next_refs, next_selectors = build_snapshot_refs(
            [
                {"role": "input", "name": "Title", "selector": "input[name='title']"},
                {"role": "button", "name": "Save", "selector": "#save"},
                {"role": "button", "name": "Publish", "selector": "#publish"},
            ],
            previous_ref_selectors=initial_selectors,
        )
        self.assertEqual(next_selectors["e1"], "#save")
        self.assertEqual(next_selectors["e2"], "input[name='title']")
        self.assertEqual(next_selectors["e3"], "#publish")

    def test_selector_from_request_prefers_snapshot_ref_mapping(self) -> None:
        runtime = PlaywrightBrowserRuntime()
        runtime._snapshot_ref_selectors_by_tab["tab-1"] = {"e1": "#submit"}  # type: ignore[attr-defined]
        selector = runtime._selector_from_request({"ref": "e1"}, "tab-1")  # type: ignore[attr-defined]
        self.assertEqual(selector, "#submit")

    def test_selector_from_request_rejects_unknown_snapshot_ref(self) -> None:
        runtime = PlaywrightBrowserRuntime()
        with self.assertRaises(BrowserRuntimeError):
            runtime._selector_from_request({"ref": "e9"}, "tab-1")  # type: ignore[attr-defined]

    def test_chrome_profile_availability_depends_on_env(self) -> None:
        runtime = PlaywrightBrowserRuntime()
        profiles = runtime.profiles()
        openpipixia = next(entry for entry in profiles["profiles"] if entry["name"] == "openpipixia")
        chrome = next(entry for entry in profiles["profiles"] if entry["name"] == "chrome")
        self.assertEqual(openpipixia["attachMode"], "launch-or-cdp")
        self.assertIn("ownershipModel", openpipixia)
        self.assertFalse(chrome["available"])
        self.assertEqual(chrome["attachMode"], "cdp-required")
        self.assertTrue(chrome["requires"]["OPENPIPIXIA_BROWSER_CHROME_CDP_URL"])
        self.assertTrue(chrome["requires"]["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"])
        self.assertIn("ownershipModel", chrome)
        chrome_status = runtime.status(profile="chrome")
        self.assertFalse(chrome_status["browserOwned"])
        self.assertFalse(chrome_status["contextOwned"])
        self.assertEqual(chrome_status["transport"], "unsupported")

        os.environ["OPENPIPIXIA_BROWSER_CHROME_CDP_URL"] = "http://127.0.0.1:9222"
        runtime2 = PlaywrightBrowserRuntime()
        profiles2 = runtime2.profiles()
        chrome2 = next(entry for entry in profiles2["profiles"] if entry["name"] == "chrome")
        self.assertTrue(chrome2["available"])

        os.environ.pop("OPENPIPIXIA_BROWSER_CHROME_CDP_URL", None)
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://127.0.0.1:9800"
        runtime3 = PlaywrightBrowserRuntime()
        profiles3 = runtime3.profiles()
        chrome3 = next(entry for entry in profiles3["profiles"] if entry["name"] == "chrome")
        self.assertTrue(chrome3["available"])
        self.assertEqual(runtime3._resolve_chrome_transport(), "relay")  # type: ignore[attr-defined]

    def test_chrome_profile_supported_when_env_present(self) -> None:
        runtime = PlaywrightBrowserRuntime()
        with self.assertRaises(BrowserRuntimeError):
            runtime._ensure_profile_supported("chrome")  # type: ignore[attr-defined]

        os.environ["OPENPIPIXIA_BROWSER_CHROME_CDP_URL"] = "http://127.0.0.1:9222"
        runtime2 = PlaywrightBrowserRuntime()
        runtime2._ensure_profile_supported("chrome")  # type: ignore[attr-defined]

    def test_resolve_cdp_url_for_profiles(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_CDP_URL"] = "http://127.0.0.1:9333"
        os.environ["OPENPIPIXIA_BROWSER_CHROME_CDP_URL"] = "http://127.0.0.1:9222"
        runtime = PlaywrightBrowserRuntime()
        self.assertEqual(
            runtime._resolve_cdp_url_for_profile("openpipixia"),  # type: ignore[attr-defined]
            "http://127.0.0.1:9333",
        )
        self.assertEqual(
            runtime._resolve_cdp_url_for_profile("chrome"),  # type: ignore[attr-defined]
            "http://127.0.0.1:9222",
        )
        os.environ.pop("OPENPIPIXIA_BROWSER_CHROME_CDP_URL", None)
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://127.0.0.1:9800"
        self.assertEqual(runtime._resolve_chrome_transport(), "relay")  # type: ignore[attr-defined]

    def test_stop_closes_browser_and_context_when_owned(self) -> None:
        class _FakeClosable:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class _FakePwManager:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

        runtime = PlaywrightBrowserRuntime()
        fake_context = _FakeClosable()
        fake_browser = _FakeClosable()
        fake_manager = _FakePwManager()
        runtime._context = fake_context  # type: ignore[attr-defined]
        runtime._browser = fake_browser  # type: ignore[attr-defined]
        runtime._pw_context_manager = fake_manager  # type: ignore[attr-defined]
        runtime._owns_context = True  # type: ignore[attr-defined]
        runtime._owns_browser = True  # type: ignore[attr-defined]

        runtime.stop(profile="openpipixia")
        self.assertTrue(fake_context.closed)
        self.assertTrue(fake_browser.closed)
        self.assertTrue(fake_manager.stopped)

    def test_stop_does_not_close_browser_or_context_when_not_owned(self) -> None:
        class _FakeClosable:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class _FakePwManager:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

        runtime = PlaywrightBrowserRuntime()
        fake_context = _FakeClosable()
        fake_browser = _FakeClosable()
        fake_manager = _FakePwManager()
        runtime._context = fake_context  # type: ignore[attr-defined]
        runtime._browser = fake_browser  # type: ignore[attr-defined]
        runtime._pw_context_manager = fake_manager  # type: ignore[attr-defined]
        runtime._owns_context = False  # type: ignore[attr-defined]
        runtime._owns_browser = False  # type: ignore[attr-defined]

        runtime.stop(profile="openpipixia")
        self.assertFalse(fake_context.closed)
        self.assertFalse(fake_browser.closed)
        self.assertTrue(fake_manager.stopped)

    def test_openpipixia_status_includes_ownership_flags(self) -> None:
        runtime = PlaywrightBrowserRuntime()
        status = runtime.status(profile="openpipixia")
        self.assertIn("browserOwned", status)
        self.assertIn("contextOwned", status)
        self.assertEqual(status["capability"]["backend"], "playwright")
        self.assertIn("act", status["capability"]["supportedActions"])
        self.assertFalse(status["browserOwned"])
        self.assertFalse(status["contextOwned"])

    def test_chrome_relay_supports_core_actions(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        responses = [
            _DummyResponse('{"running":true,"tabCount":2,"lastTargetId":"tab-2"}'),
            _DummyResponse('{"running":true,"tabs":[{"targetId":"tab-2","url":"https://example.com"}]}'),
            _DummyResponse('{"ok":true,"snapshot":"relay-snapshot"}'),
            _DummyResponse('{"ok":true,"targetId":"tab-2","url":"https://example.org"}'),
            _DummyResponse('{"ok":true,"kind":"wait"}'),
            _DummyResponse('{"ok":true,"kind":"press"}'),
        ]
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=responses):
            started = runtime.start(profile="chrome")
            self.assertTrue(started["running"])
            self.assertEqual(started["transport"], "relay")
            self.assertIn("navigate", started["capability"]["supportedActions"])
            self.assertIn("act", started["capability"]["supportedActions"])

            tabs = runtime.tabs(profile="chrome")
            self.assertEqual(tabs["mode"], "relay")
            self.assertEqual(tabs["tabs"][0]["targetId"], "tab-2")

            snap = runtime.snapshot(profile="chrome", snapshot_format="ai")
            self.assertTrue(snap["ok"])
            self.assertEqual(snap["snapshot"], "relay-snapshot")

            nav = runtime.navigate(profile="chrome", target_id="tab-2", url="https://example.org")
            self.assertTrue(nav["ok"])
            self.assertEqual(nav["url"], "https://example.org")

            acted = runtime.act(profile="chrome", target_id="tab-2", request={"kind": "wait", "timeMs": 100})
            self.assertTrue(acted["ok"])
            self.assertEqual(acted["kind"], "wait")

            pressed = runtime.act(profile="chrome", target_id="tab-2", request={"kind": "press", "key": "Enter"})
            self.assertTrue(pressed["ok"])
            self.assertEqual(pressed["kind"], "press")

    def test_chrome_relay_supports_screenshot_and_out_path(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        image_base64 = "aGVsbG8="
        with patch(
            "openpipixia.browser.playwright_runtime.urlopen",
            return_value=_DummyResponse(
                f'{{"ok":true,"targetId":"tab-2","type":"png","imageBase64":"{image_base64}"}}'
            ),
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["OPENPIPIXIA_BROWSER_ARTIFACT_ROOT"] = tmpdir
                out = runtime.screenshot(profile="chrome", target_id="tab-2", out_path=f"{tmpdir}/shot.png")
                self.assertTrue(out["ok"])
                self.assertEqual(out["backend"], "extension-relay")
                self.assertEqual(out["contentType"], "image/png")
                self.assertTrue(out["path"].endswith("shot.png"))
                with open(out["path"], "rb") as f:
                    self.assertEqual(f.read(), b"hello")

    def test_chrome_relay_supports_pdf_and_out_path(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        pdf_base64 = "aGVsbG8="
        with patch(
            "openpipixia.browser.playwright_runtime.urlopen",
            return_value=_DummyResponse(
                f'{{"ok":true,"targetId":"tab-2","pdfBase64":"{pdf_base64}"}}'
            ),
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["OPENPIPIXIA_BROWSER_ARTIFACT_ROOT"] = tmpdir
                out = runtime.pdf_save(profile="chrome", target_id="tab-2", out_path=f"{tmpdir}/tab-2.pdf")
                self.assertTrue(out["ok"])
                self.assertEqual(out["backend"], "extension-relay")
                self.assertEqual(out["contentType"], "application/pdf")
                self.assertEqual(out["bytes"], 5)
                self.assertTrue(out["path"].endswith("tab-2.pdf"))
                with open(out["path"], "rb") as f:
                    self.assertEqual(f.read(), b"hello")

    def test_chrome_relay_supports_console_and_out_path(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch(
            "openpipixia.browser.playwright_runtime.urlopen",
            return_value=_DummyResponse(
                '{"ok":true,"targetId":"tab-2","messages":[{"level":"error","text":"boom"}]}'
            ),
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["OPENPIPIXIA_BROWSER_ARTIFACT_ROOT"] = tmpdir
                out = runtime.console_messages(
                    profile="chrome",
                    target_id="tab-2",
                    level="error",
                    out_path=f"{tmpdir}/tab-2.console.json",
                )
                self.assertTrue(out["ok"])
                self.assertEqual(out["backend"], "extension-relay")
                self.assertEqual(len(out["messages"]), 1)
                self.assertEqual(out["messages"][0]["text"], "boom")
                with open(out["path"], "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self.assertEqual(saved["messages"][0]["level"], "error")

    def test_chrome_relay_supports_upload_and_ref_resolution(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            url = request_obj.full_url
            if "/snapshot" in url:
                return _DummyResponse(
                    '{"ok":true,"targetId":"tab-2","refs":{"e1":{"selector":"#fileInput","role":"textbox"}}}'
                )
            captured["body"] = request_obj.data.decode("utf-8", errors="replace") if request_obj.data else ""
            return _DummyResponse('{"ok":true,"uploaded":true}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "photo.png")
            with open(file_path, "wb") as f:
                f.write(b"img")
            os.environ["OPENPIPIXIA_BROWSER_UPLOAD_ROOT"] = tmpdir
            with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
                runtime.snapshot(profile="chrome", target_id="tab-2", snapshot_format="ai")
                out = runtime.upload(profile="chrome", target_id="tab-2", paths=[file_path], ref="e1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["backend"], "extension-relay")
        self.assertEqual(out["count"], 1)
        body = json.loads(captured["body"])
        self.assertEqual(body["targetId"], "tab-2")
        self.assertEqual(body["ref"], "#fileInput")
        self.assertEqual(len(body["paths"]), 1)

    def test_chrome_relay_upload_falls_back_to_hooks_file_chooser(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        calls: list[str] = []

        def _fake_urlopen(request_obj, timeout=10):
            calls.append(request_obj.full_url)
            if "/upload" in request_obj.full_url:
                raise HTTPError(
                    url=request_obj.full_url,
                    code=404,
                    msg="Not Found",
                    hdrs=None,
                    fp=BytesIO(b'{"error":"not found","status":404}'),
                )
            return _DummyResponse('{"ok":true,"uploaded":true}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "photo.png")
            with open(file_path, "wb") as f:
                f.write(b"img")
            os.environ["OPENPIPIXIA_BROWSER_UPLOAD_ROOT"] = tmpdir
            with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
                out = runtime.upload(profile="chrome", target_id="tab-2", paths=[file_path], ref="#fileInput")
        self.assertTrue(out["ok"])
        self.assertIn("/upload", calls[0])
        self.assertIn("/hooks/file-chooser", calls[1])

    def test_chrome_relay_supports_dialog(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = request_obj.data.decode("utf-8", errors="replace") if request_obj.data else ""
            return _DummyResponse('{"ok":true,"armed":true}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.dialog(
                profile="chrome",
                target_id="tab-2",
                accept=True,
                prompt_text="hello",
            )
        self.assertTrue(out["ok"])
        self.assertEqual(out["backend"], "extension-relay")
        self.assertTrue(out["armed"])
        body = json.loads(captured["body"])
        self.assertEqual(body["targetId"], "tab-2")
        self.assertTrue(body["accept"])
        self.assertEqual(body["promptText"], "hello")

    def test_chrome_relay_dialog_falls_back_to_hooks_dialog(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        calls: list[str] = []

        def _fake_urlopen(request_obj, timeout=10):
            calls.append(request_obj.full_url)
            if request_obj.full_url.endswith("/dialog") and "/hooks/dialog" not in request_obj.full_url:
                raise HTTPError(
                    url=request_obj.full_url,
                    code=404,
                    msg="Not Found",
                    hdrs=None,
                    fp=BytesIO(b'{"error":"not found","status":404}'),
                )
            return _DummyResponse('{"ok":true,"armed":true}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.dialog(profile="chrome", target_id="tab-2", accept=False)
        self.assertTrue(out["ok"])
        self.assertIn("/dialog", calls[0])
        self.assertIn("/hooks/dialog", calls[1])

    def test_chrome_relay_supports_open_focus_close(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: list[tuple[str, str]] = []

        def _fake_urlopen(request_obj, timeout=10):
            body = request_obj.data.decode("utf-8", errors="replace") if request_obj.data else ""
            captured.append((request_obj.full_url, body))
            url = request_obj.full_url
            if "/tabs/open" in url:
                return _DummyResponse('{"ok":true,"targetId":"tab-3","url":"https://example.com"}')
            if "/tabs/focus" in url:
                return _DummyResponse('{"ok":true,"targetId":"tab-3","focused":true}')
            return _DummyResponse('{"ok":true,"targetId":"tab-3","closed":true}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            opened = runtime.open_tab(profile="chrome", url="https://example.com")
            focused = runtime.focus_tab(profile="chrome", target_id="tab-3")
            closed = runtime.close_tab(profile="chrome", target_id="tab-3")
        self.assertTrue(opened["ok"])
        self.assertTrue(focused["ok"])
        self.assertTrue(closed["ok"])
        self.assertIn("/tabs/open", captured[0][0])
        self.assertIn("/tabs/focus", captured[1][0])
        self.assertIn("/tabs/close", captured[2][0])

    def test_chrome_relay_rejects_unsupported_act_kind(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "custom-op"})

    def test_chrome_relay_act_supports_hover(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"hover"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(profile="chrome", target_id="tab-1", request={"kind": "hover", "selector": "#menu"})
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["kind"], "hover")
        self.assertEqual(body["request"]["selector"], "#menu")

    def test_chrome_relay_act_supports_select(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"select"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(
                profile="chrome",
                target_id="tab-1",
                request={"kind": "select", "selector": "#country", "values": [" CN ", "US"]},
            )
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["kind"], "select")
        self.assertEqual(body["request"]["selector"], "#country")
        self.assertEqual(body["request"]["values"], ["CN", "US"])

    def test_chrome_relay_act_supports_drag(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"drag"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        runtime._snapshot_ref_selectors_by_tab["tab-1"] = {"e1": "#from"}  # type: ignore[attr-defined]
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(
                profile="chrome",
                target_id="tab-1",
                request={"kind": "drag", "startRef": "e1", "endSelector": "#to"},
            )
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["kind"], "drag")
        self.assertEqual(body["request"]["startSelector"], "#from")
        self.assertEqual(body["request"]["endSelector"], "#to")

    def test_chrome_relay_act_supports_fill(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"fill"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        runtime._snapshot_ref_selectors_by_tab["tab-1"] = {"e2": "#email"}  # type: ignore[attr-defined]
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(
                profile="chrome",
                target_id="tab-1",
                request={
                    "kind": "fill",
                    "fields": [
                        {"ref": "e2", "text": "a@example.com"},
                        {"selector": "#name", "value": "Alice"},
                    ],
                },
            )
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["kind"], "fill")
        self.assertEqual(body["request"]["fields"][0]["selector"], "#email")
        self.assertEqual(body["request"]["fields"][0]["text"], "a@example.com")
        self.assertEqual(body["request"]["fields"][1]["selector"], "#name")
        self.assertEqual(body["request"]["fields"][1]["text"], "Alice")

    def test_chrome_relay_act_supports_resize(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"resize"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(profile="chrome", target_id="tab-1", request={"kind": "resize", "width": 1280, "height": 720})
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["kind"], "resize")
        self.assertEqual(body["request"]["width"], 1280)
        self.assertEqual(body["request"]["height"], 720)

    def test_chrome_relay_act_supports_close(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"close","closed":true}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(profile="chrome", target_id="tab-1", request={"kind": "close"})
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["kind"], "close")
        self.assertEqual(body["targetId"], "tab-1")

    def test_chrome_relay_act_supports_evaluate_when_enabled(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"evaluate"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_EVALUATE_ENABLED"] = "1"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(profile="chrome", target_id="tab-1", request={"kind": "evaluate", "fn": "() => 1"})
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["kind"], "evaluate")
        self.assertEqual(body["request"]["fn"], "() => 1")

    def test_chrome_relay_act_supports_open(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"open","targetId":"tab-2"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(profile="chrome", request={"kind": "open", "url": "https://example.com"})
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["kind"], "open")
        self.assertEqual(body["request"]["url"], "https://example.com")

    def test_chrome_relay_validates_type_and_press_requests(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "type"})
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "press"})
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "select", "selector": "#country"})
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "drag", "startSelector": "#from"})
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "fill", "fields": []})
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "resize", "width": 1280})
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "evaluate", "fn": "() => 1"})
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "open"})

    def test_chrome_relay_act_normalizes_selector_ref_and_kind(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"click"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(
                profile="chrome",
                target_id="tab-1",
                request={"kind": " Click ", "selector": "  #submit  "},
            )
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["targetId"], "tab-1")
        self.assertEqual(body["request"]["kind"], "click")
        self.assertEqual(body["request"]["selector"], "#submit")

    def test_chrome_relay_act_click_requires_selector_or_ref(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "click"})

    def test_chrome_relay_act_can_resolve_selector_from_snapshot_ref(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            url = request_obj.full_url
            if "/snapshot" in url:
                return _DummyResponse(
                    '{"ok":true,"targetId":"tab-1","refs":{"e1":{"selector":"#submit","role":"button"}}}'
                )
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"click"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            runtime.snapshot(profile="chrome", target_id="tab-1", snapshot_format="ai")
            out = runtime.act(profile="chrome", target_id="tab-1", request={"kind": "click", "ref": "e1"})
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["selector"], "#submit")
        self.assertEqual(body["request"]["ref"], "e1")

    def test_chrome_relay_act_unknown_snapshot_ref_fails(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", target_id="tab-1", request={"kind": "click", "ref": "e9"})

    def test_chrome_relay_act_type_can_resolve_selector_from_snapshot_ref(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            url = request_obj.full_url
            if "/snapshot" in url:
                return _DummyResponse('{"ok":true,"targetId":"tab-1","refSelectors":{"e2":"#email"}}')
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"type"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            runtime.snapshot(profile="chrome", target_id="tab-1", snapshot_format="ai")
            out = runtime.act(
                profile="chrome",
                target_id="tab-1",
                request={"kind": "type", "ref": "e2", "text": "abc"},
            )
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["selector"], "#email")
        self.assertEqual(body["request"]["text"], "abc")

    def test_chrome_relay_act_non_snapshot_ref_falls_back_to_selector(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"click"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(
                profile="chrome",
                target_id="tab-1",
                request={"kind": "click", "ref": "button.primary"},
            )
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["selector"], "button.primary")

    def test_chrome_relay_act_wait_normalizes_timeout(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"wait"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            runtime.act(profile="chrome", target_id="tab-1", request={"kind": "wait", "timeMs": 999999})
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["timeMs"], 60000)

        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            runtime.act(profile="chrome", target_id="tab-1", request={"kind": "wait"})
        body2 = json.loads(captured["body"])
        self.assertEqual(body2["request"]["timeMs"], 500)

    def test_chrome_relay_act_press_trims_key(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        captured: dict[str, str] = {}

        def _fake_urlopen(request_obj, timeout=10):
            captured["body"] = (
                request_obj.data.decode("utf-8", errors="replace")
                if request_obj.data is not None
                else ""
            )
            return _DummyResponse('{"ok":true,"kind":"press"}')

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=_fake_urlopen):
            out = runtime.act(profile="chrome", target_id="tab-1", request={"kind": "press", "key": "  Enter  "})
        self.assertTrue(out["ok"])
        body = json.loads(captured["body"])
        self.assertEqual(body["request"]["key"], "Enter")

    def test_chrome_relay_maps_timeout_error(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=URLError(TimeoutError("timed out"))):
            with self.assertRaises(BrowserRuntimeError) as ctx:
                runtime.status(profile="chrome")
        self.assertEqual(ctx.exception.status, 504)
        self.assertEqual(ctx.exception.code, "relay_timeout")
        self.assertIn("timeout", str(ctx.exception).lower())

    def test_chrome_relay_maps_direct_socket_timeout_error(self) -> None:
        import socket

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=socket.timeout("timed out")):
            with self.assertRaises(BrowserRuntimeError) as ctx:
                runtime.status(profile="chrome")
        self.assertEqual(ctx.exception.status, 504)
        self.assertEqual(ctx.exception.code, "relay_timeout")

    def test_chrome_relay_maps_structured_http_error(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        http_error = HTTPError(
            url="http://relay.local:9800/status",
            code=502,
            msg="Bad Gateway",
            hdrs=None,
            fp=BytesIO(b'{"error":"rate limited","status":429}'),
        )
        with patch("openpipixia.browser.playwright_runtime.urlopen", side_effect=http_error):
            with self.assertRaises(BrowserRuntimeError) as ctx:
                runtime.status(profile="chrome")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.code, "relay_http_error")
        self.assertIn("rate limited", str(ctx.exception).lower())

    def test_chrome_relay_act_type_enforces_text_length_limit(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        too_long = "x" * 5001
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "type", "selector": "#q", "text": too_long})

    def test_chrome_relay_act_type_text_length_limit_from_env(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_TYPE_MAX_CHARS"] = "3"
        runtime = PlaywrightBrowserRuntime()
        with self.assertRaises(BrowserRuntimeError):
            runtime.act(profile="chrome", request={"kind": "type", "selector": "#q", "text": "abcd"})

        with patch("openpipixia.browser.playwright_runtime.urlopen", return_value=_DummyResponse('{"ok":true}')):
            out = runtime.act(profile="chrome", request={"kind": "type", "selector": "#q", "text": "abc"})
        self.assertTrue(out["ok"])

    def test_chrome_relay_body_size_limit_blocks_large_payload(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_MAX_BODY_BYTES"] = "300"
        runtime = PlaywrightBrowserRuntime()
        with self.assertRaises(BrowserRuntimeError) as ctx:
            runtime.navigate(profile="chrome", target_id="tab-1", url="https://example.com/" + ("a" * 400))
        self.assertEqual(ctx.exception.code, "relay_body_too_large")

    def test_chrome_relay_body_size_limit_allows_small_payload(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_MAX_BODY_BYTES"] = "300"
        runtime = PlaywrightBrowserRuntime()
        with patch("openpipixia.browser.playwright_runtime.urlopen", return_value=_DummyResponse('{"ok":true}')):
            out = runtime.navigate(profile="chrome", target_id="tab-1", url="https://example.com/a")
        self.assertTrue(out["ok"])

    def test_chrome_relay_navigate_validates_url_policy(self) -> None:
        os.environ["OPENPIPIXIA_BROWSER_CHROME_RELAY_URL"] = "http://relay.local:9800"
        runtime = PlaywrightBrowserRuntime()
        with self.assertRaises(BrowserRuntimeError):
            runtime.navigate(profile="chrome", url="file:///tmp/a.html")
        with self.assertRaises(BrowserRuntimeError):
            runtime.navigate(profile="chrome", url="http://127.0.0.1:9222")


if __name__ == "__main__":
    unittest.main()
