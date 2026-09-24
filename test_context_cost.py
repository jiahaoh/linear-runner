"""W-181 context-cost controls: compact intake and shared contract, bounded worker sessions and
risk-based review routing. Real Git, checks and state files; fake Codex and Linear only."""
import copy
import json
from pathlib import Path
import sys
import unittest

import intake
import measure
from fixtures import TEST_REGISTRY
from runner import git, usage_totals, write_json
from test_supervisor import Harness
import trajectory

FIXTURE = Path(__file__).resolve().parent / "testdata" / "trajectory"


class CompactIntakeTests(Harness):
    PROJECT = {"contract_file": "../contract.md", "context_files": ["../notes.md"],
               "guidance_files": ["../guidance.md", "../contract.md"]}

    def setUp(self):
        super().setUp()
        (self.home / "contract.md").write_text("# Shared contract\n\nEvery issue records its evidence.\n" * 20)
        (self.home / "notes.md").write_text("Background notes. " * 200)

    def run_dir(self, issue="DEV-1"):
        return next(p for p in (self.root / "runs" / issue).iterdir() if p.is_dir())

    def test_intake_references_contract_and_context_by_hash(self):
        runner = self.make_runner()
        runner.execute(limit=1)
        run = self.run_dir()
        packet = json.loads((run / "intake.json").read_text())
        contract = self.home / "contract.md"
        self.assertEqual(packet["schema"], intake.SCHEMA)
        self.assertEqual(packet["contract"]["path"], str(contract.resolve()))
        self.assertEqual(packet["contract"]["sha256"], intake.file_reference(contract)["sha256"])
        self.assertEqual(packet["acceptance_criteria"], ["Produce validated output"])
        self.assertNotIn("Shared contract", packet["guidance"])            # not inlined as guidance
        self.assertIn("Implement a tiny fixture only.", packet["guidance"])   # other guidance stays inline
        self.assertEqual([set(c) for c in packet["context_files"]], [{"path", "bytes", "sha256"}])
        self.assertNotIn("Background notes.", (run / "intake.json").read_text())
        # The full pinned issue is kept for identity and lifecycle read-back.
        issue = json.loads((run / "issue.json").read_text())
        self.assertEqual((issue["projectId"], issue["assigneeId"]), ("p", "owner"))
        self.assertEqual(packet["issue_snapshot"]["sha256"], intake.file_reference(run / "issue.json")["sha256"])
        recorded = json.loads((run / "intake-components.json").read_text())
        self.assertEqual(recorded["total"], (run / "intake.json").stat().st_size)
        self.assertIn(str(contract.resolve()), self.prompts[0])
        self.assertIn(packet["contract"]["sha256"], self.prompts[1])  # the reviewer gets the exact version
        self.assertEqual(self.linear.data["statusType"], "completed")

    def test_contract_edited_during_the_issue_stops_before_review(self):
        def edit(result):
            (self.home / "contract.md").write_text("# Shared contract\n\nA different rule.\n")
        self.hooks[("DEV-1", "implement")] = edit
        runner = self.make_runner()
        with self.assertRaisesRegex(RuntimeError, "Shared contract .* changed since intake"):
            runner.execute(limit=1)
        self.assertEqual([c[1] for c in self.calls], ["implement"])
        self.assertNotEqual(self.linear.data["statusType"], "completed")

    def test_contract_edit_also_changes_the_pinned_configuration(self):
        runner = self.make_runner()
        runner.execute(limit=1)
        (self.home / "contract.md").write_text("changed\n")
        with self.assertRaisesRegex(Exception, "changed"):
            self.make_runner()

    def test_supervised_lifecycle_reads_the_pinned_issue(self):
        self.launch()
        readback = json.loads((self.state_dir / "lifecycle" / "DEV-1" / "readback.json").read_text())
        self.assertIn("issue.json", readback["files_sha256"])

    @property
    def state_dir(self):
        return self.root / "state" / "fixture"


class FullIntakeTests(Harness):
    PROJECT = {"intake_mode": "full", "context_files": ["../notes.md"]}

    def setUp(self):
        super().setUp()
        (self.home / "notes.md").write_text("Background notes.")

    def test_full_mode_keeps_the_schema_1_packet(self):
        self.make_runner().execute(limit=1)
        run = next(p for p in (self.root / "runs" / "DEV-1").iterdir() if p.is_dir())
        packet = json.loads((run / "intake.json").read_text())
        self.assertNotIn("schema", packet)
        self.assertEqual(list(packet["references"].values())[0]["text"], "Background notes.")
        self.assertFalse((run / "issue.json").exists())
        self.assertEqual(self.linear.data["statusType"], "completed")


