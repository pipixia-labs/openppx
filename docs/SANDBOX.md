# Sandbox

openppx supports an opt-in Docker sandbox for dangerous local execution. The
default runtime behavior is unchanged: commands and skill APIs only enter the
sandbox when the caller explicitly requests it.

## Build the sandbox image

```bash
ppx sandbox build-image --image openppx-sandbox:dev
```

If Docker Hub is not reachable, pre-pull or mirror the base image and pass it
explicitly:

```bash
ppx sandbox build-image \
  --image openppx-sandbox:dev \
  --base-image registry.example/python:3.14-slim
```

Python and Node dependencies should be installed at trusted image build time,
not by runtime recipes:

```bash
ppx sandbox build-image \
  --image openppx-sandbox:dev \
  --python-requirements requirements.txt \
  --node-package-json package.json \
  --node-package-lock package-lock.json
```

The dependency build path uses a temporary Docker context and copies only the
explicit manifest files above.

## Exec opt-in

```python
exec_command("python --version", sandbox="docker")
exec_command("sh", sandbox="docker", pty=True, background=True)
```

Docker sandboxed exec uses:

- same-path workspace bind mounts
- `.git` read-only mount
- `.env` and credential-style files masked from reads
- no network by default
- bounded CPU, memory, PID, tmpfs, timeout, and output limits
- named/labeled containers with best-effort cleanup on timeout, kill, or remove

`background=True`, `yield_ms`, and `pty=True` all use `process_session` for
follow-up `poll`, `log`, `write`, `send-keys`, `kill`, and `remove` actions.

## Skill API opt-in

Command, Python, and Node declarative skill APIs can request Docker sandboxing:

```json
{
  "module": "demo_sdk",
  "function": "search",
  "sandbox": {"required": true}
}
```

Command API recipes use the same field:

```json
{
  "argv": ["python", "-c", "print('hello')"],
  "allow_system_executable": true,
  "sandbox": {"required": true}
}
```

Python and Node recipes run a small in-container runner shim. The recipe and
args payload is delivered through stdin, not through a large environment
variable.

## Network and image policy

Sandbox networking is disabled by default. A recipe may request
`"network": "enabled"`, but it is honored only when trusted configuration
allows it:

```bash
export OPENPPX_SANDBOX_ALLOW_NETWORK=1
```

A hard lock overrides all recipe requests:

```bash
export OPENPPX_SANDBOX_NETWORK_LOCK=disabled
```

Runtime recipes cannot choose arbitrary images. A recipe `sandbox.image` is
accepted only when it equals the configured default image or matches the
trusted allowlist:

```bash
export OPENPPX_SANDBOX_IMAGE=openppx-sandbox:dev
export OPENPPX_SANDBOX_TRUSTED_IMAGES='registry.example/openppx-sandbox:*'
```

Keep this allowlist narrow. Image selection is trusted configuration, not a
model-controlled capability.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OPENPPX_SANDBOX_BACKEND` | `none` | Baseline backend. `docker` prevents model-requested downgrade without confirmation. |
| `OPENPPX_EXEC_SANDBOX` | unset | Optional default sandbox for `exec_command`; keep unset unless deliberately enabling. |
| `OPENPPX_SANDBOX_DOCKER_BIN` | `docker` | Docker CLI path/name. |
| `OPENPPX_SANDBOX_IMAGE` | `openppx-sandbox:dev` | Default sandbox image. |
| `OPENPPX_SANDBOX_PYTHON_BASE_IMAGE` | `python:3.14-slim` | Base image for `ppx sandbox build-image`. |
| `OPENPPX_SANDBOX_PYTHON_REQUIREMENTS` | unset | Optional requirements file for image build. |
| `OPENPPX_SANDBOX_NODE_PACKAGE_JSON` | unset | Optional Node package manifest for image build. |
| `OPENPPX_SANDBOX_NODE_PACKAGE_LOCK` | unset | Optional lockfile; requires package.json. |
| `OPENPPX_SANDBOX_ALLOW_NETWORK` | unset | Allows recipe `network=enabled` when truthy. |
| `OPENPPX_SANDBOX_NETWORK_LOCK` | unset | `disabled` hard-locks network off. |
| `OPENPPX_SANDBOX_TRUSTED_IMAGES` | unset | Comma-separated image allowlist for recipe `sandbox.image`. |
| `OPENPPX_SANDBOX_TIMEOUT_MAX_SECONDS` | `60` for exec, `3600` for API runners | Trusted timeout cap. |
| `OPENPPX_SANDBOX_MEMORY` | `1024m` | Docker memory and memory-swap limit. |
| `OPENPPX_SANDBOX_CPUS` | `2` | Docker CPU limit. |
| `OPENPPX_SANDBOX_PIDS_LIMIT` | `256` | Docker PID limit. |
| `OPENPPX_SANDBOX_TMPFS_SIZE` | `256m` | `/tmp` tmpfs size. |

## Diagnostics and cleanup

```bash
ppx doctor
ppx sandbox prune
```

`doctor` reports Docker availability and leaked openppx sandbox containers.
`sandbox prune` explicitly removes containers labeled as openppx sandbox runs.

## Testing

Regular tests do not require Docker:

```bash
python -m pytest tests/test_runtime_sandbox.py tests/test_cli_sandbox.py tests/test_tools.py -q
```

Real Docker integration tests are opt-in:

```bash
OPENPPX_RUN_DOCKER_SANDBOX_TESTS=1 \
python -m pytest tests/test_docker_sandbox_integration.py -q
```

Run `ppx sandbox build-image` first so the configured image exists locally.

## Threat model notes

- Docker is used as a pragmatic isolation layer, not a perfect security
  boundary.
- Access to the Docker daemon is trusted and effectively host-powerful.
- Backend argv construction is part of the trusted computing base.
- Model-controlled inputs must not become raw Docker flags, arbitrary mounts,
  privileged mode, or unrestricted image selection.
