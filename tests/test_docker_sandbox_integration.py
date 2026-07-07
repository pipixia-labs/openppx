"""Opt-in Docker integration tests for the sandbox backend."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from openppx.runtime.sandbox import list_docker_sandbox_containers
from openppx.tooling.registry import exec_command, process_session


def _docker_integration_enabled() -> bool:
    return os.getenv("OPENPPX_RUN_DOCKER_SANDBOX_TESTS", "").strip().lower() in {"1", "true", "yes", "on"}


@unittest.skipUnless(_docker_integration_enabled(), "set OPENPPX_RUN_DOCKER_SANDBOX_TESTS=1 to run Docker sandbox integration tests")
class DockerSandboxIntegrationTests(unittest.TestCase):
    """Exercise the real Docker sandbox backend when explicitly enabled."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.docker_bin = os.getenv("OPENPPX_SANDBOX_DOCKER_BIN", "docker").strip() or "docker"
        cls.image = os.getenv("OPENPPX_SANDBOX_IMAGE", "openppx-sandbox:dev").strip() or "openppx-sandbox:dev"
        if shutil.which(cls.docker_bin) is None:
            raise unittest.SkipTest(f"Docker CLI not found: {cls.docker_bin}")
        inspect = subprocess.run(
            [cls.docker_bin, "image", "inspect", cls.image],
            shell=False,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if inspect.returncode != 0:
            raise unittest.SkipTest(f"Docker sandbox image not found: {cls.image}; run `ppx sandbox build-image`")

    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_real_docker_exec_enforces_workspace_masks_readonly_git_and_no_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".git").mkdir()
            (workspace / ".git" / "config").write_text("[core]\n", encoding="utf-8")
            (workspace / ".env").write_text("SECRET_VALUE=visible\n", encoding="utf-8")
            script = workspace / "check_sandbox.py"
            script.write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "import socket",
                        "env_text = Path('.env').read_text(encoding='utf-8')",
                        "print('ENV_SECRET_VISIBLE=' + str('SECRET_VALUE' in env_text))",
                        "try:",
                        "    Path('.git/config').write_text('mutated', encoding='utf-8')",
                        "except OSError:",
                        "    print('GIT_READONLY=True')",
                        "else:",
                        "    print('GIT_READONLY=False')",
                        "try:",
                        "    socket.create_connection(('1.1.1.1', 53), timeout=1).close()",
                        "except OSError:",
                        "    print('NETWORK_BLOCKED=True')",
                        "else:",
                        "    print('NETWORK_BLOCKED=False')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            os.environ["OPENPPX_WORKSPACE"] = str(workspace)
            os.environ["OPENPPX_SANDBOX_DOCKER_BIN"] = self.docker_bin
            os.environ["OPENPPX_SANDBOX_IMAGE"] = self.image

            output = exec_command("python check_sandbox.py", sandbox="docker")

        self.assertIn("ENV_SECRET_VISIBLE=False", output)
        self.assertIn("GIT_READONLY=True", output)
        self.assertIn("NETWORK_BLOCKED=True", output)

    def test_real_docker_background_kill_removes_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            os.environ["OPENPPX_WORKSPACE"] = str(workspace)
            os.environ["OPENPPX_SANDBOX_DOCKER_BIN"] = self.docker_bin
            os.environ["OPENPPX_SANDBOX_IMAGE"] = self.image
            containers_before = set(list_docker_sandbox_containers(docker_bin=self.docker_bin))

            started = exec_command(
                "python -c \"import time; print('ready', flush=True); time.sleep(30)\"",
                sandbox="docker",
                background=True,
            )
            matched = re.search(r"session ([0-9a-f-]+)", started)
            self.assertIsNotNone(matched)
            session_id = matched.group(1) if matched else ""

            poll_deadline = time.time() + 5
            while time.time() < poll_deadline:
                polled = process_session("poll", session_id=session_id, timeout_ms=200)
                if "ready" in polled:
                    break
            self.assertIn("ready", process_session("log", session_id=session_id))

            killed = process_session("kill", session_id=session_id)
            self.assertIn("Termination requested", killed)
            process_session("remove", session_id=session_id)

            containers_after = set(list_docker_sandbox_containers(docker_bin=self.docker_bin))
            self.assertTrue(containers_after.issubset(containers_before))


if __name__ == "__main__":
    unittest.main()