class ReplayTests(unittest.TestCase):
    def test_compact_replay_of_a_saved_schema_1_intake(self):
        path = FIXTURE / "root-b" / "TEAM-2" / "20260101T020000Z-0000001a" / "intake.json"
        saved = json.loads(path.read_text())
        replay = intake.compact_from_saved(saved)
        self.assertEqual(replay["contract"]["bytes"], 6000)
        self.assertEqual(replay["acceptance_criteria"], ["Produce TEAM-2 output"])
        self.assertNotIn("stateHistory", replay["issue"])
        before = measure.intake_components(saved, path.stat().st_size)["total"]
        after = len(json.dumps(replay, indent=2).encode())
        self.assertLess(after, before / 2)


def bounded_registry(**settings):
    registry = copy.deepcopy(TEST_REGISTRY)
    registry["phases"]["bounded_sessions"] = dict({"enabled": True, "input_threshold_tokens": 1000,
                                                    "handoff_after_implement": True, "max_handoff_bytes": 4000},
                                                   **settings)
    return registry


class BoundedSessionTests(Harness):
    """Checks need 'fixed'; implement writes 'ready', so every issue needs at least one repair."""
    PROJECT = {"checks": [{"name": "output", "kind": "code", "tier": "default", "inputs": ["result.txt"], "cwd": ".",
                           "command": [sys.executable, "-c", "from pathlib import Path; "
                                       "assert Path('result.txt').read_text() == 'fixed'"]}]}
    REGISTRY = bounded_registry()
    FAILING_REPAIRS = 0
    WRITE_HANDOFF = True

    def setUp(self):
        super().setUp()
        self.linear.data["relations"] = {"blockedBy": []}
        self.repairs_seen = 0
        self.kwargs = []

    def codex(self, prompt, directory, **kwargs):
        self.kwargs.append(kwargs)
        result, events, session = super().codex(prompt, directory, **kwargs)
        phase = Path(directory).name.split("-")[0]
        # Like the real controller, record start/finish so the trajectory renderer sees the attempt.
        meta_path = Path(directory) / "session.json"
        meta = json.loads(meta_path.read_text())
        stamp = f"2026-01-01T00:{len(self.calls):02d}:00+00:00"
        meta.update(started_at=stamp, finished_at=stamp, wall_seconds=1.0)
        write_json(meta_path, meta)
        if phase == "repair":
            self.repairs_seen += 1
            # A failing repair still changes the source, so it is not an "unchanged failure".
            (self.repo / "result.txt").write_text("fixed" if self.repairs_seen > self.FAILING_REPAIRS
                                                  else f"partial {self.repairs_seen}")
        if phase in ("implement", "repair") and self.WRITE_HANDOFF:
            write_json(Path(directory) / "handoff.json", {
                "schema": "linear-runner.handoff/1", "issue_id": "DEV-1", "status": "ready",
                "summary": f"{phase} finished; result.txt written.", "changed_files": ["result.txt"],
                "criteria": [{"criterion": "Produce validated output", "state": "met", "evidence": "result.txt"}],
                "next_steps": ["Run the output check."]})
        return result, events, session

    def run_issue(self):
        runner = self.make_runner()
        runner.execute(limit=1)
        return runner

    def phases(self):
        return [(phase, kwargs.get("resume")) for (_, phase), kwargs in zip(self.calls, self.kwargs)]

    def run_dir(self):
        return next(p for p in (self.root / "runs" / "DEV-1").iterdir() if p.is_dir())


class HandoffAtBoundaryTests(BoundedSessionTests):
    REGISTRY = bounded_registry(input_threshold_tokens=10_000)

    def test_first_repair_starts_fresh_from_the_worker_handoff(self):
        self.run_issue()
        self.assertEqual(self.phases(), [("implement", None), ("repair", None), ("review", None)])
        repair_prompt = self.prompts[1]
        self.assertIn("in a fresh session", repair_prompt)
        self.assertIn("implement finished; result.txt written.", repair_prompt)
        self.assertIn("handoff.json", self.prompts[0])  # the worker was asked to write one
        run = self.run_dir()
        repair = json.loads(next(run.glob("repair-*/session.json")).read_text())
        self.assertEqual((repair["handoff"]["source"], repair["handoff"]["from_session"]),
                         ("worker", "DEV-1-implement-1"))
        self.assertEqual(repair["handoff"]["reason"], "implement to self-check boundary")
        # Usage attribution: three sessions, each counted once; the trajectory shows the switch.
        totals = usage_totals([json.loads(p.read_text()) for p in run.glob("*/session.json")])
        self.assertEqual((totals["sessions"], totals["totals"]["input_tokens"]), (3, 300))
        rows = trajectory.from_roots([run])["attempts"]
        self.assertEqual([bool(r["handoff"]) for r in rows], [False, True, False])
        self.assertEqual(self.linear.data["statusType"], "completed")


