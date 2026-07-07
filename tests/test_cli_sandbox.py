"""Tests for sandbox CLI helper commands."""

from __future__ import annotations

import tempfile
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

    def test_sandbox_build_image_extends_context_with_dependency_files(self) -> None:
        completed = mock.Mock(returncode=0)
        captured: dict[str, object] = {}

        def _fake_run(argv: list[str], check: bool = False):
            _ = check
            dockerfile = Path(argv[argv.index("-f") + 1])
            context_dir = Path(argv[-1])
            captured["argv"] = argv
            captured["dockerfile_text"] = dockerfile.read_text(encoding="utf-8")
            captured["context_files"] = sorted(path.name for path in context_dir.iterdir())
            return completed

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / "requirements.txt"
            package_json = root / "package.json"
            package_lock = root / "package-lock.json"
            requirements.write_text("requests==2.32.0\n", encoding="utf-8")
            package_json.write_text('{"dependencies":{"left-pad":"1.3.0"}}\n', encoding="utf-8")
            package_lock.write_text('{"lockfileVersion":3}\n', encoding="utf-8")

            with (
                mock.patch.object(cli.subprocess, "run", side_effect=_fake_run),
                mock.patch.object(cli, "_stdout_line"),
            ):
                code = cli._cmd_sandbox_build_image(
                    image="openppx-sandbox:test",
                    docker_bin="dockerx",
                    python_requirements=str(requirements),
                    node_package_json=str(package_json),
                    node_package_lock=str(package_lock),
                )

        self.assertEqual(code, 0)
        self.assertIn("requirements.txt", captured["context_files"])
        self.assertIn("package.json", captured["context_files"])
        self.assertIn("package-lock.json", captured["context_files"])
        dockerfile_text = str(captured["dockerfile_text"])
        self.assertIn("python -m pip install --no-cache-dir", dockerfile_text)
        self.assertIn("npm ci --omit=dev", dockerfile_text)
        self.assertIn("NODE_PATH=/opt/openppx-sandbox-node/node_modules", dockerfile_text)

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

    def test_sandbox_build_image_rejects_missing_dependency_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-requirements.txt"
            with (
                mock.patch.object(cli.subprocess, "run") as mocked_run,
                mock.patch.object(cli, "_stdout_line") as mocked_stdout,
            ):
                code = cli._cmd_sandbox_build_image(
                    image="openppx-sandbox:test",
                    docker_bin="dockerx",
                    python_requirements=str(missing),
                )

        self.assertEqual(code, 1)
        mocked_run.assert_not_called()
        self.assertIn("file not found", mocked_stdout.call_args.args[0])

    def test_sandbox_build_image_rejects_node_lock_without_package_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_lock = Path(tmp) / "package-lock.json"
            package_lock.write_text('{"lockfileVersion":3}\n', encoding="utf-8")
            with (
                mock.patch.object(cli.subprocess, "run") as mocked_run,
                mock.patch.object(cli, "_stdout_line") as mocked_stdout,
            ):
                code = cli._cmd_sandbox_build_image(
                    image="openppx-sandbox:test",
                    docker_bin="dockerx",
                    node_package_lock=str(package_lock),
                )

        self.assertEqual(code, 1)
        mocked_run.assert_not_called()
        self.assertIn("--node-package-json", mocked_stdout.call_args.args[0])

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
                        "--python-requirements",
                        "requirements.txt",
                        "--node-package-json",
                        "package.json",
                        "--node-package-lock",
                        "package-lock.json",
                    ]
                )

        self.assertEqual(caught.exception.code, 0)
        mocked_build.assert_called_once_with(
            image="sandbox:test",
            docker_bin="dockerx",
            no_cache=True,
            base_image="registry.example/python:3.14-slim",
            python_requirements="requirements.txt",
            node_package_json="package.json",
            node_package_lock="package-lock.json",
        )

    def test_main_dispatches_sandbox_prune(self) -> None:
        with mock.patch.object(cli, "_cmd_sandbox_prune", return_value=0) as mocked_prune:
            with self.assertRaises(SystemExit) as caught:
                cli.main(["sandbox", "prune", "--docker-bin", "dockerx"])

        self.assertEqual(caught.exception.code, 0)
        mocked_prune.assert_called_once_with(docker_bin="dockerx")


if __name__ == "__main__":
    unittest.main()
