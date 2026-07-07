"""Tests for the declarative Node API subprocess runner wrapper."""

from __future__ import annotations

import json
import os
import unittest
from io import StringIO
from unittest.mock import patch

from openppx.runtime import node_api_runner


class NodeApiRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_prepare_payload_env_loads_combined_payload(self) -> None:
        payload = {
            "recipe": {"module": "demo_node.cjs", "function": "add"},
            "args": {"a": 2, "b": 3},
        }
        os.environ["OPENPPX_API_RUNNER_PAYLOAD_JSON"] = json.dumps(payload)

        node_api_runner._prepare_payload_env()

        recipe = json.loads(os.environ["OPENPPX_NODE_API_RECIPE_JSON"])
        args = json.loads(os.environ["OPENPPX_SKILL_ARGS_JSON"])
        self.assertEqual(recipe["module"], "demo_node.cjs")
        self.assertEqual(recipe["function"], "add")
        self.assertEqual(args, {"a": 2, "b": 3})

    def test_main_reports_node_missing_before_payload_validation(self) -> None:
        out = StringIO()

        with patch("openppx.runtime.node_api_runner.shutil.which", return_value=None), patch("sys.stdout", out):
            exit_code = node_api_runner.main()

        emitted = json.loads(out.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(emitted["ok"])
        self.assertEqual(emitted["error_type"], "NodeNotFound")


if __name__ == "__main__":
    unittest.main()