class HandoffFallbackTests(BoundedSessionTests):
    REGISTRY = bounded_registry(input_threshold_tokens=10_000)
    WRITE_HANDOFF = False

    def test_missing_worker_handoff_falls_back_to_a_runner_handoff(self):
        self.run_issue()
        run = self.run_dir()
        repair = json.loads(next(run.glob("repair-*/session.json")).read_text())
        self.assertEqual(repair["handoff"]["source"], "runner")
        self.assertEqual(repair["handoff"]["problem"], "the worker wrote no handoff.json")
        handoff = json.loads(Path(repair["handoff"]["path"]).read_text())
        self.assertEqual(handoff["issue_id"], "DEV-1")
        self.assertIn("result.txt", handoff["changed_files"])
        self.assertEqual(handoff["validation"], [{"command": "output", "outcome": "exit 1"}])
        self.assertIn('"issue_id": "DEV-1"', self.prompts[1])


class InvalidHandoffTests(BoundedSessionTests):
    REGISTRY = bounded_registry(input_threshold_tokens=10_000)

    def codex(self, prompt, directory, **kwargs):
        value = super().codex(prompt, directory, **kwargs)
        if (Path(directory) / "handoff.json").exists():
            data = json.loads((Path(directory) / "handoff.json").read_text())
            (Path(directory) / "handoff.json").write_text(json.dumps(dict(data, issue_id="DEV-9")))
        return value

    def test_handoff_for_another_issue_is_rejected(self):
        self.run_issue()
        repair = json.loads(next(self.run_dir().glob("repair-*/session.json")).read_text())
        self.assertEqual(repair["handoff"]["source"], "runner")
        self.assertIn("worker handoff rejected: issue_id 'DEV-9'", repair["handoff"]["problem"])


class ThresholdTests(BoundedSessionTests):
    REGISTRY = bounded_registry(input_threshold_tokens=50, handoff_after_implement=False)
    FAILING_REPAIRS = 1

    def test_threshold_switches_every_long_session_and_keeps_the_shared_repair_budget(self):
        runner = self.run_issue()
        # Each worker session reached 100 >= 50 input tokens, so neither repair resumes.
        self.assertEqual(self.phases(), [("implement", None), ("repair", None), ("repair", None), ("review", None)])
        run = self.run_dir()
        repairs = sorted(run.glob("repair-*/session.json"))
        metas = sorted((json.loads(p.read_text()) for p in repairs), key=lambda m: m["started_at"])
        self.assertEqual([m["handoff"]["from_session"] for m in metas], ["DEV-1-implement-1", "DEV-1-repair-2"])
        self.assertTrue(all(m["handoff"]["reason"].startswith("session input 100 reached") for m in metas))
        # Shared repair count and the single escalation survive the switches.
        self.assertEqual([m["selection"]["profile"] for m in metas], ["Economy", "Deep"])
        self.assertEqual(runner.state["history"][0]["issue_id"], "DEV-1")
        self.assertEqual(self.linear.data["statusType"], "completed")


class BelowThresholdTests(BoundedSessionTests):
    REGISTRY = bounded_registry(input_threshold_tokens=5000, handoff_after_implement=False)

    def test_session_below_threshold_is_resumed(self):
        self.run_issue()
        self.assertEqual(self.phases(), [("implement", None), ("repair", "DEV-1-implement-1"), ("review", None)])
        self.assertIn("handoff.json", self.prompts[0])


class DisabledTests(BoundedSessionTests):
    REGISTRY = TEST_REGISTRY

    def test_default_off_resumes_and_asks_for_no_handoff(self):
        self.assertFalse(json.loads((Path(__file__).resolve().parent / "registry" / "phases.json").read_text())
                         ["bounded_sessions"]["enabled"])
        self.run_issue()
        self.assertEqual(self.phases(), [("implement", None), ("repair", "DEV-1-implement-1"), ("review", None)])
        self.assertNotIn("handoff.json", self.prompts[0])


if __name__ == "__main__":
    unittest.main()
