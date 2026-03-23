"""Tests for minimal runtime heartbeat runner."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from openpipixia.runtime.heartbeat_runner import (
    HeartbeatRunner,
    parse_heartbeat_every_ms,
)


class HeartbeatEveryParseTests(unittest.TestCase):
    def test_parse_duration_with_default_minutes(self) -> None:
        self.assertEqual(parse_heartbeat_every_ms("30"), 30 * 60_000)
        self.assertEqual(parse_heartbeat_every_ms("30m"), 30 * 60_000)

    def test_parse_duration_with_explicit_units(self) -> None:
        self.assertEqual(parse_heartbeat_every_ms("250ms"), 250)
        self.assertEqual(parse_heartbeat_every_ms("2s"), 2_000)
        self.assertEqual(parse_heartbeat_every_ms("3h"), 3 * 3_600_000)

    def test_parse_duration_invalid_or_disabled(self) -> None:
        self.assertIsNone(parse_heartbeat_every_ms(""))
        self.assertIsNone(parse_heartbeat_every_ms("0m"))
        self.assertIsNone(parse_heartbeat_every_ms("abc"))
        self.assertIsNone(parse_heartbeat_every_ms(None))


class HeartbeatRunnerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_now_runs_callback_and_carries_prompt(self) -> None:
        seen: list[tuple[str, str]] = []

        async def on_run(req) -> None:
            seen.append((req.reason, req.prompt))

        runner = HeartbeatRunner(
            on_run=on_run,
            every="10m",
            prompt="ops check",
        )
        result = await runner.trigger_now(reason="manual")

        self.assertEqual(result.status, "ran")
        self.assertEqual(result.reason, "manual")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], ("manual", "ops check"))
        self.assertEqual(runner.status()["last_status"], "ran")
        self.assertIsNotNone(runner.status()["last_duration_ms"])

    async def test_trigger_now_skips_when_busy(self) -> None:
        async def on_run(_req) -> None:
            raise AssertionError("callback should not be called while busy")

        runner = HeartbeatRunner(
            on_run=on_run,
            every="1m",
            is_busy=lambda: True,
        )
        result = await runner.trigger_now(reason="manual")
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason, "busy")

    async def test_trigger_now_skips_outside_active_hours(self) -> None:
        async def on_run(_req) -> None:
            raise AssertionError("callback should not run outside active hours")

        # 2026-01-01T08:30:00Z
        now_ms = 1_767_256_200_000
        runner = HeartbeatRunner(
            on_run=on_run,
            every="1m",
            active_hours={"start": "09:00", "end": "18:00", "timezone": "UTC"},
            now_ms_fn=lambda: now_ms,
        )
        result = await runner.trigger_now(reason="manual")
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason, "quiet-hours")

    async def test_trigger_now_runs_inside_active_hours(self) -> None:
        called = False

        async def on_run(_req) -> None:
            nonlocal called
            called = True

        # 2026-01-01T09:30:00Z
        now_ms = 1_767_259_800_000
        runner = HeartbeatRunner(
            on_run=on_run,
            every="1m",
            active_hours={"start": "09:00", "end": "18:00", "timezone": "UTC"},
            now_ms_fn=lambda: now_ms,
        )
        result = await runner.trigger_now(reason="manual")
        self.assertTrue(called)
        self.assertEqual(result.status, "ran")

    async def test_start_runs_interval_tick(self) -> None:
        ticked = asyncio.Event()
        seen_reasons: list[str] = []

        async def on_run(req) -> None:
            seen_reasons.append(req.reason)
            ticked.set()

        runner = HeartbeatRunner(
            on_run=on_run,
            every="20ms",
            prompt="interval prompt",
        )
        await runner.start()
        try:
            await asyncio.wait_for(ticked.wait(), timeout=0.5)
        finally:
            await runner.stop()

        self.assertIn("interval", seen_reasons)
        self.assertFalse(bool(runner.status()["running"]))

    async def test_disabled_runner_reports_skipped(self) -> None:
        async def on_run(_req) -> None:
            raise AssertionError("disabled runner must not execute callback")

        runner = HeartbeatRunner(on_run=on_run, every="0m")
        result = await runner.trigger_now()
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason, "disabled")

    async def test_trigger_now_captures_callback_failure(self) -> None:
        async def on_run(_req) -> None:
            raise RuntimeError("boom")

        runner = HeartbeatRunner(on_run=on_run, every="1m")
        result = await runner.trigger_now(reason="manual")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "manual")
        self.assertIn("boom", result.error or "")
        self.assertEqual(runner.status()["last_status"], "failed")

    async def test_request_wake_coalesces_and_keeps_higher_priority_reason(self) -> None:
        reasons: list[str] = []
        fired = asyncio.Event()

        async def on_run(req) -> None:
            reasons.append(req.reason)
            fired.set()

        runner = HeartbeatRunner(on_run=on_run, every="10m")
        await runner.start()
        try:
            runner.request_wake(reason="interval", coalesce_ms=200)
            await asyncio.sleep(0.01)
            runner.request_wake(reason="manual", coalesce_ms=20)
            await asyncio.wait_for(fired.wait(), timeout=0.5)
        finally:
            await runner.stop()

        self.assertEqual(reasons, ["manual"])

    async def test_request_wake_retries_when_busy(self) -> None:
        reasons: list[str] = []
        fired = asyncio.Event()
        busy = {"value": True}

        async def on_run(req) -> None:
            reasons.append(req.reason)
            fired.set()

        runner = HeartbeatRunner(
            on_run=on_run,
            every="10m",
            is_busy=lambda: busy["value"],
        )
        with patch("openpipixia.runtime.heartbeat_runner.DEFAULT_WAKE_RETRY_MS", 30):
            await runner.start()
            try:
                runner.request_wake(reason="manual", coalesce_ms=0)
                await asyncio.sleep(0.02)  # first run skipped as busy
                busy["value"] = False
                await asyncio.wait_for(fired.wait(), timeout=0.5)
            finally:
                await runner.stop()

        self.assertEqual(reasons, ["manual"])

    async def test_status_tracks_recent_reason_sources(self) -> None:
        async def on_run(_req) -> None:
            return None

        runner = HeartbeatRunner(on_run=on_run, every="1m")
        await runner.trigger_now(reason="manual")
        await runner.trigger_now(reason="cron:nightly")
        await runner.trigger_now(reason="exec:foreground")
        await runner.trigger_now(reason="hook:upload")
        await runner.trigger_now(reason="unknown-reason")

        status = runner.status()
        self.assertEqual(status["recent_reason_sources"], ["manual", "cron", "exec", "hook", "other"])
        self.assertEqual(status["recent_reason_counts"], {"manual": 1, "cron": 1, "exec": 1, "hook": 1, "other": 1})


if __name__ == "__main__":
    unittest.main()
