"""Opt-in Docker integration tests for the sandbox backend."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from openppx.runtime.sandbox import list_docker_sandbox_containers
from openppx.tooling.registry import exec_command, invoke_skill_api, process_session


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

    def test_real_docker_exec_pty_outputs_fast_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            os.environ["OPENPPX_WORKSPACE"] = str(workspace)
            os.environ["OPENPPX_SANDBOX_DOCKER_BIN"] = self.docker_bin
            os.environ["OPENPPX_SANDBOX_IMAGE"] = self.image

            output = exec_command("python -c \"print('PTY_OK')\"", sandbox="docker", pty=True, yield_ms=2_000)

            if "PTY_OK" not in output:
                matched = re.search(r"session ([0-9a-f-]+)", output)
                self.assertIsNotNone(matched)
                session_id = matched.group(1) if matched else ""
                deadline = time.time() + 5
                while time.time() < deadline:
                    polled = process_session("poll", session_id=session_id, timeout_ms=200)
                    if "PTY_OK" in polled or "Process exited with code" in polled:
                        break
                output = process_session("log", session_id=session_id)
                process_session("remove", session_id=session_id)

        self.assertIn("PTY_OK", output)

    def test_real_docker_python_api_sandbox_runs_runner_and_masks_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_python_api_skill(
                tmp,
                "inspect",
                {"module": "demo_sdk", "function": "inspect", "sandbox": {"required": True}},
                (
                    "from pathlib import Path\n"
                    "def inspect(a, b):\n"
                    "    env_text = Path('.env').read_text(encoding='utf-8')\n"
                    "    return {'sum': a + b, 'env_secret_visible': 'SECRET_VALUE' in env_text}\n"
                ),
            )
            os.environ["OPENPPX_SANDBOX_DOCKER_BIN"] = self.docker_bin
            os.environ["OPENPPX_SANDBOX_IMAGE"] = self.image

            payload = json.loads(
                invoke_skill_api("demo", "inspect", args={"a": 2, "b": 3}, inline_budget_ms=20_000)
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "inline")
        normalized = payload["output"].replace(" ", "")
        self.assertIn('"sum":5', normalized)
        self.assertIn('"env_secret_visible":false', normalized)

    def test_real_docker_node_api_sandbox_runs_runner_and_masks_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_node_api_skill(
                tmp,
                "inspect",
                {"module": "demo_node.cjs", "function": "inspect", "sandbox": "docker"},
                (
                    "const fs = require('fs');\n"
                    "exports.inspect = async function(args) {\n"
                    "  const envText = fs.readFileSync('.env', 'utf8');\n"
                    "  return {sum: args.a + args.b, envSecretVisible: envText.includes('SECRET_VALUE')};\n"
                    "};\n"
                ),
            )
            os.environ["OPENPPX_SANDBOX_DOCKER_BIN"] = self.docker_bin
            os.environ["OPENPPX_SANDBOX_IMAGE"] = self.image

            payload = json.loads(
                invoke_skill_api("demo", "inspect", args={"a": 2, "b": 3}, inline_budget_ms=20_000)
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "inline")
        normalized = payload["output"].replace(" ", "")
        self.assertIn('"sum":5', normalized)
        self.assertIn('"envSecretVisible":false', normalized)

    def test_real_docker_command_api_allows_trusted_network_and_image_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_command_api_skill(
                tmp,
                "inspect",
                {
                    "argv": [
                        "python",
                        "-c",
                        "from pathlib import Path; print('visible=' + str('SECRET_VALUE' in Path('.env').read_text()))",
                    ],
                    "allow_system_executable": True,
                    "sandbox": {"required": True, "network": "enabled", "image": self.image},
                },
            )
            os.environ["OPENPPX_SANDBOX_DOCKER_BIN"] = self.docker_bin
            os.environ["OPENPPX_SANDBOX_IMAGE"] = self.image
            os.environ["OPENPPX_SANDBOX_ALLOW_NETWORK"] = "1"

            payload = json.loads(invoke_skill_api("demo", "inspect", args={}, inline_budget_ms=20_000))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "inline")
        self.assertIn("visible=False", payload["output"])

    def test_real_docker_command_api_forced_policy_masks_env_without_recipe_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_command_api_skill(
                tmp,
                "inspect",
                {
                    "argv": [
                        "python",
                        "-c",
                        "from pathlib import Path; print('visible=' + str('SECRET_VALUE' in Path('.env').read_text()))",
                    ],
                    "allow_system_executable": True,
                },
            )
            os.environ["OPENPPX_SANDBOX_DOCKER_BIN"] = self.docker_bin
            os.environ["OPENPPX_SANDBOX_IMAGE"] = self.image
            os.environ["OPENPPX_SKILL_API_SANDBOX"] = "docker"

            payload = json.loads(invoke_skill_api("demo", "inspect", args={}, inline_budget_ms=20_000))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "inline")
        self.assertIn("visible=False", payload["output"])

    def _prepare_python_api_skill(
        self,
        tmp: str,
        api_name: str,
        recipe: dict[str, object],
        module_source: str,
    ) -> None:
        skill_dir = self._prepare_skill_dir(tmp)
        (skill_dir / "demo_sdk.py").write_text(module_source, encoding="utf-8")
        (skill_dir / "apis" / f"{api_name}.python.json").write_text(json.dumps(recipe), encoding="utf-8")

    def _prepare_node_api_skill(
        self,
        tmp: str,
        api_name: str,
        recipe: dict[str, object],
        module_source: str,
    ) -> None:
        skill_dir = self._prepare_skill_dir(tmp)
        (skill_dir / "demo_node.cjs").write_text(module_source, encoding="utf-8")
        (skill_dir / "apis" / f"{api_name}.node.json").write_text(json.dumps(recipe), encoding="utf-8")

    def _prepare_command_api_skill(
        self,
        tmp: str,
        api_name: str,
        recipe: dict[str, object],
    ) -> None:
        skill_dir = self._prepare_skill_dir(tmp)
        (skill_dir / "apis" / f"{api_name}.command.json").write_text(json.dumps(recipe), encoding="utf-8")

    def _prepare_skill_dir(self, tmp: str) -> Path:
        root = Path(tmp)
        agent_home = root / "agent"
        skill_dir = agent_home / "skills" / "demo"
        apis = skill_dir / "apis"
        apis.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: demo skill\n---\n# Demo\n",
            encoding="utf-8",
        )
        (skill_dir / ".env").write_text("SECRET_VALUE=visible\n", encoding="utf-8")
        os.environ["OPENPPX_AGENT_HOME"] = str(agent_home)
        os.environ["OPENPPX_TASK_DB_PATH"] = str(root / "tasks.db")
        time.sleep(0.001)
        return skill_dir


if __name__ == "__main__":
    unittest.main()
