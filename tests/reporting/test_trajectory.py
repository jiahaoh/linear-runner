"""Deterministic trajectory/usage renderer against a checked-in fixture of fictional runner records.

The fixture (``testdata/trajectory``) has the shape of saved production evidence: two evidence
roots that overlap because run directories were copied, a legacy ``role`` session record, a
session resumed twice (implement, implement, repair), a failed then passing validation with a
reused check, delivery checks, a session without usage telemetry and an unfinished review.
The expected totals were computed by hand.
"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from linear_runner.reporting import records
from linear_runner import cli
from tests.fixtures import CHECKOUT
from linear_runner.reporting import trajectory

FIXTURE = CHECKOUT / "testdata" / "trajectory"
ROOTS = [FIXTURE / "root-a", FIXTURE / "root-b"]
GROUPS = {"complete": ["TEAM-1", "TEAM-2"], "with unknown": ["TEAM-1", "TEAM-2", "TEAM-3"]}


class TrajectoryTests(unittest.TestCase):
    def setUp(self):
        self.result = trajectory.from_roots(ROOTS, groups=GROUPS)

    def test_reproduces_recorded_totals_exactly(self):
        expected_trajectory = json.loads((FIXTURE / "expected-trajectory.json").read_text())
        expected_comparison = json.loads((FIXTURE / "expected-comparison.json").read_text())
        rows = trajectory.compare_recorded(self.result, expected_trajectory)
        self.assertEqual(len(rows), 3 * (7 + 4))
        self.assertEqual([r for r in rows if not r["match"]], [])
        for recorded, rendered in zip(expected_comparison, self.result["comparison"]):
            rows = trajectory.compare_recorded(dict(self.result, comparison=[rendered]), comparison=recorded)
            self.assertTrue(rows)
            self.assertEqual([r for r in rows if not r["match"]], [], recorded["batch"])

    def test_copies_are_deduplicated_and_listed(self):
        invocations = records.find_invocations(ROOTS)
        self.assertEqual(len(invocations), 9)  # 8 finished + 1 pending; TEAM-1 copies counted once
        first = next(i for i in invocations if i["session_id"] == "sess-t1-impl")
        self.assertEqual(len(first["copies"]), 1)
        self.assertEqual(first["phase"], "implement")  # legacy "role": "worker" record
        checks = records.find_checks(ROOTS)
        unit = next(c for c in checks if c["issue"] == "TEAM-1" and c["name"] == "unit")
        self.assertEqual((len(unit["copies"]), unit["hash_matches"], unit["rss_kib"]), (1, True, 1000))

    def test_resumed_session_counts_once_with_deltas(self):
        rows = [a for a in self.result["attempts"] if a["session_id"] == "sess-t2-impl"]
        self.assertEqual([a["phase"] for a in rows], ["implement", "implement", "repair"])
        self.assertEqual([a["usage_delta"]["input_tokens"] for a in rows], [300_000, 1_700_000, 400_000])
        session = next(s for s in self.result["sessions"] if s["session_id"] == "sess-t2-impl")
        self.assertEqual((session["invocations"], session["counter"]["input_tokens"], session["monotonic"]),
                         (3, 2_400_000, True))

    def test_unknown_stays_unknown_and_pending_is_excluded(self):
        team3 = next(s for s in self.result["summaries"] if s["issue"] == "TEAM-3")
        self.assertIsNone(team3["usage"]["input_tokens"])
        self.assertEqual((team3["attempts"], team3["pending"]), (1, 1))
        self.assertEqual([p["attempt"] for p in self.result["pending"]], ["review-20260101T041000Z-0000003c"])
        self.assertIsNone(self.result["comparison"][1]["input_tokens"])

    def test_reused_checks_add_no_time_and_delivery_is_separate(self):
        team2 = next(s for s in self.result["summaries"] if s["issue"] == "TEAM-2")
        self.assertEqual(team2["validation_seconds"], 110.0)
        team1 = next(s for s in self.result["summaries"] if s["issue"] == "TEAM-1")
        self.assertEqual(team1["delivery_check_seconds"], 5.0)

    def test_cutoff_excludes_later_invocations(self):
        early = trajectory.from_roots(ROOTS, issues=["TEAM-1"], until="2026-01-01T00:30:00+00:00")
        summary = early["summaries"][0]
        self.assertEqual((summary["attempts"], summary["usage"]["input_tokens"]), (2, 1_200_000))

    def test_mismatch_is_reported(self):
        recorded = {"summaries": [{"issue": "TEAM-1", "attempts": 99,
                                   "usage": {"input_tokens": 1_450_000}}]}
        rows = trajectory.compare_recorded(self.result, recorded)
        self.assertEqual([(r["field"], r["match"]) for r in rows], [("attempts", False), ("usage.input_tokens", True)])

    def test_markdown_html_and_json_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = trajectory.write(directory, self.result, reproduction=[])
            markdown = Path(paths["md"]).read_text()
            html = Path(paths["html"]).read_text()
            payload = json.loads(Path(paths["json"]).read_text())
        self.assertIn("| TEAM-2 | 4 | 2 | 2 |", markdown)
        self.assertIn("## Pending", markdown)
        self.assertIn("<h2>Validation audit</h2>", html)
        self.assertNotIn("<script", html)
        self.assertEqual(payload["schema"], trajectory.SCHEMA)
        self.assertEqual(payload["reproduction"], [])

    def test_report_command_is_offline_and_needs_no_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["report", "--runs", *map(str, ROOTS), "--out", str(out), "--group",
                             "complete=TEAM-1,TEAM-2", "--check-trajectory",
                             str(FIXTURE / "expected-trajectory.json")])
            payload = json.loads((out / "trajectory.json").read_text())
        self.assertEqual(payload["comparison"][0]["input_tokens"], 4_030_000)
        self.assertTrue(all(r["match"] for r in payload["reproduction"]))



class UpperBoundTests(unittest.TestCase):
    """The W-193 shape: a Codex implement turn failed before any completed turn (no counter);
    the resumed turn in the same session completed with a cumulative counter."""

    RUN = "20260101T050000Z-0000a001"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for root in ("evidence", "copy"):  # a copied run directory must not count twice
            run = self.root / root / "TEAM-7" / self.RUN
            self.session(run / "implement-20260101T050001Z-0000a002", "sess-t7-impl", "05:00:01", "05:02:00", None)
            self.session(run / "implement-20260101T051000Z-0000a003", "sess-t7-impl", "05:10:00", "05:30:00",
                         {"input_tokens": 1_602_748, "cached_input_tokens": 1_499_392, "output_tokens": 39_173,
                          "reasoning_output_tokens": 22_663})
            self.session(run / "review-20260101T053500Z-0000a004", "sess-t7-review", "05:35:00", "05:40:00",
                         {"input_tokens": 200_000, "cached_input_tokens": 150_000, "output_tokens": 3_000,
                          "reasoning_output_tokens": 1_000})
        self.roots = [self.root / "evidence", self.root / "copy"]

    @staticmethod
    def session(directory, session, start, end, counter):
        directory.mkdir(parents=True)
        (directory / "session.json").write_text(json.dumps({
            "session_id": session, "started_at": f"2026-01-01T{start}+00:00", "finished_at": f"2026-01-01T{end}+00:00",
            "wall_seconds": 60.0, "exit_code": 0 if counter else 1,
            "execution_evidence": {"usage_events": [{"usage": counter}] if counter else None}}))

    def test_resumed_delta_is_an_upper_bound_and_totals_count_once(self):
        result = trajectory.from_roots(self.roots)
        rows = [a for a in result["attempts"] if a["session_id"] == "sess-t7-impl"]
        self.assertEqual([a["usage_basis"] for a in rows], ["unavailable", "cumulative-upper-bound"])
        self.assertEqual(rows[1]["usage_delta"]["input_tokens"], 1_602_748)
        review = next(a for a in result["attempts"] if a["phase"] == "review")
        self.assertEqual(review["usage_basis"], "delta")
        summary = result["summaries"][0]
        self.assertEqual((summary["attempts"], summary["sessions"], summary["usage"]["input_tokens"],
                          summary["usage"]["output_tokens"]), (3, 2, 1_802_748, 42_173))
        self.assertEqual(result["comparison"][0]["input_tokens"], 1_802_748)
        markdown = trajectory.render_markdown(result)
        self.assertIn("| ≤ 1,602,748 |", markdown)
        self.assertIn("| 200,000 |", markdown)  # the exact review delta has no mark
        self.assertIn("upper bound", markdown)
        self.assertIn("≤ 1,602,748", trajectory.render_html(result))

    def test_measure_marks_the_upper_bound(self):
        from linear_runner.reporting import measure
        report = measure.measure(self.roots)
        rows = report["issues"]["TEAM-7"]["invocations"]
        self.assertEqual([(r["usage_basis"], r["input_added"]) for r in rows],
                         [("unavailable", None), ("cumulative-upper-bound", 1_602_748), ("delta", 200_000)])
        self.assertEqual(report["totals"]["input_added_upper_bound_phases"], ["implement"])
        self.assertIn("≤ 1,602,748", measure.render_markdown(report))


if __name__ == "__main__":
    unittest.main()
