"""Local HTTP fixture for browser remote provider contract tests."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from openppx.runtime.checkpoint_migration_catalog import (
    OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA,
    OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION,
)


@dataclass
class BrowserRemoteFixtureJob:
    """Mutable state for one fake browser remote job."""

    job_id: str
    status: str = "running"
    output: str = "fixture output"
    summary: str = "Fixture browser remote job is running."
    checkpoint_cursor: int = 1
    calls: list[dict[str, Any]] = field(default_factory=list)


class BrowserRemoteProviderFixture:
    """Start a local browser remote provider compatible with the contract harness."""

    def __init__(self, *, job_id: str = "fixture-job-1", token: str = "fixture-token") -> None:
        self.job = BrowserRemoteFixtureJob(job_id=job_id)
        self.token = token
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "BrowserRemoteProviderFixture":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    @property
    def proxy_url(self) -> str:
        """Return the fixture provider base URL."""
        if self._server is None:
            raise RuntimeError("fixture provider is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def protocol_payload(self) -> dict[str, Any]:
        """Return the provider-declared browser remote job protocol."""
        return {
            "statusPath": "/jobs/{job_id}",
            "outputPath": "/jobs/{job_id}/output",
            "pausePath": "/jobs/{job_id}/pause",
            "resumePath": "/jobs/{job_id}/resume",
            "cancelPath": "/jobs/{job_id}/cancel",
            "checkpointPath": "checkpoint",
            "checkpointSchema": OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA,
            "checkpointSchemaVersion": OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION,
            "pollTimeoutMs": 2000,
        }

    @property
    def legacy_checkpoint_payload(self) -> dict[str, Any]:
        """Return a v1 checkpoint payload that should migrate through the default catalog."""
        return {
            "schema": OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA,
            "schemaVersion": 1,
            "jobId": self.job.job_id,
            "pageUrl": "https://example.test/fixture",
            "cursor": self.job.checkpoint_cursor,
        }

    def start(self) -> None:
        """Start the fixture server on localhost."""
        if self._server is not None:
            return
        fixture = self

        class _Handler(BaseHTTPRequestHandler):
            server_version = "OpenPpxBrowserRemoteProviderFixture/1.0"

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
                fixture._handle_request(self, method="GET")

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
                fixture._handle_request(self, method="POST")

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the fixture server."""
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._server = None
        self._thread = None

    def _handle_request(self, handler: BaseHTTPRequestHandler, *, method: str) -> None:
        parsed = urlparse(handler.path)
        body = self._read_body(handler)
        self.job.calls.append({"method": method, "path": parsed.path, "body": body})
        if self.token and handler.headers.get("X-OpenPPX-Browser-Proxy-Token") != self.token:
            self._send(handler, 401, {"error": "invalid token"})
            return
        job_id = _job_id_from_path_or_query(parsed.path, parsed.query)
        if job_id != self.job.job_id:
            self._send(handler, 404, {"error": "job not found"})
            return
        if method == "GET" and parsed.path == f"/jobs/{self.job.job_id}":
            self._send(handler, 200, self._status_payload())
            return
        if method == "GET" and parsed.path == f"/jobs/{self.job.job_id}/output":
            self._send(handler, 200, {"output": self.job.output})
            return
        if method == "POST" and parsed.path == f"/jobs/{self.job.job_id}/pause":
            self.job.status = "paused"
            self.job.summary = "Fixture browser remote job is paused."
            self._send(handler, 200, self._status_payload())
            return
        if method == "POST" and parsed.path == f"/jobs/{self.job.job_id}/resume":
            self.job.status = "running"
            self.job.summary = "Fixture browser remote job resumed."
            self._send(handler, 200, self._status_payload())
            return
        if method == "POST" and parsed.path == f"/jobs/{self.job.job_id}/cancel":
            self.job.status = "cancelled"
            self.job.summary = "Fixture browser remote job cancelled."
            self._send(handler, 200, self._status_payload())
            return
        self._send(handler, 404, {"error": "unsupported fixture endpoint"})

    def _status_payload(self) -> dict[str, Any]:
        return {
            "status": self.job.status,
            "summary": self.job.summary,
            "checkpoint": self.legacy_checkpoint_payload,
        }

    @staticmethod
    def _read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        length = int(handler.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = handler.rfile.read(length).decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return parsed if isinstance(parsed, dict) else {"payload": parsed}

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


def _job_id_from_path_or_query(path: str, query: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "jobs":
        return parts[1]
    values = parse_qs(query).get("job_id") or parse_qs(query).get("jobId") or [""]
    return str(values[0] or "")
