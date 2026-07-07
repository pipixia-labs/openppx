"""Tests for sandbox CLI helper commands."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from openppx.app import cli


class SandboxCliTests(unittest.TestCase):
    def test_doctor_sandbox_status_reports_default_disabled_backend(self) -> None:
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("openppx.runtime.sandbox.diagnostics.shutil.which", return_value="/usr/bin/docker"),
        ):
            payload = cli._doctor_sandbox_status()

        self.assertEqual(payload["backend"], "none")
        self.assertEqual(payload["status"], "disabled")
        self.assertTrue(payload["docker_cli_available"])

    def test_doctor_sandbox_status_reports_legacy_bwrap_warning(self) -> None:
        with (
            mock.patch.dict("os.environ", {"OPENPPX_SANDBOX_BACKEND": "bwrap"}, clear=True),
            mock.patch("openppx.runtime.sandbox.diagnostics.shutil.which", return_value="/usr/bin/docker"),
        ):
            payload = cli._doctor_sandbox_status()

        self.assertEqual(payload["backend"], "bwrap")
        self.assertIn("legacy bwrap", payload["warnings"][0])

    def test_doctor_sandbox_status_reports_missing_docker_cli(self) -> None:
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "OPENPPX_SANDBOX_BACKEND": "docker",
                    "OPENPPX_SANDBOX_DOCKER_BIN": "dockerx",
                    "OPENPPX_SANDBOX_IMAGE": "sandbox:test",
                },
                clear=True,
            ),
            mock.patch("openppx.runtime.sandbox.diagnostics.shutil.which", return_value=None),
        ):
            payload = cli._doctor_sandbox_status()

        self.assertEqual(payload["backend"], "docker")
        self.assertEqual(payload["docker_bin"], "dockerx")
        self.assertEqual(payload["image"], "sandbox:test")
        self.assertFalse(payload["docker_cli_available"])

    def test_doctor_sandbox_status_reports_labeled_containers(self) -> None:
        with (
            mock.patch.dict("os.environ", {"OPENPPX_SANDBOX_BACKEND": "docker"}, clear=True),
            mock.patch("openppx.runtime.sandbox.diagnostics.shutil.which", return_value="/usr/bin/docker"),
            mock.patch(
                "openppx.runtime.sandbox.list_docker_sandbox_containers",
                return_value=("openppx-sandbox-a", "openppx-sandbox-b"),
            ),
        ):
            payload = cli._doctor_sandbox_status()

        self.assertEqual(payload["containers"]["count"], 2)
        self.assertEqual(payload["containers"]["openppx_sandbox"], ["openppx-sandbox-a", "openppx-sandbox-b"])
        self.assertIn("sandbox container", payload["warnings"][-1])

    def test_sandbox_build_image_invokes_docker_build(self) -> None:
        completed = mock.Mock(returncode=0)
        with (
            mock.patch.object(cli.subprocess, "run", return_value=completed) as mocked_run,
            mock.patch.object(cli, "_stdout_line"),
        ):
            code = cli._cmd_sandbox_build_image(
                image="openppx-sandbox:test",
                docker_bin="dockerx",
                no_cache=True,
                base_image="registry.example/python:3.14-slim",
            )

        self.assertEqual(code, 0)
        argv = mocked_run.call_args.args[0]
        self.assertEqual(argv[:4], ["dockerx", "build", "-t", "openppx-sandbox:test"])
        self.assertIn("--build-arg", argv)
        self.assertIn("PYTHON_BASE_IMAGE=registry.example/python:3.14-slim", argv)
        self.assertIn("--no-cache", argv)
        dockerfile = Path(argv[argv.index("-f") + 1])
        self.assertTrue(dockerfile.is_file())
        self.assertEqual(dockerfile.name, "Dockerfile")

    def test_sandbox_build_image_reports_missing_docker_cli(self) -> None:
        with (
            mock.patch.object(cli.subprocess, "run", side_effect=FileNotFoundError),
            mock.patch.object(cli, "_stdout_line") as mocked_stdout,
        ):
            code = cli._cmd_sandbox_build_image(
                image="openppx-sandbox:test",
                docker_bin="dockerx",
            )

        self.assertEqual(code, 1)
        self.assertIn("Docker CLI not found", mocked_stdout.call_args.args[0])

    def test_sandbox_build_image_reports_base_image_hint_on_build_failure(self) -> None:
        completed = mock.Mock(returncode=1)
        with (
            mock.patch.object(cli.subprocess, "run", return_value=completed),
            mock.patch.object(cli, "_stdout_line") as mocked_stdout,
        ):
            code = cli._cmd_sandbox_build_image(
                image="openppx-sandbox:test",
                docker_bin="dockerx",
            )

        self.assertEqual(code, 1)
        self.assertIn("--base-image", mocked_stdout.call_args.args[0])

    def test_sandbox_prune_removes_labeled_containers(self) -> None:
        with (
            mock.patch("openppx.runtime.sandbox.list_docker_sandbox_containers", return_value=("openppx-sandbox-a",)),
            mock.patch(
                "openppx.runtime.sandbox.prune_docker_sandbox_containers",
                return_value=(("openppx-sandbox-a",), ()),
            ) as mocked_prune,
            mock.patch.object(cli, "_stdout_line") as mocked_stdout,
        ):
            code = cli._cmd_sandbox_prune(docker_bin="dockerx")

        self.assertEqual(code, 0)
        mocked_prune.assert_called_once_with(docker_bin="dockerx", containers=("openppx-sandbox-a",))
        self.assertIn("Removed sandbox container", mocked_stdout.call_args_list[0].args[0])

    def test_sandbox_prune_reports_remove_errors(self) -> None:
        with (
            mock.patch("openppx.runtime.sandbox.list_docker_sandbox_containers", return_value=("openppx-sandbox-a",)),
            mock.patch(
                "openppx.runtime.sandbox.prune_docker_sandbox_containers",
                return_value=((), ("openppx-sandbox-a: denied",)),
            ),
            mock.patch.object(cli, "_stdout_line"),
        ):
            code = cli._cmd_sandbox_prune(docker_bin="dockerx")

        self.assertEqual(code, 1)

    def test_main_dispatches_sandbox_build_image(self) -> None:
        with mock.patch.object(cli, "_cmd_sandbox_build_image", return_value=0) as mocked_build:
            with self.assertRaises(SystemExit) as caught:
                cli.main(
                    [
                        "sandbox",
                        "build-image",
                        "--image",
                        "sandbox:test",
                        "--docker-bin",
                        "dockerx",
                        "--no-cache",
                        "--base-image",
                        "registry.example/python:3.14-slim",
                    ]
                )

        self.assertEqual(caught.exception.code, 0)
        mocked_build.assert_called_once_with(
            image="sandbox:test",
            docker_bin="dockerx",
            no_cache=True,
            base_image="registry.example/python:3.14-slim",
        )

    def test_main_dispatches_sandbox_prune(self) -> None:
        with mock.patch.object(cli, "_cmd_sandbox_prune", return_value=0) as mocked_prune:
            with self.assertRaises(SystemExit) as caught:
                cli.main(["sandbox", "prune", "--docker-bin", "dockerx"])

        self.assertEqual(caught.exception.code, 0)
        mocked_prune.assert_called_once_with(docker_bin="dockerx")


if __name__ == "__main__":
    unittest.main()
