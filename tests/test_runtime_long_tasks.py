"""Tests for the first long-task execution slice."""

from __future__ import annotations

import json
import os
import tempfile
import sys
import time
import unittest
from pathlib import Path

from openppx.runtime.long_task_context import render_long_task_context
from openppx.runtime.context_engine import LongTaskContextStore
from openppx.runtime.task_store import TaskCheckpointStore, TaskStore
from openppx.runtime.task_execution import (
    ExecutionRecipe,
    ProcessExecutionSupervisor,
    SkillApiRuntime,
    TaskController,
    TaskRunnerAdapter,
    TaskRunnerRegistry,
)
from openppx.runtime.tool_context import route_context
from openppx.tooling.registry import (
    cancel_task,
    interrupt_task,
    invoke_skill_api,
    list_tasks,
    pause_task,
    restart_task,
    resume_task,
    send_task_input,
    show_task,
    task_output,
)


class LongTaskRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_invoke_skill_api_returns_inline_for_fast_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_skill(tmp, "quick", "import os\nprint(os.environ.get('OPENPPX_SKILL_ARGS_JSON', '{}'))\n")

            payload = json.loads(invoke_skill_api("demo", "quick", args={"value": 7}, inline_budget_ms=2000))
            tasks = json.loads(list_tasks())

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mode"], "inline")
            self.assertEqual(payload["status"], "completed")
            self.assertIn('"value": 7', payload["output"])
            self.assertEqual(tasks["items"], [])

    def test_skill_api_runtime_resolves_http_recipe_without_length_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_http_skill(
                tmp,
                "hello",
                {
                    "method": "GET",
                    "url": "https://example.test/hello",
                    "query": {"name": "{name}"},
                },
            )

            recipe = SkillApiRuntime().resolve(
                skill_name="demo",
                api_name="hello",
                args={"name": "Ada"},
                scope_key="scope-1",
            )
            recipe_payload = json.loads(recipe.env["OPENPPX_HTTP_API_RECIPE_JSON"])

            self.assertEqual(recipe.task_kind, "api_call")
            self.assertEqual(recipe.scope_key, "scope-1")
            self.assertEqual(recipe.runner_payload["logical_runner"], "http_api")
            self.assertEqual(recipe.runner_payload["recipe_runner"], "http")
            self.assertEqual(recipe.runner_payload["api_recipe"], "apis/hello.json")
            self.assertEqual(recipe.cwd.name, "demo")
            self.assertEqual(recipe_payload["url"], "https://example.test/hello")
            self.assertEqual(json.loads(recipe.env["OPENPPX_SKILL_ARGS_JSON"]), {"name": "Ada"})
            self.assertTrue(recipe.argv[-1].endswith("http_api_runner.py"))

    def test_skill_api_runtime_resolves_python_recipe_without_length_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_python_api_skill(
                tmp,
                "add",
                {"module": "demo_sdk", "function": "add"},
                "def add(a, b):\n    return {'sum': a + b}\n",
            )

            recipe = SkillApiRuntime().resolve(
                skill_name="demo",
                api_name="add",
                args={"a": 2, "b": 3},
                scope_key="scope-1",
            )
            recipe_payload = json.loads(recipe.env["OPENPPX_PYTHON_API_RECIPE_JSON"])

            self.assertEqual(recipe.task_kind, "api_call")
            self.assertEqual(recipe.scope_key, "scope-1")
            self.assertEqual(recipe.runner_payload["logical_runner"], "python_api")
            self.assertEqual(recipe.runner_payload["recipe_runner"], "python")
            self.assertEqual(recipe.runner_payload["api_recipe"], "apis/add.python.json")
            self.assertEqual(recipe.cwd.name, "demo")
            self.assertEqual(recipe_payload["module"], "demo_sdk")
            self.assertEqual(json.loads(recipe.env["OPENPPX_SKILL_ARGS_JSON"]), {"a": 2, "b": 3})
            self.assertTrue(recipe.argv[-1].endswith("python_api_runner.py"))
            self.assertTrue(SkillApiRuntime._is_python_recipe_name("add.python.json"))

    def test_invoke_skill_api_returns_inline_for_fast_python_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_python_api_skill(
                tmp,
                "add",
                {"module": "demo_sdk", "function": "add"},
                "def add(a, b):\n    return {'sum': a + b}\n",
            )

            payload = json.loads(invoke_skill_api("demo", "add", args={"a": 2, "b": 3}, inline_budget_ms=2000))
            tasks = json.loads(list_tasks())

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mode"], "inline")
            self.assertEqual(payload["status"], "completed")
            self.assertIn('"sum": 5', payload["output"])
            self.assertEqual(tasks["items"], [])

    def test_invoke_skill_api_materializes_long_task_for_slow_python_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_python_api_skill(
                tmp,
                "slow_add",
                {"module": "demo_sdk", "function": "slow_add"},
                (
                    "import time\n"
                    "def slow_add(a, b):\n"
                    "    print('sdk started', flush=True)\n"
                    "    time.sleep(10)\n"
                    "    return {'sum': a + b}\n"
                ),
            )

            payload = json.loads(
                invoke_skill_api("demo", "slow_add", args={"a": 2, "b": 3}, inline_budget_ms=20)
            )

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mode"], "task")
            task = TaskStore().get_task(payload["task_id"])
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.kind, "api_call")
            self.assertEqual(task.runner_payload["logical_runner"], "python_api")
            self.assertEqual(task.runner_payload["api_recipe"], "apis/slow_add.python.json")

            output = {"ok": False, "output": "", "tail": ""}
            for _ in range(20):
                output = json.loads(task_output(task.task_id))
                if "sdk started" in output.get("output", "") + output.get("tail", ""):
                    break
                time.sleep(0.05)
            self.assertTrue(output["ok"])
            self.assertIn("sdk started", output["output"] + output["tail"])
            interrupt_task(task.task_id)

    def test_process_supervisor_materializes_api_call_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["OPENPPX_TASK_DB_PATH"] = str(root / "tasks.db")
            script = root / "fake_http_api.py"
            script.write_text(
                "import json, time\n"
                "print('http request started', flush=True)\n"
                "time.sleep(0.2)\n"
                "print(json.dumps({'ok': True, 'body': 'done after 0.2'}), flush=True)\n",
                encoding="utf-8",
            )

            class FakeApiRuntime:
                def resolve(
                    self,
                    *,
                    skill_name: str,
                    api_name: str,
                    args: object = None,
                    scope_key: str | None = None,
                ) -> ExecutionRecipe:
                    env = os.environ.copy()
                    return ExecutionRecipe(
                        title=f"{skill_name}:{api_name}",
                        command=f"{sys.executable} {script}",
                        argv=[sys.executable, str(script)],
                        cwd=root,
                        env=env,
                        scope_key=scope_key,
                        task_kind="api_call",
                        runner_payload={"logical_runner": "http_api", "api_recipe": "apis/slow.json"},
                    )

            supervisor = ProcessExecutionSupervisor(skill_runtime=FakeApiRuntime())  # type: ignore[arg-type]
            result = supervisor.invoke_skill_api(
                skill_name="demo",
                api_name="slow",
                args={},
                inline_budget_ms=0,
            )

            self.assertEqual(result.mode, "task")
            self.assertIsNotNone(result.task)
            assert result.task is not None
            task_id = result.task.task_id
            task = TaskStore().get_task(task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.kind, "api_call")
            self.assertEqual(task.runner_payload["logical_runner"], "http_api")
            self.assertEqual(task.runner_payload["api_recipe"], "apis/slow.json")

            shown = {"task": {"status": "running"}, "events": []}
            for _ in range(30):
                shown = json.loads(show_task(task_id))
                if shown["task"]["status"] == "completed":
                    break
                time.sleep(0.05)

            output = json.loads(task_output(task_id))
            self.assertEqual(shown["task"]["status"], "completed")
            self.assertIn("done after 0.2", output["output"])
            self.assertIn("task.completed", [event["event_type"] for event in shown["events"]])

    def test_invoke_skill_api_materializes_long_task_and_interrupts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_skill(
                tmp,
                "slow",
                "import time\nprint('started', flush=True)\ntime.sleep(10)\nprint('finished', flush=True)\n",
            )

            payload = json.loads(invoke_skill_api("demo", "slow", inline_budget_ms=20))

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mode"], "task")
            task_id = payload["task_id"]

            shown = json.loads(show_task(task_id))
            self.assertTrue(shown["ok"])
            self.assertEqual(shown["task"]["status"], "running")
            controls = shown["task"]["controls"]
            self.assertTrue(controls["can_interrupt"])
            self.assertEqual(controls["interrupt_tool"], "interrupt_task")
            self.assertTrue(controls["can_cancel"])
            self.assertTrue(controls["can_resume"])
            self.assertFalse(controls["can_pause"])
            self.assertIn("durable pause", controls["pause_reason"])

            output = {"ok": False, "output": "", "tail": ""}
            for _ in range(20):
                output = json.loads(task_output(task_id))
                if "started" in output.get("output", "") + output.get("tail", ""):
                    break
                time.sleep(0.05)
            self.assertTrue(output["ok"])
            self.assertIn("started", output["output"] + output["tail"])

            stopped = json.loads(interrupt_task(task_id))
            self.assertTrue(stopped["ok"])
            self.assertEqual(stopped["task"]["status"], "interrupted")

            resumed = json.loads(resume_task(task_id))
            self.assertFalse(resumed["ok"])
            self.assertEqual(resumed["action"], "not_resumable")
            self.assertEqual(resumed["resume_policy"], "not_resumable")

    def test_resume_task_rejoins_running_process_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_skill(tmp, "slow", "import time\nprint('started', flush=True)\ntime.sleep(10)\n")

            payload = json.loads(invoke_skill_api("demo", "slow", inline_budget_ms=20))
            resumed = json.loads(resume_task(payload["task_id"]))

            self.assertTrue(resumed["ok"])
            self.assertEqual(resumed["action"], "rejoined")
            self.assertEqual(resumed["task"]["status"], "running")
            self.assertTrue(resumed["task"]["runner_capabilities"]["rejoin"])
            interrupt_task(payload["task_id"])

    def test_restart_task_starts_new_run_from_explicit_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_skill(
                tmp,
                "short",
                (
                    "import os, time\n"
                    "print(os.environ.get('OPENPPX_SKILL_ARGS_JSON', '{}'), flush=True)\n"
                    "time.sleep(0.2)\n"
                    "print('done', flush=True)\n"
                ),
            )

            payload = json.loads(
                invoke_skill_api(
                    "demo",
                    "short",
                    args={"value": 11},
                    inline_budget_ms=0,
                    restartable=True,
                )
            )
            task_id = payload["task_id"]
            shown = {"task": {"status": "running"}, "events": []}
            for _ in range(30):
                shown = json.loads(show_task(task_id))
                if shown["task"]["status"] == "completed":
                    break
                time.sleep(0.05)

            self.assertEqual(shown["task"]["status"], "completed")
            self.assertTrue(shown["task"]["controls"]["can_restart"])
            self.assertEqual(shown["task"]["controls"]["restart_tool"], "restart_task")

            restarted = json.loads(restart_task(task_id, inline_budget_ms=0))
            result = restarted["result"]
            new_task_id = result["task_id"]
            new_task = TaskStore().get_task(new_task_id)
            old_events = json.loads(show_task(task_id))["events"]

            self.assertTrue(restarted["ok"])
            self.assertEqual(restarted["action"], "restarted")
            self.assertEqual(result["restarted_from_task_id"], task_id)
            self.assertNotEqual(new_task_id, task_id)
            self.assertFalse(result.get("replayed", False))
            self.assertIsNotNone(new_task)
            assert new_task is not None
            self.assertTrue(new_task.runner_payload["restartable"])
            self.assertEqual(new_task.runner_payload["restart_boundary"]["args"], {"value": 11})
            self.assertIn("task.restarted", [event["event_type"] for event in old_events])
            interrupt_task(new_task_id)

    def test_restart_task_rejects_task_without_explicit_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            os.environ["OPENPPX_TASK_DB_PATH"] = str(db_path)
            task = TaskStore(db_path=db_path).create_task(
                kind="skill_api",
                status="failed",
                title="demo:failed",
                runner_payload={"runner": "process"},
            )

            restarted = json.loads(restart_task(task.task_id))
            shown = json.loads(show_task(task.task_id))

            self.assertFalse(restarted["ok"])
            self.assertEqual(restarted["action"], "not_restartable")
            self.assertFalse(shown["task"]["controls"]["can_restart"])
            self.assertIn("restartable boundary", shown["task"]["controls"]["restart_reason"])

    def test_restart_task_rejects_running_task_even_with_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_skill(tmp, "slow", "import time\nprint('started', flush=True)\ntime.sleep(10)\n")

            payload = json.loads(invoke_skill_api("demo", "slow", inline_budget_ms=20, restartable=True))
            restarted = json.loads(restart_task(payload["task_id"]))
            shown = json.loads(show_task(payload["task_id"]))

            self.assertFalse(restarted["ok"])
            self.assertEqual(restarted["action"], "still_running")
            self.assertFalse(shown["task"]["controls"]["can_restart"])
            self.assertIn("still running", shown["task"]["controls"]["restart_reason"])
            interrupt_task(payload["task_id"])

    def test_pause_task_rejects_runner_without_pause_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_skill(tmp, "slow", "import time\nprint('started', flush=True)\ntime.sleep(10)\n")

            payload = json.loads(invoke_skill_api("demo", "slow", inline_budget_ms=20))
            paused = json.loads(pause_task(payload["task_id"]))

            self.assertFalse(paused["ok"])
            self.assertEqual(paused["action"], "not_supported")
            self.assertEqual(paused["task"]["status"], "running")
            self.assertIn("interrupt_task", paused["message"])
            interrupt_task(payload["task_id"])

    def test_pause_task_does_not_fake_runner_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            os.environ["OPENPPX_TASK_DB_PATH"] = str(db_path)
            task = TaskStore(db_path=db_path).create_task(
                kind="browser",
                status="running",
                title="Browser workflow",
                runner_payload={"runner": "browser"},
                runner_capabilities={"pause": True, "checkpoint": True},
                resume_policy="checkpoint",
                stop_policy="pause_task",
                checkpoint_ref="checkpoint-1",
            )

            paused = json.loads(pause_task(task.task_id))
            current = TaskStore(db_path=db_path).get_task(task.task_id)

            self.assertFalse(paused["ok"])
            self.assertEqual(paused["action"], "adapter_missing")
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.status, "running")

    def test_checkpoint_runner_adapter_can_pause_and_resume_from_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            task = store.create_task(
                kind="browser",
                status="running",
                title="Checkpoint-capable browser workflow",
                runner_payload={"runner": "checkpoint_fake", "target_id": "tab-1"},
                runner_capabilities={"pause": True, "checkpoint": True, "resume": True},
                resume_policy="checkpoint",
                stop_policy="pause_task",
                progress_summary="Running browser workflow.",
            )

            class CheckpointRunnerAdapter(TaskRunnerAdapter):
                def matches(self, task_to_match: object) -> bool:
                    return getattr(task_to_match, "runner_payload", {}).get("runner") == "checkpoint_fake"

                def controls(self, task_to_control: object) -> dict[str, object]:
                    controls = super().controls(task_to_control)  # type: ignore[arg-type]
                    status = getattr(task_to_control, "status", "")
                    checkpoint_ref = getattr(task_to_control, "checkpoint_ref", "")
                    if status == "running":
                        controls["can_pause"] = True
                        controls["pause_tool"] = "pause_task"
                        controls["pause_reason"] = ""
                    if status == "paused" and checkpoint_ref:
                        controls["can_resume"] = True
                        controls["resume_tool"] = "resume_task"
                        controls["resume_reason"] = ""
                    return controls

                def pause_task(self, controller: TaskController, task_to_pause: object) -> dict[str, object]:
                    payload = controller.record_task_checkpoint(
                        task_to_pause.task_id,  # type: ignore[attr-defined]
                        checkpoint_type="runner_state",
                        runner_name="checkpoint_fake",
                        checkpoint_payload={"target_id": "tab-1", "next_step": 4},
                        summary="Paused at browser step 3.",
                        status="paused",
                        resume_policy="checkpoint",
                    )
                    payload["action"] = "paused"
                    return payload

                def resume_task(self, controller: TaskController, task_to_resume: object) -> dict[str, object]:
                    checkpoint = controller.checkpoint_store.get_checkpoint(
                        task_to_resume.checkpoint_ref  # type: ignore[attr-defined]
                    )
                    assert checkpoint is not None
                    updated = controller.task_store.update_task(
                        task_to_resume.task_id,  # type: ignore[attr-defined]
                        status="running",
                        progress_summary=f"Resumed from checkpoint {checkpoint.checkpoint_id}.",
                        resume_policy="checkpoint",
                    )
                    assert updated is not None
                    controller.event_store.append_event(
                        updated.task_id,
                        "task.resumed",
                        message="Resumed from checkpoint.",
                        payload={"checkpoint": checkpoint.payload},
                    )
                    return {
                        "ok": True,
                        "action": "resumed",
                        "task": controller._task_payload(updated),
                        "checkpoint": checkpoint.payload,
                    }

            controller = TaskController(
                task_store=store,
                runner_registry=TaskRunnerRegistry([CheckpointRunnerAdapter()]),
            )

            initial = controller.show_task(task.task_id)
            paused = controller.pause_task(task.task_id)
            paused_task = store.get_task(task.task_id)
            shown = controller.show_task(task.task_id)
            resumed = controller.resume_task(task.task_id)
            events = [event["event_type"] for event in controller.show_task(task.task_id)["events"]]

            self.assertTrue(initial["task"]["controls"]["can_pause"])
            self.assertTrue(paused["ok"])
            self.assertEqual(paused["action"], "paused")
            self.assertIsNotNone(paused_task)
            assert paused_task is not None
            self.assertEqual(paused_task.status, "paused")
            self.assertEqual(paused_task.checkpoint_ref, paused["checkpoint"]["checkpoint_id"])
            self.assertEqual(shown["checkpoints"][0]["payload"], {"target_id": "tab-1", "next_step": 4})
            self.assertTrue(shown["task"]["controls"]["can_resume"])
            self.assertTrue(resumed["ok"])
            self.assertEqual(resumed["action"], "resumed")
            self.assertEqual(resumed["task"]["status"], "running")
            self.assertEqual(resumed["checkpoint"], {"target_id": "tab-1", "next_step": 4})
            self.assertIn("task.checkpoint_written", events)
            self.assertIn("task.resumed", events)

    def test_checkpoint_resume_rejects_missing_runner_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            task = store.create_task(
                kind="browser",
                status="paused",
                title="Paused browser workflow",
                runner_payload={"runner": "browser"},
                runner_capabilities={"checkpoint": True},
                resume_policy="checkpoint",
                checkpoint_ref="ckpt-1",
                progress_summary="Paused with checkpoint.",
            )

            resumed = TaskController(task_store=store).resume_task(task.task_id)

            self.assertFalse(resumed["ok"])
            self.assertEqual(resumed["action"], "adapter_missing")
            self.assertEqual(resumed["resume_policy"], "checkpoint")

    def test_cancel_task_marks_user_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_skill(tmp, "slow", "import time\ntime.sleep(10)\n")

            payload = json.loads(invoke_skill_api("demo", "slow", inline_budget_ms=20))
            cancelled = json.loads(cancel_task(payload["task_id"]))

            self.assertTrue(cancelled["ok"])
            self.assertEqual(cancelled["task"]["status"], "cancelled")

    def test_completed_process_large_output_is_saved_as_task_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_ARTIFACTS_DIR"] = str(Path(tmp) / "artifacts")
            os.environ["OPENPPX_TASK_OUTPUT_ARTIFACT_THRESHOLD_CHARS"] = "120"
            large_line = "x" * 6000
            self._prepare_skill(
                tmp,
                "large",
                (
                    "import time\n"
                    "print('begin', flush=True)\n"
                    "time.sleep(0.05)\n"
                    f"print('{large_line}', flush=True)\n"
                ),
            )

            payload = json.loads(invoke_skill_api("demo", "large", inline_budget_ms=0))
            task_id = payload["task_id"]
            shown = {"task": {"status": "running"}, "artifacts": [], "events": []}
            for _ in range(20):
                shown = json.loads(show_task(task_id))
                if shown["task"]["status"] == "completed":
                    break
                time.sleep(0.05)
            output = json.loads(task_output(task_id))

            self.assertEqual(shown["task"]["status"], "completed")
            self.assertEqual(len(shown["artifacts"]), 1)
            artifact = shown["artifacts"][0]
            artifact_path = Path(artifact["path"])
            self.assertTrue(artifact_path.exists())
            self.assertIn(large_line, artifact_path.read_text(encoding="utf-8"))
            self.assertIn("Output saved as artifact", shown["task"]["terminal_summary"])
            self.assertTrue(output["artifact_backed"])
            self.assertEqual(output["artifacts"][0]["artifact_id"], artifact["artifact_id"])
            self.assertLess(len(output["output"]), len(large_line))
            self.assertIn("task.artifact_saved", [event["event_type"] for event in shown["events"]])

    def test_background_task_records_current_delivery_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_skill(tmp, "slow", "import time\ntime.sleep(10)\n")

            with route_context("feishu", "chat-ops"):
                payload = json.loads(invoke_skill_api("demo", "slow", inline_budget_ms=20))

            task = TaskStore().get_task(payload["task_id"])
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.runner_payload["delivery"], {"channel": "feishu", "chat_id": "chat-ops"})
            interrupt_task(payload["task_id"])

    def test_non_process_task_is_not_stopped_as_process_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            task = store.create_task(
                kind="subagent",
                status="running",
                title="Sub-agent task",
                external_ref="subagent:demo",
                runner_payload={"runner": "subagent"},
                runner_capabilities={"interrupt": False},
                resume_policy="not_resumable",
                stop_policy="not_supported",
                cancel_policy="not_supported",
            )

            interrupted = TaskController(task_store=store).interrupt_task(task.task_id)

            self.assertFalse(interrupted["ok"])
            self.assertIn("does not support", interrupted["error"])
            self.assertEqual(store.get_task(task.task_id).status, "running")  # type: ignore[union-attr]
            shown = TaskController(task_store=store).show_task(task.task_id)
            controls = shown["task"]["controls"]
            self.assertFalse(controls["can_interrupt"])
            self.assertFalse(controls["can_cancel"])
            self.assertEqual(controls["interrupt_tool"], None)

    def test_custom_runner_adapter_controls_sync_and_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            task = store.create_task(
                kind="mcp",
                status="running",
                title="Fake runner task",
                runner_payload={"runner": "fake"},
                runner_capabilities={"status": True, "interrupt": True},
                progress_summary="not synced",
            )

            class FakeRunnerAdapter(TaskRunnerAdapter):
                def matches(self, task_to_match: object) -> bool:
                    return getattr(task_to_match, "runner_payload", {}).get("runner") == "fake"

                def controls(self, task_to_control: object) -> dict[str, object]:
                    controls = super().controls(task_to_control)  # type: ignore[arg-type]
                    controls["can_interrupt"] = True
                    controls["interrupt_tool"] = "interrupt_task"
                    controls["interrupt_reason"] = ""
                    return controls

                def sync_task(
                    self,
                    controller: TaskController,
                    task_to_sync: object,
                    *,
                    poll_timeout_ms: int,
                ) -> object:
                    _ = poll_timeout_ms
                    return controller.task_store.update_task(
                        task_to_sync.task_id,  # type: ignore[attr-defined]
                        progress_summary="fake synced",
                    )

                def interrupt_task(self, controller: TaskController, task_to_interrupt: object) -> dict[str, object]:
                    updated = controller.task_store.update_task(
                        task_to_interrupt.task_id,  # type: ignore[attr-defined]
                        status="interrupted",
                        progress_summary="Fake interrupted.",
                        terminal_summary="Fake interrupted.",
                        resume_policy="not_resumable",
                    )
                    assert updated is not None
                    controller.event_store.append_event(updated.task_id, "task.interrupted", message="Fake interrupted.")
                    return {"ok": True, "task": controller._task_payload(updated), "message": "Fake interrupted."}

            controller = TaskController(
                task_store=store,
                runner_registry=TaskRunnerRegistry([FakeRunnerAdapter()]),
            )

            shown = controller.show_task(task.task_id)
            interrupted = controller.interrupt_task(task.task_id)

            self.assertEqual(shown["task"]["progress_summary"], "fake synced")
            self.assertTrue(shown["task"]["controls"]["can_interrupt"])
            self.assertEqual(interrupted["task"]["status"], "interrupted")
            self.assertEqual(store.get_task(task.task_id).status, "interrupted")  # type: ignore[union-attr]

    def test_mcp_job_runner_applies_external_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            os.environ["OPENPPX_TASK_DB_PATH"] = str(db_path)
            task = TaskStore(db_path=db_path).create_task(
                kind="mcp",
                status="running",
                title="MCP remote job",
                external_ref="mcp-job-1",
                runner_payload={
                    "runner": "mcp",
                    "server": "remote",
                    "tool_name": "long_job",
                    "status_snapshot": {
                        "status": "succeeded",
                        "progress_summary": "Remote job finished.",
                        "output": "remote result",
                    },
                },
                runner_capabilities={"status": True, "output": True, "rejoin": True},
                resume_policy="rejoin",
                progress_summary="Remote job running.",
            )

            shown = json.loads(show_task(task.task_id))
            output = json.loads(task_output(task.task_id))

            self.assertEqual(shown["task"]["status"], "completed")
            self.assertEqual(shown["task"]["terminal_summary"], "remote result")
            self.assertFalse(shown["task"]["controls"]["can_cancel"])
            self.assertEqual(output["output"], "remote result")
            self.assertIn("task.completed", [event["event_type"] for event in shown["events"]])

    def test_mcp_job_runner_leaves_stale_without_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            task = store.create_task(
                kind="mcp",
                status="stale",
                title="MCP missing snapshot",
                external_ref="mcp-job-missing",
                runner_payload={"runner": "mcp", "server": "remote"},
                progress_summary="No current external status.",
            )
            controller = TaskController(task_store=store)

            reconciled = controller.reconcile_stale_task(
                task.task_id,
                stale_lost_after_ms=0,
                now_ms=task.updated_at_ms + 10_000,
            )
            interrupted = controller.interrupt_task(task.task_id)

            self.assertIsNotNone(reconciled)
            assert reconciled is not None
            self.assertEqual(reconciled.status, "stale")
            self.assertFalse(interrupted["ok"])
            self.assertEqual(interrupted["action"], "not_supported")

    def test_sync_tool_proxy_runner_reconciles_detached_task_to_lost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            task = store.create_task(
                kind="gui_task",
                status="running",
                title="Detached GUI task",
                runner_payload={"runner": "sync_tool_proxy", "tool_name": "computer_task"},
                runner_capabilities={"status": True, "output": True, "rejoin": True},
                resume_policy="rejoin",
                progress_summary="GUI task running.",
            )
            controller = TaskController(task_store=store)

            shown = controller.show_task(task.task_id)
            reconciled = controller.reconcile_stale_task(
                task.task_id,
                stale_lost_after_ms=0,
                now_ms=shown["task"]["updated_at_ms"] + 10_000,
            )

            self.assertEqual(shown["task"]["status"], "stale")
            self.assertFalse(shown["task"]["controls"]["can_interrupt"])
            self.assertFalse(shown["task"]["controls"]["can_cancel"])
            self.assertFalse(shown["task"]["controls"]["can_resume"])
            self.assertIsNotNone(reconciled)
            assert reconciled is not None
            self.assertEqual(reconciled.status, "lost")
            self.assertEqual(reconciled.resume_policy, "not_resumable")

    def test_send_task_input_records_input_for_waiting_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            os.environ["OPENPPX_TASK_DB_PATH"] = str(db_path)
            task = TaskStore(db_path=db_path).create_task(
                kind="skill_api",
                status="waiting_user",
                title="demo:ask",
                session_id="session-1",
                progress_summary="Need a path.",
            )

            sent = json.loads(send_task_input(task.task_id, "use report.csv"))
            shown = json.loads(show_task(task.task_id))

            self.assertTrue(sent["ok"])
            self.assertEqual(sent["input"]["content"], "use report.csv")
            self.assertEqual(sent["task"]["status"], "waiting_user")
            self.assertTrue(sent["task"]["controls"]["can_send_input"])
            self.assertEqual(sent["task"]["controls"]["input_tool"], "send_task_input")
            self.assertFalse(sent["task"]["controls"]["can_resume"])
            self.assertEqual(shown["inputs"][0]["content"], "use report.csv")
            self.assertEqual(shown["events"][-1]["event_type"], "task.input_received")

    def test_send_task_input_rejects_non_waiting_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            os.environ["OPENPPX_TASK_DB_PATH"] = str(db_path)
            task = TaskStore(db_path=db_path).create_task(
                kind="skill_api",
                status="running",
                title="demo:run",
            )

            sent = json.loads(send_task_input(task.task_id, "extra info"))

            self.assertFalse(sent["ok"])
            self.assertIn("not waiting", sent["error"])

    def test_reconcile_stale_process_task_waits_for_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            controller = TaskController(task_store=store)
            task = store.create_task(
                kind="skill_api",
                status="stale",
                title="demo:missing",
                external_ref="missing-session",
                runner_payload={"runner": "process"},
                progress_summary="Backing process session not found.",
            )

            reconciled = controller.reconcile_stale_task(
                task.task_id,
                stale_lost_after_ms=1_000,
                now_ms=task.updated_at_ms + 999,
            )

            self.assertIsNotNone(reconciled)
            assert reconciled is not None
            self.assertEqual(reconciled.status, "stale")
            self.assertEqual(controller.show_task(task.task_id)["events"], [])

    def test_reconcile_stale_process_task_marks_lost_after_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            controller = TaskController(task_store=store)
            task = store.create_task(
                kind="skill_api",
                status="stale",
                title="demo:missing",
                external_ref="missing-session",
                runner_payload={"runner": "process"},
                progress_summary="Backing process session not found.",
            )

            reconciled = controller.reconcile_stale_task(
                task.task_id,
                stale_lost_after_ms=1_000,
                now_ms=task.updated_at_ms + 1_000,
            )
            shown = controller.show_task(task.task_id)

            self.assertIsNotNone(reconciled)
            assert reconciled is not None
            self.assertEqual(reconciled.status, "lost")
            self.assertEqual(reconciled.resume_policy, "not_resumable")
            self.assertIn("stale grace period", reconciled.terminal_summary)
            self.assertEqual(shown["events"][-1]["event_type"], "task.lost")

    def test_render_long_task_context_includes_rules_and_active_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            os.environ["OPENPPX_TASK_DB_PATH"] = str(db_path)
            store = TaskStore(db_path=db_path)
            task = store.create_task(
                kind="skill_api",
                status="waiting_user",
                title="demo:ask",
                session_id="session-1",
                progress_summary="Need a file path.",
            )

            rendered = render_long_task_context(session_id="session-1", task_store=store)

            self.assertIn("Treat stop/pause as interrupt", rendered)
            self.assertIn(task.task_id, rendered)
            self.assertIn("Need a file path", rendered)

    def test_render_long_task_context_includes_paused_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            os.environ["OPENPPX_TASK_DB_PATH"] = str(db_path)
            store = TaskStore(db_path=db_path)
            task = store.create_task(
                kind="browser",
                status="paused",
                title="Paused browser workflow",
                session_id="session-1",
                progress_summary="Checkpoint is ready.",
            )

            rendered = render_long_task_context(session_id="session-1", task_store=store)

            self.assertIn(task.task_id, rendered)
            self.assertIn("[paused]", rendered)
            self.assertIn("Checkpoint is ready", rendered)

    def test_render_long_task_context_includes_checkpoint_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            task_store = TaskStore(db_path=db_path)
            checkpoint_store = TaskCheckpointStore(db_path=db_path)
            task = task_store.create_task(
                kind="browser",
                status="paused",
                title="Paused browser workflow",
                session_id="session-1",
                resume_policy="checkpoint",
                progress_summary="Checkpoint is ready.",
            )
            checkpoint = checkpoint_store.record_checkpoint(
                task_id=task.task_id,
                checkpoint_type="runner_state",
                runner_name="browser",
                payload={"target_id": "tab-1"},
                summary="Paused before submitting the form.",
            )
            task_store.update_task(task.task_id, checkpoint_ref=checkpoint.checkpoint_id)

            rendered = render_long_task_context(
                session_id="session-1",
                task_store=task_store,
                checkpoint_store=checkpoint_store,
            )

            self.assertIn(f"checkpoint_ref={checkpoint.checkpoint_id}", rendered)
            self.assertIn("resume_policy=checkpoint", rendered)
            self.assertIn("Paused before submitting the form", rendered)

    def test_render_long_task_context_includes_goal_mirror_and_todos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            task_store = TaskStore(db_path=db_path)
            context_store = LongTaskContextStore(db_path=db_path)
            goal = context_store.upsert_goal(
                session_id="session-1",
                objective="Refine long task runtime",
                completion_criteria="All tests pass",
                current_summary="Working on context",
            )
            context_store.replace_todos(session_id="session-1", goal_id=goal.goal_id, items=["Design", "Test"])

            rendered = render_long_task_context(
                session_id="session-1",
                task_store=task_store,
                context_store=context_store,
            )

            self.assertIn("Current goal mirror:", rendered)
            self.assertIn("Refine long task runtime", rendered)
            self.assertIn("All tests pass", rendered)
            self.assertIn("[in_progress] Design", rendered)
            self.assertIn("[pending] Test", rendered)

    def test_render_long_task_context_includes_active_task_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            task_store = TaskStore(db_path=db_path)
            context_store = LongTaskContextStore(db_path=db_path)
            flow, _ = context_store.upsert_flow(
                session_id="session-1",
                goal="Implement TaskFlow facts",
                steps=[
                    {"title": "Add store", "status": "completed"},
                    {"title": "Add tools", "status": "pending", "task_id": "task_123"},
                ],
            )

            rendered = render_long_task_context(
                session_id="session-1",
                task_store=task_store,
                context_store=context_store,
            )

            self.assertIn("Current TaskFlow:", rendered)
            self.assertIn(flow.flow_id, rendered)
            self.assertIn("Implement TaskFlow facts", rendered)
            self.assertIn("[in_progress] Add tools", rendered)
            self.assertIn("task_id=task_123", rendered)

    def test_render_long_task_context_includes_staged_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            task_store = TaskStore(db_path=db_path)
            context_store = LongTaskContextStore(db_path=db_path)
            summary = context_store.upsert_summary(
                session_id="session-1",
                title="Important decision",
                content="Keep TaskRun and TaskFlow separate.",
            )

            rendered = render_long_task_context(
                session_id="session-1",
                task_store=task_store,
                context_store=context_store,
            )

            self.assertIn("Recent staged summaries:", rendered)
            self.assertIn(summary.summary_id, rendered)
            self.assertIn("Keep TaskRun and TaskFlow separate", rendered)

    def _prepare_skill(self, tmp: str, api_name: str, script: str) -> None:
        root = Path(tmp)
        agent_home = root / "agent"
        skill_dir = agent_home / "skills" / "demo"
        scripts = skill_dir / "scripts"
        scripts.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: demo skill\n---\n# Demo\n",
            encoding="utf-8",
        )
        (scripts / f"{api_name}.py").write_text(script, encoding="utf-8")
        os.environ["OPENPPX_AGENT_HOME"] = str(agent_home)
        os.environ["OPENPPX_TASK_DB_PATH"] = str(root / "tasks.db")
        # Keep process sessions isolated per test route-free scope.
        time.sleep(0.001)

    def _prepare_http_skill(self, tmp: str, api_name: str, recipe: dict[str, object]) -> None:
        root = Path(tmp)
        agent_home = root / "agent"
        skill_dir = agent_home / "skills" / "demo"
        apis = skill_dir / "apis"
        apis.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: demo skill\n---\n# Demo\n",
            encoding="utf-8",
        )
        (apis / f"{api_name}.json").write_text(json.dumps(recipe), encoding="utf-8")
        os.environ["OPENPPX_AGENT_HOME"] = str(agent_home)
        os.environ["OPENPPX_TASK_DB_PATH"] = str(root / "tasks.db")
        time.sleep(0.001)

    def _prepare_python_api_skill(
        self,
        tmp: str,
        api_name: str,
        recipe: dict[str, object],
        module_source: str,
    ) -> None:
        root = Path(tmp)
        agent_home = root / "agent"
        skill_dir = agent_home / "skills" / "demo"
        apis = skill_dir / "apis"
        apis.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: demo skill\n---\n# Demo\n",
            encoding="utf-8",
        )
        (skill_dir / "demo_sdk.py").write_text(module_source, encoding="utf-8")
        (apis / f"{api_name}.python.json").write_text(json.dumps(recipe), encoding="utf-8")
        os.environ["OPENPPX_AGENT_HOME"] = str(agent_home)
        os.environ["OPENPPX_TASK_DB_PATH"] = str(root / "tasks.db")
        time.sleep(0.001)


if __name__ == "__main__":
    unittest.main()
