"""Supervised long-task execution runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..tooling.skills_adapter import get_registry
from ..gui.job_coordinator import gui_task_job_cancel
from ..gui.job_coordinator import gui_task_job_output
from ..gui.job_coordinator import gui_task_job_status
from ..gui.job_coordinator import resume_gui_task_job
from .artifact_service import load_artifact_config
from .browser_remote_provider import BrowserRemoteJob
from .browser_remote_provider import BrowserRemoteProviderStore
from .browser_remote_provider import browser_remote_job_payload
from .browser_remote_job_protocol import BrowserRemoteJobProtocolConfig
from .browser_remote_job_protocol import browser_remote_job_protocol_from_payload
from .browser_remote_job_protocol import call_browser_remote_job_cancel
from .browser_remote_job_protocol import call_browser_remote_job_output
from .browser_remote_job_protocol import call_browser_remote_job_pause
from .browser_remote_job_protocol import call_browser_remote_job_resume
from .browser_remote_job_protocol import call_browser_remote_job_status
from .browser_remote_job_protocol import normalize_browser_remote_job_checkpoint_payload
from .browser_remote_job_protocol import normalize_browser_remote_job_snapshot
from .checkpoint_schema import normalize_task_checkpoint_payload
from .context_engine import ContextSummary, LongTaskContextStore
from .process_sessions import get_process_session_manager
from .mcp_proxy import cancel_mcp_proxy_task
from .mcp_proxy import is_mcp_proxy_task_active
from .mcp_job_protocol import call_mcp_job_cancel
from .mcp_job_protocol import call_mcp_job_output
from .mcp_job_protocol import call_mcp_job_pause
from .mcp_job_protocol import call_mcp_job_resume
from .mcp_job_protocol import call_mcp_job_status
from .mcp_job_protocol import extract_path
from .mcp_job_protocol import mcp_job_protocol_from_payload
from .mcp_job_protocol import mcp_job_status_snapshot
from .mcp_job_protocol import normalize_mcp_job_checkpoint_payload
from .task_store import (
    TASK_ACTIVE_STATUSES,
    TASK_TERMINAL_STATUSES,
    TaskArtifactStore,
    TaskCheckpointStore,
    TaskDeliveryStore,
    TaskEventStore,
    TaskInputStore,
    TaskRun,
    TaskStore,
    ToolCallRecordStore,
)
from .sync_tool_proxy import is_sync_proxy_task_attached
from .sync_tool_proxy import request_sync_proxy_task_stop


DEFAULT_INLINE_BUDGET_MS = 5_000
MAX_INLINE_BUDGET_MS = 120_000
DEFAULT_OUTPUT_ARTIFACT_THRESHOLD_CHARS = 50_000
MAX_TERMINAL_SUMMARY_CHARS = 4_000
DEFAULT_STUCK_TASK_AFTER_MS = 30 * 60 * 1000
DEFAULT_STALE_LOST_AFTER_MS = 5 * 60 * 1000
DEFAULT_REMEDIATION_LEASE_MS = 10_000
DEFAULT_TERMINAL_RETENTION_MS = 14 * 24 * 60 * 60 * 1000
DEFAULT_CHECKPOINT_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
DEFAULT_CHECKPOINT_KEEP_LATEST_PER_TASK = 3
PROCESS_RUNNER_CAPABILITIES: dict[str, bool] = {
    "status": True,
    "cancel": True,
    "interrupt": True,
    "output": True,
    "artifact": True,
    "rejoin": True,
    "pause": False,
    "checkpoint": False,
}
GUI_JOB_RUNNER_CAPABILITIES: dict[str, bool] = {
    "status": True,
    "cancel": True,
    "interrupt": True,
    "output": True,
    "artifact": False,
    "rejoin": True,
    "pause": True,
    "checkpoint": True,
    "resume": True,
}
BROWSER_REMOTE_RUNNER_CAPABILITIES: dict[str, bool] = {
    "status": True,
    "cancel": False,
    "interrupt": False,
    "output": True,
    "artifact": False,
    "rejoin": True,
    "pause": False,
    "checkpoint": False,
    "resume": False,
}


@dataclass(frozen=True, slots=True)
class TaskInvocationContext:
    """Metadata that binds a task to one ADK/tool invocation."""

    user_id: str = ""
    session_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    channel: str = ""
    chat_id: str = ""
    invocation_id: str = ""
    function_call_id: str = ""
    tool_call_id: str = ""
    owner_key: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionRecipe:
    """Resolved process execution recipe."""

    title: str
    command: str
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    scope_key: str | None
    use_pty: bool = False
    task_kind: str = "skill_api"
    runner_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Result returned by supervised execution."""

    mode: str
    status: str
    output: str = ""
    task: TaskRun | None = None
    exit_code: int | None = None
    replayed: bool = False
    error: str = ""

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable payload."""
        payload: dict[str, Any] = {
            "ok": not self.error,
            "mode": self.mode,
            "status": self.status,
        }
        if self.output:
            payload["output"] = self.output
        if self.task is not None:
            payload["task_id"] = self.task.task_id
            payload["title"] = self.task.title
            payload["progress_summary"] = self.task.progress_summary
        if self.exit_code is not None:
            payload["exit_code"] = self.exit_code
        if self.replayed:
            payload["replayed"] = True
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class ApiRecipeRunnerSpec:
    """Declarative API recipe runner registration."""

    name: str
    logical_runner: str
    env_var: str
    runner_filename: str
    suffixes: tuple[str, ...]
    load_recipe: Callable[[Path], dict[str, Any]]
    generic_json: bool = False

    def matches_explicit_name(self, api_name: str) -> bool:
        """Return whether the API name explicitly names this recipe kind."""
        lowered = Path(api_name).name.lower()
        return any(lowered.endswith(suffix) for suffix in self.suffixes)

    def catalog_payload(self) -> dict[str, Any]:
        """Return an inspectable description of this recipe runner."""
        return {
            "name": self.name,
            "logical_runner": self.logical_runner,
            "suffixes": list(self.suffixes),
            "generic_json": self.generic_json,
            "runner_filename": self.runner_filename,
        }


class SkillApiRuntime:
    """Resolve dynamic skill API names into supervised execution recipes."""

    def resolve(
        self,
        *,
        skill_name: str,
        api_name: str,
        args: Any = None,
        scope_key: str | None = None,
    ) -> ExecutionRecipe:
        """Resolve one skill API to a supervised execution recipe.

        Scripts are resolved first for backwards compatibility. Declarative
        recipes under ``apis/`` are resolved next and executed by narrow
        subprocess runners, so unknown-duration API calls still enter the same
        long-task envelope without running arbitrary code in the agent loop.
        """
        skill = self._find_skill(skill_name)
        skill_root = skill.path.parent
        normalized_api = self._normalize_api_name(api_name)
        script_path = self._try_resolve_script(skill_root=skill_root, api_name=normalized_api)
        if script_path is None:
            return self._resolve_recipe_api(
                skill_name=skill.name,
                skill_root=skill_root,
                api_name=normalized_api,
                args=args,
                scope_key=scope_key,
            )
        argv = self._argv_for_script(script_path, args=args)
        env = os.environ.copy()
        if args is not None:
            env["OPENPPX_SKILL_ARGS_JSON"] = json.dumps(args, ensure_ascii=False, default=str)
        return ExecutionRecipe(
            title=f"{skill.name}:{normalized_api}",
            command=shlex.join(argv),
            argv=argv,
            cwd=skill_root,
            env=env,
            scope_key=scope_key,
            use_pty=False,
        )

    @staticmethod
    def _find_skill(skill_name: str) -> Any:
        normalized = str(skill_name or "").strip()
        if not normalized:
            raise ValueError("skill_name is required")
        for info in get_registry().list_skills():
            if info.name == normalized:
                return info
        raise ValueError(f"skill {normalized!r} not found")

    @staticmethod
    def _normalize_api_name(api_name: str) -> str:
        normalized = str(api_name or "").strip()
        if not normalized:
            raise ValueError("api_name is required")
        return normalized

    @classmethod
    def _try_resolve_script(cls, *, skill_root: Path, api_name: str) -> Path | None:
        candidate_names: list[str] = [api_name]
        if "." not in Path(api_name).name:
            candidate_names.extend([f"{api_name}.py", f"{api_name}.sh"])
        candidates = cls._safe_candidates(
            skill_root=skill_root,
            relative_roots=["scripts", ""],
            candidate_names=candidate_names,
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def _resolve_recipe_api(
        cls,
        *,
        skill_name: str,
        skill_root: Path,
        api_name: str,
        args: Any,
        scope_key: str | None,
    ) -> ExecutionRecipe:
        specs = cls._api_recipe_runner_specs()
        ordered_specs = [
            *[spec for spec in specs if spec.matches_explicit_name(api_name)],
            *[spec for spec in specs if not spec.matches_explicit_name(api_name)],
        ]
        for spec in ordered_specs:
            recipe_path = cls._try_resolve_recipe(skill_root=skill_root, api_name=api_name, spec=spec)
            if recipe_path is not None:
                return cls._resolve_api_recipe_from_path(
                    spec=spec,
                    skill_name=skill_name,
                    skill_root=skill_root,
                    api_name=api_name,
                    recipe_path=recipe_path,
                    args=args,
                    scope_key=scope_key,
                )
        script_paths = cls._script_search_paths(skill_root=skill_root, api_name=api_name)
        recipe_paths = [
            path
            for spec in specs
            for path in cls._recipe_search_paths(skill_root=skill_root, api_name=api_name, spec=spec)
        ]
        searched = ", ".join(
            path.relative_to(skill_root).as_posix() for path in [*script_paths, *recipe_paths]
        )
        raise ValueError(f"skill API not found for {api_name!r}; searched: {searched}")

    @classmethod
    def _resolve_api_recipe_from_path(
        cls,
        *,
        spec: ApiRecipeRunnerSpec,
        skill_name: str,
        skill_root: Path,
        api_name: str,
        recipe_path: Path,
        args: Any,
        scope_key: str | None,
    ) -> ExecutionRecipe:
        recipe = spec.load_recipe(recipe_path)
        env = os.environ.copy()
        env[spec.env_var] = json.dumps(recipe, ensure_ascii=False, default=str)
        if args is not None:
            env["OPENPPX_SKILL_ARGS_JSON"] = json.dumps(args, ensure_ascii=False, default=str)
        argv = [sys.executable, str(Path(__file__).with_name(spec.runner_filename))]
        return ExecutionRecipe(
            title=f"{skill_name}:{api_name}",
            command=shlex.join(argv),
            argv=argv,
            cwd=skill_root,
            env=env,
            scope_key=scope_key,
            use_pty=False,
            task_kind="api_call",
            runner_payload={
                "logical_runner": spec.logical_runner,
                "recipe_runner": spec.name,
                "api_recipe": recipe_path.relative_to(skill_root).as_posix(),
            },
        )

    @classmethod
    def _resolve_http_api_from_path(
        cls,
        *,
        skill_name: str,
        skill_root: Path,
        api_name: str,
        recipe_path: Path,
        args: Any,
        scope_key: str | None,
    ) -> ExecutionRecipe:
        return cls._resolve_api_recipe_from_path(
            spec=cls._http_recipe_runner_spec(),
            skill_name=skill_name,
            skill_root=skill_root,
            api_name=api_name,
            recipe_path=recipe_path,
            args=args,
            scope_key=scope_key,
        )

    @classmethod
    def _resolve_python_api_from_path(
        cls,
        *,
        skill_name: str,
        skill_root: Path,
        api_name: str,
        recipe_path: Path,
        args: Any,
        scope_key: str | None,
    ) -> ExecutionRecipe:
        return cls._resolve_api_recipe_from_path(
            spec=cls._python_recipe_runner_spec(),
            skill_name=skill_name,
            skill_root=skill_root,
            api_name=api_name,
            recipe_path=recipe_path,
            args=args,
            scope_key=scope_key,
        )

    @classmethod
    def _try_resolve_http_recipe(cls, *, skill_root: Path, api_name: str) -> Path | None:
        return cls._try_resolve_recipe(skill_root=skill_root, api_name=api_name, spec=cls._http_recipe_runner_spec())

    @classmethod
    def _try_resolve_python_recipe(cls, *, skill_root: Path, api_name: str) -> Path | None:
        return cls._try_resolve_recipe(skill_root=skill_root, api_name=api_name, spec=cls._python_recipe_runner_spec())

    @classmethod
    def _try_resolve_recipe(cls, *, skill_root: Path, api_name: str, spec: ApiRecipeRunnerSpec) -> Path | None:
        for candidate in cls._recipe_search_paths(skill_root=skill_root, api_name=api_name, spec=spec):
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def _script_search_paths(cls, *, skill_root: Path, api_name: str) -> list[Path]:
        candidate_names: list[str] = [api_name]
        if "." not in Path(api_name).name:
            candidate_names.extend([f"{api_name}.py", f"{api_name}.sh"])
        return cls._safe_candidates(
            skill_root=skill_root,
            relative_roots=["scripts", ""],
            candidate_names=candidate_names,
        )

    @classmethod
    def _http_recipe_search_paths(cls, *, skill_root: Path, api_name: str) -> list[Path]:
        return cls._recipe_search_paths(skill_root=skill_root, api_name=api_name, spec=cls._http_recipe_runner_spec())

    @classmethod
    def _python_recipe_search_paths(cls, *, skill_root: Path, api_name: str) -> list[Path]:
        return cls._recipe_search_paths(skill_root=skill_root, api_name=api_name, spec=cls._python_recipe_runner_spec())

    @classmethod
    def _recipe_search_paths(cls, *, skill_root: Path, api_name: str, spec: ApiRecipeRunnerSpec) -> list[Path]:
        candidate_names: list[str] = [api_name]
        if spec.generic_json:
            if Path(api_name).suffix.lower() != ".json":
                candidate_names.append(f"{api_name}.json")
                candidate_names.extend([f"{api_name}{suffix}" for suffix in spec.suffixes])
        elif not spec.matches_explicit_name(api_name):
            candidate_names.extend([f"{api_name}{suffix}" for suffix in spec.suffixes])
        return cls._safe_candidates(
            skill_root=skill_root,
            relative_roots=["apis"],
            candidate_names=candidate_names,
        )

    @classmethod
    def _is_python_recipe_name(cls, api_name: str) -> bool:
        return cls._python_recipe_runner_spec().matches_explicit_name(api_name)

    @classmethod
    def _api_recipe_runner_specs(cls) -> tuple[ApiRecipeRunnerSpec, ...]:
        return (
            cls._http_recipe_runner_spec(),
            cls._python_recipe_runner_spec(),
            cls._node_recipe_runner_spec(),
            cls._command_recipe_runner_spec(),
        )

    @classmethod
    def api_recipe_runner_catalog(cls) -> dict[str, Any]:
        """Return the supported declarative skill API runner catalog."""
        specs = cls._api_recipe_runner_specs()
        return {
            "schema_version": 1,
            "default_search_root": "apis/",
            "items": [spec.catalog_payload() for spec in specs],
            "notes": [
                "Scripts under scripts/ remain supported for backwards compatibility.",
                "Unknown-duration calls enter the same supervised execution envelope.",
                "Use command recipes as the stable bridge for Go, Java, Ruby, and other SDK CLIs.",
            ],
        }

    @classmethod
    def _http_recipe_runner_spec(cls) -> ApiRecipeRunnerSpec:
        return ApiRecipeRunnerSpec(
            name="http",
            logical_runner="http_api",
            env_var="OPENPPX_HTTP_API_RECIPE_JSON",
            runner_filename="http_api_runner.py",
            suffixes=(".http.json",),
            load_recipe=cls._load_http_recipe,
            generic_json=True,
        )

    @classmethod
    def _python_recipe_runner_spec(cls) -> ApiRecipeRunnerSpec:
        return ApiRecipeRunnerSpec(
            name="python",
            logical_runner="python_api",
            env_var="OPENPPX_PYTHON_API_RECIPE_JSON",
            runner_filename="python_api_runner.py",
            suffixes=(".python.json", ".sdk.json"),
            load_recipe=cls._load_python_recipe,
            generic_json=False,
        )

    @classmethod
    def _node_recipe_runner_spec(cls) -> ApiRecipeRunnerSpec:
        return ApiRecipeRunnerSpec(
            name="node",
            logical_runner="node_api",
            env_var="OPENPPX_NODE_API_RECIPE_JSON",
            runner_filename="node_api_runner.py",
            suffixes=(".node.json", ".js.json"),
            load_recipe=cls._load_node_recipe,
            generic_json=False,
        )

    @classmethod
    def _command_recipe_runner_spec(cls) -> ApiRecipeRunnerSpec:
        return ApiRecipeRunnerSpec(
            name="command",
            logical_runner="command_api",
            env_var="OPENPPX_COMMAND_API_RECIPE_JSON",
            runner_filename="command_api_runner.py",
            suffixes=(".command.json", ".cmd.json"),
            load_recipe=cls._load_command_recipe,
            generic_json=False,
        )

    @staticmethod
    def _safe_candidates(
        *,
        skill_root: Path,
        relative_roots: list[str],
        candidate_names: list[str],
    ) -> list[Path]:
        root = skill_root.resolve(strict=False)
        candidates: list[Path] = []
        seen: set[Path] = set()
        for relative_root in relative_roots:
            base = root / relative_root if relative_root else root
            for name in candidate_names:
                resolved = (base / name).resolve(strict=False)
                try:
                    resolved.relative_to(root)
                except ValueError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                candidates.append(resolved)
        return candidates

    @staticmethod
    def _load_http_recipe(recipe_path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(recipe_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid HTTP API recipe JSON in {recipe_path.name!r}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"HTTP API recipe {recipe_path.name!r} must be a JSON object")
        if not str(raw.get("url", "")).strip():
            raise ValueError(f"HTTP API recipe {recipe_path.name!r} must define a non-empty url")
        return raw

    @staticmethod
    def _load_python_recipe(recipe_path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(recipe_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid Python API recipe JSON in {recipe_path.name!r}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Python API recipe {recipe_path.name!r} must be a JSON object")
        if "callable" in raw:
            callable_ref = str(raw.get("callable", "")).strip()
            if not callable_ref or ":" not in callable_ref:
                raise ValueError(f"Python API recipe {recipe_path.name!r} callable must be module:function")
            module_name, function_name = callable_ref.split(":", 1)
        else:
            module_name = str(raw.get("module", "")).strip()
            function_name = str(raw.get("function", "")).strip()
        if not _VALID_PYTHON_DOTTED_NAME_RE.fullmatch(module_name):
            raise ValueError(f"Python API recipe {recipe_path.name!r} must define a valid module")
        if not _VALID_PYTHON_DOTTED_NAME_RE.fullmatch(function_name):
            raise ValueError(f"Python API recipe {recipe_path.name!r} must define a valid function")
        return raw

    @staticmethod
    def _load_node_recipe(recipe_path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(recipe_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid Node API recipe JSON in {recipe_path.name!r}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Node API recipe {recipe_path.name!r} must be a JSON object")
        module_path = str(raw.get("module") or raw.get("file") or "").strip()
        function_name = str(raw.get("function", "default") or "default").strip()
        if not module_path:
            raise ValueError(f"Node API recipe {recipe_path.name!r} must define a module or file")
        if Path(module_path).is_absolute():
            raise ValueError(f"Node API recipe {recipe_path.name!r} module must be relative to the skill root")
        if ".." in Path(module_path).parts:
            raise ValueError(f"Node API recipe {recipe_path.name!r} module must stay under the skill root")
        if Path(module_path).suffix.lower() not in {".js", ".mjs", ".cjs"}:
            raise ValueError(f"Node API recipe {recipe_path.name!r} module must be a .js, .mjs, or .cjs file")
        if not _VALID_JS_DOTTED_NAME_RE.fullmatch(function_name):
            raise ValueError(f"Node API recipe {recipe_path.name!r} must define a valid function")
        return raw

    @staticmethod
    def _load_command_recipe(recipe_path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(recipe_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid Command API recipe JSON in {recipe_path.name!r}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Command API recipe {recipe_path.name!r} must be a JSON object")
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv:
            raise ValueError(f"Command API recipe {recipe_path.name!r} must define a non-empty argv array")
        if any(not isinstance(item, str) or not item.strip() for item in argv):
            raise ValueError(f"Command API recipe {recipe_path.name!r} argv entries must be non-empty strings")
        executable = Path(str(argv[0]).strip())
        if not bool(raw.get("allow_system_executable", False)):
            if executable.is_absolute() or ".." in executable.parts:
                raise ValueError(
                    f"Command API recipe {recipe_path.name!r} executable must stay under the skill root"
                )
            if len(executable.parts) < 2:
                raise ValueError(
                    f"Command API recipe {recipe_path.name!r} bare executable requires allow_system_executable=true"
                )
            skill_root = recipe_path.parent.parent.resolve(strict=False)
            resolved = (skill_root / executable).resolve(strict=False)
            try:
                resolved.relative_to(skill_root)
            except ValueError as exc:
                raise ValueError(
                    f"Command API recipe {recipe_path.name!r} executable must resolve under the skill root"
                ) from exc
        env = raw.get("env")
        if env is not None and not isinstance(env, dict):
            raise ValueError(f"Command API recipe {recipe_path.name!r} env must be a JSON object")
        return raw

    @staticmethod
    def _argv_for_script(script_path: Path, *, args: Any = None) -> list[str]:
        suffix = script_path.suffix.lower()
        if suffix == ".py":
            argv = [sys.executable, str(script_path)]
        elif suffix == ".sh":
            argv = ["/bin/sh", str(script_path)]
        elif os.access(script_path, os.X_OK):
            argv = [str(script_path)]
        else:
            raise ValueError(f"skill API script {script_path.name!r} is not executable or a supported script")

        extra_argv: list[str] = []
        if isinstance(args, dict) and isinstance(args.get("argv"), list):
            extra_argv = [str(item) for item in args["argv"]]
        elif isinstance(args, list):
            extra_argv = [str(item) for item in args]
        return [*argv, *extra_argv]


_VALID_PYTHON_DOTTED_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_VALID_JS_DOTTED_NAME_RE = re.compile(r"(?:default|[A-Za-z_$][A-Za-z0-9_$]*)(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*")


class ProcessExecutionSupervisor:
    """Run process-backed tasks under the long-task execution envelope."""

    def __init__(
        self,
        *,
        task_store: TaskStore | None = None,
        event_store: TaskEventStore | None = None,
        tool_call_store: ToolCallRecordStore | None = None,
        skill_runtime: SkillApiRuntime | None = None,
    ) -> None:
        self.task_store = task_store or TaskStore()
        self.event_store = event_store or TaskEventStore(db_path=self.task_store.db_path)
        self.tool_call_store = tool_call_store or ToolCallRecordStore(db_path=self.task_store.db_path)
        self.skill_runtime = skill_runtime or SkillApiRuntime()

    def invoke_skill_api(
        self,
        *,
        skill_name: str,
        api_name: str,
        args: Any,
        inline_budget_ms: int | None,
        context: TaskInvocationContext | None = None,
        scope_key: str | None = None,
        restartable: bool = False,
    ) -> ExecutionResult:
        """Invoke a dynamic skill API through supervised process execution."""
        ctx = context or TaskInvocationContext()
        recipe = self.skill_runtime.resolve(
            skill_name=skill_name,
            api_name=api_name,
            args=args,
            scope_key=scope_key,
        )
        if restartable:
            recipe = replace(
                recipe,
                runner_payload={
                    **recipe.runner_payload,
                    "restartable": True,
                    "restart_boundary": {
                        "kind": "skill_api",
                        "skill_name": skill_name,
                        "api_name": api_name,
                        "args": args,
                        "scope_key": scope_key,
                        "created_at_ms": int(time.time() * 1000),
                    },
                },
            )
        args_hash = _stable_hash({"skill": skill_name, "api": api_name, "args": args})
        idempotency_key = _idempotency_key(
            context=ctx,
            tool_name="invoke_skill_api",
            args_hash=args_hash,
        )
        record, created = self.tool_call_store.create_or_get(
            idempotency_key=idempotency_key,
            tool_name="invoke_skill_api",
            args_hash=args_hash,
        )
        if not created:
            if record.task_id:
                task = self.task_store.get_task(record.task_id)
                if task is not None:
                    return ExecutionResult(mode="task", status=task.status, task=task, replayed=True)
            if record.status == "completed" and record.result:
                result = record.result
                return ExecutionResult(
                    mode=str(result.get("mode", "inline")),
                    status=str(result.get("status", "completed")),
                    output=str(result.get("output", "")),
                    exit_code=result.get("exit_code") if isinstance(result.get("exit_code"), int) else None,
                    replayed=True,
                )

        try:
            return self._run_process_recipe(
                recipe=recipe,
                inline_budget_ms=inline_budget_ms,
                context=ctx,
                idempotency_key=idempotency_key,
                dedupe_key=f"skill_api:{skill_name}:{api_name}:{args_hash}",
            )
        except Exception as exc:
            self.tool_call_store.settle(idempotency_key, status="failed", error=str(exc))
            return ExecutionResult(mode="error", status="failed", error=str(exc))

    def _run_process_recipe(
        self,
        *,
        recipe: ExecutionRecipe,
        inline_budget_ms: int | None,
        context: TaskInvocationContext,
        idempotency_key: str,
        dedupe_key: str,
    ) -> ExecutionResult:
        manager = get_process_session_manager()
        session, warnings = manager.start_session(
            command=recipe.command,
            argv=recipe.argv,
            cwd=recipe.cwd,
            env=recipe.env,
            use_pty=recipe.use_pty,
            scope_key=recipe.scope_key,
        )
        wait_ms = _normalize_inline_budget_ms(inline_budget_ms)
        polled = _poll_until_budget(
            session_id=session.session_id,
            timeout_ms=wait_ms,
            scope_key=recipe.scope_key,
        )
        if polled is None:
            self.tool_call_store.settle(idempotency_key, status="failed", error="failed to poll process")
            return ExecutionResult(mode="error", status="failed", error="failed to poll process")

        if bool(polled.get("exited")):
            output = _format_process_output(polled, warnings=warnings)
            exit_code = polled.get("exit_code") if isinstance(polled.get("exit_code"), int) else None
            status = "completed" if exit_code == 0 else "failed"
            manager.remove_session(session.session_id, scope_key=recipe.scope_key)
            result = {"mode": "inline", "status": status, "output": output, "exit_code": exit_code}
            self.tool_call_store.settle(idempotency_key, status=status, result=result)
            return ExecutionResult(mode="inline", status=status, output=output, exit_code=exit_code)

        manager.mark_backgrounded(session.session_id, scope_key=recipe.scope_key)
        progress = _running_summary(polled, warnings=warnings)
        runner_payload = {
            "runner": "process",
            "command": recipe.command,
            "cwd": str(recipe.cwd),
            "scope_key": recipe.scope_key,
            "pid": session.process.pid,
            "inline_budget_ms": wait_ms,
            "delivery": {
                "channel": context.channel,
                "chat_id": context.chat_id,
            },
        }
        runner_payload.update(recipe.runner_payload)
        task = self.task_store.create_task(
            kind=recipe.task_kind,
            status="running",
            title=recipe.title,
            owner_key=context.owner_key or context.user_id,
            user_id=context.user_id,
            thread_id=context.thread_id or context.session_id,
            session_id=context.session_id,
            turn_id=context.turn_id or context.invocation_id,
            invocation_id=context.invocation_id,
            function_call_id=context.function_call_id,
            tool_call_id=context.tool_call_id,
            dedupe_key=dedupe_key,
            external_ref=session.session_id,
            runner_payload=runner_payload,
            runner_capabilities=PROCESS_RUNNER_CAPABILITIES,
            resume_policy="rejoin",
            stop_policy="interrupt_task",
            cancel_policy="kill_process",
            progress_summary=progress,
        )
        self.event_store.append_event(
            task.task_id,
            "task.started",
            message=progress,
            payload={"session_id": session.session_id, "pid": session.process.pid, "warnings": warnings},
        )
        self.tool_call_store.link_task(idempotency_key, task.task_id, status="running")
        return ExecutionResult(mode="task", status="running", task=task)


class TaskRunnerAdapter:
    """Runner-specific task control and observation behavior."""

    name = "generic"

    def matches(self, task: TaskRun) -> bool:
        """Return whether this adapter handles the task."""
        return True

    def controls(self, task: TaskRun) -> dict[str, Any]:
        """Return conservative UI/action controls for this task."""
        can_resume = (task.status == "running" and bool(task.runner_capabilities.get("rejoin"))) or _can_resume_from_restart_boundary(task)
        return _build_task_controls(
            task,
            can_interrupt=False,
            interrupt_reason=_status_or_runner_reason(
                task,
                required_status="running",
                unsupported_reason="runner does not support interrupt",
            ),
            can_cancel=False,
            cancel_reason="task is terminal" if task.status in TASK_TERMINAL_STATUSES else "runner does not support cancel",
            can_pause=False,
            pause_reason=_pause_unavailable_reason(task),
            can_resume=can_resume,
            resume_reason=_resume_unavailable_reason(task),
        )

    def sync_task(self, controller: "TaskController", task: TaskRun, *, poll_timeout_ms: int) -> TaskRun | None:
        """Synchronize one task with its backing runtime."""
        _ = controller, poll_timeout_ms
        return task

    def reconcile_stale_task(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        stale_lost_after_ms: int,
        now_ms: int | None,
    ) -> TaskRun | None:
        """Reconcile a stale task for this runner."""
        _ = controller, stale_lost_after_ms, now_ms
        return task

    def task_output(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return retained task output for this runner."""
        return controller._generic_task_output(task, artifacts=artifacts)

    def interrupt_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Interrupt one task for this runner."""
        return _unsupported_runner_stop_payload(controller, task)

    def cancel_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Cancel one task for this runner."""
        return _unsupported_runner_stop_payload(controller, task)

    def pause_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Pause one task for this runner only at a durable boundary."""
        payload = controller._task_payload(task)
        if task.status == "paused":
            return {
                "ok": True,
                "task": payload,
                "action": "already_paused",
                "message": "Task is already paused.",
            }
        if task.status in TASK_TERMINAL_STATUSES:
            return {
                "ok": False,
                "task": payload,
                "action": "terminal",
                "message": "Task is terminal and cannot be paused.",
            }
        if task.status != "running":
            return {
                "ok": False,
                "task": payload,
                "action": "not_running",
                "message": f"Task is {task.status!r}, not running.",
            }
        capabilities = task.runner_capabilities
        if not bool(capabilities.get("pause")) and not bool(capabilities.get("checkpoint")):
            return {
                "ok": False,
                "task": payload,
                "action": "not_supported",
                "message": (
                    "This runner does not advertise a durable pause/checkpoint capability. "
                    "Use interrupt_task for best-effort stop, or cancel_task if the user abandons the task."
                ),
            }
        return {
            "ok": False,
            "task": payload,
            "action": "adapter_missing",
            "message": (
                "This runner advertises pause/checkpoint capability, but openppx has no registered "
                "runner-specific pause adapter for it yet."
            ),
        }

    def resume_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Resume or rejoin one task for this runner only at durable boundaries."""
        payload = controller._task_payload(task)
        policy = task.resume_policy or "not_resumable"
        if task.status == "running" and bool(task.runner_capabilities.get("rejoin")):
            return {
                "ok": True,
                "task": payload,
                "action": "rejoined",
                "resume_policy": policy,
                "message": "Task is already running; rejoined the current durable task boundary.",
            }
        if _can_resume_from_restart_boundary(task):
            restarted = controller.restart_task(task.task_id, inline_budget_ms=0)
            if restarted.get("ok"):
                return {
                    "ok": True,
                    "task": payload,
                    "action": "restarted_from_boundary",
                    "resume_policy": "restart_from_boundary",
                    "result": restarted.get("result", {}),
                    "message": "Started a new TaskRun from the recorded restart boundary.",
                }
            return {
                "ok": False,
                "task": payload,
                "action": str(restarted.get("action") or "restart_failed"),
                "resume_policy": "restart_from_boundary",
                "result": restarted,
                "message": str(restarted.get("message") or "Failed to restart from the recorded boundary."),
            }
        if task.status in {"waiting_user", "waiting_approval"}:
            return {
                "ok": False,
                "task": payload,
                "action": "waiting",
                "resume_policy": policy,
                "message": "Task is waiting for input or approval; provide the requested input instead of resuming.",
            }
        if (
            task.status == "paused"
            and policy == "checkpoint"
            and bool(task.runner_capabilities.get("checkpoint"))
            and bool(task.checkpoint_ref)
        ):
            return {
                "ok": False,
                "task": payload,
                "action": "adapter_missing",
                "resume_policy": policy,
                "message": (
                    "This task has a checkpoint, but openppx has no registered runner-specific "
                    "resume adapter for it yet."
                ),
            }
        if task.status in TASK_TERMINAL_STATUSES:
            return {
                "ok": False,
                "task": payload,
                "action": "terminal",
                "resume_policy": policy,
                "message": "Task is terminal and cannot be resumed by this runner.",
            }
        return {
            "ok": False,
            "task": payload,
            "action": "not_resumable",
            "resume_policy": policy,
            "message": (
                "This runner does not expose a checkpoint or restart implementation for this task. "
                "Continue from the previous durable boundary or start a new task explicitly."
            ),
        }


