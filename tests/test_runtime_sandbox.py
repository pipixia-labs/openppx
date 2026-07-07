"""Tests for sandbox Phase 1 planning and Docker argv building."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openppx.runtime.sandbox import (
    DockerSandboxConfig,
    NetworkMode,
    NetworkPolicy,
    PathAccessMode,
    PathGrant,
    SandboxCommand,
    SandboxExecutionPlan,
    SandboxMount,
    SandboxValidationError,
    ValidatedSandboxExecutionPlan,
    build_docker_run_spec,
    build_sandbox_diagnostics,
    build_workspace_docker_sandbox,
    list_docker_sandbox_containers,
    prune_docker_sandbox_containers,
    read_only_profile,
    resolve_backend,
    resolve_network_mode,
    resolve_recipe_sandbox_options,
    workspace_write_profile,
)


class SandboxPhaseOneTests(unittest.TestCase):
    def test_valid_workspace_plan_builds_docker_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            plan = _workspace_plan(workspace, stdin="payload")

            validated = ValidatedSandboxExecutionPlan.from_plan(plan)
            spec = build_docker_run_spec(
                validated,
                config=DockerSandboxConfig(image="openppx-sandbox:test", uid=501, gid=20),
            )

        argv = list(spec.argv)
        self.assertEqual(spec.container_name, "openppx-sandbox-test-run")
        self.assertIn("-i", argv)
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("--user", argv)
        self.assertEqual(argv[argv.index("--user") + 1], "501:20")
        self.assertIn("--workdir", argv)
        self.assertEqual(argv[argv.index("--workdir") + 1], str(workspace))
        self.assertIn("--env", argv)
        self.assertIn("HOME=/tmp/openppx-home", argv)
        self.assertIn("type=bind,src=/dev/null,dst=" + str(workspace / ".env") + ",readonly", argv)
        self.assertEqual(argv[-3:], ["openppx-sandbox:test", "python", "--version"])

    def test_docker_argv_can_request_interactive_tty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            plan = _workspace_plan(workspace)

            spec = build_docker_run_spec(
                ValidatedSandboxExecutionPlan.from_plan(plan),
                config=DockerSandboxConfig(
                    image="openppx-sandbox:test",
                    uid=501,
                    gid=20,
                    stdin_open=True,
                    tty=True,
                ),
            )

        argv = list(spec.argv)
        self.assertIn("-i", argv)
        self.assertIn("-t", argv)

    def test_mount_must_be_covered_by_profile_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp, "workspace").resolve()
            workspace.mkdir()
            outside = Path(tmp, "outside").resolve()
            outside.mkdir()
            plan = _workspace_plan(
                workspace,
                extra_mounts=(
                    SandboxMount(
                        logical_name="outside",
                        host_path=outside,
                        container_path=str(outside),
                        access=PathAccessMode.READ,
                    ),
                ),
            )

            with self.assertRaisesRegex(SandboxValidationError, "not covered"):
                ValidatedSandboxExecutionPlan.from_plan(plan)

    def test_writable_mount_requires_writable_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            profile = read_only_profile(workspace)
            plan = SandboxExecutionPlan(
                command=SandboxCommand(argv=("true",)),
                profile=profile,
                mounts=(
                    SandboxMount(
                        logical_name="workspace",
                        host_path=workspace,
                        container_path=str(workspace),
                        access=PathAccessMode.WRITE,
                    ),
                ),
                env={},
                cwd=str(workspace),
            )

            with self.assertRaisesRegex(SandboxValidationError, "not covered"):
                ValidatedSandboxExecutionPlan.from_plan(plan)

    def test_denied_root_rejects_non_mask_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            env_file = workspace / ".env"
            env_file.write_text("SECRET=1", encoding="utf-8")
            plan = _workspace_plan(
                workspace,
                extra_mounts=(
                    SandboxMount(
                        logical_name="env",
                        host_path=env_file,
                        container_path=str(env_file),
                        access=PathAccessMode.READ,
                    ),
                ),
            )

            with self.assertRaisesRegex(SandboxValidationError, "denied root"):
                ValidatedSandboxExecutionPlan.from_plan(plan)

    def test_mask_mount_source_must_be_backend_controlled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            fake_mask = workspace / "fake-mask"
            fake_mask.write_text("", encoding="utf-8")
            plan = _workspace_plan(
                workspace,
                mask_source=fake_mask,
            )

            with self.assertRaisesRegex(SandboxValidationError, "backend allowlist"):
                ValidatedSandboxExecutionPlan.from_plan(plan)

    def test_env_policy_rejects_dangerous_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            for name in ("LD_PRELOAD", "BASH_ENV", "IFS", "PYTHONPATH", "OPENAI_API_KEY"):
                plan = _workspace_plan(workspace, env={name: "x"})
                with self.subTest(name=name):
                    with self.assertRaisesRegex(SandboxValidationError, "sensitive sandbox env"):
                        ValidatedSandboxExecutionPlan.from_plan(plan)

    def test_backend_resolution_cannot_downgrade(self) -> None:
        self.assertEqual(resolve_backend(configured_backend="none", requested_backend="docker"), "docker")
        self.assertEqual(resolve_backend(configured_backend="docker", requested_backend="docker"), "docker")
        with self.assertRaisesRegex(SandboxValidationError, "downgrade"):
            resolve_backend(configured_backend="docker", requested_backend="bwrap")

    def test_sandbox_diagnostics_marks_legacy_bwrap_as_weak(self) -> None:
        with mock.patch("openppx.runtime.sandbox.diagnostics.shutil.which", return_value="/usr/bin/docker"):
            payload = build_sandbox_diagnostics(backend="bwrap").to_payload()

        self.assertEqual(payload["backend"], "bwrap")
        self.assertIn("legacy bwrap", payload["warnings"][0])

    def test_sandbox_diagnostics_reports_missing_docker_cli(self) -> None:
        with mock.patch("openppx.runtime.sandbox.diagnostics.shutil.which", return_value=None):
            payload = build_sandbox_diagnostics(backend="docker", docker_bin="dockerx").to_payload()

        self.assertEqual(payload["backend"], "docker")
        self.assertFalse(payload["docker_cli_available"])
        self.assertIn("docker CLI is not available", payload["warnings"])

    def test_list_docker_sandbox_containers_uses_label_filter(self) -> None:
        completed = mock.Mock(returncode=0, stdout="openppx-sandbox-a\nopenppx-sandbox-b\n", stderr="")
        with mock.patch("openppx.runtime.sandbox.diagnostics.subprocess.run", return_value=completed) as mocked_run:
            names = list_docker_sandbox_containers(docker_bin="dockerx")

        self.assertEqual(names, ("openppx-sandbox-a", "openppx-sandbox-b"))
        argv = mocked_run.call_args.args[0]
        self.assertEqual(argv[:3], ["dockerx", "ps", "-a"])
        self.assertIn("label=openppx.sandbox=1", argv)
        self.assertFalse(mocked_run.call_args.kwargs["shell"])

    def test_prune_docker_sandbox_containers_removes_explicit_targets(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("openppx.runtime.sandbox.diagnostics.subprocess.run", return_value=completed) as mocked_run:
            removed, errors = prune_docker_sandbox_containers(
                docker_bin="dockerx",
                containers=("openppx-sandbox-a",),
            )

        self.assertEqual(removed, ("openppx-sandbox-a",))
        self.assertEqual(errors, ())
        self.assertEqual(mocked_run.call_args.args[0], ["dockerx", "rm", "-f", "openppx-sandbox-a"])

    def test_network_resolution_uses_default_request_and_lock(self) -> None:
        with self.assertRaisesRegex(SandboxValidationError, "requires approval"):
            resolve_network_mode(
                default_mode=NetworkMode.DISABLED,
                requested_mode=NetworkMode.ENABLED,
                approved=False,
            )
        self.assertEqual(
            resolve_network_mode(
                default_mode=NetworkMode.DISABLED,
                requested_mode=NetworkMode.ENABLED,
                approved=True,
            ),
            NetworkMode.ENABLED,
        )
        self.assertEqual(
            resolve_network_mode(
                default_mode=NetworkMode.ENABLED,
                requested_mode=NetworkMode.ENABLED,
                lock_mode=NetworkMode.DISABLED,
                approved=True,
            ),
            NetworkMode.DISABLED,
        )

    def test_validated_plan_requires_network_approval_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            plan = _workspace_plan(workspace)
            profile = type(plan.profile)(
                name="network-enabled",
                filesystem=plan.profile.filesystem,
                network=NetworkPolicy(mode=NetworkMode.ENABLED),
                env=plan.profile.env,
                limits=plan.profile.limits,
                approval=plan.profile.approval,
            )
            plan = type(plan)(
                command=plan.command,
                profile=profile,
                mounts=plan.mounts,
                env=plan.env,
                cwd=plan.cwd,
                stdin=plan.stdin,
                labels=plan.labels,
            )

            with self.assertRaisesRegex(SandboxValidationError, "requires approval"):
                ValidatedSandboxExecutionPlan.from_plan(plan)

            approved_plan = type(plan)(
                command=plan.command,
                profile=profile,
                mounts=plan.mounts,
                env=plan.env,
                cwd=plan.cwd,
                stdin=plan.stdin,
                labels={**plan.labels, "openppx.network.approved": "1"},
            )
            ValidatedSandboxExecutionPlan.from_plan(approved_plan)

    def test_recipe_sandbox_options_gate_network_on_trusted_env(self) -> None:
        with self.assertRaisesRegex(ValueError, "OPENPPX_SANDBOX_ALLOW_NETWORK"):
            resolve_recipe_sandbox_options(
                {"required": True, "network": "enabled"},
                runner_name="Python",
                env={},
            )

        options = resolve_recipe_sandbox_options(
            {"required": True, "network": "enabled"},
            runner_name="Python",
            env={"OPENPPX_SANDBOX_ALLOW_NETWORK": "1"},
        )

        self.assertIsNotNone(options)
        assert options is not None
        self.assertEqual(options.network_mode, NetworkMode.ENABLED)
        self.assertTrue(options.network_approved)
        self.assertEqual(options.labels["openppx.network.approved"], "1")

    def test_recipe_sandbox_options_gate_custom_image_on_allowlist(self) -> None:
        with self.assertRaisesRegex(ValueError, "TRUSTED_IMAGES"):
            resolve_recipe_sandbox_options(
                {"required": True, "image": "registry.example/openppx-sandbox:tool"},
                runner_name="Node",
                env={},
            )

        options = resolve_recipe_sandbox_options(
            {"required": True, "image": "registry.example/openppx-sandbox:tool"},
            runner_name="Node",
            env={"OPENPPX_SANDBOX_TRUSTED_IMAGES": "registry.example/openppx-sandbox:*"},
        )

        self.assertIsNotNone(options)
        assert options is not None
        self.assertEqual(options.image, "registry.example/openppx-sandbox:tool")
        self.assertEqual(options.labels["openppx.image.approved"], "1")

    def test_workspace_docker_sandbox_can_enable_approved_network_and_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            sandbox = build_workspace_docker_sandbox(
                command_argv=["python", "--version"],
                workspace=workspace,
                cwd=workspace,
                timeout_seconds=10,
                image="registry.example/openppx-sandbox:tool",
                network_mode=NetworkMode.ENABLED,
                network_approved=True,
                labels={"openppx.tool": "test"},
            )

        argv = sandbox.argv
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "bridge")
        self.assertIn("--label", argv)
        self.assertIn("openppx.network.approved=1", argv)
        self.assertEqual(argv[-3:], ["registry.example/openppx-sandbox:tool", "python", "--version"])

    def test_workspace_docker_sandbox_network_lock_overrides_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ",
            {"OPENPPX_SANDBOX_NETWORK_LOCK": "disabled"},
            clear=False,
        ):
            workspace = Path(tmp).resolve()
            sandbox = build_workspace_docker_sandbox(
                command_argv=["python", "--version"],
                workspace=workspace,
                cwd=workspace,
                timeout_seconds=10,
                network_mode=NetworkMode.ENABLED,
                network_approved=True,
            )

        argv = sandbox.argv
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")

    def test_grant_can_disallow_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            profile = workspace_write_profile(target)
            grant = PathGrant(
                logical_name="link",
                host_path=link,
                container_path=str(link),
                access=PathAccessMode.WRITE,
                follow_symlinks=False,
            )
            profile = type(profile)(
                name=profile.name,
                filesystem=type(profile.filesystem)(
                    readable_roots=(grant,),
                    writable_roots=(grant,),
                    denied_roots=(),
                ),
                network=profile.network,
                env=profile.env,
                limits=profile.limits,
                approval=profile.approval,
            )
            plan = SandboxExecutionPlan(
                command=SandboxCommand(argv=("true",)),
                profile=profile,
                mounts=(
                    SandboxMount(
                        logical_name="link",
                        host_path=link,
                        container_path=str(link),
                        access=PathAccessMode.WRITE,
                    ),
                ),
                env={},
                cwd=str(link),
            )

            with self.assertRaisesRegex(SandboxValidationError, "symlink"):
                ValidatedSandboxExecutionPlan.from_plan(plan)


def _workspace_plan(
    workspace: Path,
    *,
    env: dict[str, str] | None = None,
    stdin: str | bytes | None = None,
    extra_mounts: tuple[SandboxMount, ...] = (),
    mask_source: Path = Path("/dev/null"),
) -> SandboxExecutionPlan:
    workspace = workspace.resolve(strict=False)
    return SandboxExecutionPlan(
        command=SandboxCommand(argv=("python", "--version")),
        profile=workspace_write_profile(workspace),
        mounts=(
            SandboxMount(
                logical_name="workspace",
                host_path=workspace,
                container_path=str(workspace),
                access=PathAccessMode.WRITE,
            ),
            SandboxMount(
                logical_name="env-mask",
                host_path=mask_source,
                container_path=str(workspace / ".env"),
                access=PathAccessMode.READ,
                mask=True,
            ),
            *extra_mounts,
        ),
        env=env or {},
        cwd=str(workspace),
        stdin=stdin,
        labels={"openppx.run_id": "test-run"},
    )


if __name__ == "__main__":
    unittest.main()
