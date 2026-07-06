"""Pure sandbox diagnostic helpers used by doctor and tests."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SandboxDiagnostics:
    """Sandbox backend availability summary."""

    backend: str
    docker_bin: str
    docker_cli_available: bool
    image: str
    status: str
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-serializable diagnostics payload."""
        return {
            "backend": self.backend,
            "docker_bin": self.docker_bin,
            "docker_cli_available": self.docker_cli_available,
            "image": self.image,
            "status": self.status,
            "warnings": list(self.warnings),
        }


def build_sandbox_diagnostics(
    *,
    backend: str = "none",
    docker_bin: str = "docker",
    image: str = "openppx-sandbox:dev",
) -> SandboxDiagnostics:
    """Build a non-invasive sandbox diagnostics summary."""
    normalized_backend = backend.strip().lower() or "none"
    docker_available = shutil.which(docker_bin) is not None
    warnings: list[str] = []
    if normalized_backend == "bwrap":
        warnings.append("legacy bwrap does not provide docker-equivalent network/env/resource isolation")
    if normalized_backend == "docker" and not docker_available:
        warnings.append("docker CLI is not available")
    status = "disabled" if normalized_backend == "none" else "configured"
    return SandboxDiagnostics(
        backend=normalized_backend,
        docker_bin=docker_bin,
        docker_cli_available=docker_available,
        image=image,
        status=status,
        warnings=tuple(warnings),
    )


def list_docker_sandbox_containers(*, docker_bin: str = "docker") -> tuple[str, ...]:
    """Return openppx sandbox container names known to Docker."""
    try:
        completed = subprocess.run(
            [
                docker_bin,
                "ps",
                "-a",
                "--filter",
                "label=openppx.sandbox=1",
                "--format",
                "{{.Names}}",
            ],
            shell=False,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return ()
    if completed.returncode != 0:
        return ()
    return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())


def prune_docker_sandbox_containers(
    *,
    docker_bin: str = "docker",
    containers: tuple[str, ...] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Remove openppx sandbox containers and return removed names plus errors."""
    targets = containers if containers is not None else list_docker_sandbox_containers(docker_bin=docker_bin)
    removed: list[str] = []
    errors: list[str] = []
    for name in targets:
        try:
            completed = subprocess.run(
                [docker_bin, "rm", "-f", name],
                shell=False,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        if completed.returncode == 0:
            removed.append(name)
            continue
        error = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        errors.append(f"{name}: {error}")
    return tuple(removed), tuple(errors)
