"""Tests for runtime cron scheduler service."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sentientagent_v2.runtime.cron_service import CronSchedule, CronService, _compute_next_run


class _FakeClock:
    def __init__(self, start_ms: int) -> None:
        self.now_ms = start_ms

    def now(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


class CronServiceTests(unittest.TestCase):
    def test_compute_next_run_every(self) -> None:
        now_ms = 1_000
        next_run = _compute_next_run(CronSchedule(kind="every", every_seconds=30), now_ms)
        self.assertEqual(next_run, 31_000)

    def test_compute_next_run_at(self) -> None:
        now_ms = 1_000
        self.assertEqual(_compute_next_run(CronSchedule(kind="at", at_ms=5_000), now_ms), 5_000)
        self.assertIsNone(_compute_next_run(CronSchedule(kind="at", at_ms=900), now_ms))

    def test_compute_next_run_cron_with_tz(self) -> None:
        now = datetime(2026, 2, 18, 0, 1, tzinfo=timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        next_run = _compute_next_run(
            CronSchedule(kind="cron", cron_expr="0 9 * * *", tz="Asia/Shanghai"),
            now_ms,
        )
        self.assertIsNotNone(next_run)
        if next_run is None:
            return
        run_dt = datetime.fromtimestamp(next_run / 1000, tz=timezone.utc)
        self.assertEqual(run_dt.hour, 1)  # 09:00 Asia/Shanghai == 01:00 UTC
        self.assertEqual(run_dt.minute, 0)

    def test_loads_legacy_store_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "cron_jobs.json"
            store_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "abc12345",
                            "name": "legacy",
                            "message": "legacy message",
                            "schedule": "every:30s",
                            "created_at": "2026-02-18T10:00:00",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            service = CronService(store_path)
            jobs = service.list_jobs(include_disabled=True)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].id, "abc12345")
            self.assertEqual(jobs[0].schedule.kind, "every")
            self.assertEqual(jobs[0].schedule.every_seconds, 30)
            self.assertEqual(jobs[0].payload.message, "legacy message")

    def test_keeps_last_good_store_when_disk_json_becomes_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "cron_jobs.json"
            service = CronService(store_path)
            service.add_job(
                name="stable",
                schedule=CronSchedule(kind="every", every_seconds=30),
                message="hello",
            )
            initial = service.list_jobs(include_disabled=True)
            self.assertEqual(len(initial), 1)

            # Simulate external writer leaving a temporarily broken JSON file.
            store_path.write_text("{broken-json", encoding="utf-8")
            after_corruption = service.list_jobs(include_disabled=True)
            self.assertEqual(len(after_corruption), 1)
            self.assertEqual(after_corruption[0].name, "stable")

    def test_add_job_recovers_from_broken_store_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "cron_jobs.json"
            store_path.parent.mkdir(parents=True, exist_ok=True)
            store_path.write_text("{broken-json", encoding="utf-8")

            service = CronService(store_path)
            job = service.add_job(
                name="recover",
                schedule=CronSchedule(kind="every", every_seconds=15),
                message="repair",
            )
            self.assertEqual(job.name, "recover")
            payload = json.loads(store_path.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("version"), 2)
            self.assertEqual(len(payload.get("jobs", [])), 1)


class CronServiceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_once_executes_due_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _FakeClock(start_ms=1_000_000)
            seen: list[str] = []

            async def on_job(job) -> str:
                seen.append(job.id)
                return "ok"

            service = CronService(Path(tmp) / "cron_jobs.json", on_job=on_job, now_ms_fn=clock.now)
            job = service.add_job(
                name="demo",
                schedule=CronSchedule(kind="every", every_seconds=1),
                message="hello",
            )
            self.assertIsNotNone(job.state.next_run_at_ms)

            clock.advance(1_500)
            executed = await service.tick_once()
            self.assertEqual(executed, 1)
            self.assertEqual(seen, [job.id])

            jobs = service.list_jobs(include_disabled=True)
            self.assertEqual(jobs[0].state.last_status, "ok")
            self.assertIsNotNone(jobs[0].state.next_run_at_ms)
            self.assertGreater(jobs[0].state.next_run_at_ms or 0, clock.now())

    async def test_run_job_honors_force_for_disabled_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen: list[str] = []

            async def on_job(job) -> str:
                seen.append(job.id)
                return "ok"

            service = CronService(Path(tmp) / "cron_jobs.json", on_job=on_job)
            job = service.add_job(
                name="once",
                schedule=CronSchedule(kind="every", every_seconds=10),
                message="run",
            )
            service.enable_job(job.id, enabled=False)

            without_force = await service.run_job(job.id, force=False)
            self.assertFalse(without_force)
            self.assertEqual(seen, [])

            with_force = await service.run_job(job.id, force=True)
            self.assertTrue(with_force)
            self.assertEqual(seen, [job.id])

    async def test_run_job_with_result_reports_no_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = CronService(Path(tmp) / "cron_jobs.json")
            job = service.add_job(
                name="manual",
                schedule=CronSchedule(kind="every", every_seconds=10),
                message="run",
            )

            result = await service.run_job_with_result(job.id, force=False)
            self.assertFalse(result.executed)
            self.assertEqual(result.reason, "no_callback")
            self.assertEqual(result.error, "no on_job callback configured")

            persisted = service.list_jobs(include_disabled=True)
            self.assertEqual(persisted[0].state.last_status, "skipped")
            self.assertEqual(persisted[0].state.last_error, "no on_job callback configured")

    async def test_at_job_with_delete_after_run_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _FakeClock(start_ms=2_000_000)
            service = CronService(Path(tmp) / "cron_jobs.json", now_ms_fn=clock.now)
            job = service.add_job(
                name="once",
                schedule=CronSchedule(kind="at", at_ms=clock.now() + 1000),
                message="once",
                delete_after_run=True,
            )
            self.assertEqual(len(service.list_jobs(include_disabled=True)), 1)
            clock.advance(1001)
            executed = await service.tick_once()
            self.assertEqual(executed, 1)
            self.assertEqual(service.list_jobs(include_disabled=True), [])

    async def test_start_and_stop_toggle_running_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = CronService(Path(tmp) / "cron_jobs.json")
            await service.start()
            self.assertTrue(bool(service.status()["running"]))
            service.stop()
            self.assertFalse(bool(service.status()["running"]))

    async def test_status_reports_runtime_active_while_service_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = CronService(Path(tmp) / "cron_jobs.json")
            await service.start()
            try:
                info = service.status()
                self.assertTrue(bool(info["running"]))
                self.assertTrue(bool(info["runtime_active"]))
                self.assertEqual(info["runtime_pid"], os.getpid())
                self.assertIsNotNone(info["runtime_last_seen_at_ms"])
            finally:
                service.stop()

            stopped_info = service.status()
            self.assertFalse(bool(stopped_info["running"]))
            self.assertFalse(bool(stopped_info["runtime_active"]))

    async def test_status_can_observe_runtime_heartbeat_from_another_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "cron_jobs.json"
            running = CronService(store_path)
            observer = CronService(store_path)

            await running.start()
            try:
                observed = observer.status()
                self.assertTrue(bool(observed["runtime_active"]))
                self.assertEqual(observed["runtime_pid"], os.getpid())
            finally:
                running.stop()

            observed_after_stop = observer.status()
            self.assertFalse(bool(observed_after_stop["runtime_active"]))

    async def test_running_service_reloads_jobs_from_updated_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _FakeClock(start_ms=3_000_000)
            seen: list[str] = []
            store_path = Path(tmp) / "cron_jobs.json"

            async def on_job(job) -> str:
                seen.append(job.id)
                return "ok"

            running = CronService(store_path, on_job=on_job, now_ms_fn=clock.now)
            await running.start()
            try:
                writer = CronService(store_path, now_ms_fn=clock.now)
                job = writer.add_job(
                    name="external",
                    schedule=CronSchedule(kind="every", every_seconds=1),
                    message="sync",
                )
                clock.advance(1_100)
                executed = await running.tick_once()
                self.assertEqual(executed, 1)
                self.assertEqual(seen, [job.id])
            finally:
                running.stop()


if __name__ == "__main__":
    unittest.main()
