"""Docker argv builder for validated sandbox execution plans."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .plan import NetworkMode, PathAccessMode, ValidatedSandboxExecutionPlan


@dataclass(frozen=True, slots=True)
class DockerSandboxConfig:
    """Trusted Docker sandbox backend configuration."""

    image: str = "openppx-sandbox:dev"
    docker_bin: str = "docker"
    uid: int | None = None
    gid: int | None = None
    home: str = "/tmp/openppx-home"


@dataclass(frozen=True, slots=True)
class DockerRunSpec:
    """Concrete Docker CLI argv and metadata for one sandbox execution."""

    argv: tuple[str, ...]
    container_name: str
    labels: Mapping[str, str]
    stdin: str | bytes | None


def build_docker_run_spec(
    validated_plan: ValidatedSandboxExecutionPlan,
    *,
    config: DockerSandboxConfig | None = None,
) -> DockerRunSpec:
    """Build a Docker CLI argv for a validated sandbox plan."""
    cfg = config or DockerSandboxConfig()
    plan = validated_plan.plan
    labels = {"openppx.sandbox": "1", **{str(k): str(v) for k, v in plan.labels.items()}}
    run_id = labels.get("openppx.run_id") or labels.get("run_id") or uuid.uuid4().hex
    labels.setdefault("openppx.run_id", run_id)
    container_name = _container_name(run_id)

    argv: list[str] = [
        cfg.docker_bin,
        "run",
        "--name",
        container_name,
    ]
    for key in sorted(labels):
        argv.extend(["--label", f"{key}={labels[key]}"])
    argv.extend(
        [
            "--rm",
            "--init",
            "--user",
            _user_spec(cfg),
            "--network",
            _network_arg(plan.profile.network.mode),
            "--memory",
            plan.profile.limits.memory,
            "--memory-swap",
            plan.profile.limits.memory,
            "--pids-limit",
            str(plan.profile.limits.pids_limit),
            "--cpus",
            _format_cpus(plan.profile.limits.cpus),
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,mode=1777,size={plan.profile.limits.tmpfs_size}",
        ]
    )
    if plan.stdin is not None:
        argv.append("-i")
    env = {"HOME": cfg.home, "TMPDIR": "/tmp", **{str(k): str(v) for k, v in plan.env.items()}}
    for key in sorted(env):
        argv.extend(["--env", f"{key}={env[key]}"])
    for mount in plan.mounts:
        argv.extend(["--mount", _mount_arg(mount)])
    argv.extend(["--workdir", plan.cwd, cfg.image, *plan.command.argv])
    return DockerRunSpec(argv=tuple(argv), container_name=container_name, labels=labels, stdin=plan.stdin)


def _mount_arg(mount: object) -> str:
    if bool(getattr(mount, "mask", False)) and Path(str(getattr(mount, "container_path"))).is_dir():
        return f"type=tmpfs,dst={getattr(mount, 'container_path')},tmpfs-mode=0700,tmpfs-size=1m"

    src = str(getattr(mount, "host_path"))
    dst = str(getattr(mount, "container_path"))
    access = getattr(mount, "access")
    parts = ["type=bind", f"src={src}", f"dst={dst}"]
    if access == PathAccessMode.READ:
        parts.append("readonly")
    return ",".join(parts)


def _container_name(run_id: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_id).strip("-._")
    if not suffix:
        suffix = uuid.uuid4().hex
    return f"openppx-sandbox-{suffix[:48]}"


def _user_spec(config: DockerSandboxConfig) -> str:
    uid = config.uid if config.uid is not None else getattr(os, "getuid", lambda: 1000)()
    gid = config.gid if config.gid is not None else getattr(os, "getgid", lambda: 1000)()
    return f"{uid}:{gid}"


def _network_arg(mode: NetworkMode) -> str:
    if mode == NetworkMode.DISABLED:
        return "none"
    if mode == NetworkMode.ENABLED:
        return "bridge"
    raise ValueError("proxy_only network mode is not implemented")


def _format_cpus(value: float) -> str:
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return str(numeric)
