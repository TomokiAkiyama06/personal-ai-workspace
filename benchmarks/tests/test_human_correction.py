"""Tests for the human correction time stopwatch (Decision 0041 8, Decision 0082)."""

import fcntl
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from benchmarks.human_correction import (
    CAP_MS,
    CorrectionLogError,
    main,
    replay,
)

FIXTURE = (
    Path(__file__).parent / "fixtures" / "result-schema" / "valid" / "complete.json"
)
TASK = "paw-seed-v2-spec-03-unittest-report"
MODEL = "Qwen3.8-27B-FP8"
SESSION = f"{MODEL}/{TASK}/run1"
T0 = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class StopwatchTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.log = Path(self.directory.name) / "hct" / "events.jsonl"
        self.clock = Clock()

    def run_cli(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--log", str(self.log), *argv], clock=self.clock)
        return code, stdout.getvalue(), stderr.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return out

    def start(self, task=TASK, run="run1"):
        return self.ok("start", "--task", task, "--model", MODEL, "--run", run)

    def summary(self):
        return json.loads(self.ok("summary"))

    def test_active_time_excludes_pauses_and_counts_until_finish(self):
        self.start()
        self.clock.advance(minutes=10)
        self.ok("pause")
        self.clock.advance(minutes=30)  # break: not counted
        self.ok("resume")
        self.clock.advance(minutes=5)
        report = json.loads(self.ok("finish"))
        self.assertEqual(report["outcome"], "accepted")
        self.assertTrue(report["human_resolved"])
        self.assertEqual(report["human_correction_ms"], 15 * 60 * 1000)
        self.assertEqual(report["wall_ms"], 45 * 60 * 1000)
        self.assertEqual(report["pauses"], 1)

    def test_finish_while_paused_ends_at_the_pause(self):
        self.start()
        self.clock.advance(minutes=7)
        self.ok("pause")
        self.clock.advance(hours=3)
        report = json.loads(self.ok("finish"))
        self.assertEqual(report["human_correction_ms"], 7 * 60 * 1000)

    def test_over_the_cap_is_unresolved_and_recorded_as_the_cap(self):
        self.start()
        self.clock.advance(minutes=60, seconds=1)
        report = json.loads(self.ok("finish"))
        self.assertEqual(report["outcome"], "capped")
        self.assertFalse(report["human_resolved"])
        self.assertEqual(report["human_correction_ms"], CAP_MS)

    def test_exactly_the_cap_is_still_accepted(self):
        self.start()
        self.clock.advance(minutes=60)
        report = json.loads(self.ok("finish"))
        self.assertEqual(report["outcome"], "accepted")
        self.assertEqual(report["human_correction_ms"], CAP_MS)

    def test_abandon_is_unresolved_and_recorded_as_the_cap(self):
        self.start()
        self.clock.advance(minutes=20)
        report = json.loads(self.ok("abandon"))
        self.assertEqual(report["outcome"], "abandoned")
        self.assertFalse(report["human_resolved"])
        self.assertEqual(report["human_correction_ms"], CAP_MS)
        self.assertEqual(report["active_ms"], 20 * 60 * 1000)

    def test_unchanged_records_zero(self):
        report = json.loads(
            self.ok("unchanged", "--task", TASK, "--model", MODEL, "--run", "run2")
        )
        self.assertEqual(report["outcome"], "unchanged")
        self.assertEqual(report["human_correction_ms"], 0)

    def test_open_session_has_no_metric_and_status_shows_running_time(self):
        self.start()
        self.clock.advance(minutes=61)
        status = json.loads(self.ok("status"))
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["active_ms"], 61 * 60 * 1000)
        self.assertTrue(status["cap_reached"])
        self.assertNotIn("human_correction_ms", status)
        summary = self.summary()
        self.assertEqual(summary["open"], 1)
        self.assertNotIn("human_correction_ms", summary["sessions"][0])

    def test_invalid_transitions_are_rejected_without_writing(self):
        self.start()
        before = self.log.read_text(encoding="utf-8")
        code, _, err = self.run_cli("resume")
        self.assertEqual(code, 1)
        self.assertIn("not paused", err)
        code, _, err = self.run_cli(
            "start", "--task", TASK, "--model", MODEL, "--run", "run1"
        )
        self.assertEqual(code, 1)
        self.assertIn("already recorded", err)
        self.ok("finish")
        code, _, err = self.run_cli("pause", "--session", SESSION)
        self.assertEqual(code, 1)
        self.assertIn("closed", err)
        self.assertTrue(self.log.read_text(encoding="utf-8").startswith(before))
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 2)

    def test_session_must_be_named_when_several_are_open(self):
        self.start(run="run1")
        self.start(run="run2")
        code, _, err = self.run_cli("pause")
        self.assertEqual(code, 1)
        self.assertIn("2 sessions are open", err)
        self.ok("pause", "--session", f"{MODEL}/{TASK}/run2")

    def test_names_with_a_slash_are_rejected(self):
        code, _, err = self.run_cli("start", "--task", "a/b", "--model", MODEL)
        self.assertIn("without", err)
        self.assertEqual(code, 1)
        self.assertEqual(self.log.read_text(encoding="utf-8"), "")

    def test_clock_going_backwards_is_rejected(self):
        self.start()
        self.clock.advance(minutes=-1)
        code, _, err = self.run_cli("finish")
        self.assertEqual(code, 1)
        self.assertIn("backwards", err)

    def test_log_is_private_and_holds_only_names_and_times(self):
        self.start()
        self.ok("finish")
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        for line in self.log.read_text(encoding="utf-8").splitlines():
            self.assertLessEqual(
                set(json.loads(line)),
                {"v", "event", "session", "at", "task_id", "model", "run"},
            )

    def test_existing_log_is_made_private(self):
        self.log.parent.mkdir(parents=True)
        self.log.touch(mode=0o644)
        self.log.chmod(0o644)
        self.start()
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_symlinked_log_is_refused(self):
        target = Path(self.directory.name) / "elsewhere.txt"
        target.write_text("", encoding="utf-8")
        self.log.parent.mkdir(parents=True)
        self.log.symlink_to(target)
        code, _, err = self.run_cli("start", "--task", TASK, "--model", MODEL)
        self.assertEqual(code, 1)
        self.assertIn("regular file", err)
        self.assertEqual(target.read_text(encoding="utf-8"), "")

    def test_read_commands_refuse_a_symlinked_log(self):
        real = Path(self.directory.name) / "real.jsonl"
        self.log.parent.mkdir(parents=True)
        self.log.symlink_to(real)
        link = self.log
        self.log = real
        self.start()
        self.log = link
        for argv in (("summary",), ("status",)):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("regular file", err)
                self.assertNotIn(TASK, out)

    def test_fifo_log_is_refused_without_blocking(self):
        self.log.parent.mkdir(parents=True)
        os.mkfifo(self.log)
        for argv in (
            ("summary",),
            ("status",),
            ("start", "--task", TASK, "--model", MODEL),
        ):
            with self.subTest(argv=argv[0]):
                outcome = {}
                worker = threading.Thread(
                    target=lambda a=argv, o=outcome: o.setdefault(
                        "code", self.run_cli(*a)[0]
                    ),
                    daemon=True,
                )
                worker.start()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(outcome["code"], 1)

    def test_validation_and_append_hold_an_exclusive_lock(self):
        self.start()
        self.log.touch()
        with self.log.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            outcome = {}
            worker = threading.Thread(
                target=lambda: outcome.setdefault("code", self.run_cli("pause")[0])
            )
            worker.start()
            worker.join(0.3)
            # The second writer waits for the lock instead of validating a stale log.
            self.assertTrue(worker.is_alive())
            self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 1)
        worker.join(5)
        self.assertEqual(outcome["code"], 0)
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 2)

    def test_summary_counts_outcomes(self):
        self.start(run="run1")
        self.clock.advance(minutes=5)
        self.ok("finish")
        self.start(run="run2")
        self.clock.advance(minutes=5)
        self.ok("abandon")
        output = Path(self.directory.name) / "summary.json"
        self.ok("summary", "--output", str(output))
        summary = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(summary["cap_ms"], CAP_MS)
        self.assertEqual(summary["open"], 0)
        self.assertEqual(
            summary["outcomes"],
            {"accepted": 1, "unchanged": 0, "capped": 0, "abandoned": 1},
        )

    def test_summary_never_overwrites_the_log(self):
        self.start()
        before = self.log.read_text(encoding="utf-8")
        alias = Path(self.directory.name) / "alias.json"
        hard = Path(self.directory.name) / "hard.json"
        alias.symlink_to(self.log)
        hard.hardlink_to(self.log)
        for output in (self.log, alias, hard):
            with self.subTest(output=output.name):
                code, _, err = self.run_cli("summary", "--output", str(output))
                self.assertEqual(code, 1)
                self.assertIn("event log", err)
                self.assertEqual(self.log.read_text(encoding="utf-8"), before)

    def test_summary_never_takes_the_place_of_an_absent_log(self):
        (self.log.parent / "sub").mkdir(parents=True)
        output = self.log.parent / "sub" / ".." / self.log.name
        code, _, err = self.run_cli("summary", "--output", str(output))
        self.assertEqual(code, 1)
        self.assertIn("event log", err)
        self.assertFalse(self.log.exists())

    def test_append_after_an_unterminated_last_line_keeps_lines_apart(self):
        self.start()
        text = self.log.read_text(encoding="utf-8")
        self.log.write_text(text.rstrip("\n"), encoding="utf-8")
        self.clock.advance(minutes=3)
        self.ok("finish")
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.summary()["outcomes"]["accepted"], 1)

    def write_result(self, task=TASK, model=MODEL):
        result = json.loads(FIXTURE.read_text(encoding="utf-8"))
        result["task_id"] = task
        result["candidate"]["model"] = model
        del result["metrics"]["human_correction_ms"]
        path = Path(self.directory.name) / "result.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        return path

    def test_apply_writes_the_metric_into_a_valid_result(self):
        path = self.write_result()
        self.start()
        self.clock.advance(minutes=12)
        self.ok("finish")
        self.ok("apply", "--session", SESSION, "--result", str(path))
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["metrics"]["human_correction_ms"], 12 * 60 * 1000)
        self.assertEqual(result["metrics"]["wall_clock_ms"], 1234.5)

    def test_apply_refuses_open_sessions_and_mismatched_results(self):
        self.start()
        path = self.write_result()
        code, _, err = self.run_cli(
            "apply", "--session", SESSION, "--result", str(path)
        )
        self.assertEqual(code, 1)
        self.assertIn("still open", err)
        self.ok("finish")
        for task, model in (("other-task", MODEL), (TASK, "other-model")):
            path = self.write_result(task=task, model=model)
            code, _, err = self.run_cli(
                "apply", "--session", SESSION, "--result", str(path)
            )
            self.assertEqual(code, 1)
            self.assertIn("does not match", err)
            self.assertNotIn(
                "human_correction_ms",
                json.loads(path.read_text(encoding="utf-8"))["metrics"],
            )


