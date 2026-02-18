"""Tests for shared cron schedule parsing helpers."""

from __future__ import annotations

import unittest

from sentientagent_v2.runtime.cron_schedule_parser import parse_schedule_input


class CronScheduleParserTests(unittest.TestCase):
    def test_parse_every_schedule(self) -> None:
        parsed, error = parse_schedule_input(
            every_seconds=30,
            cron_expr=None,
            at=None,
            tz=None,
        )
        self.assertIsNone(error)
        self.assertIsNotNone(parsed)
        if parsed is None:
            return
        self.assertEqual(parsed.schedule.kind, "every")
        self.assertEqual(parsed.schedule.every_seconds, 30)
        self.assertFalse(parsed.delete_after_run)

    def test_parse_cron_with_timezone(self) -> None:
        parsed, error = parse_schedule_input(
            every_seconds=None,
            cron_expr="0 9 * * 1-5",
            at=None,
            tz="Asia/Shanghai",
        )
        self.assertIsNone(error)
        self.assertIsNotNone(parsed)
        if parsed is None:
            return
        self.assertEqual(parsed.schedule.kind, "cron")
        self.assertEqual(parsed.schedule.cron_expr, "0 9 * * 1-5")
        self.assertEqual(parsed.schedule.tz, "Asia/Shanghai")

    def test_parse_at_schedule_marks_delete_after_run(self) -> None:
        parsed, error = parse_schedule_input(
            every_seconds=None,
            cron_expr=None,
            at="2026-02-19T09:30:00",
            tz=None,
        )
        self.assertIsNone(error)
        self.assertIsNotNone(parsed)
        if parsed is None:
            return
        self.assertEqual(parsed.schedule.kind, "at")
        self.assertTrue(parsed.delete_after_run)

    def test_parse_rejects_multiple_modes(self) -> None:
        parsed, error = parse_schedule_input(
            every_seconds=30,
            cron_expr="0 * * * *",
            at=None,
            tz=None,
        )
        self.assertIsNone(parsed)
        self.assertIn("exactly one schedule source", str(error))

    def test_parse_rejects_non_positive_every(self) -> None:
        parsed, error = parse_schedule_input(
            every_seconds=0,
            cron_expr=None,
            at=None,
            tz=None,
        )
        self.assertIsNone(parsed)
        self.assertEqual(error, "every_seconds must be > 0")

    def test_parse_rejects_invalid_timezone(self) -> None:
        parsed, error = parse_schedule_input(
            every_seconds=None,
            cron_expr="0 9 * * *",
            at=None,
            tz="Mars/Phobos",
        )
        self.assertIsNone(parsed)
        self.assertIn("unknown timezone", str(error))


if __name__ == "__main__":
    unittest.main()
