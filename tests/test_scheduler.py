import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


SPEC = importlib.util.spec_from_file_location("scheduler", Path(__file__).resolve().parents[1] / "scripts/scheduler.py")
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 20, 19, 0, tzinfo=timezone.utc)

    def test_unarmed_tick_does_not_run(self):
        self.assertFalse(scheduler.transition(None, "tick", self.now)[1])

    def test_immediate_runs_once_then_waits(self):
        state, run, _ = scheduler.transition(None, "immediate", self.now, "15")
        self.assertTrue(run)
        self.assertEqual(state["next_due"], "2026-09-20T19:15:00+00:00")
        self.assertFalse(scheduler.transition(state, "tick", self.now)[1])
        self.assertTrue(scheduler.transition(state, "tick", datetime.fromisoformat(state["next_due"]))[1])

    def test_beijing_start_and_rollover(self):
        state, run, _ = scheduler.transition(None, "scheduled", self.now, "10", "04:00")
        self.assertFalse(run)
        self.assertEqual(state["next_due"], "2026-09-21T04:00:00+08:00")
        later = datetime(2026, 9, 20, 21, tzinfo=timezone.utc)
        state, run, _ = scheduler.transition(None, "scheduled", later, "30", "04:00")
        self.assertEqual(state["next_due"], "2026-09-22T04:00:00+08:00")

    def test_stop_and_replace(self):
        state, _, _ = scheduler.transition(None, "immediate", self.now)
        stopped, run, _ = scheduler.transition(state, "stop", self.now)
        self.assertFalse(run)
        self.assertFalse(scheduler.transition(stopped, "tick", self.now)[1])
        replaced, run, _ = scheduler.transition(state, "scheduled", self.now, "30")
        self.assertFalse(run)
        self.assertEqual(replaced["interval_minutes"], 30)

    def test_invalid_inputs(self):
        for interval in ["0", "4", "1441", "hello"]:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                scheduler.transition(None, "immediate", self.now, interval)
        for start in ["25:00", "04:60", "4:00"]:
            with self.subTest(start=start), self.assertRaises(ValueError):
                scheduler.transition(None, "scheduled", self.now, "15", start)

    def test_repository_variable_create_update_and_read(self):
        with patch.dict(scheduler.os.environ, {"GITHUB_REPOSITORY": "owner/repo", "SCHEDULER_TOKEN": "mock-token"}):
            repository = scheduler.RepositoryState()
        with patch.object(repository, "request", return_value={"value": '{"enabled":false}'}) as request:
            self.assertEqual(repository.read(), {"enabled": False})
            request.assert_called_once_with("GET", "/CODEX_SCHEDULE_STATE")
        with patch.object(repository, "request") as request:
            repository.save({"enabled": False}, False)
            self.assertEqual(request.call_args.args[0], "POST")
            repository.save({"enabled": False}, True)
            self.assertEqual(request.call_args.args[:2], ("PATCH", "/CODEX_SCHEDULE_STATE"))

    def test_repository_errors_fail_closed(self):
        with patch.dict(scheduler.os.environ, {"GITHUB_REPOSITORY": "owner/repo", "SCHEDULER_TOKEN": "mock-token"}):
            repository = scheduler.RepositoryState()
        with patch.object(repository, "request", side_effect=HTTPError("", 404, "missing", None, None)):
            self.assertIsNone(repository.read())
        with patch.object(repository, "request", side_effect=HTTPError("", 403, "forbidden", None, None)):
            with self.assertRaises(HTTPError):
                repository.read()


if __name__ == "__main__":
    unittest.main()
