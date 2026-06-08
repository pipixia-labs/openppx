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
from .artifact_service import load_artifact_config
from .process_sessions import get_process_session_manager
from .mcp_proxy import cancel_mcp_proxy_task
from .mcp_proxy import is_mcp_proxy_task_active
from .mcp_job_protocol import call_mcp_job_cancel
from .mcp_job_protocol import call_mcp_job_output
from .mcp_job_protocol import call_mcp_job_status
from .mcp_job_protocol import mcp_job_protocol_from_payload
from .mcp_job_protocol import mcp_job_status_snapshot
from .task_store import (
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
        return (cls._http_recipe_runner_spec(), cls._python_recipe_runner_spec())

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
            can_resume=task.status == "running" and bool(task.runner_capabilities.get("rejoin")),
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
            can_resume=running and bool(capabilities.get("rejoin")),
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


class McpJobTaskRunnerAdapter(TaskRunnerAdapter):
    """Task runner adapter for MCP tools that expose external job status."""

    name = "mcp"

    def matches(self, task: TaskRun) -> bool:
        """Return whether the task is an MCP/job task."""
        return _task_runner_name(task) == "mcp" or task.kind == "mcp"

    def controls(self, task: TaskRun) -> dict[str, Any]:
        """Return controls for MCP/job tasks without inventing cancel support."""
        running = task.status == "running"
        terminal = task.status in TASK_TERMINAL_STATUSES
        protocol = _mcp_job_protocol(task)
        can_cancel = running and protocol is not None and bool(protocol.cancel_tool)
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
            can_pause=False,
            pause_reason=_pause_unavailable_reason(task),
            can_resume=running and bool(task.runner_capabilities.get("rejoin")),
            resume_reason=_resume_unavailable_reason(task),
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
        runner_registry: TaskRunnerRegistry | None = None,
    ) -> None:
        self.task_store = task_store or TaskStore()
        self.event_store = event_store or TaskEventStore(db_path=self.task_store.db_path)
        self.input_store = input_store or TaskInputStore(db_path=self.task_store.db_path)
        self.artifact_store = artifact_store or TaskArtifactStore(db_path=self.task_store.db_path)
        self.checkpoint_store = checkpoint_store or TaskCheckpointStore(db_path=self.task_store.db_path)
        self.delivery_store = delivery_store or TaskDeliveryStore(db_path=self.task_store.db_path)
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
        checkpoint = self.checkpoint_store.record_checkpoint(
            task_id=task.task_id,
            checkpoint_type=checkpoint_type,
            runner_name=runner_name,
            payload=checkpoint_payload,
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

    def _task_payload(self, task: TaskRun, *, delivery_summary: dict[str, Any] | None = None) -> dict[str, Any]:
        """Project a task using this controller's runner registry."""
        return _task_payload(
            task,
            controls=self.runner_registry.controls(task),
            delivery_summary=delivery_summary,
        )

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
            resume_policy="not_resumable",
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
            summary, artifact_payload = self._summarize_terminal_output(
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
            )
            if updated is not None:
                if artifact_payload is not None:
                    self.event_store.append_event(
                        task.task_id,
                        "task.artifact_saved",
                        message=f"Saved task output artifact {artifact_payload['artifact_id']}.",
                        payload=artifact_payload,
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
    ) -> tuple[str, dict[str, Any] | None]:
        """Return a compact terminal summary and optional artifact payload."""
        threshold = _output_artifact_threshold_chars()
        if len(output) <= threshold:
            return output, None
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
            return _compact_output_summary(output, artifact_payload=None), None
        artifact_payload = _artifact_payload(artifact)
        return _compact_output_summary(output, artifact_payload=artifact_payload), artifact_payload

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
            resume_policy="not_resumable" if terminal_status == "interrupted" else task.resume_policy,
            cancel_policy="kill_process" if terminal_status == "cancelled" else task.cancel_policy,
        )
        if updated is None:
            return {"ok": False, "error": "failed to update task"}
        self.event_store.append_event(updated.task_id, event_type, message=summary)
        return {"ok": True, "task": self._task_payload(updated), "message": summary}


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
        "can_inspect_output": bool(capabilities.get("output")) or bool(task.terminal_summary or task.progress_summary),
        "output_tool": "task_output",
    }


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
    if not bool(task.runner_payload.get("restartable")):
        return False
    if not isinstance(task.runner_payload.get("restart_boundary"), dict):
        return False
    return task.status in TASK_TERMINAL_STATUSES or task.status in {"interrupted", "stale"}


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
        return task
    runner_payload = dict(payload)
    runner_payload["status_snapshot"] = snapshot
    runner_payload["last_status_result"] = result.raw_result
    return controller.task_store.update_task(task.task_id, runner_payload=runner_payload)


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