class ReplayTest(unittest.TestCase):
    def event(self, **fields):
        return json.dumps({"v": 1, **fields})

    def test_rejects_unknown_session_event_and_bad_lines(self):
        start = self.event(
            event="start",
            session=SESSION,
            at="2026-10-08T01:00:00.000Z",
            task_id=TASK,
            model=MODEL,
            run="run1",
        )
        cases = {
            "unknown session": [
                self.event(event="pause", session="x/y/z", at="2026-10-08T01:00:00Z")
            ],
            "unknown event": [
                start,
                self.event(event="stop", session=SESSION, at="2026-10-08T01:01:00Z"),
            ],
            "not valid JSON": ["{"],
            "not a version 1 event": [json.dumps({"event": "start"})],
            "no time zone": [
                start,
                self.event(event="pause", session=SESSION, at="2026-10-08T01:01:00"),
            ],
            "session id does not match": [
                self.event(
                    event="start",
                    session="wrong",
                    at="2026-10-08T01:00:00Z",
                    task_id=TASK,
                    model=MODEL,
                    run="run1",
                )
            ],
        }
        for message, lines in cases.items():
            with self.subTest(message), self.assertRaises(CorrectionLogError) as raised:
                replay(lines)
            self.assertIn(message, str(raised.exception))

    def test_duplicate_keys_are_rejected(self):
        line = (
            '{"v": 1, "event": "start", "event": "unchanged", "session": "s",'
            ' "at": "2026-10-08T01:00:00Z"}'
        )
        with self.assertRaises(CorrectionLogError):
            replay([line])


if __name__ == "__main__":
    unittest.main()
