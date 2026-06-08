"""Tests for the declarative Python API subprocess runner."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from openppx.runtime import python_api_runner


class PythonApiRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_main_imports_skill_module_and_preserves_typed_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo_sdk.py").write_text(
                "def build(a, nested, label):\n"
                "    return {'total': a + nested['b'], 'label': label, "
                "'types': [type(a).__name__, type(nested).__name__]}\n",
                encoding="utf-8",
            )
            recipe = {
                "module": "demo_sdk",
                "function": "build",
                "kwargs": {
                    "a": "{a}",
                    "nested": "{nested}",
                    "label": "item-{a}",
                },
            }
            os.environ["OPENPPX_PYTHON_API_RECIPE_JSON"] = json.dumps(recipe)
            os.environ["OPENPPX_SKILL_ARGS_JSON"] = json.dumps({"a": 2, "nested": {"b": 3}})
            out = StringIO()

            with patch("sys.stdout", out), patch("os.getcwd", return_value=str(root)):
                exit_code = python_api_runner.main()

        emitted = json.loads(out.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(emitted["ok"])
        self.assertEqual(emitted["result"]["total"], 5)
        self.assertEqual(emitted["result"]["label"], "item-2")
        self.assertEqual(emitted["result"]["types"], ["int", "dict"])

    def test_main_supports_callable_ref_and_full_args_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo_callable_sdk.py").write_text(
                "class Client:\n"
                "    @staticmethod\n"
                "    def run(payload):\n"
                "        return {'keys': sorted(payload.keys()), 'limit_type': type(payload['limit']).__name__}\n",
                encoding="utf-8",
            )
            recipe = {"callable": "demo_callable_sdk:Client.run", "args": ["{args}"]}
            os.environ["OPENPPX_PYTHON_API_RECIPE_JSON"] = json.dumps(recipe)
            os.environ["OPENPPX_SKILL_ARGS_JSON"] = json.dumps({"query": "hello", "limit": 3})
            out = StringIO()

            with patch("sys.stdout", out), patch("os.getcwd", return_value=str(root)):
                exit_code = python_api_runner.main()

        emitted = json.loads(out.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(emitted["ok"])
        self.assertEqual(emitted["result"]["keys"], ["limit", "query"])
        self.assertEqual(emitted["result"]["limit_type"], "int")

    def test_main_rejects_unsafe_module_name(self) -> None:
        os.environ["OPENPPX_PYTHON_API_RECIPE_JSON"] = json.dumps(
            {"module": "../demo_sdk", "function": "run"}
        )
        out = StringIO()

        with patch("sys.stdout", out):
            exit_code = python_api_runner.main()

        emitted = json.loads(out.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(emitted["ok"])
        self.assertIn("module", emitted["error"])

    def test_main_rejects_non_skill_local_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_PYTHON_API_RECIPE_JSON"] = json.dumps(
                {"module": "json", "function": "loads", "args": ["{}"]}
            )
            out = StringIO()

            with patch("sys.stdout", out), patch("os.getcwd", return_value=tmp):
                exit_code = python_api_runner.main()

        emitted = json.loads(out.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(emitted["ok"])
        self.assertIn("skill root", emitted["error"])


if __name__ == "__main__":
    unittest.main()
