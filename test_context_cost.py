"""W-181 context-cost controls: compact intake and shared contract, bounded worker sessions and
risk-based review routing. Real Git, checks and state files; fake Codex and Linear only."""
import json
from pathlib import Path
import unittest

import intake
import measure
from fixtures import TEST_REGISTRY
from runner import git
from test_supervisor import Harness

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


if __name__ == "__main__":
    unittest.main()
