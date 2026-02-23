"""Tests for browser control service + routes."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from openheron.browser_service import (
    BrowserDispatchRequest,
    get_browser_control_service,
    reset_browser_control_service,
)
from openheron.browser_runtime import configure_browser_runtime


class BrowserServiceTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("OPENHERON_BROWSER_CONTROL_TOKEN", None)
        os.environ.pop("OPENHERON_BROWSER_MUTATION_TOKEN", None)
        os.environ.pop("OPENHERON_BROWSER_UPLOAD_ROOT", None)
        os.environ.pop("OPENHERON_BROWSER_ENFORCE_UPLOAD_ROOT", None)
        configure_browser_runtime(None)
        reset_browser_control_service()

    def test_dispatch_basic_lifecycle_routes(self) -> None:
        service = get_browser_control_service()

        status = service.dispatch(BrowserDispatchRequest(method="GET", path="/"))
        self.assertEqual(status.status, 200)
        self.assertFalse(status.body["running"])

        started = service.dispatch(BrowserDispatchRequest(method="POST", path="/start"))
        self.assertEqual(started.status, 200)
        self.assertTrue(started.body["running"])

        stopped = service.dispatch(BrowserDispatchRequest(method="POST", path="/stop"))
        self.assertEqual(stopped.status, 200)
        self.assertFalse(stopped.body["running"])

    def test_dispatch_agent_routes(self) -> None:
        service = get_browser_control_service()
        service.dispatch(BrowserDispatchRequest(method="POST", path="/start"))

        opened = service.dispatch(
            BrowserDispatchRequest(
                method="POST",
                path="/tabs/open",
                body={"url": "https://example.com"},
            )
        )
        self.assertEqual(opened.status, 200)
        target_id = opened.body["targetId"]

        focused = service.dispatch(
            BrowserDispatchRequest(
                method="POST",
                path="/tabs/focus",
                body={"targetId": target_id},
            )
        )
        self.assertEqual(focused.status, 200)
        self.assertTrue(focused.body["focused"])

        snap = service.dispatch(
            BrowserDispatchRequest(
                method="GET",
                path="/snapshot",
                query={"targetId": target_id, "format": "ai"},
            )
        )
        self.assertEqual(snap.status, 200)
        self.assertEqual(snap.body["targetId"], target_id)

        navigated = service.dispatch(
            BrowserDispatchRequest(
                method="POST",
                path="/navigate",
                body={"targetId": target_id, "url": "https://example.org"},
            )
        )
        self.assertEqual(navigated.status, 200)
        self.assertIn("example.org", navigated.body["url"])

        with tempfile.TemporaryDirectory() as tmp:
            shot_path = Path(tmp) / "shots" / "service.png"
            shot = service.dispatch(
                BrowserDispatchRequest(
                    method="POST",
                    path="/screenshot",
                    body={"targetId": target_id, "type": "png", "path": str(shot_path)},
                )
            )
            self.assertEqual(shot.status, 200)
            self.assertTrue(shot.body["imageBase64"])
            self.assertEqual(Path(shot.body["path"]).resolve(), shot_path.resolve())
            self.assertTrue(shot_path.exists())

        with tempfile.TemporaryDirectory() as tmp:
            upload_file = Path(tmp) / "demo.txt"
            upload_file.write_text("demo", encoding="utf-8")
            os.environ["OPENHERON_BROWSER_UPLOAD_ROOT"] = tmp
            uploaded = service.dispatch(
                BrowserDispatchRequest(
                    method="POST",
                    path="/hooks/file-chooser",
                    body={"targetId": target_id, "paths": [str(upload_file)], "ref": "#file"},
                )
            )
        self.assertEqual(uploaded.status, 200)
        self.assertEqual(uploaded.body["uploadedPaths"], [str(upload_file.resolve())])

        dialog = service.dispatch(
            BrowserDispatchRequest(
                method="POST",
                path="/hooks/dialog",
                body={"targetId": target_id, "accept": True, "promptText": "yes"},
            )
        )
        self.assertEqual(dialog.status, 200)
        self.assertTrue(dialog.body["armed"])

        acted = service.dispatch(
            BrowserDispatchRequest(
                method="POST",
                path="/act",
                body={"targetId": target_id, "request": {"kind": "click", "ref": "e1"}},
            )
        )
        self.assertEqual(acted.status, 200)
        self.assertEqual(acted.body["kind"], "click")

        acted_flat = service.dispatch(
            BrowserDispatchRequest(
                method="POST",
                path="/act",
                body={"targetId": target_id, "kind": "wait", "timeMs": 10},
            )
        )
        self.assertEqual(acted_flat.status, 200)
        self.assertEqual(acted_flat.body["kind"], "wait")

        closed = service.dispatch(
            BrowserDispatchRequest(
                method="POST",
                path="/tabs/close",
                body={"targetId": target_id},
            )
        )
        self.assertEqual(closed.status, 200)
        self.assertTrue(closed.body["closed"])

    def test_dispatch_reports_404_for_unknown_route(self) -> None:
        service = get_browser_control_service()
        res = service.dispatch(BrowserDispatchRequest(method="GET", path="/missing"))
        self.assertEqual(res.status, 404)
        self.assertFalse(res.body["ok"])

    def test_dispatch_validates_upload_and_dialog_inputs(self) -> None:
        service = get_browser_control_service()
        no_paths = service.dispatch(
            BrowserDispatchRequest(method="POST", path="/hooks/file-chooser", body={"paths": []})
        )
        self.assertEqual(no_paths.status, 400)
        self.assertFalse(no_paths.body["ok"])

        missing_accept = service.dispatch(
            BrowserDispatchRequest(method="POST", path="/hooks/dialog", body={"promptText": "x"})
        )
        self.assertEqual(missing_accept.status, 400)
        self.assertFalse(missing_accept.body["ok"])

    def test_dispatch_rejects_unsupported_target_routing(self) -> None:
        service = get_browser_control_service()

        sandbox_req = service.dispatch(
            BrowserDispatchRequest(method="GET", path="/", query={"target": "sandbox"})
        )
        self.assertEqual(sandbox_req.status, 501)
        self.assertFalse(sandbox_req.body["ok"])
        self.assertIn("not implemented", sandbox_req.body["error"])

        invalid_req = service.dispatch(
            BrowserDispatchRequest(method="GET", path="/", query={"target": "invalid"})
        )
        self.assertEqual(invalid_req.status, 400)
        self.assertFalse(invalid_req.body["ok"])
        self.assertIn("target must be", invalid_req.body["error"])

    def test_dispatch_profiles_support_chrome_metadata(self) -> None:
        service = get_browser_control_service()

        profiles = service.dispatch(BrowserDispatchRequest(method="GET", path="/profiles"))
        self.assertEqual(profiles.status, 200)
        names = {entry["name"] for entry in profiles.body["profiles"]}
        self.assertIn("openheron", names)
        self.assertIn("chrome", names)

        chrome_status = service.dispatch(
            BrowserDispatchRequest(method="GET", path="/", query={"profile": "chrome"})
        )
        self.assertEqual(chrome_status.status, 200)
        self.assertEqual(chrome_status.body["profile"], "chrome")
        self.assertFalse(chrome_status.body["running"])

        chrome_start = service.dispatch(
            BrowserDispatchRequest(method="POST", path="/start", query={"profile": "chrome"})
        )
        self.assertEqual(chrome_start.status, 501)
        self.assertFalse(chrome_start.body["ok"])

    def test_dispatch_requires_auth_token_when_enabled(self) -> None:
        os.environ["OPENHERON_BROWSER_CONTROL_TOKEN"] = "token-1"
        reset_browser_control_service()
        service = get_browser_control_service()

        unauthorized = service.dispatch(BrowserDispatchRequest(method="GET", path="/"))
        self.assertEqual(unauthorized.status, 401)
        self.assertFalse(unauthorized.body["ok"])

        authorized = service.dispatch(
            BrowserDispatchRequest(method="GET", path="/", auth_token="token-1")
        )
        self.assertEqual(authorized.status, 200)
        self.assertIn("running", authorized.body)

    def test_dispatch_requires_mutation_token_for_mutating_routes(self) -> None:
        os.environ["OPENHERON_BROWSER_CONTROL_TOKEN"] = "token-2"
        os.environ["OPENHERON_BROWSER_MUTATION_TOKEN"] = "mut-2"
        reset_browser_control_service()
        service = get_browser_control_service()

        get_ok = service.dispatch(BrowserDispatchRequest(method="GET", path="/", auth_token="token-2"))
        self.assertEqual(get_ok.status, 200)

        no_mutation_token = service.dispatch(
            BrowserDispatchRequest(method="POST", path="/start", auth_token="token-2")
        )
        self.assertEqual(no_mutation_token.status, 403)
        self.assertFalse(no_mutation_token.body["ok"])

        started = service.dispatch(
            BrowserDispatchRequest(
                method="POST",
                path="/start",
                auth_token="token-2",
                mutation_token="mut-2",
            )
        )
        self.assertEqual(started.status, 200)
        self.assertTrue(started.body["running"])


if __name__ == "__main__":
    unittest.main()
