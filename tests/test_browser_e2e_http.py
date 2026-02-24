"""Minimal end-to-end HTTP tests for browser proxy routing."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from openheron.browser_runtime import configure_browser_runtime
from openheron.tools import browser


class BrowserE2EHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        self._captured: dict[str, object] = {"post_paths": []}

        captured = self._captured

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                captured["path"] = self.path
                captured["proxy_token"] = self.headers.get("X-OpenHeron-Browser-Proxy-Token", "")
                captured["relay_token"] = self.headers.get("X-OpenHeron-Browser-Relay-Token", "")
                parsed = urlparse(self.path)
                relay_mode = str(captured.get("relay_mode") or "").strip().lower()
                query = parse_qs(parsed.query)
                node_name = str((query.get("node") or [""])[0]).strip()
                if not node_name:
                    node_name = str(captured.get("proxy_mode") or "").strip().lower()
                if not node_name:
                    token_mode = str(captured.get("proxy_token") or "").strip().lower()
                    if token_mode.startswith("mode-"):
                        node_name = token_mode.removeprefix("mode-")
                if node_name == "auth-required" and captured.get("proxy_token") != "node-token-ok":
                    payload = {"error": "unauthorized", "status": 401}
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if node_name == "rate-limit":
                    payload = {"error": "node proxy rate limited", "status": 429}
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if node_name == "structured-error":
                    payload = {
                        "error": "node proxy structured error",
                        "status": 431,
                        "errorCode": "proxy_structured_error",
                    }
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if node_name == "slow":
                    time.sleep(0.3)
                if node_name == "invalid-json":
                    body = b"this-is-not-json"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if node_name == "non-object-json":
                    body = b"[1,2,3]"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if node_name == "empty-body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parsed.path.startswith("/status") and relay_mode == "invalid-json":
                    body = b"this-is-not-json"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path.startswith("/status") and relay_mode == "non-object-json":
                    body = b"[1,2,3]"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path.startswith("/status") and relay_mode == "empty-body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parsed.path.startswith("/status"):
                    payload = {"running": True, "tabCount": 2, "lastTargetId": "tab-2"}
                elif parsed.path.startswith("/tabs"):
                    payload = {
                        "running": True,
                        "tabs": [{"targetId": "tab-2", "url": "https://example.com", "title": "Example"}],
                    }
                elif parsed.path.startswith("/snapshot"):
                    payload = {
                        "ok": True,
                        "targetId": "tab-2",
                        "format": "ai",
                        "snapshot": "relay snapshot",
                        "refs": {"e1": {"selector": "#login", "role": "button", "name": "Login"}},
                    }
                elif parsed.path.startswith("/console"):
                    payload = {
                        "ok": True,
                        "targetId": "tab-2",
                        "messages": [
                            {"level": "error", "text": "boom"},
                            {"level": "info", "text": "ok"},
                        ],
                    }
                else:
                    payload = {"ok": True, "via": "e2e-http"}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                captured["path"] = self.path
                post_paths = captured.get("post_paths")
                if isinstance(post_paths, list):
                    post_paths.append(self.path)
                captured["relay_token"] = self.headers.get("X-OpenHeron-Browser-Relay-Token", "")
                body_len = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(body_len).decode("utf-8", errors="replace") if body_len > 0 else ""
                captured["body"] = raw
                if self.path.startswith("/navigate"):
                    payload = {
                        "ok": True,
                        "targetId": "tab-2",
                        "url": "https://example.org",
                        "title": "Example Org",
                    }
                elif self.path.startswith("/tabs/open"):
                    payload = {"ok": True, "targetId": "tab-3", "url": "https://opened.example"}
                elif self.path.startswith("/tabs/focus"):
                    payload = {"ok": True, "targetId": "tab-3", "focused": True}
                elif self.path.startswith("/tabs/close"):
                    payload = {"ok": True, "targetId": "tab-3", "closed": True}
                elif self.path.startswith("/act"):
                    request_payload = json.loads(raw) if raw else {}
                    act_request = request_payload.get("request", {}) if isinstance(request_payload, dict) else {}
                    kind = str(act_request.get("kind", "")).strip().lower()
                    if kind == "press" and str(act_request.get("key", "")).strip().upper() == "AUTH_REQUIRED":
                        if captured.get("relay_token") != "relay-token-ok":
                            payload = {"error": "unauthorized", "status": 401}
                            body = json.dumps(payload).encode("utf-8")
                            self.send_response(401)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            return
                    if kind == "wait" and int(act_request.get("timeMs", 0) or 0) >= 60000:
                        time.sleep(0.3)
                        payload = {"ok": True, "kind": "wait"}
                        body = json.dumps(payload).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if kind == "press" and str(act_request.get("key", "")).strip().upper() == "RATE_LIMIT":
                        payload = {"error": "rate limited", "status": 429}
                        body = json.dumps(payload).encode("utf-8")
                        self.send_response(429)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if kind == "press" and str(act_request.get("key", "")).strip().upper() == "STRUCTURED_STATUS":
                        payload = {"error": "structured relay failure", "status": 418}
                        body = json.dumps(payload).encode("utf-8")
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    payload = {
                        "ok": True,
                        "targetId": request_payload.get("targetId"),
                        "kind": kind or "unknown",
                    }
                elif self.path.startswith("/upload"):
                    payload = {"error": "not found", "status": 404}
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                elif self.path.startswith("/hooks/file-chooser"):
                    payload = {"ok": True, "uploaded": True}
                elif self.path.startswith("/dialog"):
                    payload = {"error": "not found", "status": 404}
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                elif self.path.startswith("/hooks/dialog"):
                    payload = {"ok": True, "armed": True}
                elif self.path.startswith("/screenshot"):
                    payload = {
                        "ok": True,
                        "targetId": "tab-2",
                        "type": "png",
                        "contentType": "image/png",
                        "imageBase64": "aGVsbG8=",
                    }
                elif self.path.startswith("/pdf"):
                    payload = {
                        "ok": True,
                        "targetId": "tab-2",
                        "contentType": "application/pdf",
                        "pdfBase64": "aGVsbG8=",
                    }
                else:
                    payload = {"ok": True}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1.0)
        configure_browser_runtime(None)
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _server_base(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def _configure_relay_runtime(self, *, token: str | None = None) -> None:
        os.environ["OPENHERON_BROWSER_RUNTIME"] = "playwright"
        os.environ["OPENHERON_BROWSER_CHROME_RELAY_URL"] = self._server_base()
        if token is None:
            os.environ.pop("OPENHERON_BROWSER_CHROME_RELAY_TOKEN", None)
        else:
            os.environ["OPENHERON_BROWSER_CHROME_RELAY_TOKEN"] = token
        configure_browser_runtime(None)

    def test_browser_node_proxy_minimal_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_NODE_PROXY_TOKEN"] = "node-token-e2e"

        payload = json.loads(
            browser(
                action="status",
                target="node",
                node="node-e2e",
                timeout_ms=1200,
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["via"], "e2e-http")
        self.assertEqual(payload["target"], "node")
        self.assertEqual(payload["capability"]["backend"], "node-proxy")
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])
        self.assertTrue(self._captured["path"].startswith("/"))
        self.assertIn("node=node-e2e", self._captured["path"])
        self.assertIn("timeoutMs=1200", self._captured["path"])
        self.assertEqual(self._captured["proxy_token"], "node-token-e2e")

    def test_browser_node_proxy_status_includes_recommendations_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["snapshot", "status", "profiles", "act"]}}
        )
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_ORDER_JSON"] = json.dumps(
            ["act", "profiles", "status", "snapshot"]
        )
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_LIMIT"] = "2"

        payload = json.loads(browser(action="status", target="node"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "node")
        self.assertEqual(payload["supportedActions"], ["act", "profiles", "status", "snapshot"])
        self.assertEqual(payload["recommendedActions"], ["act", "profiles"])
        self.assertEqual(payload["capability"]["recommendedOrder"], ["act", "profiles", "status", "snapshot"])

    def test_browser_node_proxy_profiles_normalizes_shape_and_recommendations_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["profiles", "status", "tabs"]}}
        )
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_ORDER_JSON"] = json.dumps(
            ["profiles", "status", "tabs"]
        )
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_LIMIT"] = "2"

        payload = json.loads(browser(action="profiles", target="node"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "node")
        self.assertEqual(payload["profiles"], [])
        self.assertEqual(payload["supportedActions"], ["profiles", "status", "tabs"])
        self.assertEqual(payload["recommendedActions"], ["profiles", "status"])
        self.assertEqual(payload["capability"]["recommendedOrder"], ["profiles", "status", "tabs"])

    def test_browser_sandbox_proxy_minimal_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_TOKEN"] = "sandbox-token-e2e"

        payload = json.loads(browser(action="status", target="sandbox", timeout_ms=900))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["via"], "e2e-http")
        self.assertEqual(payload["target"], "sandbox")
        self.assertEqual(payload["capability"]["backend"], "sandbox-proxy")
        self.assertIn("timeoutMs=900", self._captured["path"])
        self.assertEqual(self._captured["proxy_token"], "sandbox-token-e2e")

    def test_browser_sandbox_proxy_blocks_unsupported_action_by_capability_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "profiles"]}}
        )

        payload = json.loads(browser(action="navigate", target="sandbox", target_url="https://example.com"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 501)
        self.assertIn('not supported by target "sandbox"', payload["error"])
        self.assertEqual(payload["supportedActions"], ["profiles", "status"])
        self.assertIn("action=status or action=profiles", payload["hint"])

    def test_browser_node_proxy_invalid_capability_json_warns_and_falls_back_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = "{not-json"

        payload = json.loads(browser(action="status", target="node"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "node")
        self.assertEqual(payload["capability"]["backend"], "node-proxy")
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])
        self.assertIn("capabilityWarnings", payload)
        self.assertIn("invalid JSON", payload["capabilityWarnings"][0])

    def test_browser_node_proxy_non_object_capability_json_warns_and_falls_back_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(["not-an-object"])

        payload = json.loads(browser(action="status", target="node"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "node")
        self.assertEqual(payload["capability"]["backend"], "node-proxy")
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])
        self.assertIn("capabilityWarnings", payload)
        self.assertIn("must be a JSON object", payload["capabilityWarnings"][0])

    def test_browser_node_proxy_invalid_capability_error_codes_shape_warns_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status"], "errorCodes": "bad-shape"}}
        )

        payload = json.loads(browser(action="status", target="node"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "node")
        self.assertEqual(payload["capability"]["supportedActions"], ["status"])
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])
        self.assertIn("capabilityWarnings", payload)
        self.assertIn("errorCodes", payload["capabilityWarnings"][0])

    def test_browser_sandbox_proxy_invalid_capability_json_warns_and_falls_back_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_CAPABILITY_JSON"] = "{bad-json"

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "sandbox")
        self.assertEqual(payload["capability"]["backend"], "sandbox-proxy")
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])
        self.assertIn("capabilityWarnings", payload)
        self.assertIn("invalid JSON", payload["capabilityWarnings"][0])

    def test_browser_sandbox_proxy_non_object_capability_json_warns_and_falls_back_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_CAPABILITY_JSON"] = json.dumps(["not-an-object"])

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "sandbox")
        self.assertEqual(payload["capability"]["backend"], "sandbox-proxy")
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])
        self.assertIn("capabilityWarnings", payload)
        self.assertIn("must be a JSON object", payload["capabilityWarnings"][0])

    def test_browser_sandbox_proxy_invalid_capability_error_codes_shape_warns_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status"], "errorCodes": "bad-shape"}}
        )

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "sandbox")
        self.assertEqual(payload["capability"]["supportedActions"], ["status"])
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])
        self.assertIn("capabilityWarnings", payload)
        self.assertIn("errorCodes", payload["capabilityWarnings"][0])

    def test_browser_sandbox_proxy_status_includes_recommendations_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["snapshot", "status", "profiles", "act"]}}
        )
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_ORDER_JSON"] = json.dumps(
            ["profiles", "status", "act", "snapshot"]
        )
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_LIMIT"] = "3"

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "sandbox")
        self.assertEqual(payload["supportedActions"], ["profiles", "status", "act", "snapshot"])
        self.assertEqual(payload["recommendedActions"], ["profiles", "status", "act"])
        self.assertEqual(payload["capability"]["recommendedOrder"], ["profiles", "status", "act", "snapshot"])

    def test_browser_sandbox_proxy_recommendation_limit_is_clamped_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "profiles", "tabs"]}}
        )
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_ORDER_JSON"] = json.dumps(
            ["status", "profiles", "tabs"]
        )
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_LIMIT"] = "0"

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["supportedActions"], ["status", "profiles", "tabs"])
        self.assertEqual(payload["recommendedActions"], ["status"])

    def test_browser_sandbox_proxy_profiles_normalizes_shape_and_recommendations_e2e(self) -> None:
        configure_browser_runtime(None)
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["profiles", "status", "tabs"]}}
        )
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_ORDER_JSON"] = json.dumps(
            ["profiles", "status", "tabs"]
        )
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_LIMIT"] = "2"

        payload = json.loads(browser(action="profiles", target="sandbox"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "sandbox")
        self.assertEqual(payload["profiles"], [])
        self.assertEqual(payload["supportedActions"], ["profiles", "status", "tabs"])
        self.assertEqual(payload["recommendedActions"], ["profiles", "status"])
        self.assertEqual(payload["capability"]["recommendedOrder"], ["profiles", "status", "tabs"])

    def test_browser_sandbox_proxy_invalid_json_mapping_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_TOKEN"] = "mode-invalid-json"

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 502)
        self.assertEqual(payload["errorCode"], "proxy_invalid_json")
        self.assertIn("invalid proxy response", payload["error"].lower())

    def test_browser_sandbox_proxy_non_object_json_mapping_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_TOKEN"] = "mode-non-object-json"

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 502)
        self.assertEqual(payload["errorCode"], "proxy_invalid_payload_type")
        self.assertIn("invalid proxy response", payload["error"].lower())

    def test_browser_sandbox_proxy_timeout_mapping_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_TOKEN"] = "mode-slow"

        payload = json.loads(browser(action="status", target="sandbox", timeout_ms=50))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 504)
        self.assertEqual(payload["errorCode"], "proxy_timeout")
        self.assertIn("timeout", payload["error"].lower())

    def test_browser_sandbox_proxy_connection_refused_mapping_e2e(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        _, free_port = probe.getsockname()
        probe.close()

        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://127.0.0.1:{free_port}"

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 503)
        self.assertEqual(payload["errorCode"], "proxy_connection_refused")
        self.assertIn("connection refused", payload["error"].lower())

    def test_browser_sandbox_proxy_http_error_uses_structured_status_and_error_code_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_TOKEN"] = "mode-structured-error"

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 431)
        self.assertEqual(payload["errorCode"], "proxy_structured_error")
        self.assertIn("structured error", payload["error"].lower())

    def test_browser_sandbox_proxy_auth_error_mapping_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        self._captured["proxy_mode"] = "auth-required"

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 401)
        self.assertEqual(payload["errorCode"], "proxy_http_error")
        self.assertIn("unauthorized", payload["error"].lower())

    def test_browser_sandbox_proxy_auth_success_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_TOKEN"] = "node-token-ok"
        self._captured["proxy_mode"] = "auth-required"

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "sandbox")

    def test_browser_sandbox_proxy_empty_body_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_TOKEN"] = "mode-empty-body"

        payload = json.loads(browser(action="status", target="sandbox"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "sandbox")
        self.assertEqual(payload["capability"]["backend"], "sandbox-proxy")
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])

    def test_browser_chrome_relay_status_minimal_e2e(self) -> None:
        self._configure_relay_runtime(token="relay-token-e2e")

        payload = json.loads(browser(action="status", profile="chrome"))

        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["running"])
        self.assertEqual(payload["profile"], "chrome")
        self.assertEqual(payload["transport"], "relay")
        self.assertEqual(payload["tabCount"], 2)
        self.assertEqual(payload["lastTargetId"], "tab-2")
        self.assertEqual(payload["capability"]["backend"], "extension-relay")
        self.assertEqual(self._captured["relay_token"], "relay-token-e2e")

    def test_browser_chrome_relay_status_invalid_json_mapping_e2e(self) -> None:
        self._captured["relay_mode"] = "invalid-json"
        self._configure_relay_runtime()

        payload = json.loads(browser(action="status", profile="chrome"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 502)
        self.assertEqual(payload["errorCode"], "relay_invalid_json")
        self.assertIn("invalid json", payload["error"].lower())

    def test_browser_chrome_relay_status_non_object_json_mapping_e2e(self) -> None:
        self._captured["relay_mode"] = "non-object-json"
        self._configure_relay_runtime()

        payload = json.loads(browser(action="status", profile="chrome"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 502)
        self.assertEqual(payload["errorCode"], "relay_non_object_json")
        self.assertIn("non-object", payload["error"].lower())

    def test_browser_chrome_relay_status_empty_body_e2e(self) -> None:
        self._captured["relay_mode"] = "empty-body"
        self._configure_relay_runtime()

        payload = json.loads(browser(action="status", profile="chrome"))

        self.assertTrue(payload["enabled"])
        self.assertFalse(payload["running"])
        self.assertEqual(payload["profile"], "chrome")
        self.assertEqual(payload["transport"], "relay")
        self.assertEqual(payload["tabCount"], 0)
        self.assertEqual(payload["capability"]["backend"], "extension-relay")

    def test_browser_chrome_relay_tabs_minimal_e2e(self) -> None:
        self._configure_relay_runtime()

        payload = json.loads(browser(action="tabs", profile="chrome"))

        self.assertTrue(payload["running"])
        self.assertEqual(payload["profile"], "chrome")
        self.assertEqual(payload["mode"], "relay")
        self.assertEqual(payload["tabs"][0]["targetId"], "tab-2")

    def test_browser_chrome_relay_snapshot_minimal_e2e(self) -> None:
        self._configure_relay_runtime()

        payload = json.loads(
            browser(
                action="snapshot",
                profile="chrome",
                target_id="tab-2",
                snapshot_format="ai",
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["profile"], "chrome")
        self.assertEqual(payload["mode"], "relay")
        self.assertEqual(payload["targetId"], "tab-2")
        self.assertIn("e1", payload["refs"])

    def test_browser_chrome_relay_navigate_minimal_e2e(self) -> None:
        self._configure_relay_runtime(token="relay-token-e2e")

        payload = json.loads(
            browser(
                action="navigate",
                profile="chrome",
                target_id="tab-2",
                target_url="https://example.org",
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["profile"], "chrome")
        self.assertEqual(payload["mode"], "relay")
        self.assertEqual(payload["targetId"], "tab-2")
        self.assertEqual(payload["url"], "https://example.org")
        self.assertEqual(self._captured["relay_token"], "relay-token-e2e")
        sent = json.loads(self._captured["body"])
        self.assertEqual(sent["targetId"], "tab-2")
        self.assertEqual(sent["url"], "https://example.org")

    def test_browser_chrome_relay_act_click_minimal_e2e(self) -> None:
        self._configure_relay_runtime()

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "click", "selector": "#login"}),
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["kind"], "click")
        sent = json.loads(self._captured["body"])
        self.assertEqual(sent["targetId"], "tab-2")
        self.assertEqual(sent["request"]["kind"], "click")
        self.assertEqual(sent["request"]["selector"], "#login")

    def test_browser_chrome_relay_act_type_minimal_e2e(self) -> None:
        self._configure_relay_runtime()

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "type", "selector": "#email", "text": "user@example.com"}),
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["kind"], "type")
        sent = json.loads(self._captured["body"])
        self.assertEqual(sent["request"]["kind"], "type")
        self.assertEqual(sent["request"]["selector"], "#email")
        self.assertEqual(sent["request"]["text"], "user@example.com")

    def test_browser_chrome_relay_act_extended_kinds_minimal_e2e(self) -> None:
        self._configure_relay_runtime()
        browser(action="snapshot", profile="chrome", target_id="tab-2")

        cases = [
            (
                {"kind": "open", "url": "https://example.net"},
                lambda req: self.assertEqual(req["url"], "https://example.net"),
            ),
            (
                {"kind": "press", "key": "  Enter  "},
                lambda req: self.assertEqual(req["key"], "Enter"),
            ),
            (
                {"kind": "wait", "timeMs": 1234},
                lambda req: self.assertEqual(req["timeMs"], 1234),
            ),
            (
                {"kind": "hover", "ref": "e1"},
                lambda req: self.assertEqual(req["selector"], "#login"),
            ),
            (
                {"kind": "select", "selector": "#country", "values": [" CN ", "US"]},
                lambda req: self.assertEqual(req["values"], ["CN", "US"]),
            ),
            (
                {"kind": "drag", "startRef": "e1", "endSelector": "#drop"},
                lambda req: self.assertEqual(req["startSelector"], "#login"),
            ),
            (
                {"kind": "fill", "fields": [{"ref": "e1", "text": "hello"}]},
                lambda req: self.assertEqual(req["fields"][0]["selector"], "#login"),
            ),
            (
                {"kind": "resize", "width": 1280, "height": 720},
                lambda req: self.assertEqual((req["width"], req["height"]), (1280, 720)),
            ),
            (
                {"kind": "close"},
                lambda req: self.assertEqual(req["kind"], "close"),
            ),
        ]

        for request, assertion in cases:
            with self.subTest(kind=request["kind"]):
                payload = json.loads(
                    browser(
                        action="act",
                        profile="chrome",
                        target_id="tab-2",
                        request=json.dumps(request),
                    )
                )
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["kind"], request["kind"])
                sent = json.loads(self._captured["body"])
                self.assertEqual(sent["targetId"], "tab-2")
                self.assertEqual(sent["request"]["kind"], request["kind"])
                assertion(sent["request"])

    def test_browser_chrome_relay_act_evaluate_minimal_e2e(self) -> None:
        self._configure_relay_runtime()
        os.environ["OPENHERON_BROWSER_CHROME_RELAY_EVALUATE_ENABLED"] = "1"

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "evaluate", "fn": "() => document.title"}),
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["kind"], "evaluate")
        sent = json.loads(self._captured["body"])
        self.assertEqual(sent["request"]["kind"], "evaluate")
        self.assertEqual(sent["request"]["fn"], "() => document.title")

    def test_browser_chrome_relay_act_evaluate_disabled_e2e(self) -> None:
        self._configure_relay_runtime()

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "evaluate", "fn": "() => 1"}),
            )
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 501)
        self.assertEqual(payload["errorCode"], "browser_not_implemented")
        self.assertIn("evaluate is disabled", payload["error"])
        self.assertEqual(self._captured["post_paths"], [])

    def test_browser_chrome_relay_act_evaluate_too_long_e2e(self) -> None:
        self._configure_relay_runtime()
        os.environ["OPENHERON_BROWSER_CHROME_RELAY_EVALUATE_ENABLED"] = "1"
        os.environ["OPENHERON_BROWSER_CHROME_RELAY_EVALUATE_MAX_CHARS"] = "8"

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "evaluate", "fn": "() => 123456789"}),
            )
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 400)
        self.assertEqual(payload["errorCode"], "browser_bad_request")
        self.assertIn("too long", payload["error"].lower())
        self.assertEqual(self._captured["post_paths"], [])

    def test_browser_chrome_relay_act_body_too_large_e2e(self) -> None:
        self._configure_relay_runtime()
        os.environ["OPENHERON_BROWSER_CHROME_RELAY_MAX_BODY_BYTES"] = "256"
        os.environ["OPENHERON_BROWSER_CHROME_RELAY_TYPE_MAX_CHARS"] = "200000"

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "type", "selector": "#email", "text": "x" * 1200}),
            )
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 400)
        self.assertEqual(payload["errorCode"], "relay_body_too_large")
        self.assertIn("too large", payload["error"].lower())
        self.assertEqual(self._captured["post_paths"], [])

    def test_browser_chrome_relay_act_validation_errors_e2e(self) -> None:
        self._configure_relay_runtime()

        cases = [
            ("press-missing-key", {"kind": "press"}, "request.key is required"),
            ("select-missing-values", {"kind": "select", "selector": "#country"}, "request.values"),
            ("drag-missing-end", {"kind": "drag", "startSelector": "#from"}, "request.start"),
            ("fill-empty-fields", {"kind": "fill", "fields": []}, "request.fields"),
            ("resize-missing-height", {"kind": "resize", "width": 1280}, "request.width"),
            ("open-missing-url", {"kind": "open"}, "request.url is required"),
        ]

        for _name, request, error_fragment in cases:
            with self.subTest(case=_name):
                payload = json.loads(
                    browser(
                        action="act",
                        profile="chrome",
                        target_id="tab-2",
                        request=json.dumps(request),
                    )
                )
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["status"], 400)
                self.assertEqual(payload["errorCode"], "browser_bad_request")
                self.assertIn(error_fragment.lower(), payload["error"].lower())
                self.assertEqual(self._captured["post_paths"], [])

    def test_browser_chrome_relay_act_http_error_mapping_e2e(self) -> None:
        self._configure_relay_runtime()

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "press", "key": "RATE_LIMIT"}),
            )
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 429)
        self.assertEqual(payload["errorCode"], "relay_http_error")
        self.assertIn("rate limited", payload["error"])

    def test_browser_chrome_relay_act_http_error_uses_structured_status_e2e(self) -> None:
        self._configure_relay_runtime()

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "press", "key": "STRUCTURED_STATUS"}),
            )
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 418)
        self.assertEqual(payload["errorCode"], "relay_http_error")
        self.assertIn("structured relay failure", payload["error"].lower())

    def test_browser_chrome_relay_act_timeout_mapping_e2e(self) -> None:
        self._configure_relay_runtime()
        os.environ["OPENHERON_BROWSER_CHROME_RELAY_TIMEOUT_MS"] = "50"

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "wait", "timeMs": 99999}),
            )
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 504)
        self.assertEqual(payload["errorCode"], "relay_timeout")
        self.assertIn("timeout", payload["error"].lower())

    def test_browser_chrome_relay_connection_refused_mapping_e2e(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        _, free_port = probe.getsockname()
        probe.close()

        os.environ["OPENHERON_BROWSER_RUNTIME"] = "playwright"
        os.environ["OPENHERON_BROWSER_CHROME_RELAY_URL"] = f"http://127.0.0.1:{free_port}"
        configure_browser_runtime(None)

        payload = json.loads(browser(action="status", profile="chrome"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 503)
        self.assertEqual(payload["errorCode"], "relay_connection_refused")
        self.assertIn("connection refused", payload["error"].lower())

    def test_browser_chrome_relay_dns_failed_mapping_e2e(self) -> None:
        os.environ["OPENHERON_BROWSER_RUNTIME"] = "playwright"
        os.environ["OPENHERON_BROWSER_CHROME_RELAY_URL"] = "http://does-not-exist-relay-host.invalid:9800"
        configure_browser_runtime(None)

        payload = json.loads(browser(action="status", profile="chrome"))

        self.assertFalse(payload["ok"])
        # Some environments return direct DNS failures; others surface a 502 from
        # upstream resolvers/proxies for non-existent hosts; in some sandboxes
        # this can also manifest as timeout.
        if payload.get("errorCode") == "relay_dns_failed":
            self.assertEqual(payload["status"], 503)
            self.assertIn("dns", payload["error"].lower())
        elif payload.get("errorCode") == "relay_timeout":
            self.assertEqual(payload["status"], 504)
            self.assertIn("timeout", payload["error"].lower())
        else:
            self.assertEqual(payload["status"], 502)
            self.assertEqual(payload["errorCode"], "relay_http_error")

    def test_browser_node_proxy_http_error_mapping_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"

        payload = json.loads(browser(action="status", target="node", node="rate-limit"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 429)
        self.assertEqual(payload["errorCode"], "proxy_http_error")
        self.assertIn("rate limited", payload["error"])

    def test_browser_node_proxy_timeout_mapping_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"

        payload = json.loads(browser(action="status", target="node", node="slow", timeout_ms=50))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 504)
        self.assertEqual(payload["errorCode"], "proxy_timeout")
        self.assertIn("timeout", payload["error"].lower())

    def test_browser_node_proxy_invalid_json_mapping_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"

        payload = json.loads(browser(action="status", target="node", node="invalid-json"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 502)
        self.assertEqual(payload["errorCode"], "proxy_invalid_json")
        self.assertIn("invalid proxy response", payload["error"].lower())

    def test_browser_node_proxy_non_object_json_mapping_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"

        payload = json.loads(browser(action="status", target="node", node="non-object-json"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 502)
        self.assertEqual(payload["errorCode"], "proxy_invalid_payload_type")
        self.assertIn("invalid proxy response", payload["error"].lower())

    def test_browser_node_proxy_empty_body_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"

        payload = json.loads(browser(action="status", target="node", node="empty-body"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["capability"]["backend"], "node-proxy")
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])

    def test_browser_node_proxy_http_error_uses_structured_status_and_error_code_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"

        payload = json.loads(browser(action="status", target="node", node="structured-error"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 431)
        self.assertEqual(payload["errorCode"], "proxy_structured_error")
        self.assertIn("structured error", payload["error"].lower())

    def test_browser_node_proxy_auth_error_mapping_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"

        payload = json.loads(browser(action="status", target="node", node="auth-required"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 401)
        self.assertEqual(payload["errorCode"], "proxy_http_error")
        self.assertIn("unauthorized", payload["error"].lower())

    def test_browser_node_proxy_auth_success_e2e(self) -> None:
        host, port = self._server.server_address
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = f"http://{host}:{port}"
        os.environ["OPENHERON_BROWSER_NODE_PROXY_TOKEN"] = "node-token-ok"

        payload = json.loads(browser(action="status", target="node", node="auth-required"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "node")

    def test_browser_chrome_relay_auth_error_mapping_e2e(self) -> None:
        self._configure_relay_runtime()

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "press", "key": "AUTH_REQUIRED"}),
            )
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 401)
        self.assertEqual(payload["errorCode"], "relay_http_error")
        self.assertIn("unauthorized", payload["error"].lower())

    def test_browser_chrome_relay_auth_success_e2e(self) -> None:
        self._configure_relay_runtime(token="relay-token-ok")

        payload = json.loads(
            browser(
                action="act",
                profile="chrome",
                target_id="tab-2",
                request=json.dumps({"kind": "press", "key": "AUTH_REQUIRED"}),
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["kind"], "press")

    def test_browser_chrome_relay_upload_fallback_e2e(self) -> None:
        self._configure_relay_runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "upload.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("demo")
            os.environ["OPENHERON_BROWSER_UPLOAD_ROOT"] = tmpdir
            payload = json.loads(browser(action="upload", profile="chrome", target_id="tab-2", paths=[path]))

        self.assertTrue(payload["ok"])
        post_paths = self._captured.get("post_paths")
        self.assertTrue(isinstance(post_paths, list))
        self.assertIn("/upload", post_paths)
        self.assertIn("/hooks/file-chooser", post_paths)

    def test_browser_chrome_relay_dialog_fallback_e2e(self) -> None:
        self._configure_relay_runtime()

        payload = json.loads(
            browser(
                action="dialog",
                profile="chrome",
                target_id="tab-2",
                accept=True,
                prompt_text="hello",
            )
        )

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["armed"])
        post_paths = self._captured.get("post_paths")
        self.assertTrue(isinstance(post_paths, list))
        self.assertIn("/dialog", post_paths)
        self.assertIn("/hooks/dialog", post_paths)

    def test_browser_chrome_relay_screenshot_save_e2e(self) -> None:
        self._configure_relay_runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OPENHERON_BROWSER_ARTIFACT_ROOT"] = tmpdir
            out_path = os.path.join(tmpdir, "shot.png")
            payload = json.loads(
                browser(
                    action="screenshot",
                    profile="chrome",
                    target_id="tab-2",
                    screenshot_path=out_path,
                    screenshot_type="png",
                )
            )
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["path"], os.path.realpath(out_path))
            with open(payload["path"], "rb") as f:
                self.assertEqual(f.read(), b"hello")

    def test_browser_chrome_relay_pdf_save_e2e(self) -> None:
        self._configure_relay_runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OPENHERON_BROWSER_ARTIFACT_ROOT"] = tmpdir
            out_path = os.path.join(tmpdir, "tab-2.pdf")
            payload = json.loads(
                browser(
                    action="pdf",
                    profile="chrome",
                    target_id="tab-2",
                    pdf_path=out_path,
                )
            )
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["contentType"], "application/pdf")
            self.assertEqual(payload["bytes"], 5)
            self.assertEqual(payload["path"], os.path.realpath(out_path))
            with open(payload["path"], "rb") as f:
                self.assertEqual(f.read(), b"hello")

    def test_browser_chrome_relay_console_save_e2e(self) -> None:
        self._configure_relay_runtime()

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OPENHERON_BROWSER_ARTIFACT_ROOT"] = tmpdir
            out_path = os.path.join(tmpdir, "tab-2.console.json")
            payload = json.loads(
                browser(
                    action="console",
                    profile="chrome",
                    target_id="tab-2",
                    console_level="error",
                    console_path=out_path,
                )
            )
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["path"], os.path.realpath(out_path))
            with open(payload["path"], "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertTrue(isinstance(saved.get("messages"), list))
            self.assertEqual(saved["messages"][0]["level"], "error")

    def test_browser_chrome_relay_open_focus_close_e2e(self) -> None:
        self._configure_relay_runtime()

        opened = json.loads(browser(action="open", profile="chrome", target_url="https://opened.example"))
        focused = json.loads(browser(action="focus", profile="chrome", target_id="tab-3"))
        closed = json.loads(browser(action="close", profile="chrome", target_id="tab-3"))

        self.assertTrue(opened["ok"])
        self.assertEqual(opened["targetId"], "tab-3")
        self.assertTrue(focused["ok"])
        self.assertTrue(focused["focused"])
        self.assertTrue(closed["ok"])
        self.assertTrue(closed["closed"])


if __name__ == "__main__":
    unittest.main()
