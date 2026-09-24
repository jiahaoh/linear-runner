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

import records
import runner
import trajectory

FIXTURE = Path(__file__).resolve().parent / "testdata" / "trajectory"
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
                runner.main(["report", "--runs", *map(str, ROOTS), "--out", str(out), "--group",
                             "complete=TEAM-1,TEAM-2", "--check-trajectory",
                             str(FIXTURE / "expected-trajectory.json")])
            payload = json.loads((out / "trajectory.json").read_text())
        self.assertEqual(payload["comparison"][0]["input_tokens"], 4_030_000)
        self.assertTrue(all(r["match"] for r in payload["reproduction"]))


if __name__ == "__main__":
    unittest.main()
