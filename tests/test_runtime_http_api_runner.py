"""Tests for the declarative HTTP API subprocess runner."""

from __future__ import annotations

import json
import os
import unittest
from email.message import Message
from io import StringIO
from unittest.mock import patch

from openppx.runtime import http_api_runner


class HttpApiRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_main_renders_recipe_and_emits_response(self) -> None:
        headers = Message()
        headers["Content-Type"] = "text/plain; charset=utf-8"
        fake_response = _FakeResponse(status=200, headers=headers, body=b"created")
        recipe = {
            "method": "POST",
            "url": "https://api.example.test/items/{item_id}",
            "query": {"q": "{query}"},
            "headers": {"X-User": "{user.name}"},
            "body_from_args": True,
        }
        args = {"item_id": "42", "query": "hello world", "user": {"name": "Ada"}}
        os.environ["OPENPPX_HTTP_API_RECIPE_JSON"] = json.dumps(recipe)
        os.environ["OPENPPX_SKILL_ARGS_JSON"] = json.dumps(args)
        out = StringIO()

        with patch.object(http_api_runner, "urlopen", return_value=fake_response) as mocked_urlopen:
            with patch("sys.stdout", out):
                exit_code = http_api_runner.main()

        request = mocked_urlopen.call_args.args[0]
        timeout = mocked_urlopen.call_args.kwargs["timeout"]
        emitted = json.loads(out.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.example.test/items/42?q=hello+world")
        self.assertEqual(request.get_header("X-user"), "Ada")
        self.assertEqual(json.loads(request.data.decode("utf-8")), args)
        self.assertGreater(timeout, 0)
        self.assertTrue(emitted["ok"])
        self.assertEqual(emitted["status_code"], 200)
        self.assertEqual(emitted["body"], "created")

    def test_main_rejects_non_http_url(self) -> None:
        os.environ["OPENPPX_HTTP_API_RECIPE_JSON"] = json.dumps({"url": "file:///tmp/data"})
        out = StringIO()

        with patch("sys.stdout", out):
            exit_code = http_api_runner.main()

        emitted = json.loads(out.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(emitted["ok"])
        self.assertIn("http or https", emitted["error"])


class _FakeResponse:
    def __init__(self, *, status: int, headers: Message, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._body
        return self._body[:size]


if __name__ == "__main__":
    unittest.main()
