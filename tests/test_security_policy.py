"""Tests for security policy and path guard helpers."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from sentientagent_v2.security import PathGuard, SecurityPolicy, load_security_policy


class SecurityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_load_security_policy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SENTIENTAGENT_V2_WORKSPACE"] = tmp
            policy = load_security_policy()

        self.assertFalse(policy.strict_mode)
        self.assertFalse(policy.restrict_to_workspace)
        self.assertTrue(policy.allow_exec)
        self.assertTrue(policy.allow_network)
        self.assertEqual(policy.exec_allowlist, ())

    def test_strict_mode_forces_restriction_and_disables_exec_network_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SENTIENTAGENT_V2_WORKSPACE"] = tmp
            os.environ["SENTIENTAGENT_V2_STRICT_MODE"] = "1"
            policy = load_security_policy()

        self.assertTrue(policy.strict_mode)
        self.assertTrue(policy.restrict_to_workspace)
        self.assertFalse(policy.allow_exec)
        self.assertFalse(policy.allow_network)

    def test_allowlist_is_parsed_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SENTIENTAGENT_V2_WORKSPACE"] = tmp
            os.environ["SENTIENTAGENT_V2_EXEC_ALLOWLIST"] = "python, ls,python"
            policy = load_security_policy()

        self.assertEqual(policy.exec_allowlist, ("python", "ls"))
        self.assertTrue(policy.is_exec_allowed("python"))
        self.assertFalse(policy.is_exec_allowed("git"))


class PathGuardTests(unittest.TestCase):
    def test_path_guard_blocks_outside_workspace_when_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            policy = SecurityPolicy(
                workspace_root=workspace,
                restrict_to_workspace=True,
                strict_mode=False,
                allow_exec=True,
                allow_network=True,
                exec_allowlist=(),
            )
            guard = PathGuard(policy)

            with self.assertRaises(PermissionError):
                guard.resolve_path("../outside.txt", base_dir=workspace)

    def test_path_guard_allows_inside_workspace_when_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            policy = SecurityPolicy(
                workspace_root=workspace,
                restrict_to_workspace=True,
                strict_mode=False,
                allow_exec=True,
                allow_network=True,
                exec_allowlist=(),
            )
            guard = PathGuard(policy)
            resolved = guard.resolve_path("nested/file.txt", base_dir=workspace)
            self.assertTrue(str(resolved).startswith(str(workspace)))


if __name__ == "__main__":
    unittest.main()