class ProcessTaskRunnerAdapter(TaskRunnerAdapter):
    """Task runner adapter for process-backed execution."""

    name = "process"

    def matches(self, task: TaskRun) -> bool:
        """Return whether the task is process-backed."""
        return _task_runner_name(task) == "process"

    def controls(self, task: TaskRun) -> dict[str, Any]:
        """Return controls for process-backed tasks."""
        running = task.status == "running"
        terminal = task.status in TASK_TERMINAL_STATUSES
        capabilities = task.runner_capabilities
        has_ref = bool(task.external_ref)
        can_resume = (running and bool(capabilities.get("rejoin"))) or _can_resume_from_restart_boundary(task)
        return _build_task_controls(
            task,
            can_interrupt=running and bool(capabilities.get("interrupt")) and has_ref,
            interrupt_reason=_status_or_runner_reason(
                task,
                required_status="running",
                unsupported_reason="runner does not support interrupt",
            )
            if not has_ref or not bool(capabilities.get("interrupt")) or not running
            else "",
            can_cancel=(not terminal) and bool(capabilities.get("cancel")) and has_ref,
            cancel_reason=(
                "task is terminal"
                if terminal
                else "runner does not support cancel"
                if not bool(capabilities.get("cancel")) or not has_ref
                else ""
            ),
            can_pause=False,
            pause_reason=_pause_unavailable_reason(task),
            can_resume=can_resume,
            resume_reason=_resume_unavailable_reason(task),
        )

    def sync_task(self, controller: "TaskController", task: TaskRun, *, poll_timeout_ms: int) -> TaskRun | None:
        """Synchronize process state."""
        return controller._sync_process_task(task, poll_timeout_ms=poll_timeout_ms)

    def reconcile_stale_task(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        stale_lost_after_ms: int,
        now_ms: int | None,
    ) -> TaskRun | None:
        """Reconcile a stale process-backed task."""
        return controller._reconcile_process_stale_task(
            task,
            stale_lost_after_ms=stale_lost_after_ms,
            now_ms=now_ms,
        )

    def task_output(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return retained process output."""
        return controller._process_task_output(task, artifacts=artifacts)

    def interrupt_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Interrupt a process-backed task."""
        return controller._stop_process_task(task, terminal_status="interrupted", event_type="task.interrupted")

    def cancel_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Cancel a process-backed task."""
        return controller._stop_process_task(task, terminal_status="cancelled", event_type="task.cancelled")


class GuiJobTaskRunnerAdapter(TaskRunnerAdapter):
    """Task runner adapter for checkpointable GUI/background browser jobs."""

    name = "gui_job"

    def matches(self, task: TaskRun) -> bool:
        """Return whether the task is backed by the GUI job coordinator."""
        return _task_runner_name(task) == "gui_job"

    def controls(self, task: TaskRun) -> dict[str, Any]:
        """Return controls for checkpointable GUI jobs."""
        running = task.status == "running"
        paused = task.status == "paused"
        terminal = task.status in TASK_TERMINAL_STATUSES
        job_id = _gui_job_id(task)
        capabilities = task.runner_capabilities
        stop_requested = bool(task.runner_payload.get("stop_requested"))
        can_pause = running and bool(capabilities.get("pause")) and bool(job_id) and not stop_requested
        can_resume = (
            (running and bool(capabilities.get("rejoin")))
            or (
                paused
                and bool(capabilities.get("checkpoint"))
                and bool(task.checkpoint_ref)
                and task.resume_policy == "checkpoint"
            )
        )
        return _build_task_controls(
            task,
            can_interrupt=running and bool(capabilities.get("interrupt")) and bool(job_id) and not stop_requested,
            interrupt_reason=_gui_job_stop_reason(
                task,
                job_id=job_id,
                capability="interrupt",
                stop_requested=stop_requested,
            ),
            can_cancel=(
                (running and bool(capabilities.get("cancel")) and bool(job_id) and not stop_requested)
                or paused
            ),
            cancel_reason=_gui_job_stop_reason(
                task,
                job_id=job_id,
                capability="cancel",
                stop_requested=stop_requested,
                allow_paused=True,
            ),
            can_pause=can_pause,
            pause_reason=_gui_job_stop_reason(
                task,
                job_id=job_id,
                capability="pause",
                stop_requested=stop_requested,
            ),
            can_resume=can_resume,
            resume_reason=_gui_job_resume_reason(task),
        )

    def sync_task(self, controller: "TaskController", task: TaskRun, *, poll_timeout_ms: int) -> TaskRun | None:
        """Synchronize GUI job status with TaskRun facts."""
        _ = poll_timeout_ms
        if task.status in TASK_TERMINAL_STATUSES:
            return task
        return _sync_gui_job_task(controller, task)

    def reconcile_stale_task(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        stale_lost_after_ms: int,
        now_ms: int | None,
    ) -> TaskRun | None:
        """Mark stale unattached GUI jobs lost after a grace period."""
        if task.status != "stale":
            return _sync_gui_job_task(controller, task)
        current_ms = _wall_now_ms() if now_ms is None else int(now_ms)
        if current_ms - task.updated_at_ms < max(0, int(stale_lost_after_ms)):
            return task
        status = _fetch_gui_job_status_payload(task)
        if status.get("ok"):
            return _sync_gui_job_task(controller, task, status_result=status)
        summary = "GUI job was lost because it is not attached to this process."
        updated = controller.task_store.update_task(
            task.task_id,
            status="lost",
            terminal_summary=summary,
            progress_summary=summary,
            last_error=str(status.get("error") or summary),
            resume_policy=_gui_job_checkpoint_resume_policy(task),
        )
        if updated is not None:
            controller.event_store.append_event(
                updated.task_id,
                "task.lost",
                message=summary,
                payload={"runner": "gui_job", "job_id": _gui_job_id(task)},
            )
            return updated
        return controller.task_store.get_task(task.task_id) or task

    def task_output(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return GUI job output or retained task summary."""
        job_id = _gui_job_id(task)
        if job_id:
            result = gui_task_job_output(job_id)
            if result.get("ok"):
                output = _render_gui_job_output(result)
                return {
                    "ok": True,
                    "task_id": task.task_id,
                    "status": task.status,
                    "output": output,
                    "tail": output[-1000:],
                    "truncated": False,
                    "artifact_backed": bool(artifacts),
                    "artifacts": artifacts,
                }
        return controller._generic_task_output(task, artifacts=artifacts)

    def pause_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Request a cooperative pause and write the latest GUI checkpoint."""
        synced = _sync_gui_job_task(controller, task) or task
        if synced.status == "paused":
            return {
                "ok": True,
                "task": controller._task_payload(synced),
                "action": "already_paused",
                "message": "GUI job is already paused.",
            }
        if synced.status != "running":
            return {
                "ok": False,
                "task": controller._task_payload(synced),
                "action": "not_running",
                "message": f"Task is {synced.status!r}, not running.",
            }
        job_id = _gui_job_id(synced)
        if not job_id:
            return {
                "ok": False,
                "task": controller._task_payload(synced),
                "action": "missing_job",
                "message": "GUI job id is missing.",
            }
        status = gui_task_job_status(job_id)
        if not status.get("ok"):
            stale = _mark_gui_job_status_unavailable(controller, synced, str(status.get("error") or "status unavailable"))
            return {
                "ok": False,
                "task": controller._task_payload(stale or synced),
                "action": "status_unavailable",
                "message": str(status.get("error") or "GUI job status unavailable."),
            }
        checkpoint = status.get("checkpoint") if isinstance(status.get("checkpoint"), dict) else {}
        checkpoint_result = _record_gui_checkpoint_if_present(
            controller,
            synced,
            checkpoint,
            status=None,
            summary=str(status.get("summary") or "GUI job pause checkpoint."),
        )
        current = checkpoint_result or controller.task_store.get_task(synced.task_id) or synced
        cancel_result = gui_task_job_cancel(
            job_id,
            terminal_status="paused",
            reason="GUI job pause requested by user.",
        )
        if not cancel_result.get("ok"):
            return {
                "ok": False,
                "task": controller._task_payload(current),
                "action": "pause_failed",
                "message": str(cancel_result.get("error") or "Failed to request GUI job pause."),
            }
        payload = dict(current.runner_payload)
        payload["stop_requested"] = {
            "terminal_status": "paused",
            "requested_at_ms": _wall_now_ms(),
            "job_result": cancel_result,
        }
        summary = "GUI job pause requested; waiting for the next checkpoint boundary."
        updated = controller.task_store.update_task(
            current.task_id,
            runner_payload=payload,
            progress_summary=summary,
            resume_policy="checkpoint" if current.checkpoint_ref else current.resume_policy,
        ) or current
        controller.event_store.append_event(
            updated.task_id,
            "task.pause_requested",
            message=summary,
            payload={"runner": "gui_job", "job_id": job_id, "result": cancel_result},
        )
        return {
            "ok": True,
            "task": controller._task_payload(updated),
            "action": "pause_requested",
            "message": summary,
        }

    def resume_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Resume a paused GUI job from its recorded checkpoint."""
        synced = _sync_gui_job_task(controller, task) or task
        if synced.status == "running":
            return {
                "ok": True,
                "task": controller._task_payload(synced),
                "action": "rejoined",
                "resume_policy": synced.resume_policy or "rejoin",
                "message": "GUI job is already running.",
            }
        if synced.status != "paused":
            return {
                "ok": False,
                "task": controller._task_payload(synced),
                "action": "not_paused",
                "resume_policy": synced.resume_policy or "not_resumable",
                "message": f"Task is {synced.status!r}, not paused.",
            }
        if not synced.checkpoint_ref:
            return {
                "ok": False,
                "task": controller._task_payload(synced),
                "action": "missing_checkpoint",
                "resume_policy": synced.resume_policy or "not_resumable",
                "message": "Paused GUI job has no checkpoint.",
            }
        checkpoint = controller.checkpoint_store.get_checkpoint(synced.checkpoint_ref)
        if checkpoint is None:
            return {
                "ok": False,
                "task": controller._task_payload(synced),
                "action": "checkpoint_not_found",
                "resume_policy": synced.resume_policy or "checkpoint",
                "message": "Paused GUI checkpoint was not found.",
            }
        result = resume_gui_task_job(checkpoint=checkpoint.payload)
        if not result.get("ok"):
            return {
                "ok": False,
                "task": controller._task_payload(synced),
                "action": "resume_failed",
                "resume_policy": "checkpoint",
                "message": str(result.get("error") or "Failed to resume GUI job."),
            }
        job_id = str(result.get("job_id") or "").strip()
        payload = dict(synced.runner_payload)
        payload.update(
            {
                "runner": "gui_job",
                "job_id": job_id,
                "status_snapshot": {
                    "status": "running",
                    "summary": str(result.get("summary") or "GUI job resumed from checkpoint."),
                },
                "resumed_from_checkpoint_id": checkpoint.checkpoint_id,
                "stop_requested": None,
            }
        )
        updated = controller.task_store.update_task(
            synced.task_id,
            status="running",
            external_ref=job_id,
            runner_payload=payload,
            runner_capabilities={**GUI_JOB_RUNNER_CAPABILITIES, **synced.runner_capabilities},
            progress_summary="GUI job resumed from checkpoint.",
            terminal_summary="",
            last_error="",
            resume_policy="checkpoint",
            stop_policy="pause_task",
            cancel_policy="cooperative_cancel",
            ended_at_ms=None,
        ) or synced
        controller.event_store.append_event(
            updated.task_id,
            "task.resumed",
            message="GUI job resumed from checkpoint.",
            payload={
                "runner": "gui_job",
                "job_id": job_id,
                "checkpoint": _checkpoint_payload(checkpoint),
                "result": result,
            },
        )
        return {
            "ok": True,
            "task": controller._task_payload(updated),
            "action": "resumed",
            "resume_policy": "checkpoint",
            "job_id": job_id,
            "checkpoint": _checkpoint_payload(checkpoint),
        }

    def interrupt_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Request a cooperative interrupt for a GUI job."""
        return _request_gui_job_stop(
            controller,
            task,
            terminal_status="interrupted",
            event_type="task.interrupt_requested",
        )

    def cancel_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Cancel or abandon a GUI job."""
        if task.status == "paused":
            summary = "Paused GUI job cancelled by user."
            updated = controller.task_store.update_task(
                task.task_id,
                status="cancelled",
                terminal_summary=summary,
                progress_summary=summary,
                last_error="",
            ) or task
            controller.event_store.append_event(
                updated.task_id,
                "task.cancelled",
                message=summary,
                payload={"runner": "gui_job", "job_id": _gui_job_id(task), "abandoned_paused_job": True},
            )
            return {"ok": True, "task": controller._task_payload(updated), "action": "cancelled", "message": summary}
        return _request_gui_job_stop(
            controller,
            task,
            terminal_status="cancelled",
            event_type="task.cancel_requested",
        )


class BrowserRemoteJobTaskRunnerAdapter(TaskRunnerAdapter):
    """Task runner adapter for remote browser jobs observed through a proxy."""

    name = "browser_remote"

    def matches(self, task: TaskRun) -> bool:
        """Return whether the task is backed by a remote browser job."""
        return _task_runner_name(task) == "browser_remote" or task.kind == "browser_remote"

    def controls(self, task: TaskRun) -> dict[str, Any]:
        """Return conservative controls for remote browser jobs."""
        running = task.status == "running"
        paused = task.status == "paused"
        terminal = task.status in TASK_TERMINAL_STATUSES
        capabilities = task.runner_capabilities
        protocol = _browser_remote_protocol_for_task(task)
        can_pause = running and protocol is not None and bool(protocol.pause_path)
        can_resume = (running and bool(capabilities.get("rejoin"))) or (
            paused and protocol is not None and bool(protocol.resume_path)
        )
        can_cancel = running and protocol is not None and bool(protocol.cancel_path)
        return _build_task_controls(
            task,
            can_interrupt=False,
            interrupt_reason=(
                "task is terminal"
                if terminal
                else "browser remote runner does not support direct interrupt yet"
            ),
            can_cancel=can_cancel,
            cancel_reason=(
                "task is terminal"
                if terminal
                else "task is not running"
                if not running
                else ""
                if can_cancel
                else "browser remote cancel protocol is not configured"
            ),
            can_pause=can_pause,
            pause_reason=(
                "task is terminal"
                if terminal
                else "task is not running"
                if not running
                else "browser remote pause protocol is not configured"
            ),
            can_resume=can_resume,
            resume_reason=(
                ""
                if can_resume
                else "task is terminal"
                if terminal
                else "browser remote resume protocol is not configured"
                if paused and (protocol is None or not protocol.resume_path)
                else _resume_unavailable_reason(task)
            ),
        )

    def sync_task(self, controller: "TaskController", task: TaskRun, *, poll_timeout_ms: int) -> TaskRun | None:
        """Synchronize from the latest observed remote browser job fact."""
        _ = poll_timeout_ms
        return _sync_browser_remote_job_task(controller, task)

    def reconcile_stale_task(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        stale_lost_after_ms: int,
        now_ms: int | None,
    ) -> TaskRun | None:
        """Use the latest registry snapshot; otherwise leave stale state intact."""
        _ = stale_lost_after_ms, now_ms
        return _sync_browser_remote_job_task(controller, task)

    def task_output(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return output from the latest remote browser status snapshot."""
        output_result = _fetch_browser_remote_job_output(task)
        if output_result is not None and output_result.ok:
            output = _render_browser_remote_job_output(
                normalize_browser_remote_job_snapshot(output_result.payload, default_status=task.status)
            )
            if output:
                return {
                    "ok": True,
                    "task_id": task.task_id,
                    "status": task.status,
                    "output": output,
                    "tail": output[-1000:],
                    "truncated": False,
                    "artifact_backed": bool(artifacts),
                    "artifacts": artifacts,
                }
        snapshot = _external_status_snapshot(task)
        output = _render_browser_remote_job_output(snapshot)
        if output:
            return {
                "ok": True,
                "task_id": task.task_id,
                "status": task.status,
                "output": output,
                "tail": output[-1000:],
                "truncated": False,
                "artifact_backed": bool(artifacts),
                "artifacts": artifacts,
            }
        return controller._generic_task_output(task, artifacts=artifacts)

    def cancel_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Cancel a remote browser job only when the proxy declares a cancel protocol."""
        return _cancel_browser_remote_job_task(controller, task)

    def pause_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Pause a remote browser job only when the proxy declares a pause protocol."""
        return _pause_browser_remote_job_task(controller, task)

    def resume_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Resume or rejoin a remote browser job."""
        if task.status == "running" and bool(task.runner_capabilities.get("rejoin")):
            return {
                "ok": True,
                "task": controller._task_payload(task),
                "action": "rejoined",
                "resume_policy": task.resume_policy or "rejoin",
                "message": "Browser remote job is already running; rejoined the external job boundary.",
            }
        return _resume_browser_remote_job_task(controller, task)


class McpJobTaskRunnerAdapter(TaskRunnerAdapter):
    """Task runner adapter for MCP tools that expose external job status."""

    name = "mcp"

    def matches(self, task: TaskRun) -> bool:
        """Return whether the task is an MCP/job task."""
        return _task_runner_name(task) == "mcp" or task.kind == "mcp"

    def controls(self, task: TaskRun) -> dict[str, Any]:
        """Return controls for MCP/job tasks without inventing cancel support."""
        running = task.status == "running"
        paused = task.status == "paused"
        terminal = task.status in TASK_TERMINAL_STATUSES
        protocol = _mcp_job_protocol(task)
        can_cancel = running and protocol is not None and bool(protocol.cancel_tool)
        can_pause = running and protocol is not None and bool(protocol.pause_tool)
        can_resume = (running and bool(task.runner_capabilities.get("rejoin"))) or (
            paused and protocol is not None and bool(protocol.resume_tool)
        )
        return _build_task_controls(
            task,
            can_interrupt=False,
            interrupt_reason="task is terminal" if terminal else "mcp runner does not support direct interrupt yet",
            can_cancel=can_cancel,
            cancel_reason=(
                "task is terminal"
                if terminal
                else "task is not running"
                if not running
                else "mcp job cancel tool is not configured"
                if protocol is None or not protocol.cancel_tool
                else "runner does not support direct cancel"
            ),
            can_pause=can_pause,
            pause_reason=(
                "task is terminal"
                if terminal
                else "task is not running"
                if not running
                else "mcp job pause tool is not configured"
            ),
            can_resume=can_resume,
            resume_reason=(
                ""
                if can_resume
                else "task is terminal"
                if terminal
                else "mcp job resume tool is not configured"
                if paused and (protocol is None or not protocol.resume_tool)
                else _resume_unavailable_reason(task)
            ),
        )

    def sync_task(self, controller: "TaskController", task: TaskRun, *, poll_timeout_ms: int) -> TaskRun | None:
        """Poll MCP job status when a configured status tool is available."""
        _ = poll_timeout_ms
        if task.status in TASK_TERMINAL_STATUSES:
            return task
        polled = _poll_mcp_job_status(controller, task)
        if polled is not None:
            task = polled
        return controller._sync_external_snapshot_task(task, runner_name="mcp")

    def reconcile_stale_task(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        stale_lost_after_ms: int,
        now_ms: int | None,
    ) -> TaskRun | None:
        """Use any fresh MCP snapshot, otherwise leave stale state untouched."""
        _ = stale_lost_after_ms, now_ms
        polled = _poll_mcp_job_status(controller, task)
        if polled is not None:
            task = polled
        return controller._sync_external_snapshot_task(task, runner_name="mcp")

    def task_output(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return MCP snapshot output when available."""
        output_result = _fetch_mcp_job_output(task)
        if output_result is not None and output_result.ok:
            output = _render_mcp_job_output(output_result.payload)
            return {
                "ok": True,
                "task_id": task.task_id,
                "status": task.status,
                "output": output,
                "tail": output[-1000:],
                "truncated": False,
                "artifact_backed": bool(artifacts),
                "artifacts": artifacts,
            }
        snapshot = _external_status_snapshot(task)
        output = str(snapshot.get("output", "") or "").strip()
        if output:
            return {
                "ok": True,
                "task_id": task.task_id,
                "status": task.status,
                "output": output,
                "tail": output[-1000:],
                "truncated": False,
                "artifact_backed": bool(artifacts),
                "artifacts": artifacts,
            }
        return controller._generic_task_output(task, artifacts=artifacts)

    def interrupt_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Return a stable unsupported interrupt response for MCP tasks."""
        return _unsupported_mcp_control_payload(controller, task, action="interrupt")

    def pause_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Pause a MCP external job only when a configured pause tool exists."""
        return _pause_mcp_job_task(controller, task)

    def resume_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Resume or rejoin a MCP external job."""
        if task.status == "running" and bool(task.runner_capabilities.get("rejoin")):
            return {
                "ok": True,
                "task": controller._task_payload(task),
                "action": "rejoined",
                "resume_policy": task.resume_policy or "rejoin",
                "message": "MCP job is already running; rejoined the external job boundary.",
            }
        return _resume_mcp_job_task(controller, task)

    def cancel_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Cancel a MCP external job only when a configured cancel tool exists."""
        return _cancel_mcp_job_task(controller, task)


class McpProxyTaskRunnerAdapter(TaskRunnerAdapter):
    """Task runner adapter for current-process MCP proxy calls."""

    name = "mcp_proxy"

    def matches(self, task: TaskRun) -> bool:
        """Return whether the task is backed by the MCP long-task proxy."""
        return _task_runner_name(task) == "mcp_proxy"

    def controls(self, task: TaskRun) -> dict[str, Any]:
        """Return controls for current-process MCP proxy tasks."""
        running = task.status == "running"
        terminal = task.status in TASK_TERMINAL_STATUSES
        attached = running and is_mcp_proxy_task_active(task.task_id)
        capabilities = task.runner_capabilities
        detached_reason = "mcp proxy background call is not attached to this process"
        return _build_task_controls(
            task,
            can_interrupt=attached and bool(capabilities.get("interrupt")),
            interrupt_reason=(
                "task is terminal"
                if terminal
                else "task is not running"
                if not running
                else detached_reason
                if not attached
                else "runner does not support interrupt"
            ),
            can_cancel=attached and bool(capabilities.get("cancel")),
            cancel_reason=(
                "task is terminal"
                if terminal
                else "task is not running"
                if not running
                else detached_reason
                if not attached
                else "runner does not support cancel"
            ),
            can_pause=False,
            pause_reason=_pause_unavailable_reason(task),
            can_resume=running and bool(capabilities.get("rejoin")),
            resume_reason=_resume_unavailable_reason(task),
        )

    def sync_task(self, controller: "TaskController", task: TaskRun, *, poll_timeout_ms: int) -> TaskRun | None:
        """Mark detached current-process MCP proxy calls as stale."""
        _ = poll_timeout_ms
        if task.status != "running":
            return task
        if is_mcp_proxy_task_active(task.task_id):
            return task
        summary = "MCP proxy background call is not attached to this process."
        updated = controller.task_store.update_task(
            task.task_id,
            status="stale",
            progress_summary=summary,
            last_error=summary,
        )
        if updated is not None:
            controller.event_store.append_event(
                updated.task_id,
                "task.stale",
                message=summary,
                payload={"runner": "mcp_proxy"},
            )
            return updated
        return controller.task_store.get_task(task.task_id) or task

    def reconcile_stale_task(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        stale_lost_after_ms: int,
        now_ms: int | None,
    ) -> TaskRun | None:
        """Mark detached stale MCP proxy tasks as lost after grace period."""
        if task.status != "stale":
            return task
        current_ms = _wall_now_ms() if now_ms is None else int(now_ms)
        if current_ms - task.updated_at_ms < max(0, int(stale_lost_after_ms)):
            return task
        summary = "MCP proxy background call was lost because it is not attached to this process."
        updated = controller.task_store.update_task(
            task.task_id,
            status="lost",
            terminal_summary=summary,
            progress_summary=summary,
            last_error=summary,
            resume_policy="not_resumable",
        )
        if updated is not None:
            controller.event_store.append_event(
                updated.task_id,
                "task.lost",
                message=summary,
                payload={"runner": "mcp_proxy"},
            )
            return updated
        return controller.task_store.get_task(task.task_id) or task

    def interrupt_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Interrupt a current-process MCP proxy task."""
        return _stop_mcp_proxy_task(controller, task, terminal_status="interrupted", event_type="task.interrupted")

    def cancel_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Cancel a current-process MCP proxy task."""
        return _stop_mcp_proxy_task(controller, task, terminal_status="cancelled", event_type="task.cancelled")


class SyncToolProxyTaskRunnerAdapter(TaskRunnerAdapter):
    """Task runner adapter for current-process sync builtin tool proxy calls."""

    name = "sync_tool_proxy"

    def matches(self, task: TaskRun) -> bool:
        """Return whether the task is backed by a sync tool proxy thread."""
        return _task_runner_name(task) == "sync_tool_proxy"

    def controls(self, task: TaskRun) -> dict[str, Any]:
        """Return honest controls for sync proxy tasks."""
        running = task.status == "running"
        terminal = task.status in TASK_TERMINAL_STATUSES
        attached = is_sync_proxy_task_attached(task.task_id)
        cooperative_cancel = bool(task.runner_payload.get("cooperative_cancel"))
        stop_requested = bool(task.runner_payload.get("cancel_requested"))
        return _build_task_controls(
            task,
            can_interrupt=running and attached and cooperative_cancel and not stop_requested,
            interrupt_reason=(
                "task is terminal"
                if terminal
                else "task is not running"
                if not running
                else "sync tool proxy stop is already requested"
                if stop_requested
                else "sync tool proxy is not attached to this process"
                if not attached
                else "sync tool proxy cannot safely interrupt a running Python thread"
                if not cooperative_cancel
                else ""
            ),
            can_cancel=running and attached and cooperative_cancel and not stop_requested,
            cancel_reason=(
                "task is terminal"
                if terminal
                else "task is not running"
                if not running
                else "sync tool proxy stop is already requested"
                if stop_requested
                else "sync tool proxy is not attached to this process"
                if not attached
                else "sync tool proxy cannot safely cancel a running Python thread"
                if not cooperative_cancel
                else ""
            ),
            can_pause=False,
            pause_reason=_pause_unavailable_reason(task),
            can_resume=running and bool(task.runner_capabilities.get("rejoin")),
            resume_reason=_resume_unavailable_reason(task),
        )

    def sync_task(self, controller: "TaskController", task: TaskRun, *, poll_timeout_ms: int) -> TaskRun | None:
        """Mark detached current-process sync proxy calls as stale."""
        _ = poll_timeout_ms
        if task.status != "running":
            return task
        if is_sync_proxy_task_attached(task.task_id):
            return task
        summary = "Sync tool proxy background call is not attached to this process."
        updated = controller.task_store.update_task(
            task.task_id,
            status="stale",
            progress_summary=summary,
            last_error=summary,
        )
        if updated is not None:
            controller.event_store.append_event(
                updated.task_id,
                "task.stale",
                message=summary,
                payload={"runner": "sync_tool_proxy"},
            )
            return updated
        return controller.task_store.get_task(task.task_id) or task

    def reconcile_stale_task(
        self,
        controller: "TaskController",
        task: TaskRun,
        *,
        stale_lost_after_ms: int,
        now_ms: int | None,
    ) -> TaskRun | None:
        """Mark detached stale sync proxy tasks as lost after grace period."""
        if task.status != "stale":
            return task
        current_ms = _wall_now_ms() if now_ms is None else int(now_ms)
        if current_ms - task.updated_at_ms < max(0, int(stale_lost_after_ms)):
            return task
        summary = "Sync tool proxy background call was lost because it is not attached to this process."
        updated = controller.task_store.update_task(
            task.task_id,
            status="lost",
            terminal_summary=summary,
            progress_summary=summary,
            last_error=summary,
            resume_policy="not_resumable",
        )
        if updated is not None:
            controller.event_store.append_event(
                updated.task_id,
                "task.lost",
                message=summary,
                payload={"runner": "sync_tool_proxy"},
            )
            return updated
        return controller.task_store.get_task(task.task_id) or task

    def interrupt_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Request cooperative stop for a current-process sync proxy task."""
        return _request_sync_proxy_stop(
            controller,
            task,
            terminal_status="interrupted",
            event_type="task.interrupt_requested",
        )

    def cancel_task(self, controller: "TaskController", task: TaskRun) -> dict[str, Any]:
        """Request cooperative cancellation for a current-process sync proxy task."""
        return _request_sync_proxy_stop(
            controller,
            task,
            terminal_status="cancelled",
            event_type="task.cancel_requested",
        )


def _request_sync_proxy_stop(
    controller: "TaskController",
    task: TaskRun,
    *,
    terminal_status: str,
    event_type: str,
) -> dict[str, Any]:
    """Request cooperative stop for a sync proxy task without faking completion."""
    if task.status != "running":
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_running",
            "message": "Task is not running.",
        }
    if not bool(task.runner_payload.get("cooperative_cancel")):
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_supported",
            "message": "Sync tool proxy task does not expose cooperative cancellation.",
        }
    normalized_status = "cancelled" if terminal_status == "cancelled" else "interrupted"
    message = (
        "Cooperative cancellation requested; waiting for the tool to stop at its next boundary."
        if normalized_status == "cancelled"
        else "Cooperative interrupt requested; waiting for the tool to stop at its next boundary."
    )
    if not request_sync_proxy_task_stop(task.task_id, terminal_status=normalized_status, reason=message):
        updated = controller.task_store.update_task(
            task.task_id,
            status="stale",
            progress_summary="Sync tool proxy background call is not attached to this process.",
            last_error="Sync tool proxy background call is not attached to this process.",
        )
        stale_task = updated or controller.task_store.get_task(task.task_id) or task
        controller.event_store.append_event(
            stale_task.task_id,
            "task.stale",
            message="Sync tool proxy background call is not attached to this process.",
            payload={"runner": "sync_tool_proxy"},
        )
        return {
            "ok": False,
            "task": controller._task_payload(stale_task),
            "action": "adapter_detached",
            "message": "Sync tool proxy task is not attached to this process.",
        }
    payload = dict(task.runner_payload)
    payload["cancel_requested"] = {
        "terminal_status": normalized_status,
        "requested_at_ms": _wall_now_ms(),
    }
    updated = controller.task_store.update_task(
        task.task_id,
        runner_payload=payload,
        progress_summary=message,
    )
    current = updated or controller.task_store.get_task(task.task_id) or task
    controller.event_store.append_event(
        task.task_id,
        event_type,
        message=message,
        payload={"runner": "sync_tool_proxy", "terminal_status": normalized_status},
    )
    return {
        "ok": True,
        "task": controller._task_payload(current),
        "action": "stop_requested",
        "message": message,
    }


class TaskRunnerRegistry:
    """Resolve runner adapters for task facts."""

    def __init__(self, adapters: list[TaskRunnerAdapter] | None = None) -> None:
        self._fallback = TaskRunnerAdapter()
        self._adapters = list(
            adapters
            or [
                ProcessTaskRunnerAdapter(),
                GuiJobTaskRunnerAdapter(),
                BrowserRemoteJobTaskRunnerAdapter(),
                McpProxyTaskRunnerAdapter(),
                SyncToolProxyTaskRunnerAdapter(),
                McpJobTaskRunnerAdapter(),
            ]
        )

    def for_task(self, task: TaskRun) -> TaskRunnerAdapter:
        """Return the first adapter that handles the task."""
        for adapter in self._adapters:
            if adapter.matches(task):
                return adapter
        return self._fallback

    def controls(self, task: TaskRun) -> dict[str, Any]:
        """Return controls for one task using its adapter."""
        return self.for_task(task).controls(task)


DEFAULT_TASK_RUNNER_REGISTRY = TaskRunnerRegistry()


class TaskController:
    """User-visible task inspection and control operations."""

    def __init__(
        self,
        *,
        task_store: TaskStore | None = None,
        event_store: TaskEventStore | None = None,
        input_store: TaskInputStore | None = None,
        artifact_store: TaskArtifactStore | None = None,
        checkpoint_store: TaskCheckpointStore | None = None,
        delivery_store: TaskDeliveryStore | None = None,
        context_store: LongTaskContextStore | None = None,
        runner_registry: TaskRunnerRegistry | None = None,
    ) -> None:
        self.task_store = task_store or TaskStore()
        self.event_store = event_store or TaskEventStore(db_path=self.task_store.db_path)
        self.input_store = input_store or TaskInputStore(db_path=self.task_store.db_path)
        self.artifact_store = artifact_store or TaskArtifactStore(db_path=self.task_store.db_path)
        self.checkpoint_store = checkpoint_store or TaskCheckpointStore(db_path=self.task_store.db_path)
        self.delivery_store = delivery_store or TaskDeliveryStore(db_path=self.task_store.db_path)
        self.context_store = context_store or LongTaskContextStore(db_path=self.task_store.db_path)
        self.runner_registry = runner_registry or DEFAULT_TASK_RUNNER_REGISTRY

    def show_task(self, task_id: str) -> dict[str, Any]:
        """Return a task status payload, opportunistically syncing process state."""
        task = self.sync_task(task_id)
        if task is None:
            return {"ok": False, "error": f"task {task_id!r} not found"}
        deliveries = self.delivery_store.list_deliveries(task_id)
        return {
            "ok": True,
            "task": self._task_payload(task, delivery_summary=_delivery_summary_payload(deliveries)),
            "events": [_event_payload(e) for e in self.event_store.list_events(task_id)],
            "inputs": [_input_payload(item) for item in self.input_store.list_inputs(task_id, limit=20)],
            "artifacts": [_artifact_payload(item) for item in self.artifact_store.list_artifacts(task_id, limit=20)],
            "context_summaries": [
                _context_summary_payload(item)
                for item in self._list_task_context_summaries(task)
            ],
            "checkpoints": [
                _checkpoint_payload(item)
                for item in self.checkpoint_store.list_checkpoints(task_id, limit=20)
            ],
            "deliveries": [_delivery_payload(item) for item in deliveries],
        }

    def list_tasks(self, *, session_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Return recent task status payloads."""
        tasks = [self.sync_task(task.task_id) or task for task in self.task_store.list_tasks(session_id=session_id, limit=limit)]
        delivery_summaries = self.delivery_store.summarize_by_task_ids([task.task_id for task in tasks])
        return {
            "ok": True,
            "items": [
                self._task_payload(task, delivery_summary=delivery_summaries.get(task.task_id))
                for task in tasks
            ],
        }

    def task_control_snapshot(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return UI/app-ready task control snapshots."""
        if task_id:
            shown = self.show_task(task_id)
            if not shown.get("ok"):
                return shown
            task = shown["task"]
            return {
                "ok": True,
                "items": [_task_control_snapshot_payload(task)],
                "task": task,
                "events": shown.get("events", []),
                "checkpoints": shown.get("checkpoints", []),
                "deliveries": shown.get("deliveries", []),
            }
        listed = self.list_tasks(session_id=session_id, limit=limit)
        return {
            "ok": True,
            "items": [_task_control_snapshot_payload(task) for task in listed.get("items", [])],
        }

    def materialize_browser_remote_job(
        self,
        job: BrowserRemoteJob,
        *,
        context: TaskInvocationContext | None = None,
    ) -> dict[str, Any]:
        """Create or update a TaskRun for one explicitly observed remote browser job."""
        task_context = context or TaskInvocationContext()
        dedupe_key = f"browser_remote:{job.job_record_id}"
        runner_payload = _browser_remote_runner_payload(job, db_path=self.task_store.db_path)
        runner_capabilities = _browser_remote_runner_capabilities(runner_payload)
        status = _normalize_external_task_status(runner_payload["status_snapshot"].get("status")) or "running"
        progress = _browser_remote_progress_summary(job)
        last_error = str(job.last_error or runner_payload["status_snapshot"].get("error") or "")
        existing = self.task_store.get_task_by_dedupe_key(dedupe_key)
        if existing is not None:
            staged = self.task_store.update_task(
                existing.task_id,
                runner_payload=runner_payload,
                runner_capabilities=runner_capabilities,
                progress_summary=progress or existing.progress_summary,
                last_error=last_error if status != "completed" else "",
            ) or existing
            synced = self._sync_external_snapshot_task(staged, runner_name="browser_remote") or staged
            return {
                "ok": True,
                "action": "updated",
                "task": self._task_payload(synced),
            }

        task = self.task_store.create_task(
            kind="browser_remote",
            status=status,
            title=_browser_remote_task_title(job),
            owner_key=task_context.owner_key,
            user_id=task_context.user_id,
            thread_id=task_context.thread_id,
            session_id=task_context.session_id,
            turn_id=task_context.turn_id,
            invocation_id=task_context.invocation_id,
            function_call_id=task_context.function_call_id,
            tool_call_id=task_context.tool_call_id,
            dedupe_key=dedupe_key,
            external_ref=job.external_job_id,
            runner_payload=runner_payload,
            runner_capabilities=runner_capabilities,
            resume_policy="rejoin",
            stop_policy="remote_protocol",
            cancel_policy="remote_protocol",
            progress_summary=progress,
            terminal_summary=_browser_remote_terminal_summary(job) if status in TASK_TERMINAL_STATUSES else "",
            last_error=last_error if status != "completed" else "",
        )
        event_type = f"task.{status}" if status in TASK_TERMINAL_STATUSES else "task.started"
        self.event_store.append_event(
            task.task_id,
            event_type,
            message=task.terminal_summary or task.progress_summary,
            payload={
                "runner": "browser_remote",
                "job_record_id": job.job_record_id,
                "provider_id": job.provider_id,
                "external_job_id": job.external_job_id,
                "remote_job": browser_remote_job_payload(job),
            },
        )
        return {
            "ok": True,
            "action": "created",
            "task": self._task_payload(task),
        }

    def runtime_status(
        self,
        *,
        session_id: str | None = None,
        stuck_after_ms: int = DEFAULT_STUCK_TASK_AFTER_MS,
        stuck_limit: int = 10,
    ) -> dict[str, Any]:
        """Return a compact health snapshot for long-task runtime facts."""
        counts = self.task_store.count_by_status(session_id=session_id)
        stuck_tasks = self.task_store.list_stuck_tasks(
            older_than_ms=stuck_after_ms,
            session_id=session_id,
            limit=stuck_limit,
        )
        active_count = sum(counts.get(status, 0) for status in TASK_ACTIVE_STATUSES)
        terminal_count = sum(counts.get(status, 0) for status in TASK_TERMINAL_STATUSES)
        orphan_artifact_count = self.artifact_store.count_orphaned_artifacts()
        orphan_checkpoint_count = self.checkpoint_store.count_orphaned_checkpoints()
        checkpoint_retention_candidate_count = self.checkpoint_store.count_retention_candidates(
            older_than_ms=DEFAULT_CHECKPOINT_RETENTION_MS,
            keep_latest_per_task=DEFAULT_CHECKPOINT_KEEP_LATEST_PER_TASK,
            session_id=session_id,
        )
        return {
            "ok": True,
            "session_id": session_id or "",
            "status_counts": counts,
            "active_count": active_count,
            "terminal_count": terminal_count,
            "orphan_artifact_count": orphan_artifact_count,
            "orphan_checkpoint_count": orphan_checkpoint_count,
            "checkpoint_retention_candidate_count": checkpoint_retention_candidate_count,
            "stuck_after_ms": max(0, int(stuck_after_ms)),
            "stuck_count": len(stuck_tasks),
            "stuck_tasks": [self._task_payload(task) for task in stuck_tasks],
        }

    def audit_stuck_tasks(
        self,
        *,
        older_than_ms: int = DEFAULT_STUCK_TASK_AFTER_MS,
        session_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return stale-looking active tasks without mutating runtime state."""
        tasks = self.task_store.list_stuck_tasks(
            older_than_ms=older_than_ms,
            session_id=session_id,
            limit=limit,
        )
        return {
            "ok": True,
            "session_id": session_id or "",
            "older_than_ms": max(0, int(older_than_ms)),
            "count": len(tasks),
            "items": [self._task_payload(task) for task in tasks],
        }

    def remediate_stuck_tasks(
        self,
        *,
        older_than_ms: int = DEFAULT_STUCK_TASK_AFTER_MS,
        stale_lost_after_ms: int = DEFAULT_STALE_LOST_AFTER_MS,
        session_id: str | None = None,
        limit: int = 50,
        dry_run: bool = True,
        confirm: bool = False,
        lease_ms: int = DEFAULT_REMEDIATION_LEASE_MS,
        poll_timeout_ms: int = 0,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Conservatively synchronize stuck running/stale tasks.

        This does not cancel, restart, or resume work. It only asks the
        runner adapter to observe existing backing state and reconcile facts.
        """
        current_ms = _wall_now_ms() if now_ms is None else int(now_ms)
        candidates = self.task_store.list_stuck_tasks(
            older_than_ms=older_than_ms,
            statuses=("running", "stale"),
            session_id=session_id,
            now_ms=current_ms,
            limit=limit,
        )
        if dry_run:
            return {
                "ok": True,
                "action": "dry_run",
                "older_than_ms": max(0, int(older_than_ms)),
                "stale_lost_after_ms": max(0, int(stale_lost_after_ms)),
                "session_id": session_id or "",
                "candidate_count": len(candidates),
                "items": [self._task_payload(task) for task in candidates],
            }
        if not confirm:
            return {
                "ok": False,
                "action": "confirmation_required",
                "message": "Set dry_run=false and confirm=true to synchronize stuck task facts.",
                "older_than_ms": max(0, int(older_than_ms)),
                "stale_lost_after_ms": max(0, int(stale_lost_after_ms)),
                "session_id": session_id or "",
                "candidate_count": len(candidates),
                "items": [self._task_payload(task) for task in candidates],
            }
        owner = f"remediate-stuck:{os.getpid()}"
        results: list[dict[str, Any]] = []
        remediated_count = 0
        skipped_count = 0
        for candidate in candidates:
            claim = self.task_store.claim_task(
                candidate.task_id,
                lease_owner=owner,
                lease_ms=lease_ms,
                now_ms=current_ms,
            )
            if claim is None:
                skipped_count += 1
                results.append(
                    {
                        "task_id": candidate.task_id,
                        "before_status": candidate.status,
                        "after_status": candidate.status,
                        "action": "lease_busy",
                        "changed": False,
                    }
                )
                continue
            try:
                updated = self._remediate_claimed_stuck_task(
                    claim,
                    stale_lost_after_ms=stale_lost_after_ms,
                    poll_timeout_ms=poll_timeout_ms,
                    now_ms=current_ms,
                )
            finally:
                self.task_store.release_claim(
                    claim.task_id,
                    lease_owner=owner,
                    claim_token=claim.claim_token,
                )
            refreshed = updated or self.task_store.get_task(claim.task_id) or claim
            changed = refreshed.status != claim.status
            if changed:
                remediated_count += 1
            results.append(
                {
                    "task_id": claim.task_id,
                    "before_status": claim.status,
                    "after_status": refreshed.status,
                    "action": _remediation_action(claim, refreshed),
                    "changed": changed,
                    "task": self._task_payload(refreshed),
                }
            )
        return {
            "ok": True,
            "action": "remediated",
            "older_than_ms": max(0, int(older_than_ms)),
            "stale_lost_after_ms": max(0, int(stale_lost_after_ms)),
            "session_id": session_id or "",
            "candidate_count": len(candidates),
            "remediated_count": remediated_count,
            "skipped_count": skipped_count,
            "items": results,
        }

    def cleanup_terminal_tasks(
        self,
        *,
        older_than_ms: int = DEFAULT_TERMINAL_RETENTION_MS,
        session_id: str | None = None,
        limit: int = 100,
        dry_run: bool = True,
        confirm: bool = False,
        delete_artifact_files: bool = False,
    ) -> dict[str, Any]:
        """Delete old terminal TaskRuntime facts only after explicit confirmation."""
        candidates = self.task_store.list_terminal_tasks_older_than(
            older_than_ms=older_than_ms,
            session_id=session_id,
            limit=limit,
        )
        task_ids = [task.task_id for task in candidates]
        artifacts = self._artifacts_for_task_ids(task_ids)
        artifact_files = _artifact_file_cleanup(
            artifacts,
            delete=False,
            delete_requested=delete_artifact_files,
        )
        if dry_run:
            deleted = 0
            action = "dry_run"
        elif not confirm:
            return {
                "ok": False,
                "action": "confirmation_required",
                "message": "Set dry_run=false and confirm=true to delete terminal task facts.",
                "older_than_ms": max(0, int(older_than_ms)),
                "candidate_count": len(candidates),
                "task_ids": task_ids,
                "items": [self._task_payload(task) for task in candidates],
                "artifacts": [_artifact_payload(artifact) for artifact in artifacts],
                "artifact_files": artifact_files,
            }
        else:
            if delete_artifact_files:
                artifact_files = _artifact_file_cleanup(
                    artifacts,
                    delete=True,
                    delete_requested=True,
                )
            deleted = self.task_store.delete_tasks(task_ids)
            action = "deleted"
        return {
            "ok": True,
            "action": action,
            "session_id": session_id or "",
            "older_than_ms": max(0, int(older_than_ms)),
            "candidate_count": len(candidates),
            "deleted_count": deleted,
            "task_ids": task_ids,
            "items": [self._task_payload(task) for task in candidates],
            "artifacts": [_artifact_payload(artifact) for artifact in artifacts],
            "artifact_files": artifact_files,
        }

    def audit_orphan_runtime_facts(self, *, limit: int = 100) -> dict[str, Any]:
        """Return orphaned TaskRuntime child facts without mutating state."""
        artifacts = self.artifact_store.list_orphaned_artifacts(limit=limit)
        checkpoints = self.checkpoint_store.list_orphaned_checkpoints(limit=limit)
        return {
            "ok": True,
            "orphan_artifact_count": self.artifact_store.count_orphaned_artifacts(),
            "orphan_checkpoint_count": self.checkpoint_store.count_orphaned_checkpoints(),
            "artifacts": [_artifact_payload(artifact) for artifact in artifacts],
            "checkpoints": [_checkpoint_payload(checkpoint) for checkpoint in checkpoints],
        }

    def cleanup_orphan_runtime_facts(
        self,
        *,
        limit: int = 100,
        dry_run: bool = True,
        confirm: bool = False,
        delete_artifact_files: bool = False,
    ) -> dict[str, Any]:
        """Delete orphaned TaskRuntime child facts after explicit confirmation."""
        artifacts = self.artifact_store.list_orphaned_artifacts(limit=limit)
        checkpoints = self.checkpoint_store.list_orphaned_checkpoints(limit=limit)
        artifact_files = _artifact_file_cleanup(
            artifacts,
            delete=False,
            delete_requested=delete_artifact_files,
        )
        if dry_run:
            action = "dry_run"
            deleted_artifacts = 0
            deleted_checkpoints = 0
        elif not confirm:
            return {
                "ok": False,
                "action": "confirmation_required",
                "message": "Set dry_run=false and confirm=true to delete orphaned runtime facts.",
                "orphan_artifact_count": len(artifacts),
                "orphan_checkpoint_count": len(checkpoints),
                "artifacts": [_artifact_payload(artifact) for artifact in artifacts],
                "checkpoints": [_checkpoint_payload(checkpoint) for checkpoint in checkpoints],
                "artifact_files": artifact_files,
            }
        else:
            if delete_artifact_files:
                artifact_files = _artifact_file_cleanup(
                    artifacts,
                    delete=True,
                    delete_requested=True,
                )
            deleted_artifacts = self.artifact_store.delete_artifact_records(
                [artifact.artifact_id for artifact in artifacts]
            )
            deleted_checkpoints = self.checkpoint_store.delete_checkpoints(
                [checkpoint.checkpoint_id for checkpoint in checkpoints]
            )
            action = "deleted"
        return {
            "ok": True,
            "action": action,
            "orphan_artifact_count": len(artifacts),
            "orphan_checkpoint_count": len(checkpoints),
            "deleted_artifact_count": deleted_artifacts,
            "deleted_checkpoint_count": deleted_checkpoints,
            "artifacts": [_artifact_payload(artifact) for artifact in artifacts],
            "checkpoints": [_checkpoint_payload(checkpoint) for checkpoint in checkpoints],
            "artifact_files": artifact_files,
        }

    def audit_checkpoint_retention(
        self,
        *,
        older_than_ms: int = DEFAULT_CHECKPOINT_RETENTION_MS,
        keep_latest_per_task: int = DEFAULT_CHECKPOINT_KEEP_LATEST_PER_TASK,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return old non-current checkpoints eligible for retention cleanup."""
        checkpoints = self.checkpoint_store.list_retention_candidates(
            older_than_ms=older_than_ms,
            keep_latest_per_task=keep_latest_per_task,
            task_id=task_id,
            session_id=session_id,
            limit=limit,
        )
        return {
            "ok": True,
            "action": "audit",
            "older_than_ms": max(0, int(older_than_ms)),
            "keep_latest_per_task": max(0, int(keep_latest_per_task)),
            "task_id": task_id or "",
            "session_id": session_id or "",
            "candidate_count": self.checkpoint_store.count_retention_candidates(
                older_than_ms=older_than_ms,
                keep_latest_per_task=keep_latest_per_task,
                task_id=task_id,
                session_id=session_id,
            ),
            "items": [_checkpoint_payload(checkpoint) for checkpoint in checkpoints],
        }

    def cleanup_checkpoint_retention(
        self,
        *,
        older_than_ms: int = DEFAULT_CHECKPOINT_RETENTION_MS,
        keep_latest_per_task: int = DEFAULT_CHECKPOINT_KEEP_LATEST_PER_TASK,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Delete old non-current checkpoints after explicit confirmation."""
        checkpoints = self.checkpoint_store.list_retention_candidates(
            older_than_ms=older_than_ms,
            keep_latest_per_task=keep_latest_per_task,
            task_id=task_id,
            session_id=session_id,
            limit=limit,
        )
        checkpoint_ids = [checkpoint.checkpoint_id for checkpoint in checkpoints]
        if dry_run:
            action = "dry_run"
            deleted = 0
        elif not confirm:
            return {
                "ok": False,
                "action": "confirmation_required",
                "message": "Set dry_run=false and confirm=true to delete old checkpoint facts.",
                "older_than_ms": max(0, int(older_than_ms)),
                "keep_latest_per_task": max(0, int(keep_latest_per_task)),
                "task_id": task_id or "",
                "session_id": session_id or "",
                "candidate_count": len(checkpoints),
                "checkpoint_ids": checkpoint_ids,
                "items": [_checkpoint_payload(checkpoint) for checkpoint in checkpoints],
            }
        else:
            deleted = self.checkpoint_store.delete_retention_checkpoints(checkpoint_ids)
            action = "deleted"
        return {
            "ok": True,
            "action": action,
            "older_than_ms": max(0, int(older_than_ms)),
            "keep_latest_per_task": max(0, int(keep_latest_per_task)),
            "task_id": task_id or "",
            "session_id": session_id or "",
            "candidate_count": len(checkpoints),
            "deleted_count": deleted,
            "checkpoint_ids": checkpoint_ids,
            "items": [_checkpoint_payload(checkpoint) for checkpoint in checkpoints],
        }

    def task_output(self, task_id: str) -> dict[str, Any]:
        """Return retained output for one process-backed task."""
        task = self.sync_task(task_id, poll_timeout_ms=250)
        if task is None:
            return {"ok": False, "error": f"task {task_id!r} not found"}
        artifacts = [_artifact_payload(item) for item in self.artifact_store.list_artifacts(task_id, limit=20)]
        return self.runner_registry.for_task(task).task_output(self, task, artifacts=artifacts)

    def interrupt_task(self, task_id: str) -> dict[str, Any]:
        """Stop a task without marking it as user-cancelled."""
        task = self.task_store.get_task(task_id)
        if task is None:
            return {"ok": False, "error": f"task {task_id!r} not found"}
        return self.runner_registry.for_task(task).interrupt_task(self, task)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Cancel a task because the user explicitly abandoned it."""
        task = self.task_store.get_task(task_id)
        if task is None:
            return {"ok": False, "error": f"task {task_id!r} not found"}
        return self.runner_registry.for_task(task).cancel_task(self, task)

    def pause_task(self, task_id: str) -> dict[str, Any]:
        """Pause a task only when a runner-specific durable pause exists."""
        task = self.sync_task(task_id)
        if task is None:
            return {"ok": False, "error": f"task {task_id!r} not found"}
        return self.runner_registry.for_task(task).pause_task(self, task)

    def dispatch_task_action(
        self,
        task_id: str,
        action: str,
        *,
        content: str = "",
        inline_budget_ms: int | None = None,
        context: TaskInvocationContext | None = None,
    ) -> dict[str, Any]:
        """Dispatch one UI/app action through the existing task control methods."""
        normalized = str(action or "").strip().lower()
        task = self.sync_task(task_id)
        if task is None:
            return {"ok": False, "error": f"task {task_id!r} not found"}
        controls = self.runner_registry.controls(task)
        action_payload = _find_control_action(controls, normalized)
        if action_payload is None:
            return {
                "ok": False,
                "task": self._task_payload(task),
                "action": normalized,
                "message": f"Unsupported task action {action!r}.",
            }
        if not bool(action_payload.get("enabled")):
            return {
                "ok": False,
                "task": self._task_payload(task),
                "action": normalized,
                "message": str(action_payload.get("reason") or "Task action is not available."),
            }
        if normalized == "interrupt":
            return self.interrupt_task(task_id)
        if normalized == "cancel":
            return self.cancel_task(task_id)
        if normalized == "pause":
            return self.pause_task(task_id)
        if normalized == "resume":
            return self.resume_task(task_id)
        if normalized == "restart":
            return self.restart_task(task_id, inline_budget_ms=inline_budget_ms, context=context)
        if normalized == "send_input":
            return self.send_task_input(task_id, content)
        if normalized == "inspect_output":
            return self.task_output(task_id)
        return {
            "ok": False,
            "task": self._task_payload(task),
            "action": normalized,
            "message": f"Unsupported task action {action!r}.",
        }

    def resume_task(self, task_id: str) -> dict[str, Any]:
        """Resume or rejoin a task only at runner-supported durable boundaries."""
        task = self.sync_task(task_id)
        if task is None:
            return {"ok": False, "error": f"task {task_id!r} not found"}
        return self.runner_registry.for_task(task).resume_task(self, task)

    def restart_task(
        self,
        task_id: str,
        *,
        inline_budget_ms: int | None = None,
        context: TaskInvocationContext | None = None,
    ) -> dict[str, Any]:
        """Start a new run from an explicit restartable boundary."""
        task = self.sync_task(task_id)
        if task is None:
            return {"ok": False, "error": f"task {task_id!r} not found"}
        boundary = task.runner_payload.get("restart_boundary")
        if not isinstance(boundary, dict) or not bool(task.runner_payload.get("restartable")):
            return {
                "ok": False,
                "task": self._task_payload(task),
                "action": "not_restartable",
                "message": "Task has no explicit restartable boundary.",
            }
        if not _can_restart_from_task(task):
            return {
                "ok": False,
                "task": self._task_payload(task),
                "action": "still_running" if task.status == "running" else "not_restartable_now",
                "message": _restart_unavailable_reason(task),
            }
        if boundary.get("kind") != "skill_api":
            return {
                "ok": False,
                "task": self._task_payload(task),
                "action": "unsupported_boundary",
                "message": f"Restart boundary {boundary.get('kind')!r} is not supported.",
            }
        restart_context = context or TaskInvocationContext(
            user_id=task.user_id,
            session_id=task.session_id,
            thread_id=task.thread_id,
            owner_key=task.owner_key,
        )
        has_invocation_identity = any(
            [
                restart_context.invocation_id,
                restart_context.function_call_id,
                restart_context.tool_call_id,
            ]
        )
        if not has_invocation_identity:
            restart_call_id = f"restart:{task.task_id}:{uuid.uuid4().hex}"
            restart_context = replace(
                restart_context,
                turn_id=restart_context.turn_id or restart_call_id,
                invocation_id=restart_call_id,
                function_call_id=restart_call_id,
                tool_call_id=restart_call_id,
            )
        supervisor = ProcessExecutionSupervisor(
            task_store=self.task_store,
            event_store=self.event_store,
            tool_call_store=ToolCallRecordStore(db_path=self.task_store.db_path),
        )
        result = supervisor.invoke_skill_api(
            skill_name=str(boundary.get("skill_name", "")),
            api_name=str(boundary.get("api_name", "")),
            args=boundary.get("args"),
            inline_budget_ms=inline_budget_ms,
            context=restart_context,
            scope_key=str(boundary.get("scope_key") or "") or None,
            restartable=True,
        )
        payload = result.to_payload()
        payload["restarted_from_task_id"] = task.task_id
        self.event_store.append_event(
            task.task_id,
            "task.restarted",
            message="Task explicitly restarted from recorded boundary.",
            payload={
                "result": payload,
                "restart_boundary": boundary,
                "new_task_id": payload.get("task_id", ""),
            },
        )
        return {"ok": not bool(payload.get("error")), "action": "restarted", "result": payload}

    def record_task_checkpoint(
        self,
        task_id: str,
        *,
        checkpoint_type: str = "runner",
        runner_name: str = "",
        checkpoint_payload: dict[str, Any] | None = None,
        summary: str = "",
        status: str | None = None,
        resume_policy: str | None = None,
    ) -> dict[str, Any]:
        """Record a durable checkpoint and point the task at it.

        This is a runner-facing framework hook, not a generic promise that any
        task can be resumed. The runner adapter remains responsible for making
        the checkpoint meaningful.
        """
        task = self.task_store.get_task(task_id)
        if task is None:
            return {"ok": False, "error": f"task {task_id!r} not found"}
        try:
            normalized_checkpoint_payload = normalize_task_checkpoint_payload(
                runner_name=runner_name,
                checkpoint_type=checkpoint_type,
                payload=checkpoint_payload,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "action": "invalid_checkpoint_payload",
            }
        checkpoint = self.checkpoint_store.record_checkpoint(
            task_id=task.task_id,
            checkpoint_type=checkpoint_type,
            runner_name=runner_name,
            payload=normalized_checkpoint_payload,
            summary=summary,
        )
        updates: dict[str, Any] = {"checkpoint_ref": checkpoint.checkpoint_id}
        normalized_summary = str(summary or "").strip()
        if normalized_summary:
            updates["progress_summary"] = normalized_summary
        if status:
            updates["status"] = status
        if resume_policy:
            updates["resume_policy"] = resume_policy
        updated = self.task_store.update_task(task.task_id, **updates) or task
        self.event_store.append_event(
            task.task_id,
            "task.checkpoint_written",
            message=normalized_summary or "Task checkpoint written.",
            payload={"checkpoint": _checkpoint_payload(checkpoint)},
        )
        return {
            "ok": True,
            "task": self._task_payload(updated),
            "checkpoint": _checkpoint_payload(checkpoint),
        }

    def send_task_input(
        self,
        task_id: str,
        content: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record user input for a waiting task without pretending runner consumption."""
        task = self.task_store.get_task(task_id)
        if task is None:
            return {"ok": False, "error": f"task {task_id!r} not found"}
        if task.status not in {"waiting_user", "waiting_approval"}:
            return {
                "ok": False,
                "task": self._task_payload(task),
                "error": f"task is {task.status!r}, not waiting for input",
            }
        normalized = str(content or "").strip()
        if not normalized:
            return {"ok": False, "task": self._task_payload(task), "error": "content is required"}
        task_input = self.input_store.append_input(task_id, normalized, payload=payload)
        summary = f"Input received for task {task_id}."
        updated = self.task_store.update_task(
            task_id,
            progress_summary=summary,
        )
        self.event_store.append_event(
            task_id,
            "task.input_received",
            message=summary,
            payload={"input_id": task_input.input_id},
        )
        return {
            "ok": True,
            "task": self._task_payload(updated or task),
            "input": _input_payload(task_input),
            "message": "Input recorded. The task will remain waiting until its runner consumes the input.",
        }

    def sync_task(self, task_id: str, *, poll_timeout_ms: int = 0) -> TaskRun | None:
        """Synchronize a process-backed task with its current process state."""
        task = self.task_store.get_task(task_id)
        if task is None:
            return None
        return self.runner_registry.for_task(task).sync_task(self, task, poll_timeout_ms=poll_timeout_ms)

    def reconcile_stale_task(
        self,
        task_id: str,
        *,
        stale_lost_after_ms: int,
        now_ms: int | None = None,
    ) -> TaskRun | None:
        """Reconcile a stale task after its grace period without replaying work."""
        task = self.task_store.get_task(task_id)
        if task is None:
            return None
        return self.runner_registry.for_task(task).reconcile_stale_task(
            self,
            task,
            stale_lost_after_ms=stale_lost_after_ms,
            now_ms=now_ms,
        )

    def _remediate_claimed_stuck_task(
        self,
        task: TaskRun,
        *,
        stale_lost_after_ms: int,
        poll_timeout_ms: int,
        now_ms: int,
    ) -> TaskRun | None:
        """Synchronize one claimed stuck task without changing user intent."""
        if task.status == "running":
            return self.runner_registry.for_task(task).sync_task(
                self,
                task,
                poll_timeout_ms=poll_timeout_ms,
            )
        if task.status == "stale":
            return self.runner_registry.for_task(task).reconcile_stale_task(
                self,
                task,
                stale_lost_after_ms=stale_lost_after_ms,
                now_ms=now_ms,
            )
        return task

    def _task_payload(self, task: TaskRun, *, delivery_summary: dict[str, Any] | None = None) -> dict[str, Any]:
        """Project a task using this controller's runner registry."""
        return _task_payload(
            task,
            controls=self.runner_registry.controls(task),
            delivery_summary=delivery_summary,
        )

    def _artifacts_for_task_ids(self, task_ids: list[str]) -> list[Any]:
        """Return artifact rows for explicit task ids."""
        artifacts: list[Any] = []
        for task_id in task_ids:
            artifacts.extend(self.artifact_store.list_artifacts(task_id, limit=500))
        return artifacts

    def _list_task_context_summaries(self, task: TaskRun) -> list[ContextSummary]:
        """Return staged context summaries associated with one task."""
        if not task.session_id:
            return []
        try:
            return self.context_store.list_summaries(
                session_id=task.session_id,
                task_id=task.task_id,
                limit=20,
            )
        except Exception:
            return []

    def _sync_process_task(self, task: TaskRun, *, poll_timeout_ms: int = 0) -> TaskRun | None:
        """Synchronize a process-backed task with its current process state."""
        if task.status != "running" or task.status in TASK_TERMINAL_STATUSES or not task.external_ref:
            return task
        payload = get_process_session_manager().poll_session(
            task.external_ref,
            timeout_ms=poll_timeout_ms,
            scope_key=task.runner_payload.get("scope_key"),
        )
        if payload is None:
            if task.status == "running":
                updated = self.task_store.update_task(
                    task.task_id,
                    status="stale",
                    last_error="backing process session not found",
                    progress_summary=task.progress_summary or "Backing process session not found.",
                )
                if updated is not None:
                    self.event_store.append_event(
                        task.task_id,
                        "task.stale",
                        message="Backing process session not found.",
                    )
                    return updated
            return task
        return self._apply_process_payload(task, payload)

    def _reconcile_process_stale_task(
        self,
        task: TaskRun,
        *,
        stale_lost_after_ms: int,
        now_ms: int | None,
    ) -> TaskRun | None:
        """Reconcile a stale process-backed task after its grace period."""
        if task.status != "stale":
            return task
        current_ms = _wall_now_ms() if now_ms is None else int(now_ms)
        grace_ms = max(0, int(stale_lost_after_ms))
        if current_ms - task.updated_at_ms < grace_ms:
            return task
        payload = None
        if task.external_ref:
            payload = get_process_session_manager().poll_session(
                task.external_ref,
                timeout_ms=0,
                scope_key=task.runner_payload.get("scope_key"),
            )
        if payload is not None:
            recovered = self._apply_process_payload(task, payload)
            if recovered.status == "running":
                self.event_store.append_event(
                    recovered.task_id,
                    "task.recovered",
                    message="Backing process session found after stale state.",
                )
            return recovered
        summary = "Backing process session was not found after stale grace period."
        updated = self.task_store.update_task(
            task.task_id,
            status="lost",
            terminal_summary=summary,
            progress_summary=summary,
            last_error=summary,
            resume_policy=_terminal_resume_policy(task, "lost"),
        )
        if updated is not None:
            self.event_store.append_event(updated.task_id, "task.lost", message=summary)
            return updated
        return self.task_store.get_task(task.task_id)

    def _process_task_output(self, task: TaskRun, *, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
        """Return retained output for a process-backed task."""
        if task.external_ref:
            payload = get_process_session_manager().log_session(
                task.external_ref,
                scope_key=task.runner_payload.get("scope_key"),
            )
            if payload is not None:
                if artifacts and task.status in TASK_TERMINAL_STATUSES:
                    return {
                        "ok": True,
                        "task_id": task.task_id,
                        "status": task.status,
                        "output": task.terminal_summary or task.progress_summary,
                        "tail": str(payload.get("tail", "")),
                        "truncated": True,
                        "artifact_backed": True,
                        "artifacts": artifacts,
                    }
                return {
                    "ok": True,
                    "task_id": task.task_id,
                    "status": task.status,
                    "output": str(payload.get("aggregated", "")),
                    "tail": str(payload.get("tail", "")),
                    "truncated": bool(payload.get("truncated", False)),
                    "artifact_backed": False,
                    "artifacts": artifacts,
                }
        return self._generic_task_output(task, artifacts=artifacts)

    def _generic_task_output(self, task: TaskRun, *, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
        """Return generic retained task output from task facts."""
        return {
            "ok": True,
            "task_id": task.task_id,
            "status": task.status,
            "output": task.terminal_summary or task.progress_summary,
            "tail": task.progress_summary,
            "truncated": False,
            "artifact_backed": bool(artifacts),
            "artifacts": artifacts,
        }

    def _sync_external_snapshot_task(self, task: TaskRun, *, runner_name: str) -> TaskRun | None:
        """Apply a runner payload status snapshot for externally managed jobs."""
        if task.status in TASK_TERMINAL_STATUSES:
            return task
        snapshot = _external_status_snapshot(task)
        if not snapshot:
            return task
        next_status = _normalize_external_task_status(snapshot.get("status"))
        if not next_status:
            return task
        progress_summary = str(
            snapshot.get("progress_summary")
            or snapshot.get("message")
            or snapshot.get("summary")
            or task.progress_summary
            or ""
        )
        terminal_summary = str(snapshot.get("terminal_summary") or snapshot.get("output") or "")
        last_error = str(snapshot.get("last_error") or snapshot.get("error") or "")
        updates: dict[str, Any] = {
            "status": next_status,
            "progress_summary": progress_summary,
            "last_error": "" if next_status == "completed" else last_error,
        }
        if next_status in TASK_TERMINAL_STATUSES:
            updates["terminal_summary"] = terminal_summary or progress_summary or last_error
            if next_status in {"failed", "lost"} and not updates["last_error"]:
                updates["last_error"] = updates["terminal_summary"]
        elif terminal_summary:
            updates["terminal_summary"] = terminal_summary

        if _task_snapshot_noop(task, updates):
            return task
        updated = self.task_store.update_task(task.task_id, **updates)
        if updated is None:
            return self.task_store.get_task(task.task_id)
        event_type = f"task.{next_status}" if next_status in TASK_TERMINAL_STATUSES else "runner.status_polled"
        self.event_store.append_event(
            updated.task_id,
            event_type,
            message=updates.get("terminal_summary") or progress_summary,
            payload={
                "runner": runner_name,
                "external_ref": task.external_ref,
                "snapshot": snapshot,
            },
        )
        return updated

    def _apply_process_payload(self, task: TaskRun, payload: dict[str, object]) -> TaskRun:
        """Apply one observed process state to a process-backed task."""
        progress = _running_summary(payload)
        if bool(payload.get("exited")):
            process_status = str(payload.get("status", ""))
            exit_code = payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None
            terminal_status = "completed" if process_status == "completed" else "failed"
            output = _format_process_output(payload)
            summary, artifact_payload, context_summary_payload = self._summarize_terminal_output(
                task,
                output,
                payload=payload,
            )
            updated = self.task_store.update_task(
                task.task_id,
                status=terminal_status,
                terminal_summary=summary,
                progress_summary=progress,
                last_error="" if terminal_status == "completed" else summary[:1000],
                resume_policy=_terminal_resume_policy(task, terminal_status),
            )
            if updated is not None:
                if artifact_payload is not None:
                    self.event_store.append_event(
                        task.task_id,
                        "task.artifact_saved",
                        message=f"Saved task output artifact {artifact_payload['artifact_id']}.",
                        payload=artifact_payload,
                    )
                if context_summary_payload is not None:
                    self.event_store.append_event(
                        task.task_id,
                        "task.context_summary_saved",
                        message=f"Saved context summary {context_summary_payload['summary_id']}.",
                        payload=context_summary_payload,
                    )
                self.event_store.append_event(
                    task.task_id,
                    f"task.{terminal_status}",
                    message=summary[:2000],
                    payload={
                        "exit_code": exit_code,
                        "process_status": process_status,
                        "artifact_id": artifact_payload.get("artifact_id") if artifact_payload else None,
                    },
                )
                return updated
            refreshed = self.task_store.get_task(task.task_id)
            return refreshed or task
        updated = self.task_store.update_task(
            task.task_id,
            status="running",
            progress_summary=progress,
            last_error="",
        )
        return updated or task

    def _summarize_terminal_output(
        self,
        task: TaskRun,
        output: str,
        *,
        payload: dict[str, object],
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
        """Return a compact terminal summary plus optional artifact/summary payloads."""
        threshold = _output_artifact_threshold_chars()
        if len(output) <= threshold:
            return output, None, None
        artifact = _write_task_output_artifact(
            task=task,
            output=output,
            artifact_store=self.artifact_store,
            metadata={
                "source": "process_terminal_output",
                "truncated_by_process_session": bool(payload.get("truncated", False)),
                "exit_code": payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None,
            },
        )
        if artifact is None:
            return _compact_output_summary(output, artifact_payload=None), None, None
        artifact_payload = _artifact_payload(artifact)
        summary = _compact_output_summary(output, artifact_payload=artifact_payload)
        context_summary_payload = _write_task_artifact_context_summary(
            task=task,
            output=output,
            artifact_payload=artifact_payload,
            summary=summary,
            context_store=self.context_store,
        )
        return summary, artifact_payload, context_summary_payload

    def _stop_process_task(self, task: TaskRun, *, terminal_status: str, event_type: str) -> dict[str, Any]:
        """Stop a process-backed task with best-effort process termination."""
        if task.status in TASK_TERMINAL_STATUSES:
            return {"ok": True, "task": self._task_payload(task), "message": "Task is already terminal."}
        if not task.external_ref:
            updated = self.task_store.update_task(task.task_id, status=terminal_status, terminal_summary="No backing process.")
            if updated is None:
                return {"ok": False, "error": "failed to update task"}
            self.event_store.append_event(task.task_id, event_type, message="No backing process.")
            return {"ok": True, "task": self._task_payload(updated)}
        manager = get_process_session_manager()
        err = manager.kill_session(task.external_ref, scope_key=task.runner_payload.get("scope_key"))
        if err and "already exited" not in err.lower():
            synced = self.sync_task(task.task_id)
            if synced is not None and synced.status in TASK_TERMINAL_STATUSES:
                return {"ok": True, "task": self._task_payload(synced), "message": "Task already finished."}
            updated = self.task_store.update_task(
                task.task_id,
                status="lost",
                last_error=err,
                terminal_summary=err,
            )
            if updated is not None:
                self.event_store.append_event(task.task_id, "task.lost", message=err)
                return {"ok": False, "task": self._task_payload(updated), "error": err}
            return {"ok": False, "error": err}
        summary = "Task interrupted." if terminal_status == "interrupted" else "Task cancelled."
        updated = self.task_store.update_task(
            task.task_id,
            status=terminal_status,
            terminal_summary=summary,
            progress_summary=summary,
            resume_policy=(
                _restart_boundary_resume_policy(task)
                if terminal_status == "interrupted"
                else task.resume_policy
            ),
            cancel_policy="kill_process" if terminal_status == "cancelled" else task.cancel_policy,
        )
        if updated is None:
            return {"ok": False, "error": "failed to update task"}
        self.event_store.append_event(updated.task_id, event_type, message=summary)
        return {"ok": True, "task": self._task_payload(updated), "message": summary}


def _sync_browser_remote_job_task(controller: TaskController, task: TaskRun) -> TaskRun | None:
    """Synchronize a browser-remote TaskRun from the latest observed job row."""
    if task.status in TASK_TERMINAL_STATUSES:
        return task
    if task.status == "paused":
        return task
    job_record_id = str(task.runner_payload.get("job_record_id") or "").strip()
    if not job_record_id:
        summary = "Browser remote job record id is missing."
        updated = controller.task_store.update_task(
            task.task_id,
            status="stale",
            progress_summary=summary,
            last_error=summary,
        )
        if updated is not None:
            controller.event_store.append_event(
                updated.task_id,
                "task.stale",
                message=summary,
                payload={"runner": "browser_remote"},
            )
            return updated
        return task
    try:
        job = BrowserRemoteProviderStore(db_path=controller.task_store.db_path).get_job(job_record_id)
    except Exception as exc:
        summary = f"Browser remote job registry unavailable: {exc}"
        updated = controller.task_store.update_task(
            task.task_id,
            status="stale",
            progress_summary=summary,
            last_error=summary,
        )
        if updated is not None:
            controller.event_store.append_event(
                updated.task_id,
                "task.stale",
                message=summary,
                payload={"runner": "browser_remote", "error": str(exc)},
            )
            return updated
        return task
    if job is None:
        summary = "Browser remote job observation was not found."
        updated = controller.task_store.update_task(
            task.task_id,
            status="stale",
            progress_summary=summary,
            last_error=summary,
        )
        if updated is not None:
            controller.event_store.append_event(
                updated.task_id,
                "task.stale",
                message=summary,
                payload={"runner": "browser_remote", "job_record_id": job_record_id},
            )
            return updated
        return task
    protocol = _browser_remote_protocol_for_job(job, db_path=controller.task_store.db_path)
    if protocol is not None and protocol.status_path:
        live_result = _fetch_browser_remote_job_status(task, protocol=protocol)
        if live_result is not None and live_result.ok:
            snapshot = normalize_browser_remote_job_snapshot(live_result.payload, default_status=job.status)
            try:
                job = BrowserRemoteProviderStore(db_path=controller.task_store.db_path).record_job_observation(
                    provider_id=job.provider_id,
                    target=job.target,
                    node=job.node,
                    proxy_url=job.proxy_url,
                    action=job.action,
                    external_job_id=job.external_job_id,
                    status=str(snapshot.get("status") or job.status),
                    payload=snapshot,
                    last_error=str(snapshot.get("error") or ""),
                )
            except Exception:
                pass
        elif live_result is not None and not live_result.ok:
            return _mark_browser_remote_job_status_unavailable(controller, task, live_result.error)
    runner_payload = _browser_remote_runner_payload(job, db_path=controller.task_store.db_path)
    staged = controller.task_store.update_task(
        task.task_id,
        runner_payload=runner_payload,
        runner_capabilities={**task.runner_capabilities, **_browser_remote_runner_capabilities(runner_payload)},
    ) or task
    synced = controller._sync_external_snapshot_task(staged, runner_name="browser_remote") or staged
    if protocol is not None:
        checkpointed = _record_browser_remote_checkpoint_if_present(
            controller,
            synced,
            protocol,
            _external_status_snapshot(synced),
            status="paused" if synced.status == "paused" else None,
            summary=str(
                _external_status_snapshot(synced).get("summary")
                or _external_status_snapshot(synced).get("message")
                or "Browser remote job checkpoint."
            ),
        )
        return checkpointed or synced
    return synced


def _browser_remote_runner_payload(job: BrowserRemoteJob, *, db_path: Any | None = None) -> dict[str, Any]:
    """Build runner payload for a remote browser job TaskRun."""
    protocol = _browser_remote_protocol_for_job(job, db_path=db_path)
    payload = {
        "runner": "browser_remote",
        "job_record_id": job.job_record_id,
        "provider_id": job.provider_id,
        "target": job.target,
        "node": job.node,
        "proxy_url": job.proxy_url,
        "action": job.action,
        "external_job_id": job.external_job_id,
        "remote_job": browser_remote_job_payload(job),
        "status_snapshot": _browser_remote_job_status_snapshot(job),
    }
    if protocol is not None:
        payload["job_protocol"] = protocol.to_payload()
    return payload


def _browser_remote_runner_capabilities(runner_payload: dict[str, Any]) -> dict[str, bool]:
    """Return capabilities for one browser remote runner payload."""
    capabilities = dict(BROWSER_REMOTE_RUNNER_CAPABILITIES)
    protocol = _browser_remote_protocol_from_runner_payload(runner_payload)
    if protocol is not None:
        protocol_capabilities = protocol.runner_capabilities
        capabilities["status"] = capabilities["status"] or protocol_capabilities["status"]
        capabilities["cancel"] = protocol_capabilities["cancel"]
        capabilities["output"] = capabilities["output"] or protocol_capabilities["output"]
        capabilities["pause"] = protocol_capabilities["pause"]
        capabilities["checkpoint"] = protocol_capabilities["checkpoint"]
        capabilities["resume"] = protocol_capabilities["resume"]
    return capabilities


def _browser_remote_protocol_for_job(
    job: BrowserRemoteJob,
    *,
    db_path: Any | None = None,
) -> BrowserRemoteJobProtocolConfig | None:
    """Resolve browser remote job protocol from job payload or provider capability."""
    payload_protocol = _extract_browser_remote_job_protocol(job.payload)
    if payload_protocol is not None:
        return payload_protocol
    try:
        provider = BrowserRemoteProviderStore(db_path=db_path).get_provider(job.provider_id)
    except Exception:
        provider = None
    if provider is None:
        return None
    return _extract_browser_remote_job_protocol(provider.capability)


def _browser_remote_protocol_for_task(task: TaskRun) -> BrowserRemoteJobProtocolConfig | None:
    """Resolve browser remote job protocol from a TaskRun payload."""
    return _browser_remote_protocol_from_runner_payload(task.runner_payload)


def _browser_remote_protocol_from_runner_payload(
    runner_payload: dict[str, Any],
) -> BrowserRemoteJobProtocolConfig | None:
    raw = runner_payload.get("job_protocol") or runner_payload.get("jobProtocol")
    return browser_remote_job_protocol_from_payload(raw)


def _extract_browser_remote_job_protocol(payload: dict[str, Any]) -> BrowserRemoteJobProtocolConfig | None:
    """Extract a job protocol declaration from a provider/job payload."""
    raw = payload.get("jobProtocol") or payload.get("job_protocol")
    if raw is None and isinstance(payload.get("capability"), dict):
        capability = payload["capability"]
        raw = capability.get("jobProtocol") or capability.get("job_protocol")
    return browser_remote_job_protocol_from_payload(raw)


def _fetch_browser_remote_job_status(
    task: TaskRun,
    *,
    protocol: BrowserRemoteJobProtocolConfig | None = None,
) -> Any | None:
    """Fetch live status for one browser remote job when configured."""
    resolved_protocol = protocol or _browser_remote_protocol_for_task(task)
    if resolved_protocol is None or not resolved_protocol.status_path or not task.external_ref:
        return None
    return call_browser_remote_job_status(
        proxy_url=str(task.runner_payload.get("proxy_url") or ""),
        protocol=resolved_protocol,
        job_id=task.external_ref,
        token=_browser_remote_proxy_token(task),
        context_payload=_browser_remote_job_context(task),
    )


def _fetch_browser_remote_job_output(task: TaskRun) -> Any | None:
    """Fetch live output for one browser remote job when configured."""
    protocol = _browser_remote_protocol_for_task(task)
    if protocol is None or not protocol.output_path or not task.external_ref:
        return None
    return call_browser_remote_job_output(
        proxy_url=str(task.runner_payload.get("proxy_url") or ""),
        protocol=protocol,
        job_id=task.external_ref,
        token=_browser_remote_proxy_token(task),
        context_payload=_browser_remote_job_context(task),
    )


def _record_browser_remote_checkpoint_if_present(
    controller: TaskController,
    task: TaskRun,
    protocol: BrowserRemoteJobProtocolConfig,
    snapshot: dict[str, Any],
    *,
    status: str | None,
    summary: str,
) -> TaskRun | None:
    """Record a configured browser remote checkpoint once per distinct payload."""
    try:
        checkpoint = _browser_remote_checkpoint_from_snapshot(protocol, snapshot)
    except ValueError as exc:
        error = str(exc)
        payload = dict(task.runner_payload)
        payload["last_checkpoint_error"] = error
        updated = controller.task_store.update_task(task.task_id, runner_payload=payload) or task
        controller.event_store.append_event(
            task.task_id,
            "runner.checkpoint_rejected",
            message=error,
            payload={"runner": "browser_remote", "external_ref": task.external_ref, "error": error},
        )
        return updated
    if not checkpoint:
        return task
    fingerprint = _stable_hash(checkpoint)
    if task.runner_payload.get("checkpoint_fingerprint") == fingerprint and task.checkpoint_ref:
        return task
    if task.checkpoint_ref:
        existing = controller.checkpoint_store.get_checkpoint(task.checkpoint_ref)
        if existing is not None and _stable_hash(existing.payload) == fingerprint:
            payload = dict(task.runner_payload)
            payload["checkpoint_fingerprint"] = fingerprint
            payload["latest_checkpoint"] = checkpoint
            return controller.task_store.update_task(task.task_id, runner_payload=payload) or task
    recorded = controller.record_task_checkpoint(
        task.task_id,
        checkpoint_type="browser_remote_job_state",
        runner_name="browser_remote",
        checkpoint_payload=checkpoint,
        summary=summary,
        status=status,
        resume_policy="checkpoint" if status == "paused" and protocol.resume_path else None,
    )
    if not recorded.get("ok"):
        return controller.task_store.get_task(task.task_id) or task
    updated = controller.task_store.get_task(task.task_id) or task
    payload = dict(updated.runner_payload)
    payload["checkpoint_fingerprint"] = fingerprint
    payload["latest_checkpoint"] = checkpoint
    return controller.task_store.update_task(updated.task_id, runner_payload=payload) or updated


def _browser_remote_checkpoint_from_snapshot(
    protocol: BrowserRemoteJobProtocolConfig,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Extract a browser remote checkpoint payload from a provider snapshot."""
    checkpoint_path = str(protocol.checkpoint_path or "").strip()
    raw = extract_path(snapshot, checkpoint_path) if checkpoint_path else snapshot.get("checkpoint")
    if not isinstance(raw, dict) or not raw:
        return {}
    return normalize_browser_remote_job_checkpoint_payload(protocol=protocol, payload=raw)


def _cancel_browser_remote_job_task(controller: TaskController, task: TaskRun) -> dict[str, Any]:
    """Cancel one browser remote job through its declared protocol."""
    if task.status in TASK_TERMINAL_STATUSES:
        return {"ok": True, "task": controller._task_payload(task), "message": "Task is already terminal."}
    if task.status != "running":
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_running",
            "message": f"Task is {task.status!r}, not running.",
        }
    protocol = _browser_remote_protocol_for_task(task)
    if protocol is None or not protocol.cancel_path or not task.external_ref:
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_supported",
            "message": "Browser remote cancel protocol is not configured.",
        }
    result = call_browser_remote_job_cancel(
        proxy_url=str(task.runner_payload.get("proxy_url") or ""),
        protocol=protocol,
        job_id=task.external_ref,
        token=_browser_remote_proxy_token(task),
        context_payload=_browser_remote_job_context(task),
    )
    if not result.ok:
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "cancel_failed",
            "message": result.error,
        }
    snapshot = normalize_browser_remote_job_snapshot(result.payload, default_status="cancelled")
    runner_payload = dict(task.runner_payload)
    runner_payload["status_snapshot"] = snapshot
    runner_payload["last_cancel_result"] = result.raw_payload
    updated = controller.task_store.update_task(task.task_id, runner_payload=runner_payload)
    if updated is None:
        return {"ok": False, "task": controller._task_payload(task), "message": "Failed to update task."}
    synced = controller._sync_external_snapshot_task(updated, runner_name="browser_remote") or updated
    return {
        "ok": True,
        "task": controller._task_payload(synced),
        "action": "cancel_requested",
        "message": "Browser remote job cancel requested.",
    }


def _pause_browser_remote_job_task(controller: TaskController, task: TaskRun) -> dict[str, Any]:
    """Pause one browser remote job through its declared protocol."""
    if task.status in TASK_TERMINAL_STATUSES:
        return {"ok": True, "task": controller._task_payload(task), "message": "Task is already terminal."}
    if task.status == "paused":
        return {
            "ok": True,
            "task": controller._task_payload(task),
            "action": "already_paused",
            "message": "Browser remote job is already paused.",
        }
    if task.status != "running":
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_running",
            "message": f"Task is {task.status!r}, not running.",
        }
    protocol = _browser_remote_protocol_for_task(task)
    if protocol is None or not protocol.pause_path or not task.external_ref:
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_supported",
            "message": "Browser remote pause protocol is not configured.",
        }
    result = call_browser_remote_job_pause(
        proxy_url=str(task.runner_payload.get("proxy_url") or ""),
        protocol=protocol,
        job_id=task.external_ref,
        token=_browser_remote_proxy_token(task),
        context_payload=_browser_remote_job_context(task),
    )
    if not result.ok:
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "pause_failed",
            "message": result.error,
        }
    snapshot = normalize_browser_remote_job_snapshot(result.payload, default_status="paused")
    runner_payload = dict(task.runner_payload)
    runner_payload["status_snapshot"] = snapshot
    runner_payload["last_pause_result"] = result.raw_payload
    updated = controller.task_store.update_task(
        task.task_id,
        runner_payload=runner_payload,
        resume_policy="checkpoint" if protocol.resume_path else task.resume_policy,
    )
    if updated is None:
        return {"ok": False, "task": controller._task_payload(task), "message": "Failed to update task."}
    synced = controller._sync_external_snapshot_task(updated, runner_name="browser_remote") or updated
    checkpointed = _record_browser_remote_checkpoint_if_present(
        controller,
        synced,
        protocol,
        snapshot,
        status="paused" if synced.status == "paused" else None,
        summary=str(snapshot.get("summary") or snapshot.get("message") or "Browser remote pause checkpoint."),
    )
    synced = checkpointed or synced
    return {
        "ok": True,
        "task": controller._task_payload(synced),
        "action": "paused" if synced.status == "paused" else "pause_requested",
        "message": "Browser remote job pause requested.",
    }


def _resume_browser_remote_job_task(controller: TaskController, task: TaskRun) -> dict[str, Any]:
    """Resume one browser remote job through its declared protocol."""
    if task.status in TASK_TERMINAL_STATUSES:
        return {"ok": True, "task": controller._task_payload(task), "message": "Task is already terminal."}
    if task.status != "paused":
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_paused",
            "message": f"Task is {task.status!r}, not paused.",
        }
    protocol = _browser_remote_protocol_for_task(task)
    if protocol is None or not protocol.resume_path or not task.external_ref:
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_supported",
            "message": "Browser remote resume protocol is not configured.",
        }
    checkpoint_payload: dict[str, Any] = {}
    if task.checkpoint_ref:
        checkpoint = controller.checkpoint_store.get_checkpoint(task.checkpoint_ref)
        if checkpoint is not None:
            checkpoint_payload = checkpoint.payload
    result = call_browser_remote_job_resume(
        proxy_url=str(task.runner_payload.get("proxy_url") or ""),
        protocol=protocol,
        job_id=task.external_ref,
        token=_browser_remote_proxy_token(task),
        context_payload=_browser_remote_job_context(task),
        checkpoint_payload=checkpoint_payload,
    )
    if not result.ok:
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "resume_failed",
            "message": result.error,
        }
    snapshot = normalize_browser_remote_job_snapshot(result.payload, default_status="running")
    runner_payload = dict(task.runner_payload)
    runner_payload["status_snapshot"] = snapshot
    runner_payload["last_resume_result"] = result.raw_payload
    updated = controller.task_store.update_task(
        task.task_id,
        runner_payload=runner_payload,
        resume_policy="checkpoint",
    )
    if updated is None:
        return {"ok": False, "task": controller._task_payload(task), "message": "Failed to update task."}
    synced = controller._sync_external_snapshot_task(updated, runner_name="browser_remote") or updated
    checkpointed = _record_browser_remote_checkpoint_if_present(
        controller,
        synced,
        protocol,
        snapshot,
        status=None,
        summary=str(snapshot.get("summary") or snapshot.get("message") or "Browser remote resume checkpoint."),
    )
    synced = checkpointed or synced
    return {
        "ok": True,
        "task": controller._task_payload(synced),
        "action": "resumed" if synced.status == "running" else "resume_requested",
        "message": "Browser remote job resume requested.",
    }


def _mark_browser_remote_job_status_unavailable(
    controller: TaskController,
    task: TaskRun,
    error: str,
) -> TaskRun | None:
    """Mark a browser remote job stale when live status is unavailable."""
    summary = f"Browser remote job status unavailable: {error}"
    updates: dict[str, Any] = {
        "progress_summary": summary,
        "last_error": summary,
    }
    if task.status == "running":
        updates["status"] = "stale"
    updated = controller.task_store.update_task(task.task_id, **updates)
    if updated is not None:
        controller.event_store.append_event(
            updated.task_id,
            "task.stale" if updated.status == "stale" else "runner.status_poll_failed",
            message=summary,
            payload={"runner": "browser_remote", "external_ref": task.external_ref},
        )
    return updated


def _browser_remote_proxy_token(task: TaskRun) -> str:
    """Return the configured proxy token for a browser remote task."""
    target = str(task.runner_payload.get("target") or "").strip().lower()
    if target == "node":
        return os.getenv("OPENPPX_BROWSER_NODE_PROXY_TOKEN", "").strip() or os.getenv(
            "OPENPPX_BROWSER_PROXY_TOKEN", ""
        ).strip()
    if target == "sandbox":
        return os.getenv("OPENPPX_BROWSER_SANDBOX_PROXY_TOKEN", "").strip() or os.getenv(
            "OPENPPX_BROWSER_PROXY_TOKEN", ""
        ).strip()
    return os.getenv("OPENPPX_BROWSER_PROXY_TOKEN", "").strip()


def _browser_remote_job_context(task: TaskRun) -> dict[str, Any]:
    """Return stored context for remote browser job control calls."""
    return {
        "user_id": task.user_id,
        "session_id": task.session_id,
        "invocation_id": task.invocation_id,
        "function_call_id": task.function_call_id,
        "job_record_id": str(task.runner_payload.get("job_record_id") or ""),
        "provider_id": str(task.runner_payload.get("provider_id") or ""),
    }


def _browser_remote_job_status_snapshot(job: BrowserRemoteJob) -> dict[str, Any]:
    """Normalize one observed remote browser job payload for TaskRun sync."""
    payload = dict(job.payload)
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    snapshot = dict(response)
    snapshot.update(payload)
    snapshot["status"] = job.status or snapshot.get("status") or snapshot.get("jobStatus")
    if "summary" not in snapshot and "message" not in snapshot:
        snapshot["summary"] = f"Remote browser job {job.external_job_id} is {snapshot['status']}."
    if job.last_error and "error" not in snapshot:
        snapshot["error"] = job.last_error
    return snapshot


def _browser_remote_progress_summary(job: BrowserRemoteJob) -> str:
    """Return a compact progress summary for one remote browser job."""
    snapshot = _browser_remote_job_status_snapshot(job)
    for key in ("progress_summary", "summary", "message", "output", "error"):
        value = str(snapshot.get(key) or "").strip()
        if value:
            return value[:MAX_TERMINAL_SUMMARY_CHARS]
    return f"Remote browser job {job.external_job_id} is {job.status}."


def _browser_remote_terminal_summary(job: BrowserRemoteJob) -> str:
    """Return a terminal summary for one remote browser job."""
    snapshot = _browser_remote_job_status_snapshot(job)
    for key in ("terminal_summary", "output", "summary", "message", "error"):
        value = str(snapshot.get(key) or "").strip()
        if value:
            return value[:MAX_TERMINAL_SUMMARY_CHARS]
    return _browser_remote_progress_summary(job)


def _browser_remote_task_title(job: BrowserRemoteJob) -> str:
    """Return a readable TaskRun title for a remote browser job."""
    parts = ["browser"]
    if job.target:
        parts.append(job.target)
    if job.action:
        parts.append(job.action)
    if job.external_job_id:
        parts.append(job.external_job_id)
    return ":".join(parts)


def _render_browser_remote_job_output(snapshot: dict[str, Any]) -> str:
    """Render useful output from a browser remote job snapshot."""
    for key in ("output", "result", "summary", "message", "error"):
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
    return ""


def _gui_job_id(task: TaskRun) -> str:
    """Return the backing GUI job id for one TaskRun."""
    return str(task.external_ref or task.runner_payload.get("job_id") or "").strip()


def _sync_gui_job_task(
    controller: TaskController,
    task: TaskRun,
    *,
    status_result: dict[str, Any] | None = None,
) -> TaskRun | None:
    """Poll one GUI job and apply the latest status/checkpoint facts."""
    if task.status in TASK_TERMINAL_STATUSES:
        return task
    job_id = _gui_job_id(task)
    if not job_id:
        return _mark_gui_job_status_unavailable(controller, task, "GUI job id is missing.")
    result = status_result if status_result is not None else _fetch_gui_job_status_payload(task)
    if not result.get("ok"):
        return _mark_gui_job_status_unavailable(controller, task, str(result.get("error") or "status unavailable"))

    checkpoint = result.get("checkpoint") if isinstance(result.get("checkpoint"), dict) else {}
    runner_payload = dict(task.runner_payload)
    runner_payload.update(
        {
            "runner": "gui_job",
            "job_id": job_id,
            "status_snapshot": _gui_job_status_snapshot(result),
            "last_status_result": result,
        }
    )
    if checkpoint:
        runner_payload["latest_checkpoint"] = checkpoint
    if _normalize_external_task_status(result.get("status")) in {"paused", "interrupted", "failed", "lost", "stale"}:
        runner_payload.pop("stop_requested", None)
    payload_task = controller.task_store.update_task(
        task.task_id,
        runner_payload=runner_payload,
        runner_capabilities={**GUI_JOB_RUNNER_CAPABILITIES, **task.runner_capabilities},
    ) or task
    synced = controller._sync_external_snapshot_task(payload_task, runner_name="gui_job") or payload_task
    if synced.status in {"paused", "interrupted", "failed", "lost", "stale"}:
        return _record_gui_checkpoint_if_present(
            controller,
            synced,
            checkpoint,
            status="paused" if synced.status == "paused" else None,
            summary=str(result.get("summary") or "GUI job checkpoint."),
        ) or synced
    return synced


def _fetch_gui_job_status_payload(task: TaskRun) -> dict[str, Any]:
    """Fetch one GUI job status payload and normalize exceptions."""
    job_id = _gui_job_id(task)
    if not job_id:
        return {"ok": False, "error": "GUI job id is missing."}
    try:
        result = gui_task_job_status(job_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return result if isinstance(result, dict) else {"ok": False, "error": "invalid GUI job status payload"}


def _gui_job_status_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Build a TaskRun-compatible status snapshot from GUI job payload."""
    snapshot: dict[str, Any] = {
        "status": str(result.get("status") or ""),
        "summary": str(result.get("summary") or ""),
        "error": str(result.get("error") or ""),
    }
    checkpoint = result.get("checkpoint")
    if isinstance(checkpoint, dict):
        snapshot["checkpoint"] = checkpoint
    job_result = result.get("result")
    if isinstance(job_result, dict):
        snapshot["result"] = job_result
        for key in ("final_summary", "message", "error"):
            value = job_result.get(key)
            if value not in (None, ""):
                snapshot.setdefault("output", str(value))
                break
    return snapshot


def _record_gui_checkpoint_if_present(
    controller: TaskController,
    task: TaskRun,
    checkpoint: Any,
    *,
    status: str | None,
    summary: str,
) -> TaskRun | None:
    """Record a GUI checkpoint once per distinct checkpoint payload."""
    if not isinstance(checkpoint, dict) or not checkpoint:
        return task
    fingerprint = _stable_hash(checkpoint)
    if task.runner_payload.get("checkpoint_fingerprint") == fingerprint and task.checkpoint_ref:
        return task
    if task.checkpoint_ref:
        existing = controller.checkpoint_store.get_checkpoint(task.checkpoint_ref)
        if existing is not None and _stable_hash(existing.payload) == fingerprint:
            payload = dict(task.runner_payload)
            payload["checkpoint_fingerprint"] = fingerprint
            payload["latest_checkpoint"] = checkpoint
            return controller.task_store.update_task(task.task_id, runner_payload=payload) or task
    recorded = controller.record_task_checkpoint(
        task.task_id,
        checkpoint_type="gui_runner_state",
        runner_name="gui_job",
        checkpoint_payload=checkpoint,
        summary=summary,
        status=status,
        resume_policy="checkpoint",
    )
    if not recorded.get("ok"):
        return controller.task_store.get_task(task.task_id) or task
    updated = controller.task_store.get_task(task.task_id) or task
    payload = dict(updated.runner_payload)
    payload["checkpoint_fingerprint"] = fingerprint
    payload["latest_checkpoint"] = checkpoint
    return controller.task_store.update_task(updated.task_id, runner_payload=payload) or updated


def _mark_gui_job_status_unavailable(controller: TaskController, task: TaskRun, error: str) -> TaskRun | None:
    """Mark a GUI job stale when its backing job cannot be observed."""
    summary = f"GUI job status unavailable: {error}"
    updates: dict[str, Any] = {
        "progress_summary": summary,
        "last_error": summary,
    }
    if task.status == "running":
        updates["status"] = "stale"
    updated = controller.task_store.update_task(task.task_id, **updates)
    if updated is not None:
        controller.event_store.append_event(
            updated.task_id,
            "task.stale" if updated.status == "stale" else "runner.status_poll_failed",
            message=summary,
            payload={"runner": "gui_job", "job_id": _gui_job_id(task)},
        )
    return updated


def _request_gui_job_stop(
    controller: TaskController,
    task: TaskRun,
    *,
    terminal_status: str,
    event_type: str,
) -> dict[str, Any]:
    """Request cooperative stop for a GUI job without faking completion."""
    synced = _sync_gui_job_task(controller, task) or task
    if synced.status in TASK_TERMINAL_STATUSES:
        return {"ok": True, "task": controller._task_payload(synced), "message": "Task is already terminal."}
    if synced.status != "running":
        return {
            "ok": False,
            "task": controller._task_payload(synced),
            "action": "not_running",
            "message": f"Task is {synced.status!r}, not running.",
        }
    job_id = _gui_job_id(synced)
    if not job_id:
        return {
            "ok": False,
            "task": controller._task_payload(synced),
            "action": "missing_job",
            "message": "GUI job id is missing.",
        }
    normalized = "cancelled" if terminal_status == "cancelled" else "interrupted"
    result = gui_task_job_cancel(
        job_id,
        terminal_status=normalized,
        reason=f"GUI job {normalized} requested by user.",
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "task": controller._task_payload(synced),
            "action": "stop_failed",
            "message": str(result.get("error") or "Failed to request GUI job stop."),
        }
    payload = dict(synced.runner_payload)
    payload["stop_requested"] = {
        "terminal_status": normalized,
        "requested_at_ms": _wall_now_ms(),
        "job_result": result,
    }
    summary = (
        "GUI job cancellation requested; waiting for the runner to stop."
        if normalized == "cancelled"
        else "GUI job interrupt requested; waiting for the runner to stop."
    )
    updated = controller.task_store.update_task(
        synced.task_id,
        runner_payload=payload,
        progress_summary=summary,
    ) or synced
    controller.event_store.append_event(
        updated.task_id,
        event_type,
        message=summary,
        payload={"runner": "gui_job", "job_id": job_id, "result": result},
    )
    return {
        "ok": True,
        "task": controller._task_payload(updated),
        "action": "stop_requested",
        "message": summary,
    }


def _render_gui_job_output(payload: dict[str, Any]) -> str:
    """Render a GUI job output payload as user-visible text."""
    for key in ("output", "result", "checkpoint"):
        value = payload.get(key)
        if isinstance(value, dict) and value:
            try:
                return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            except Exception:
                return str(value)
        if value not in (None, ""):
            return str(value)
    return str(payload.get("summary") or "")


def _gui_job_stop_reason(
    task: TaskRun,
    *,
    job_id: str,
    capability: str,
    stop_requested: bool,
    allow_paused: bool = False,
) -> str:
    """Return why a GUI job stop/pause control is unavailable."""
    if task.status in TASK_TERMINAL_STATUSES:
        return "task is terminal"
    if allow_paused and task.status == "paused":
        return ""
    if task.status == "paused":
        return "task is already paused"
    if task.status != "running":
        return "task is not running"
    if stop_requested:
        return "GUI job stop is already requested"
    if not job_id:
        return "GUI job id is missing"
    if not bool(task.runner_capabilities.get(capability)):
        return f"GUI job runner does not support {capability}"
    return ""


def _gui_job_resume_reason(task: TaskRun) -> str:
    """Return why GUI job resume/rejoin is unavailable."""
    if task.status == "running" and bool(task.runner_capabilities.get("rejoin")):
        return ""
    if task.status == "paused":
        if not task.checkpoint_ref:
            return "paused GUI job has no checkpoint"
        if task.resume_policy != "checkpoint":
            return "paused GUI job is not checkpoint-resumable"
        return ""
    if task.status in TASK_TERMINAL_STATUSES:
        return "task is terminal"
    return "task is not paused"


def _gui_job_checkpoint_resume_policy(task: TaskRun) -> str:
    """Return terminal resume policy for a GUI job."""
    if task.checkpoint_ref or isinstance(task.runner_payload.get("latest_checkpoint"), dict):
        return "checkpoint"
    return "not_resumable"


def _normalize_inline_budget_ms(value: int | None) -> int:
    """Clamp inline wait budget to safe bounds."""
    if value is None:
        return DEFAULT_INLINE_BUDGET_MS
    try:
        parsed = int(value)
    except Exception:
        return DEFAULT_INLINE_BUDGET_MS
    return max(0, min(parsed, MAX_INLINE_BUDGET_MS))


def _wall_now_ms() -> int:
    """Return the current wall-clock timestamp in milliseconds."""
    return int(time.time() * 1000)


def _poll_until_budget(
    *,
    session_id: str,
    timeout_ms: int,
    scope_key: str | None,
) -> dict[str, object] | None:
    """Poll a process until it exits or the inline budget is exhausted.

    `ProcessSessionManager.poll_session` returns as soon as new output arrives.
    For inline execution we need to keep waiting inside the same budget, or a
    quick script that prints before exit can be misclassified as a background
    task.
    """
    manager = get_process_session_manager()
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    last_payload: dict[str, object] | None = None
    while True:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        payload = manager.poll_session(session_id, timeout_ms=remaining_ms, scope_key=scope_key)
        if payload is None:
            return last_payload
        last_payload = payload
        if bool(payload.get("exited")):
            return payload
        if remaining_ms <= 0:
            return payload
        time.sleep(min(0.02, max(0.001, remaining_ms / 1000.0)))


def _stable_hash(payload: Any) -> str:
    """Return a stable sha256 hash for JSON-like payloads."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _idempotency_key(*, context: TaskInvocationContext, tool_name: str, args_hash: str) -> str:
    """Build an execution-level idempotency key."""
    if context.invocation_id or context.function_call_id:
        return ":".join(
            [
                "openppx",
                context.user_id,
                context.session_id,
                context.invocation_id,
                context.function_call_id,
                tool_name,
                args_hash,
            ]
        )
    return f"openppx:{tool_name}:{args_hash}:{os.getpid()}:{uuid.uuid4().hex}"


def _format_process_output(payload: dict[str, object], *, warnings: list[str] | None = None) -> str:
    """Format process output for inline results and terminal summaries."""
    parts: list[str] = []
    if warnings:
        parts.extend(warnings)
    stdout = str(payload.get("stdout", "") or "")
    stderr = str(payload.get("stderr", "") or "")
    aggregated = str(payload.get("aggregated", "") or "")
    if aggregated:
        parts.append(aggregated)
    elif stdout:
        parts.append(stdout)
    if stderr and not aggregated:
        parts.append(f"STDERR:\n{stderr}")
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        parts.append(f"Exit code: {exit_code}")
    text = "\n".join(part.strip() for part in parts if part.strip()).strip()
    return text or "(no output)"


def _running_summary(payload: dict[str, object], *, warnings: list[str] | None = None) -> str:
    """Return a compact progress summary for a running process."""
    pieces: list[str] = []
    if warnings:
        pieces.extend(warnings)
    stdout = str(payload.get("stdout", "") or "").strip()
    stderr = str(payload.get("stderr", "") or "").strip()
    tail = str(payload.get("tail", "") or "").strip()
    if stdout:
        pieces.append(stdout[-1000:])
    if stderr:
        pieces.append(stderr[-1000:])
    if not pieces and tail:
        pieces.append(tail[-1000:])
    if not pieces:
        pieces.append("Process still running.")
    return "\n".join(pieces)


def _task_payload(
    task: TaskRun,
    *,
    controls: dict[str, Any] | None = None,
    delivery_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a task into a stable API payload."""
    return {
        "task_id": task.task_id,
        "kind": task.kind,
        "status": task.status,
        "title": task.title,
        "session_id": task.session_id,
        "thread_id": task.thread_id,
        "external_ref": task.external_ref,
        "resume_policy": task.resume_policy,
        "stop_policy": task.stop_policy,
        "cancel_policy": task.cancel_policy,
        "checkpoint_ref": task.checkpoint_ref,
        "runner_capabilities": task.runner_capabilities,
        "progress_summary": task.progress_summary,
        "terminal_summary": task.terminal_summary,
        "last_error": task.last_error,
        "created_at_ms": task.created_at_ms,
        "updated_at_ms": task.updated_at_ms,
        "ended_at_ms": task.ended_at_ms,
        "controls": controls or DEFAULT_TASK_RUNNER_REGISTRY.controls(task),
        "delivery_summary": delivery_summary or _empty_delivery_summary(),
    }


def _task_control_snapshot_payload(task_payload: dict[str, Any]) -> dict[str, Any]:
    """Return a compact UI/app task control snapshot from a task payload."""
    controls = task_payload.get("controls") if isinstance(task_payload.get("controls"), dict) else {}
    return {
        "task_id": task_payload.get("task_id", ""),
        "kind": task_payload.get("kind", ""),
        "status": task_payload.get("status", ""),
        "title": task_payload.get("title", ""),
        "progress_summary": task_payload.get("progress_summary", ""),
        "terminal_summary": task_payload.get("terminal_summary", ""),
        "last_error": task_payload.get("last_error", ""),
        "checkpoint_ref": task_payload.get("checkpoint_ref", ""),
        "resume_policy": task_payload.get("resume_policy", ""),
        "updated_at_ms": task_payload.get("updated_at_ms", 0),
        "actions": list(controls.get("actions") or []),
        "controls": controls,
    }


def _task_runner_name(task: TaskRun) -> str:
    """Return the normalized backing runner name for one task."""
    runner = str(task.runner_payload.get("runner", "") or "").strip()
    if runner:
        return runner
    if task.external_ref:
        return "process"
    return ""


def _external_status_snapshot(task: TaskRun) -> dict[str, Any]:
    """Return a normalized external runner status snapshot from task payload."""
    payload = task.runner_payload
    raw = payload.get("status_snapshot")
    if isinstance(raw, dict):
        return dict(raw)
    raw = payload.get("status")
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _normalize_external_task_status(value: Any) -> str:
    """Map external runner status labels to TaskRun statuses."""
    raw = str(value or "").strip().lower()
    aliases = {
        "queued": "queued",
        "pending": "queued",
        "created": "queued",
        "running": "running",
        "in_progress": "running",
        "processing": "running",
        "paused": "paused",
        "pausing": "paused",
        "interrupted": "interrupted",
        "interrupt": "interrupted",
        "waiting_user": "waiting_user",
        "input_required": "waiting_user",
        "waiting_approval": "waiting_approval",
        "approval_required": "waiting_approval",
        "completed": "completed",
        "complete": "completed",
        "succeeded": "completed",
        "success": "completed",
        "failed": "failed",
        "error": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "lost": "lost",
        "stale": "stale",
    }
    return aliases.get(raw, "")


def _task_snapshot_noop(task: TaskRun, updates: dict[str, Any]) -> bool:
    """Return whether applying snapshot updates would not change task facts."""
    if updates.get("status") != task.status:
        return False
    for key in ("progress_summary", "terminal_summary", "last_error"):
        if key in updates and str(updates[key] or "") != str(getattr(task, key) or ""):
            return False
    return True


def _build_task_controls(
    task: TaskRun,
    *,
    can_interrupt: bool,
    interrupt_reason: str,
    can_cancel: bool,
    cancel_reason: str,
    can_pause: bool,
    pause_reason: str,
    can_resume: bool,
    resume_reason: str,
) -> dict[str, Any]:
    """Build the stable task controls payload from adapter decisions."""
    waiting = task.status in {"waiting_user", "waiting_approval"}
    capabilities = task.runner_capabilities
    can_send_input = waiting
    can_interrupt, interrupt_reason = _control_decision(
        allowed=can_interrupt,
        unavailable_reason=interrupt_reason,
    )
    can_cancel, cancel_reason = _control_decision(
        allowed=can_cancel,
        unavailable_reason=cancel_reason,
    )
    can_pause, pause_reason = _control_decision(
        allowed=can_pause,
        unavailable_reason=pause_reason,
    )
    can_resume, resume_reason = _control_decision(
        allowed=can_resume,
        unavailable_reason=resume_reason,
    )
    can_restart, restart_reason = _control_decision(
        allowed=_can_restart_from_task(task),
        unavailable_reason=_restart_unavailable_reason(task),
    )
    can_inspect_output = bool(capabilities.get("output")) or bool(task.terminal_summary or task.progress_summary)
    actions = [
        _control_action("interrupt", "interrupt_task", can_interrupt, interrupt_reason, risk="medium"),
        _control_action("cancel", "cancel_task", can_cancel, cancel_reason, risk="high"),
        _control_action("pause", "pause_task", can_pause, pause_reason, risk="medium"),
        _control_action("resume", "resume_task", can_resume, resume_reason, risk="medium"),
        _control_action("restart", "restart_task", can_restart, restart_reason, risk="medium"),
        _control_action(
            "send_input",
            "send_task_input",
            can_send_input,
            "" if can_send_input else "task is not waiting for input",
            risk="medium",
        ),
        _control_action(
            "inspect_output",
            "task_output",
            can_inspect_output,
            "task has no output yet",
            risk="low",
            read_only=True,
        ),
    ]
    return {
        "can_interrupt": can_interrupt,
        "interrupt_tool": "interrupt_task" if can_interrupt else None,
        "interrupt_reason": interrupt_reason,
        "can_cancel": can_cancel,
        "cancel_tool": "cancel_task" if can_cancel else None,
        "cancel_reason": cancel_reason,
        "can_pause": can_pause,
        "pause_tool": "pause_task" if can_pause else None,
        "pause_reason": pause_reason,
        "can_resume": can_resume,
        "resume_tool": "resume_task" if can_resume else None,
        "resume_reason": resume_reason,
        "resume_policy": task.resume_policy or "not_resumable",
        "can_restart": can_restart,
        "restart_tool": "restart_task" if can_restart else None,
        "restart_reason": restart_reason,
        "can_send_input": can_send_input,
        "input_tool": "send_task_input" if can_send_input else None,
        "input_reason": "" if can_send_input else "task is not waiting for input",
        "can_inspect_output": can_inspect_output,
        "output_tool": "task_output",
        "actions": actions,
    }


def _control_action(
    action: str,
    tool: str,
    enabled: bool,
    reason: str,
    *,
    risk: str,
    read_only: bool = False,
) -> dict[str, Any]:
    """Return a stable UI/app action descriptor for one task control."""
    return {
        "action": action,
        "tool": tool if enabled else None,
        "enabled": bool(enabled),
        "reason": "" if enabled else reason,
        "risk": risk,
        "read_only": read_only,
    }


def _find_control_action(controls: dict[str, Any], action: str) -> dict[str, Any] | None:
    """Return one action descriptor from a controls payload."""
    for item in controls.get("actions") or []:
        if isinstance(item, dict) and str(item.get("action") or "").strip().lower() == action:
            return item
    return None


def _pause_unavailable_reason(task: TaskRun) -> str:
    """Return why durable pause is unavailable for a task."""
    if task.status in TASK_TERMINAL_STATUSES:
        return "task is terminal"
    if task.status == "paused":
        return "task is already paused"
    if task.status != "running":
        return "task is not running"
    capabilities = task.runner_capabilities
    if bool(capabilities.get("pause")) or bool(capabilities.get("checkpoint")):
        return "runner does not expose a registered durable pause adapter"
    return "runner does not support durable pause/checkpoint"


def _can_restart_from_task(task: TaskRun) -> bool:
    """Return whether explicit restart can start a new run for this task."""
    if not _has_restart_boundary(task):
        return False
    return task.status in TASK_TERMINAL_STATUSES or task.status in {"interrupted", "stale"}


def _has_restart_boundary(task: TaskRun) -> bool:
    """Return whether a task stores an explicit restart boundary."""
    return bool(task.runner_payload.get("restartable")) and isinstance(
        task.runner_payload.get("restart_boundary"),
        dict,
    )


def _can_resume_from_restart_boundary(task: TaskRun) -> bool:
    """Return whether resume may restart from an explicit durable boundary."""
    if task.status not in {"interrupted", "stale", "failed", "lost"}:
        return False
    return _can_restart_from_task(task)


def _restart_boundary_resume_policy(task: TaskRun) -> str:
    """Return the resume policy for tasks with an explicit restart boundary."""
    return "restart_from_boundary" if _has_restart_boundary(task) else "not_resumable"


def _terminal_resume_policy(task: TaskRun, terminal_status: str) -> str:
    """Return the terminal resume policy without implying completed work needs resume."""
    if terminal_status in {"failed", "lost"}:
        return _restart_boundary_resume_policy(task)
    return task.resume_policy


def _remediation_action(before: TaskRun, after: TaskRun) -> str:
    """Return a compact action label for one remediation result."""
    if before.status == after.status:
        return "synced"
    if after.status == "stale":
        return "marked_stale"
    if after.status == "lost":
        return "marked_lost"
    if after.status in TASK_TERMINAL_STATUSES:
        return f"marked_{after.status}"
    return "status_changed"


def _restart_unavailable_reason(task: TaskRun) -> str:
    """Return why explicit restart is unavailable for a task."""
    has_boundary = isinstance(task.runner_payload.get("restart_boundary"), dict)
    if not bool(task.runner_payload.get("restartable")) or not has_boundary:
        return "task has no explicit restartable boundary"
    if task.status == "running":
        return "task is still running; interrupt or cancel it before explicit restart"
    if task.status == "paused":
        return "task is paused; use resume_task instead of restart_task"
    if task.status in {"waiting_user", "waiting_approval"}:
        return "task is waiting for input; use send_task_input or interrupt it before restart"
    return f"task status {task.status!r} does not allow explicit restart"


def _resume_unavailable_reason(task: TaskRun) -> str:
    """Return why resume/rejoin is unavailable for a task."""
    if _can_resume_from_restart_boundary(task):
        return ""
    if task.status in TASK_TERMINAL_STATUSES:
        return "task is terminal"
    if task.status in {"waiting_user", "waiting_approval"}:
        return "task is waiting for input or approval"
    if task.status == "paused":
        return "checkpoint resume is not implemented"
    return "runner cannot be resumed from current state"


def _status_or_runner_reason(task: TaskRun, *, required_status: str, unsupported_reason: str) -> str:
    """Return a concise reason for status-sensitive unavailable controls."""
    if task.status in TASK_TERMINAL_STATUSES:
        return "task is terminal"
    if task.status != required_status:
        return f"task is not {required_status}"
    return unsupported_reason


def _control_decision(*, allowed: bool, unavailable_reason: str) -> tuple[bool, str]:
    """Return a stable boolean/reason pair for task action controls."""
    return bool(allowed), "" if allowed else unavailable_reason


def _unsupported_runner_stop_payload(controller: TaskController, task: TaskRun) -> dict[str, Any]:
    """Return a stable unsupported stop/cancel response for non-process runners."""
    if task.status in TASK_TERMINAL_STATUSES:
        return {"ok": True, "task": controller._task_payload(task), "message": "Task is already terminal."}
    runner = _task_runner_name(task) or "unknown"
    return {
        "ok": False,
        "task": controller._task_payload(task),
        "error": f"runner {runner!r} does not support direct task stop yet",
    }


def _unsupported_mcp_control_payload(controller: TaskController, task: TaskRun, *, action: str) -> dict[str, Any]:
    """Return a stable unsupported control response for MCP/job tasks."""
    if task.status in TASK_TERMINAL_STATUSES:
        return {"ok": True, "task": controller._task_payload(task), "message": "Task is already terminal."}
    return {
        "ok": False,
        "task": controller._task_payload(task),
        "action": "not_supported",
        "message": (
            f"MCP/job runner does not support direct {action} yet. "
            "Use the external MCP job status/cancel protocol when the specific server exposes one."
        ),
    }


def _mcp_job_protocol(task: TaskRun) -> Any | None:
    """Return the configured MCP job protocol for a task, if any."""
    return mcp_job_protocol_from_payload(task.runner_payload.get("job_protocol"))


def _poll_mcp_job_status(controller: TaskController, task: TaskRun) -> TaskRun | None:
    """Poll a configured MCP status tool and persist the latest snapshot."""
    protocol = _mcp_job_protocol(task)
    if protocol is None or not task.external_ref:
        return None
    payload = task.runner_payload
    result = call_mcp_job_status(
        server_name=str(payload.get("server", "") or "unknown"),
        protocol=protocol,
        job_id=task.external_ref,
        context_payload=_mcp_job_context(task),
    )
    if not result.ok:
        return _mark_mcp_job_status_unavailable(controller, task, result.error)
    snapshot = mcp_job_status_snapshot(result.payload, default_status=task.status or "running")
    if payload.get("status_snapshot") == snapshot:
        synced = controller._sync_external_snapshot_task(task, runner_name="mcp") or task
        checkpointed = _record_mcp_job_checkpoint_if_present(
            controller,
            synced,
            protocol,
            snapshot,
            status="paused" if synced.status == "paused" else None,
            summary=str(snapshot.get("summary") or snapshot.get("message") or "MCP job checkpoint."),
        )
        return checkpointed or synced
    runner_payload = dict(payload)
    runner_payload["status_snapshot"] = snapshot
    runner_payload["last_status_result"] = result.raw_result
    updated = controller.task_store.update_task(task.task_id, runner_payload=runner_payload)
    if updated is None:
        return None
    synced = controller._sync_external_snapshot_task(updated, runner_name="mcp") or updated
    checkpointed = _record_mcp_job_checkpoint_if_present(
        controller,
        synced,
        protocol,
        snapshot,
        status="paused" if synced.status == "paused" else None,
        summary=str(snapshot.get("summary") or snapshot.get("message") or "MCP job checkpoint."),
    )
    return checkpointed or synced


def _mark_mcp_job_status_unavailable(controller: TaskController, task: TaskRun, error: str) -> TaskRun | None:
    """Mark a MCP job as stale when openppx cannot observe its status."""
    summary = f"MCP job status unavailable: {error}"
    updates: dict[str, Any] = {
        "progress_summary": summary,
        "last_error": summary,
    }
    if task.status == "running":
        updates["status"] = "stale"
    updated = controller.task_store.update_task(task.task_id, **updates)
    if updated is not None:
        controller.event_store.append_event(
            updated.task_id,
            "task.stale" if updated.status == "stale" else "runner.status_poll_failed",
            message=summary,
            payload={"runner": "mcp", "external_ref": task.external_ref},
        )
    return updated


def _fetch_mcp_job_output(task: TaskRun) -> Any | None:
    """Fetch external MCP job output through the configured output tool."""
    protocol = _mcp_job_protocol(task)
    if protocol is None or not protocol.output_tool or not task.external_ref:
        return None
    payload = task.runner_payload
    return call_mcp_job_output(
        server_name=str(payload.get("server", "") or "unknown"),
        protocol=protocol,
        job_id=task.external_ref,
        context_payload=_mcp_job_context(task),
    )


def _record_mcp_job_checkpoint_if_present(
    controller: TaskController,
    task: TaskRun,
    protocol: Any,
    snapshot: dict[str, Any],
    *,
    status: str | None,
    summary: str,
) -> TaskRun | None:
    """Record a configured MCP job checkpoint once per distinct payload."""
    try:
        checkpoint = _mcp_job_checkpoint_from_snapshot(protocol, snapshot)
    except ValueError as exc:
        error = str(exc)
        payload = dict(task.runner_payload)
        payload["last_checkpoint_error"] = error
        updated = controller.task_store.update_task(task.task_id, runner_payload=payload) or task
        controller.event_store.append_event(
            task.task_id,
            "runner.checkpoint_rejected",
            message=error,
            payload={"runner": "mcp", "external_ref": task.external_ref, "error": error},
        )
        return updated
    if not checkpoint:
        return task
    fingerprint = _stable_hash(checkpoint)
    if task.runner_payload.get("checkpoint_fingerprint") == fingerprint and task.checkpoint_ref:
        return task
    if task.checkpoint_ref:
        existing = controller.checkpoint_store.get_checkpoint(task.checkpoint_ref)
        if existing is not None and _stable_hash(existing.payload) == fingerprint:
            payload = dict(task.runner_payload)
            payload["checkpoint_fingerprint"] = fingerprint
            payload["latest_checkpoint"] = checkpoint
            return controller.task_store.update_task(task.task_id, runner_payload=payload) or task
    recorded = controller.record_task_checkpoint(
        task.task_id,
        checkpoint_type="mcp_job_state",
        runner_name="mcp",
        checkpoint_payload=checkpoint,
        summary=summary,
        status=status,
        resume_policy="checkpoint" if status == "paused" and getattr(protocol, "resume_tool", "") else None,
    )
    if not recorded.get("ok"):
        return controller.task_store.get_task(task.task_id) or task
    updated = controller.task_store.get_task(task.task_id) or task
    payload = dict(updated.runner_payload)
    payload["checkpoint_fingerprint"] = fingerprint
    payload["latest_checkpoint"] = checkpoint
    return controller.task_store.update_task(updated.task_id, runner_payload=payload) or updated


def _mcp_job_checkpoint_from_snapshot(protocol: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Extract a checkpoint payload from a MCP job snapshot when configured."""
    checkpoint_path = str(getattr(protocol, "checkpoint_path", "") or "").strip()
    raw = extract_path(snapshot, checkpoint_path) if checkpoint_path else snapshot.get("checkpoint")
    if not isinstance(raw, dict) or not raw:
        return {}
    return normalize_mcp_job_checkpoint_payload(protocol=protocol, payload=raw)


def _pause_mcp_job_task(controller: TaskController, task: TaskRun) -> dict[str, Any]:
    """Pause one external MCP job using its configured pause tool."""
    if task.status in TASK_TERMINAL_STATUSES:
        return {"ok": True, "task": controller._task_payload(task), "message": "Task is already terminal."}
    if task.status == "paused":
        return {
            "ok": True,
            "task": controller._task_payload(task),
            "action": "already_paused",
            "message": "MCP job is already paused.",
        }
    if task.status != "running":
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_running",
            "message": f"Task is {task.status!r}, not running.",
        }
    protocol = _mcp_job_protocol(task)
    if protocol is None or not protocol.pause_tool or not task.external_ref:
        return _unsupported_mcp_control_payload(controller, task, action="pause")
    payload = task.runner_payload
    result = call_mcp_job_pause(
        server_name=str(payload.get("server", "") or "unknown"),
        protocol=protocol,
        job_id=task.external_ref,
        context_payload=_mcp_job_context(task),
    )
    if not result.ok:
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "pause_failed",
            "message": result.error,
        }
    snapshot = mcp_job_status_snapshot(result.payload, default_status="paused")
    runner_payload = dict(payload)
    runner_payload["status_snapshot"] = snapshot
    runner_payload["last_pause_result"] = result.raw_result
    updated = controller.task_store.update_task(
        task.task_id,
        runner_payload=runner_payload,
        resume_policy="checkpoint" if protocol.resume_tool else task.resume_policy,
    )
    if updated is None:
        return {"ok": False, "task": controller._task_payload(task), "message": "Failed to update task."}
    synced = controller._sync_external_snapshot_task(updated, runner_name="mcp") or updated
    checkpointed = _record_mcp_job_checkpoint_if_present(
        controller,
        synced,
        protocol,
        snapshot,
        status="paused" if synced.status == "paused" else None,
        summary=str(snapshot.get("summary") or snapshot.get("message") or "MCP job pause checkpoint."),
    )
    synced = checkpointed or synced
    return {
        "ok": True,
        "task": controller._task_payload(synced),
        "action": "paused" if synced.status == "paused" else "pause_requested",
        "message": "MCP job pause requested.",
    }


def _resume_mcp_job_task(controller: TaskController, task: TaskRun) -> dict[str, Any]:
    """Resume one paused external MCP job using its configured resume tool."""
    if task.status in TASK_TERMINAL_STATUSES:
        return {"ok": True, "task": controller._task_payload(task), "message": "Task is already terminal."}
    if task.status != "paused":
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_paused",
            "message": f"Task is {task.status!r}, not paused.",
        }
    protocol = _mcp_job_protocol(task)
    if protocol is None or not protocol.resume_tool or not task.external_ref:
        return _unsupported_mcp_control_payload(controller, task, action="resume")
    payload = task.runner_payload
    result = call_mcp_job_resume(
        server_name=str(payload.get("server", "") or "unknown"),
        protocol=protocol,
        job_id=task.external_ref,
        context_payload=_mcp_job_context(task),
    )
    if not result.ok:
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "resume_failed",
            "message": result.error,
        }
    snapshot = mcp_job_status_snapshot(result.payload, default_status="running")
    runner_payload = dict(payload)
    runner_payload["status_snapshot"] = snapshot
    runner_payload["last_resume_result"] = result.raw_result
    updated = controller.task_store.update_task(
        task.task_id,
        runner_payload=runner_payload,
        resume_policy="rejoin",
    )
    if updated is None:
        return {"ok": False, "task": controller._task_payload(task), "message": "Failed to update task."}
    synced = controller._sync_external_snapshot_task(updated, runner_name="mcp") or updated
    checkpointed = _record_mcp_job_checkpoint_if_present(
        controller,
        synced,
        protocol,
        snapshot,
        status=None,
        summary=str(snapshot.get("summary") or snapshot.get("message") or "MCP job resume checkpoint."),
    )
    synced = checkpointed or synced
    return {
        "ok": True,
        "task": controller._task_payload(synced),
        "action": "resumed" if synced.status == "running" else "resume_requested",
        "message": "MCP job resume requested.",
    }


def _cancel_mcp_job_task(controller: TaskController, task: TaskRun) -> dict[str, Any]:
    """Cancel one external MCP job using its configured cancel tool."""
    if task.status in TASK_TERMINAL_STATUSES:
        return {"ok": True, "task": controller._task_payload(task), "message": "Task is already terminal."}
    if task.status != "running":
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "not_running",
            "message": f"Task is {task.status!r}, not running.",
        }
    protocol = _mcp_job_protocol(task)
    if protocol is None or not protocol.cancel_tool or not task.external_ref:
        return _unsupported_mcp_control_payload(controller, task, action="cancel")
    payload = task.runner_payload
    result = call_mcp_job_cancel(
        server_name=str(payload.get("server", "") or "unknown"),
        protocol=protocol,
        job_id=task.external_ref,
        context_payload=_mcp_job_context(task),
    )
    if not result.ok:
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "action": "cancel_failed",
            "message": result.error,
        }
    snapshot = mcp_job_status_snapshot(result.payload, default_status="cancelled")
    runner_payload = dict(payload)
    runner_payload["status_snapshot"] = snapshot
    runner_payload["last_cancel_result"] = result.raw_result
    updated = controller.task_store.update_task(task.task_id, runner_payload=runner_payload)
    if updated is None:
        return {"ok": False, "task": controller._task_payload(task), "message": "Failed to update task."}
    synced = controller._sync_external_snapshot_task(updated, runner_name="mcp") or updated
    return {"ok": True, "task": controller._task_payload(synced), "message": "MCP job cancel requested."}


def _mcp_job_context(task: TaskRun) -> dict[str, Any]:
    """Return stored context for background MCP job control calls."""
    raw = task.runner_payload.get("job_context")
    if isinstance(raw, dict):
        return dict(raw)
    return {
        "user_id": task.user_id,
        "session_id": task.session_id,
        "invocation_id": task.invocation_id,
        "function_call_id": task.function_call_id,
    }


def _render_mcp_job_output(payload: Any) -> str:
    """Render a MCP job output payload as user-visible text."""
    if isinstance(payload, dict):
        for key in ("output", "result", "text", "message", "summary"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return str(payload)


def _stop_mcp_proxy_task(
    controller: TaskController,
    task: TaskRun,
    *,
    terminal_status: str,
    event_type: str,
) -> dict[str, Any]:
    """Stop a current-process MCP proxy task with best-effort cancellation."""
    if task.status in TASK_TERMINAL_STATUSES:
        return {"ok": True, "task": controller._task_payload(task), "message": "Task is already terminal."}
    if task.status != "running":
        return {
            "ok": False,
            "task": controller._task_payload(task),
            "message": f"Task is {task.status!r}, not running.",
        }
    if not cancel_mcp_proxy_task(task.task_id):
        summary = "MCP proxy background call is not attached to this process."
        updated = controller.task_store.update_task(
            task.task_id,
            status="stale",
            progress_summary=summary,
            last_error=summary,
        )
        if updated is not None:
            controller.event_store.append_event(
                updated.task_id,
                "task.stale",
                message=summary,
                payload={"runner": "mcp_proxy", "requested_stop": terminal_status},
            )
            return {
                "ok": False,
                "task": controller._task_payload(updated),
                "action": "detached",
                "message": summary,
            }
        return {"ok": False, "error": "failed to update task"}
    summary = "MCP proxy task interrupted." if terminal_status == "interrupted" else "MCP proxy task cancelled."
    updated = controller.task_store.update_task(
        task.task_id,
        status=terminal_status,
        terminal_summary=summary,
        progress_summary=summary,
        last_error=summary if terminal_status == "interrupted" else "",
        resume_policy="not_resumable",
    )
    if updated is None:
        return {"ok": False, "error": "failed to update task"}
    controller.event_store.append_event(
        updated.task_id,
        event_type,
        message=summary,
        payload={"runner": "mcp_proxy"},
    )
    return {"ok": True, "task": controller._task_payload(updated), "message": summary}


def _event_payload(event: Any) -> dict[str, Any]:
    """Project a task event into a stable API payload."""
    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "event_type": event.event_type,
        "message": event.message,
        "payload": event.payload,
        "created_at_ms": event.created_at_ms,
    }


def _input_payload(task_input: Any) -> dict[str, Any]:
    """Project a task input into a stable API payload."""
    return {
        "input_id": task_input.input_id,
        "task_id": task_input.task_id,
        "content": task_input.content,
        "payload": task_input.payload,
        "consumed_at_ms": task_input.consumed_at_ms,
        "created_at_ms": task_input.created_at_ms,
    }


def _artifact_payload(artifact: Any) -> dict[str, Any]:
    """Project a task artifact into a stable API payload."""
    return {
        "artifact_id": artifact.artifact_id,
        "task_id": artifact.task_id,
        "artifact_type": artifact.artifact_type,
        "label": artifact.label,
        "media_type": artifact.media_type,
        "path": artifact.path,
        "size_bytes": artifact.size_bytes,
        "metadata": artifact.metadata,
        "created_at_ms": artifact.created_at_ms,
    }


def _checkpoint_payload(checkpoint: Any) -> dict[str, Any]:
    """Project a task checkpoint into a stable API payload."""
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "task_id": checkpoint.task_id,
        "checkpoint_type": checkpoint.checkpoint_type,
        "runner_name": checkpoint.runner_name,
        "payload": checkpoint.payload,
        "summary": checkpoint.summary,
        "created_at_ms": checkpoint.created_at_ms,
    }


def _context_summary_payload(summary: ContextSummary) -> dict[str, Any]:
    """Project a staged context summary into a stable API payload."""
    return {
        "summary_id": summary.summary_id,
        "session_id": summary.session_id,
        "scope": summary.scope,
        "goal_id": summary.goal_id,
        "flow_id": summary.flow_id,
        "task_id": summary.task_id,
        "title": summary.title,
        "content": summary.content,
        "source_kind": summary.source_kind,
        "metadata": summary.metadata,
        "created_at_ms": summary.created_at_ms,
        "updated_at_ms": summary.updated_at_ms,
    }


def _artifact_file_cleanup(
    artifacts: list[Any],
    *,
    delete: bool,
    delete_requested: bool | None = None,
) -> dict[str, Any]:
    """Preview or delete task artifact files under the configured artifact root."""
    root = Path(load_artifact_config().root_dir).expanduser().resolve(strict=False)
    result: dict[str, Any] = {
        "delete_requested": bool(delete if delete_requested is None else delete_requested),
        "delete_enabled": bool(delete),
        "root_dir": str(root),
        "eligible_count": 0,
        "deleted_count": 0,
        "missing_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "items": [],
    }
    for artifact in artifacts:
        item = _artifact_file_cleanup_item(artifact, root=root, delete=delete)
        result["items"].append(item)
        status = item["status"]
        if status == "eligible":
            result["eligible_count"] += 1
        elif status == "deleted":
            result["eligible_count"] += 1
            result["deleted_count"] += 1
        elif status == "missing":
            result["missing_count"] += 1
        elif status == "error":
            result["error_count"] += 1
        else:
            result["skipped_count"] += 1
    return result


def _artifact_file_cleanup_item(artifact: Any, *, root: Path, delete: bool) -> dict[str, Any]:
    """Return one artifact file cleanup result."""
    raw_path = Path(str(getattr(artifact, "path", "") or "")).expanduser()
    resolved = raw_path.resolve(strict=False)
    item = {
        "artifact_id": getattr(artifact, "artifact_id", None),
        "task_id": getattr(artifact, "task_id", ""),
        "path": str(raw_path),
        "status": "eligible",
        "reason": "",
    }
    try:
        resolved.relative_to(root)
    except ValueError:
        item["status"] = "skipped"
        item["reason"] = "outside_artifact_root"
        return item
    if raw_path.is_symlink():
        item["status"] = "skipped"
        item["reason"] = "symlink"
        return item
    if not raw_path.exists():
        item["status"] = "missing"
        item["reason"] = "file_missing"
        return item
    if not raw_path.is_file():
        item["status"] = "skipped"
        item["reason"] = "not_a_file"
        return item
    if not delete:
        return item
    try:
        raw_path.unlink()
        _remove_empty_artifact_dirs(resolved.parent, root=root)
    except Exception as exc:
        item["status"] = "error"
        item["reason"] = str(exc)
        return item
    item["status"] = "deleted"
    return item


def _remove_empty_artifact_dirs(start: Path, *, root: Path) -> None:
    """Remove empty artifact subdirectories up to but not including root."""
    current = start
    while current != root:
        try:
            current.relative_to(root)
        except ValueError:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _delivery_payload(delivery: Any) -> dict[str, Any]:
    """Project a task delivery record into a stable API payload."""
    return {
        "delivery_key": delivery.delivery_key,
        "task_id": delivery.task_id,
        "delivery_type": delivery.delivery_type,
        "payload": delivery.payload,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "last_error": delivery.last_error,
        "next_attempt_at_ms": delivery.next_attempt_at_ms,
        "delivered_at_ms": delivery.delivered_at_ms,
        "ack_status": delivery.ack_status,
        "ack_payload": delivery.ack_payload,
        "provider_message_id": delivery.provider_message_id,
        "acked_at_ms": delivery.acked_at_ms,
        "created_at_ms": delivery.created_at_ms,
    }


def _delivery_summary_payload(deliveries: list[Any]) -> dict[str, Any]:
    """Return a compact delivery summary for one task."""
    if not deliveries:
        return _empty_delivery_summary()
    summary = _empty_delivery_summary()
    for delivery in deliveries:
        summary["count"] += 1
        status_key = f"{delivery.status}_count"
        if status_key in summary:
            summary[status_key] += 1
        summary["latest"] = {
            "delivery_key": delivery.delivery_key,
            "delivery_type": delivery.delivery_type,
            "status": delivery.status,
            "attempts": delivery.attempts,
            "last_error": delivery.last_error,
            "next_attempt_at_ms": delivery.next_attempt_at_ms,
            "delivered_at_ms": delivery.delivered_at_ms,
            "ack_status": delivery.ack_status,
            "ack_payload": delivery.ack_payload,
            "provider_message_id": delivery.provider_message_id,
            "acked_at_ms": delivery.acked_at_ms,
            "created_at_ms": delivery.created_at_ms,
        }
    return summary


def _empty_delivery_summary() -> dict[str, Any]:
    """Return the no-delivery summary shape."""
    return {
        "count": 0,
        "pending_count": 0,
        "failed_count": 0,
        "delivered_count": 0,
        "latest": None,
    }


def _output_artifact_threshold_chars() -> int:
    """Return the output length threshold for task artifact persistence."""
    raw = os.getenv("OPENPPX_TASK_OUTPUT_ARTIFACT_THRESHOLD_CHARS", "").strip()
    if not raw:
        return DEFAULT_OUTPUT_ARTIFACT_THRESHOLD_CHARS
    try:
        return max(0, int(raw))
    except Exception:
        return DEFAULT_OUTPUT_ARTIFACT_THRESHOLD_CHARS


def _write_task_output_artifact(
    *,
    task: TaskRun,
    output: str,
    artifact_store: TaskArtifactStore,
    metadata: dict[str, Any],
) -> Any | None:
    """Persist oversized task output to the configured artifact directory."""
    config = load_artifact_config()
    if not config.enabled:
        return None
    try:
        root = Path(config.root_dir).expanduser()
        target_dir = root / "task-runs" / task.task_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"process-output-{uuid.uuid4().hex[:12]}.txt"
        target_path.write_text(output, encoding="utf-8")
        return artifact_store.record_artifact(
            task_id=task.task_id,
            artifact_type="process_output",
            label="Process output",
            media_type="text/plain; charset=utf-8",
            path=str(target_path),
            size_bytes=target_path.stat().st_size,
            metadata=metadata,
        )
    except Exception:
        return None


def _write_task_artifact_context_summary(
    *,
    task: TaskRun,
    output: str,
    artifact_payload: dict[str, Any],
    summary: str,
    context_store: LongTaskContextStore,
) -> dict[str, Any] | None:
    """Write a deterministic staged context summary for one oversized artifact."""
    if not task.session_id:
        return None
    try:
        stored = context_store.upsert_summary(
            session_id=task.session_id,
            task_id=task.task_id,
            scope="task",
            title=f"Task output artifact: {task.title}",
            content=summary,
            source_kind="task_artifact",
            metadata={
                "artifact_id": artifact_payload.get("artifact_id"),
                "artifact_type": artifact_payload.get("artifact_type"),
                "media_type": artifact_payload.get("media_type"),
                "size_bytes": artifact_payload.get("size_bytes"),
                "source_chars": len(output),
            },
            max_chars=MAX_TERMINAL_SUMMARY_CHARS,
        )
    except Exception:
        return None
    return _context_summary_payload(stored)


def _compact_output_summary(output: str, *, artifact_payload: dict[str, Any] | None) -> str:
    """Return a compact terminal summary for oversized output."""
    output_bytes = len(output.encode("utf-8", errors="replace"))
    if artifact_payload is None:
        header = (
            f"Output exceeded inline summary budget ({output_bytes} bytes), "
            "but artifact persistence was unavailable."
        )
    else:
        header = (
            f"Output saved as artifact {artifact_payload['artifact_id']} "
            f"({artifact_payload['size_bytes']} bytes): {artifact_payload['path']}"
        )
    tail_budget = max(500, MAX_TERMINAL_SUMMARY_CHARS - len(header) - 16)
    tail = output[-tail_budget:].strip()
    return f"{header}\n\nTail:\n{tail or '(no output)'}"
