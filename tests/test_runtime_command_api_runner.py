"""Tests for the declarative command API subprocess runner."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from openppx.runtime import command_api_runner


class CommandApiRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_main_runs_sandboxed_command_recipe_through_docker(self) -> None:
        captured: dict[str, object] = {}

        def _fake_run_streaming_command(**kwargs):
            captured.update(kwargs)
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolved_root = root.resolve(strict=False)
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            recipe = {
                "argv": ["echo", "{value}"],
                "allow_system_executable": True,
                "env": {"VISIBLE_VALUE": "{value}"},
                "stdin": "{args}",
                "sandbox": {"required": True},
            }
            os.environ["OPENPPX_COMMAND_API_RECIPE_JSON"] = json.dumps(recipe)
            os.environ["OPENPPX_SKILL_ARGS_JSON"] = json.dumps({"value": "Ada"})
            os.environ["OPENAI_API_KEY"] = "host-secret"

            with (
                patch("openppx.runtime.command_api_runner._run_streaming_command", side_effect=_fake_run_streaming_command),
                patch("os.getcwd", return_value=str(root)),
            ):
                exit_code = command_api_runner.main()

        self.assertEqual(exit_code, 0)
        argv = captured["argv"]
        self.assertIsInstance(argv, list)
        assert isinstance(argv, list)
        self.assertEqual(argv[:2], ["docker", "run"])
        self.assertIn("-i", argv)
        self.assertIn("openppx-sandbox:dev", argv)
        self.assertEqual(argv[-2:], ["echo", "Ada"])
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "--mount"]
        self.assertIn(f"type=bind,src={resolved_root},dst={resolved_root}", mounts)
        self.assertIn(f"type=bind,src=/dev/null,dst={resolved_root / '.env'},readonly", mounts)
        self.assertEqual(json.loads(str(captured["stdin"])), {"value": "Ada"})
        self.assertIn("VISIBLE_VALUE=Ada", argv)
        self.assertNotIn("OPENAI_API_KEY=host-secret", argv)
        self.assertEqual(captured["timeout"], 3600)

    def test_main_rejects_sandbox_network_enablement_without_approval(self) -> None:
        recipe = {
            "argv": ["echo", "hello"],
            "allow_system_executable": True,
            "sandbox": {"required": True, "network": "enabled"},
        }
        os.environ["OPENPPX_COMMAND_API_RECIPE_JSON"] = json.dumps(recipe)
        out = StringIO()

        with patch("sys.stdout", out):
            exit_code = command_api_runner.main()

        emitted = json.loads(out.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(emitted["ok"])
        self.assertIn("network enablement", emitted["error"])

    def test_main_cleans_docker_container_after_sandbox_timeout(self) -> None:
        captured: dict[str, object] = {}

        def _fake_run_streaming_command(**kwargs):
            captured.update(kwargs)
            raise subprocess.TimeoutExpired(kwargs["argv"], kwargs["timeout"])

        recipe = {
            "argv": ["sleep", "10"],
            "allow_system_executable": True,
            "sandbox": "docker",
            "timeout_seconds": 5,
        }
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_COMMAND_API_RECIPE_JSON"] = json.dumps(recipe)
            with (
                patch("openppx.runtime.command_api_runner._run_streaming_command", side_effect=_fake_run_streaming_command),
                patch("openppx.runtime.command_api_runner.cleanup_docker_sandbox_container") as mocked_cleanup,
                patch("os.getcwd", return_value=tmp),
                patch("sys.stdout", StringIO()) as out,
            ):
                exit_code = command_api_runner.main()

        argv = captured["argv"]
        self.assertIsInstance(argv, list)
        assert isinstance(argv, list)
        container_name = argv[argv.index("--name") + 1]
        self.assertEqual(exit_code, 124)
        mocked_cleanup.assert_called_once_with("docker", container_name)
        emitted = json.loads(out.getvalue())
        self.assertEqual(emitted["error_type"], "TimeoutExpired")


if __name__ == "__main__":
    unittest.main()
