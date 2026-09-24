"""Offline context-cost measurement on the fictional fixture in ``testdata/trajectory``."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import unittest.mock

from linear_runner.reporting import measure
from linear_runner import cli

FIXTURE = Path(__file__).resolve().parent / "testdata" / "trajectory"
ROOTS = [FIXTURE / "root-a", FIXTURE / "root-b"]


class MeasureTests(unittest.TestCase):
    def setUp(self):
        self.report = measure.measure(ROOTS, rollouts=FIXTURE / "rollouts")

    def test_intake_components_add_up_to_the_file(self):
        intake = self.report["issues"]["TEAM-1"]["intakes"][0]
        parts = intake["components"]
        self.assertEqual(parts["total"], Path(intake["path"]).stat().st_size)
        self.assertEqual(sum(v for k, v in parts.items() if k != "total"), parts["total"])
        self.assertGreater(parts["context_files"], parts["issue_description"])  # the inlined contract dominates
        self.assertEqual((intake["unchecked_criteria"], intake["versions"]), (2, 1))

    def test_invocation_input_uses_session_deltas(self):
        rows = self.report["issues"]["TEAM-2"]["invocations"]
        self.assertEqual([r["input_added"] for r in rows], [300_000, 1_700_000, 400_000, 180_000])
        self.assertEqual(self.report["totals"]["input_added_by_phase"],
                         {"implement": 3_000_000, "review": 630_000, "repair": 400_000})

    def test_rollout_growth_is_exact_bookkeeping(self):
        growth = self.report["issues"]["TEAM-2"]["invocations"][0]["rollout"]
        self.assertEqual((growth["calls"], growth["first_context"], growth["last_context"], growth["input"]),
                         (4, 10_000, 24_000, 72_000))
        self.assertEqual(growth["prefix"], 40_000)
        self.assertEqual(growth["growth"]["intake_or_context_reads"], 24_000)
        self.assertEqual(growth["growth"]["file_reads_and_search"], 4_000)
        self.assertEqual(growth["growth"]["test_and_check_runs"], 4_000)
        self.assertEqual(growth["prefix"] + sum(growth["growth"].values()), growth["input"])
        # The rollout covers only the first invocation's time window.
        self.assertNotIn("rollout", self.report["issues"]["TEAM-2"]["invocations"][1])

    def test_compaction_is_reported_separately(self):
        calls = [{"context": c, "after": [], "after_output_bytes": 0} for c in (100, 300, 50, 80)]
        growth = measure.context_growth(calls, ["intake.json"])
        self.assertEqual(growth["growth"]["context_compaction"], -250 * 2)
        self.assertEqual(growth["prefix"] + sum(growth["growth"].values()), growth["input"])

    def test_classification(self):
        markers = ["intake.json", "contract.md"]
        self.assertEqual(measure.classify(["cat run/intake.json"], markers), "intake_or_context_reads")
        self.assertEqual(measure.classify(["uv run pytest test/ -q"], markers), "test_and_check_runs")
        self.assertEqual(measure.classify(["rg -n foo src; sed -n 1,80p a.py"], markers), "file_reads_and_search")
        self.assertEqual(measure.classify(["python3 - <<'PY'\nprint(1)\nPY"], markers), "other_commands")
        self.assertEqual(measure.classify([], markers), "model_turns_without_tools")

    def test_measure_command_writes_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.json"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cli.main(["measure", "--runs", *map(str, ROOTS), "--issues", "TEAM-2",
                             "--rollouts", str(FIXTURE / "rollouts"), "--json", str(path)])
            saved = json.loads(path.read_text())
        self.assertEqual(list(saved["issues"]), ["TEAM-2"])
        self.assertIn("## Intake packets", output.getvalue())
        self.assertEqual(saved["totals"]["rollout"]["input"], 72_000)

    def test_session_log_location_defaults_and_missing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with unittest.mock.patch.dict("os.environ", {"CODEX_HOME": directory}):
                self.assertEqual(measure.default_rollouts(), str(Path(directory) / "sessions"))
                status = measure.rollout_location()
                self.assertFalse(status["found"])
                self.assertIn("not found at", status["note"])
                err, out = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    cli.main(["measure", "--runs", *map(str, ROOTS), "--issues", "TEAM-2"])
                self.assertIn("per-call context growth is omitted", err.getvalue())
                self.assertIn("per-call context growth is omitted", out.getvalue())
                (Path(directory) / "sessions").mkdir()
                self.assertTrue(measure.rollout_location()["found"])
        self.assertEqual(measure.rollout_location(disabled=True)["location"], None)
        self.assertEqual(measure.rollout_location(str(FIXTURE / "rollouts"))["found"], True)


if __name__ == "__main__":
    unittest.main()
