"""Capacity retries through real fake-CLI subprocesses, state and comments; no network."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from linear_runner.engine.runner import Runner, SessionFailure, write_json
from linear_runner.config import load_config, pin_resolution
from linear_runner.linear.attention import classify_stop
from tests.fixtures import FakeLinear, TEST_REGISTRY, make_home

CAPACITY = "Selected model is at capacity; Please try a different model."


class CapacityRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.plan = self.root / "plan.json"
        fake = self.root / "fake-codex"
        fake.write_text(f"#!{sys.executable}\n" + "PLAN = " + repr(str(self.plan)) + "\n" + r'''
import json, pathlib, sys
plan_path = pathlib.Path(PLAN)
plan = json.loads(plan_path.read_text())
args = sys.argv[1:]
prompt = sys.stdin.read()
index = len(plan.setdefault("calls", []))
plan["calls"].append({"argv": args, "prompt": prompt})
step = plan["steps"][index]
plan_path.write_text(json.dumps(plan))
if step.get("session", True):
    print(json.dumps({"type": "thread.started", "thread_id": "test-session"}), flush=True)
if step.get("error"):
    print(json.dumps({"type": "turn.failed", "error": {"message": step["error"]}}), flush=True)
    sys.exit(1)
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps({"issue_id": "DEV-1", "status": "ready",
    "summary": "Done", "commit": "", "acceptance": [], "limitations": [], "deliverables": []}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}), flush=True)
''')
        fake.chmod(0o755)
        home, batch = make_home(self.root, self.repo, registry=TEST_REGISTRY,
                               site={"executables": {"codex": str(fake), "python": sys.executable}})
        self.linear = FakeLinear()
        config, _ = pin_resolution(load_config(batch, home), self.linear)
        self.runner = Runner(config, self.linear)
        # Availability was established before model_phase, without calling a real CLI.
        self.runner.verify_model = lambda selection: None
        self.active = {"issue_id": "DEV-1", "issue": self.linear.issue("DEV-1"), "step": "implement",
                       "repairs": 1, "escalation": None, "run_dir": str(self.root / "run"),
                       "session_id": "worker-session", "session_backend": "codex"}
        self.runner.save(active=self.active)
        self.pauses = []

    def run_phase(self, steps, phase="implement", **kwargs):
        write_json(self.plan, {"steps": steps})
        def pause(seconds):
            # State and announcement are durable BEFORE waiting, with no model child.
            self.assertIsNone(self.runner.child)
            state = json.loads(self.runner.state_path.read_text())
            self.assertEqual(state["active"]["capacity_retries"][-1]["delay_seconds"], seconds)
            self.assertEqual(self.linear.kinds("DEV-1")[-1], "capacity-retry")
            self.pauses.append(seconds)
        with patch.object(self.runner, "wait_for_capacity", side_effect=pause):
            return self.runner.model_phase(self.active, phase, "Reply with a result.", **kwargs)

    def calls(self):
        return json.loads(self.plan.read_text())["calls"]

    def records(self):
        return [json.loads(Path(s["attempt"], "session.json").read_text()) for s in self.active["stages"]]

    def test_capacity_then_success_starts_fresh_without_a_session(self):
        result = self.run_phase([{"error": CAPACITY, "session": False}, {}])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.pauses, [120])
        self.assertEqual(len(self.calls()), 2)
        self.assertNotIn("resume", self.calls()[1]["argv"])
        self.assertEqual(self.active["repairs"], 1)
        self.assertIsNone(self.active["escalation"])
        record = self.active["capacity_retries"][0]
        self.assertEqual(record["failure"], CAPACITY)
        self.assertIsNone(record["session_id"])
        self.assertNotEqual(record["failed_attempt"], record["retry_attempt"])
        self.assertEqual(self.records()[1]["capacity_retry"]["failure"], CAPACITY)
        self.assertIn("Retry 1 of 2 runs after 120 seconds", self.linear.last("DEV-1", "capacity-retry"))
        for s in self.active["stages"]:
            directory = Path(s["attempt"])
            self.assertTrue((directory / "events.jsonl").exists())
            self.assertIn(str(directory / "outbox"), (directory / "prompt.txt").read_text())
        self.assertEqual([(m["phase"], m["requested_model"], m["requested_reasoning_effort"])
                          for m in self.records()], [("implement", "astra", "medium")] * 2)

    def test_capacity_on_every_try_stops_after_two_retries_with_the_same_recovery(self):
        with self.assertRaises(SessionFailure) as caught:
            self.run_phase([{"error": CAPACITY}] * 3)
        self.assertEqual(self.pauses, [120, 600])
        self.assertEqual((len(self.calls()), len(self.records())), (3, 3))
        self.assertEqual(classify_stop(caught.exception), "environment")
        self.assertEqual(self.active["repairs"], 1)
        self.assertEqual(self.linear.kinds("DEV-1"), ["capacity-retry", "capacity-retry"])
        self.assertIn("Codex failed or did not finish a turn: " + CAPACITY, str(caught.exception))
        stop = self.runner.record_stop(caught.exception)
        body = self.runner.blocked_body(stop)
        self.assertIn("recover resume", body)

    def test_another_failure_stops_without_retry(self):
        with self.assertRaises(SessionFailure) as caught:
            self.run_phase([{"error": "Authentication failed"}])
        self.assertEqual(classify_stop(caught.exception), "environment")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual((self.pauses, self.linear.posts), ([], []))
        self.assertNotIn("capacity_retries", self.active)

    def test_exhausted_review_keeps_worker_session_and_review_recovery(self):
        self.active["step"] = "review"
        with self.assertRaises(SessionFailure) as caught:
            self.run_phase([{"error": CAPACITY}] * 3, "review", writable=False)
        self.assertEqual(self.active["session_id"], "worker-session")
        self.assertEqual(self.active["repairs"], 1)
        self.assertEqual(self.pauses, [120, 600])
        stop = self.runner.record_stop(caught.exception)
        self.assertIn("recover review", self.runner.blocked_body(stop))

    def test_capacity_after_session_started_resumes_it_for_each_phase(self):
        for phase in ("implement", "repair", "review"):
            with self.subTest(phase=phase):
                self.active["session_id"] = "worker-session"
                self.active["step"] = phase
                self.run_phase([{"error": CAPACITY}, {}], phase, writable=phase != "review")
                self.assertIn("resume", self.calls()[1]["argv"])
                self.assertIn("test-session", self.calls()[1]["argv"])
                record = self.active["capacity_retries"][-1]
                self.assertEqual(record["session_id"], "test-session")
                self.assertEqual(record["phase"], phase)
                self.assertEqual(self.active["repairs"], 1)
                if phase == "review":
                    self.assertEqual(self.active["session_id"], "worker-session")

    def test_stop_during_pause_starts_no_retry(self):
        def stop(seconds):
            (self.runner.root / "STOP").touch()
            Runner.wait_for_capacity(self.runner, seconds)
        with patch.object(self.runner, "wait_for_capacity", side_effect=stop):
            write_json(self.plan, {"steps": [{"error": CAPACITY}, {}]})
            with self.assertRaisesRegex(RuntimeError, "STOP marker"):
                self.runner.model_phase(self.active, "implement", "Reply with a result.")
        self.assertEqual(len(self.calls()), 1)

    def test_pause_has_heartbeat_and_is_outside_child_timeout(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        with patch("linear_runner.engine.runner.time.monotonic", side_effect=lambda: clock[0]), \
                patch("linear_runner.engine.runner.time.sleep", side_effect=sleep), \
                patch.object(self.runner, "save", wraps=self.runner.save) as save:
            self.runner.wait_for_capacity(600)
        self.assertEqual(clock[0], 600)
        self.assertGreaterEqual(save.call_count, 19)

    def test_claude_has_no_capacity_pattern(self):
        backend = self.runner.backend("claude")
        self.assertFalse(backend.at_capacity([{"type": "result", "is_error": True,
            "subtype": "error_during_execution", "result": CAPACITY}]))
