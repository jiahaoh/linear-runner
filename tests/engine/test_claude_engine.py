"""The engine's guarantees hold for Claude-backed pools as for Codex: resume, the repair limit
and single escalation, frozen source, schema-bound review validation, outbox drafts and usage
attribution. Real Git, checks, argv and stream-json parsing; a fake `claude` executable and a
fake Linear. No model, network or Linear."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from linear_runner.config import load_config, pin_resolution
from linear_runner.engine.runner import Runner, git, resolve_profile, usage_totals
from linear_runner.linear.attention import classify_stop
from tests.fixtures import TEST_REGISTRY, FakeLinear, fake_claude, fake_claude_log, make_home, set_pools
from tests.linear.test_updates import GOOD_PROGRESS

OPUS_MEDIUM = {"backend": "claude", "model": "claude-opus-5-5", "effort": "medium"}
OPUS_HIGH = {"backend": "claude", "model": "claude-opus-5-5", "effort": "high"}
ASTRA_HIGH = {"backend": "codex", "model": "astra", "effort": "high"}


def claude_registry(deep=(OPUS_HIGH, ASTRA_HIGH)):
    registry = copy.deepcopy(TEST_REGISTRY)
    registry["models"]["models"]["claude-opus-5-5"] = {"backend": "claude", "efforts": ["medium", "high"]}
    registry["profiles"]["phase_overrides"] = {}
    set_pools(registry, "Standard", {phase: [OPUS_MEDIUM] for phase in ("implement", "repair", "review")})
    set_pools(registry, "Deep", {phase: list(deep) for phase in ("implement", "repair", "review")})
    return registry


class ClaudeEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q"); git(self.repo, "checkout", "-q", "-b", "codex/test")
        git(self.repo, "config", "user.name", "Test"); git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("fixture")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "baseline")
        self.linear = FakeLinear()

    def runner(self, steps, registry=None, check="ready"):
        executable, self.plan = fake_claude(self.root, steps)
        project = None
        if check != "ready":
            project = {"checks": [{"name": "output", "kind": "code", "tier": "default", "inputs": ["result.txt"], "cwd": ".",
                                   "command": [sys.executable, "-c", "from pathlib import Path; "
                                               f"assert Path('result.txt').read_text() == {check!r}"]}]}
        home, batch = make_home(self.root, self.repo, registry=registry or claude_registry(), project=project,
                                site={"executables": {"codex": "codex", "claude": str(executable), "python": sys.executable}})
        config, _ = pin_resolution(load_config(batch, home), self.linear)
        return Runner(config, self.linear)

    def calls(self):
        return fake_claude_log(self.plan)

    def sessions(self, runner):
        run = Path(runner.state["history"][0]["run_dir"] if runner.state["history"] else runner.state["active"]["run_dir"])
        return {p.parent.name.split("-")[0] + ":" + p.parent.name: json.loads(p.read_text())
                for p in sorted(run.glob("*/session.json"))}

    def test_lifecycle_records_selection_backend_and_claim(self):
        runner = self.runner([{"write": {"result.txt": "ready"}}, {}])
        runner.execute(limit=1)
        self.assertEqual(self.linear.data["statusType"], "completed")
        calls = self.calls()
        self.assertEqual([c["mode"] for c in calls], ["acceptEdits", "dontAsk"])
        self.assertEqual([(c["model"], c["effort"]) for c in calls], [("claude-opus-5-5", "medium")] * 2)
        self.assertTrue(all(c["resume"] is None for c in calls))
        for meta in self.sessions(runner).values():
            self.assertEqual(meta["backend"], "claude")
            self.assertEqual(meta["selection"]["model_source"], "pool default")
            self.assertEqual(meta["selection"]["pool"], meta["selection"]["pool"].split("/")[0] + "/Standard/" + meta["phase"])
            self.assertEqual(meta["backend_details"]["auth"], {"loggedIn": True, "authMethod": "claude.ai"})
        # Each stage comment names the model, effort and backend that ran it.
        opus = "claude-opus-5-5 (medium effort, Claude)"
        self.assertIn(f"Implementation runs with {opus}. Repairs, if needed, use the same model. "
                      f"The review runs with {opus}.", self.linear.last("DEV-1", "claim"))
        self.assertIn(f"Implemented with {opus}.", self.linear.last("DEV-1", "ready"))
        self.assertIn(f"Reviewed with {opus}.", self.linear.last("DEV-1", "review"))
        self.assertIn(f"It was implemented with {opus} and reviewed with {opus}.", self.linear.last("DEV-1", "done"))

    def test_repairs_resume_the_session_then_the_limit_and_one_escalation_hold(self):
        runner = self.runner([{"write": {"result.txt": "ready"}}, {"write": {"result.txt": "still"}},
                              {"write": {"result.txt": "nope"}}], check="fixed")
        with self.assertRaisesRegex(RuntimeError, "repair limit|Repeated") as caught:
            runner.execute(limit=1)
        self.assertEqual(classify_stop(caught.exception), "technical-block")
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        first = calls[0]["session"]
        # Both repairs resume the implement session; the second runs escalated, on the Deep pool's first entry.
        self.assertEqual([c["resume"] for c in calls[1:]], [first, first])
        self.assertEqual([(c["model"], c["effort"]) for c in calls],
                         [("claude-opus-5-5", "medium"), ("claude-opus-5-5", "medium"), ("claude-opus-5-5", "high")])
        active = runner.state["active"]
        self.assertEqual((active["repairs"], active["escalation"], active["session_backend"]), (2, "Deep", "claude"))
        validations = [b for b, k in zip(self.linear.bodies("DEV-1"), self.linear.kinds("DEV-1")) if k == "validation"]
        self.assertIn("Repair 1 runs with claude-opus-5-5 (medium effort, Claude).", validations[0])
        self.assertIn("Repair 2 runs with claude-opus-5-5 (high effort, Claude), escalated to the Deep profile.",
                      validations[1])
        self.assertEqual([(s["phase"], s["effort"]) for s in active["stages"]],
                         [("implement", "medium"), ("repair", "medium"), ("repair", "high")])
        self.assertIn("The repair phase ran with claude-opus-5-5 (high effort, Claude).", runner.blocked_body(
            {"issue": "DEV-1", "class": "technical-block", "event": "checks_failed", "error": "limit", "step": "repair"}))
        # A recovery comment names the model the next phase would run with.
        from linear_runner.linear import messages
        planned = runner.planned_selection(active, "repair")
        self.assertEqual((planned["profile"], planned["effort"]), ("Deep", "high"))
        body = messages.recovery(runner.ctx, record={"kind": "resume", "authorized_by": "Owner", "reason": "retry",
                                                     "details": {"issue": "DEV-1"}}, step="repair", stage=planned)
        self.assertIn("The repair phase runs with claude-opus-5-5 (high effort, Claude).", body)
        with self.assertRaisesRegex(RuntimeError, "repair limit|Repeated"):
            runner.execute(limit=1, resume=True)
        self.assertEqual(len(self.calls()), 3)
        # Usage attribution: one session, three invocations; counters cumulative, each counted once.
        records = list(self.sessions(runner).values())
        self.assertEqual(usage_totals(records)["totals"]["input_tokens"], 300)
        self.assertEqual(usage_totals(records)["sessions"], 1)
        deltas = [json.loads(p.read_text())["input_tokens"] for p in sorted(Path(active["run_dir"]).glob("*/phase-usage.json"))]
        self.assertEqual(deltas, [100, 100, 100])

    def test_escalation_to_another_backend_starts_a_fresh_session_from_a_handoff(self):
        runner = self.runner([{"write": {"result.txt": "ready"}}], registry=claude_registry(deep=(ASTRA_HIGH,)))
        # An escalated repair of a Claude session whose Deep pool runs on Codex.
        active = {"issue_id": "DEV-1", "issue": self.linear.issue("DEV-1"), "session_id": "claude-session",
                  "session_backend": "claude", "escalation": "Deep", "run_dir": str(self.root / "runs" / "x"),
                  "starting_commit": git(self.repo, "rev-parse", "HEAD")}
        Path(active["run_dir"]).mkdir(parents=True)
        runner.state["active"] = active
        resume, seed, record = runner.worker_session(active, "repair")
        self.assertIsNone(resume)
        self.assertIn("runs on the codex backend, not claude", record["reason"])
        self.assertIn("fresh session", seed)
        self.assertIsNone(active["session_id"])

    def test_review_cannot_change_frozen_source(self):
        runner = self.runner([{"write": {"result.txt": "ready"}}, {"write": {"README.md": "reviewer edit"}}])
        with self.assertRaisesRegex(RuntimeError, "Frozen validated source changed"):
            runner.execute(limit=1)
        self.assertNotEqual(self.linear.data["statusType"], "completed")

    def test_schema_bound_review_is_validated_independently(self):
        runner = self.runner([{"write": {"result.txt": "ready"}}, {"acceptance": []}])
        with self.assertRaisesRegex(RuntimeError, "omitted original checklist criteria") as caught:
            runner.execute(limit=1)
        self.assertEqual(caught.exception.event, "review_blocked")
        review = self.calls()[1]["argv"]
        schema = json.loads(review[review.index("--json-schema") + 1])
        self.assertEqual(schema["properties"]["issue_id"]["enum"], ["DEV-1"])
        self.assertNotIn("deliverables", schema["properties"])
        self.assertNotEqual(self.linear.data["statusType"], "completed")

    def test_outbox_progress_draft_is_posted(self):
        runner = self.runner([{"write": {"result.txt": "ready"}, "outbox": GOOD_PROGRESS}, {}])
        runner.execute(limit=1)
        self.assertIn("progress", self.linear.kinds("DEV-1"))
        self.assertIn("The QC report now renders for all tiles", self.linear.last("DEV-1", "progress"))

    def test_error_result_stops_as_environment_without_retry(self):
        runner = self.runner([{"error": True}])
        with self.assertRaisesRegex(RuntimeError, "Claude failed or did not finish a turn: error result") as caught:
            runner.execute(limit=1)
        self.assertEqual(classify_stop(caught.exception), "environment")
        self.assertEqual(len(self.calls()), 1)


if __name__ == "__main__":
    unittest.main()
