"""Run summary and batch usage tables, rendered from saved records with the report's accounting.

The fixture (``testdata/run-summary``) holds fictional saved runner records: TEAM-12 has
implement, a failed validation, one repair in the same session, a passing validation and a
review; TEAM-13 has a failed implement turn without a counter and a blocked resumed turn.
"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import unittest.mock

from linear_runner import cli
from linear_runner.linear import messages, updates
from linear_runner.reporting import trajectory
from tests.fixtures import CHECKOUT
from tests.supervision.test_supervisor import Harness

FIXTURE = CHECKOUT / "testdata" / "run-summary"
TEAM12 = FIXTURE / "TEAM-12" / "20260923T101500Z-1a2b3c4d"
TEAM13 = FIXTURE / "TEAM-13" / "20260923T130000Z-2b3c4d5e"
CTX = {"batch": "demo", "batch_arg": "demo", "home": None, "prefix": "python3 runner.py", "mention": "",
       "branch": "codex/demo", "max_repairs": 2}
RUNNER_LIMITS = {"max_chars": 3500, "max_lines": 60, "max_first_sentence_chars": 240}


def report_json(*roots):
    """``runner.py report`` on ``roots``, as its JSON."""
    with tempfile.TemporaryDirectory() as directory:
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["report", "--runs", *map(str, roots), "--out", directory, "--format", "md"])
        return (json.loads((Path(directory) / "trajectory.json").read_text()),
                (Path(directory) / "trajectory.md").read_text())


class FormatTests(unittest.TestCase):
    def test_compact_numbers_and_times(self):
        self.assertEqual([messages.amount(v) for v in (812, 2_104, 9_949, 39_173, 999_499, 1_602_748, 16_240_000,
                                                       123_400_000)],
                         ["812", "2.1k", "9.9k", "39k", "999k", "1.60M", "16.2M", "123M"])
        self.assertEqual([messages.duration(v) for v in (45, 59.4, 1_452, 3_570, 3_900, 7_260)],
                         ["45 s", "59 s", "24 min", "1 h 00 min", "1 h 05 min", "2 h 01 min"])

    def test_stage_names(self):
        attempts = [{"phase": "implement", "exit_code": 1, "status": None}, {"phase": "implement", "status": "ready"},
                    {"phase": "repair", "status": "blocked"},
                    {"phase": "review", "status": "ready", "selection_source": "low-risk review rule"}]
        result = {"attempts": [dict(a, issue="X-1", requested_model="m", requested_effort="e", attempt=str(i),
                                    usage_delta=None, usage_basis="unavailable") for i, a in enumerate(attempts)],
                  "validation_audit": [], "summaries": [], "pending": []}
        self.assertEqual([r["stage"] for r in trajectory.run_summary(result, "X-1")["rows"]],
                         ["Implement 1 (failed)", "Implement 2", "Repair 1 (blocked)", "Lighter review"])

    def test_a_repair_of_review_findings_is_labelled_from_review(self):
        attempts = [{"phase": "implement", "status": "ready"}, {"phase": "repair", "status": "ready"},
                    {"phase": "review", "status": "blocked"},
                    {"phase": "repair", "status": "blocked", "repair_source": "review"},
                    {"phase": "repair", "status": "ready", "repair_source": "review"}, {"phase": "review", "status": "ready"}]
        rows = [dict(a, issue="X-1") for a in attempts]
        self.assertEqual(trajectory.stage_labels(rows),
                         ["Implement", "Repair 1", "Review 1 (blocked)", "Repair 2 (from review, blocked)",
                          "Repair 3 (from review)", "Review 2"])
        self.assertEqual(trajectory.stage_labels(rows, outcomes=False)[3], "Repair 2 (from review)")

    def test_unknown_is_a_dash_never_zero(self):
        unknown = trajectory.figure(None)
        self.assertEqual(messages.figure_text(unknown), "—")
        self.assertEqual(messages.figure_text(trajectory.figure(0)), "0")
        values = dict(trajectory.attempt_figures({"usage_delta": None, "usage_basis": "unavailable"}))
        self.assertEqual(messages.figure_cells(values), ["—", "—", "—", "—"])


class RunSummaryTests(unittest.TestCase):
    def test_implement_failed_validation_repair_and_review(self):
        result = trajectory.from_roots([TEAM12])
        summary = trajectory.run_summary(result, "TEAM-12")
        table, notes = messages.run_summary_table(summary)
        self.assertEqual(table, "\n".join([
            "| Stage | Model | Effort | Input (cached) | Output | Tool calls | Time |",
            "|---|---|---|---|---|---|---|",
            "| Implement | gpt-6-luna | max | 1.60M (1.50M) | 39k | 57 (2 failed) | 15 min |",
            "| Repair 1 | gpt-6-luna | max | 287k (273k) | 8.0k | 9 | 13 min |",
            "| Review | gpt-6-astra | medium | 272k (250k) | 6.2k | 31 | 5 min |",
            # Both validations ran (the failed one too); reused checks add no time: 150 + 12 + 148 + 11 s.
            "| Checks | — | — | — | — | — | 5 min |",
            "| **Total** |  |  | **2.16M (2.02M)** | **53k** | **97 (2 failed)** | **37 min** |"]))
        self.assertNotIn("≤", notes + table)
        # The total is the report's per-issue total: 880 + 760 + 285 s of model time, 321 s of checks.
        total = summary["total"]
        self.assertEqual((total["input_tokens"]["value"], total["cached_input_tokens"]["value"],
                          total["output_tokens"]["value"], total["tool_calls"]["value"], total["seconds"]["value"]),
                         (2_162_024, 2_022_016, 53_420, 97, 2_246.0))
        self.assertEqual(summary["check_seconds"], {"value": 321.0, "bound": trajectory.EXACT})
        body = messages.run_summary(CTX, issue="TEAM-12", outcome="done", summary=summary, evidence_paths=[TEAM12])
        self.assertTrue(body.startswith("TEAM-12 is Done, and this is what its 3 model attempts and the runner's "
                                        "checks used; no action is needed.\n\n| Stage |"))
        self.assertEqual(updates.lint(body, kind="runner", limits=RUNNER_LIMITS, allow_tables=True), [])
        self.assertNotIn("{", body)

    def test_unknown_and_upper_bound_attempts_and_totals_match_the_report(self):
        payload, markdown = report_json(FIXTURE)
        result = trajectory.from_roots([FIXTURE])
        summary = trajectory.run_summary(result, "TEAM-13")
        table, notes = messages.run_summary_table(summary)
        self.assertIn("| Implement 1 (failed) | gpt-6-luna | max | — | — | 3 | 2 min |", table)
        self.assertIn("| Implement 2 (blocked) | gpt-6-luna | max | ≤ 1.21M (1.11M) | ≤ 28k | 41 (1 failed) | 21 min |",
                      table)
        self.assertIn("| Checks | — | — | — | — | — | — |", table)  # no check ran
        self.assertIn("| **Total** |  |  | **≥ 1.21M (1.11M)** | **≥ 28k** | **44 (1 failed)** | **23 min** |", table)
        for mark in ("≤ marks an upper bound", "≥ marks a lower bound", "— means no figure was recorded"):
            self.assertIn(mark, notes)
        # Same figures as `runner.py report` on the same records, issue by issue and for the batch.
        for issue in ("TEAM-10", "TEAM-11", "TEAM-12", "TEAM-13"):
            reported = next(s for s in payload["summaries"] if s["issue"] == issue)
            self.assertEqual(trajectory.run_summary(result, issue)["total"], reported["totals"], issue)
        self.assertIn("| TEAM-13 | 2 | 1 | 1 | 1,375.0 | 1,865.0 | unknown | unknown | ≥ 1,210,400 | ≥ 1,105,920 | "
                      "≥ 28,344 | ≥ 15,002 | 44 | 1 |", markdown)
        batch = trajectory.batch_summary(result, {i: "Done" for i in ("TEAM-10", "TEAM-11", "TEAM-12", "TEAM-13")})
        self.assertEqual({k: v for k, v in batch["total"].items() if k != "attempts"}, payload["comparison"][0]["totals"])
        # Totals never double-count a resumed session: its latest counter counts once.
        team12 = next(s for s in payload["summaries"] if s["issue"] == "TEAM-12")
        self.assertEqual(team12["totals"]["input_tokens"]["value"], team12["usage"]["input_tokens"])

    def test_outcome_headlines(self):
        summary = trajectory.run_summary(trajectory.from_roots([TEAM13]), "TEAM-13")
        deferred = messages.run_summary(CTX, issue="TEAM-13", outcome="deferred", summary=summary)
        owner = messages.run_summary(CTX, issue="TEAM-13", outcome="set-aside", summary=summary)
        self.assertTrue(deferred.startswith("TEAM-13 was set aside, and this is what its 2 model attempts"))
        self.assertTrue(owner.startswith("TEAM-13 was set aside by its owner, and this is what its 2 model attempts"))
        for body in (deferred, owner):
            self.assertEqual(updates.lint(body, kind="runner", limits=RUNNER_LIMITS, allow_tables=True), [])


class BatchTableTests(unittest.TestCase):
    def test_one_row_per_issue_and_a_bold_batch_total(self):
        result = trajectory.from_roots([FIXTURE])
        usage = trajectory.batch_summary(result, {"TEAM-10": "Done", "TEAM-11": "Done", "TEAM-12": "Done",
                                                  "TEAM-13": "Set aside", "TEAM-14": "Not started"})
        table, _ = messages.batch_table(usage)
        lines = table.splitlines()
        self.assertEqual(lines[0], "| Issue | Outcome | Attempts | Input (cached) | Output | Tool calls | Time |")
        self.assertEqual(lines[3], "| TEAM-11 | Done | 2 | 958k (910k) | 22k | 49 (1 failed) | 17 min |")
        self.assertEqual(lines[5], "| TEAM-13 | Set aside | 2 | ≥ 1.21M (1.11M) | ≥ 28k | 44 (1 failed) | 23 min |")
        self.assertEqual(lines[6], "| TEAM-14 | Not started | 0 | — | — | — | — |")
        self.assertEqual(lines[7], "| **Batch total** |  | **9** | **≥ 5.14M (4.78M)** | **≥ 123k** | "
                                   "**230 (4 failed)** | **1 h 33 min** |")
        body = messages.batch_finished(CTX, outcome="partial", done=["TEAM-10", "TEAM-11", "TEAM-12"], total=5,
                                       issues="Done: TEAM-10, TEAM-11 and TEAM-12.", usage=usage, deferred=["TEAM-13"])
        self.assertIn("**Usage per issue**\n\n| Issue | Outcome |", body)
        self.assertEqual(updates.lint(body, kind="runner", limits=RUNNER_LIMITS, allow_commands=True,
                                      allow_tables=True), [])
        fallback = messages.batch_finished(CTX, outcome="complete", done=[], total=1, issues="None.")
        self.assertIn("The usage table could not be rendered", fallback)


class TableLintTests(unittest.TestCase):
    BODY = "The run used this much; no action is needed.\n\n{table}\n\nEvidence: /absolute/path/to/runs"

    def lint(self, table, **kwargs):
        return updates.lint(self.BODY.format(table=table), kind="runner", limits=RUNNER_LIMITS, **kwargs)

    def test_well_formed_tables_are_allowed_only_in_runner_comments(self):
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        self.assertEqual(self.lint(table, allow_tables=True), [])
        self.assertIn("line 3: tables are not allowed", self.lint(table))
        draft = updates.draft_template("progress")
        self.assertIn("line 3: tables are not allowed",
                      updates.lint(self.BODY.format(table=table), kind="progress", limits=RUNNER_LIMITS,
                                   sections=draft["sections"]))

    def test_malformed_tables_and_records_in_cells_are_rejected(self):
        self.assertIn("line 3: a table needs a header row, a delimiter row and at least one row",
                      self.lint("| A | B |\n| 1 | 2 |", allow_tables=True))
        self.assertIn("line 3: every table row needs 2 cells", self.lint("| A | B |\n|---|---|\n| 1 |", allow_tables=True))
        self.assertIn("line 5: long hashes are not allowed; put them in artifacts",
                      self.lint("| A | B |\n|---|---|\n| " + "a" * 40 + " | 2 |", allow_tables=True))
        glued = updates.lint("One sentence here.\n\nText\n| A |\n|---|\n| 1 |", kind="runner", limits=RUNNER_LIMITS,
                             allow_tables=True)
        self.assertIn("line 4: a table must be its own paragraph (blank lines around it)", glued)

    def test_table_lines_do_not_count_against_prose_limits(self):
        rows = "\n".join(f"| TEAM-{i} | Done | 2 | 1.60M (1.50M) | 39k | 57 | 15 min |" for i in range(80))
        table = "| Issue | Outcome | Attempts | Input (cached) | Output | Tool calls | Time |\n|---|---|---|---|---|---|---|\n" + rows
        self.assertEqual(self.lint(table, allow_tables=True), [])
        self.assertTrue(any(p.startswith("too long") for p in self.lint(table + "\n\n" + "word " * 800,
                                                                          allow_tables=True)))


class Recorded(Harness):
    """The supervisor harness with fake sessions that record what real ones do: start and
    finish times, wall time, a cumulative usage counter and tool-call counts."""

    def codex(self, prompt, directory, **kwargs):
        result, events, session = super().codex(prompt, directory, **kwargs)
        path = Path(directory) / "session.json"
        meta = json.loads(path.read_text())
        n = len(self.calls)
        meta.update(started_at=f"2026-01-01T00:{n:02d}:00+00:00", finished_at=f"2026-01-01T00:{n:02d}:30+00:00",
                    wall_seconds=30.0, execution_evidence={
                        "usage_events": [{"usage": {"input_tokens": 100 * n, "cached_input_tokens": 40 * n,
                                                    "output_tokens": 5 * n}}],
                        "completed_tool_calls": 4, "failed_tool_calls": 1})
        path.write_text(json.dumps(meta))
        return result, events, session

    def table(self, body):
        return [line for line in body.splitlines() if line.startswith("|")]


class BatchCommentTests(Recorded):
    BATCH = {"supervision": {"on_block": "continue_independent"}}

    def test_batch_finished_comment_has_the_per_issue_table(self):
        self.linear.others["DEV-3"]["relations"] = {"blockedBy": [{"id": "DEV-1"}]}
        self.hooks[("DEV-1", "implement")] = self.blocked()
        self.assertEqual(self.launch()["started"]["outcome"], "partial")
        rows = self.table(self.linear.last("DEV-3", "batch-finished"))
        self.assertEqual(rows[0], "| Issue | Outcome | Attempts | Input (cached) | Output | Tool calls | Time |")
        # DEV-1: one blocked implement attempt (the 1st call); DEV-2: implement (2nd) and review (3rd).
        self.assertEqual(rows[2], "| DEV-1 | Set aside | 1 | 100 (40) | 5 | 4 (1 failed) | 30 s |")
        self.assertRegex(rows[3], r"^\| DEV-2 \| Done \| 2 \| 500 \(200\) \| 25 \| 8 \(2 failed\) \| (1 min|\d+ s) \|$")
        self.assertEqual(rows[4], "| DEV-3 | Waiting | 0 | — | — | — | — |")
        self.assertRegex(rows[5], r"^\| \*\*Batch total\*\* \|  \| \*\*3\*\* \| \*\*600 \(240\)\*\* \| "
                                  r"\*\*30\*\* \| \*\*12 \(3 failed\)\*\* \| \*\*.+\*\* \|$")
        # The same figures as the terminal trajectory report written next to it.
        report = json.loads((self.state_dir / "terminal-trajectory.json").read_text())
        self.assertEqual(report["comparison"][0]["totals"]["input_tokens"], {"value": 600, "bound": "exact"})


    def test_a_set_aside_issue_gets_the_table_after_its_deferred_comment(self):
        self.hooks[("DEV-1", "implement")] = self.blocked()
        self.launch()
        self.assertEqual(self.linear.kinds("DEV-1"), ["claim", "deferred", "run-summary"])
        body = self.linear.last("DEV-1", "run-summary")
        self.assertTrue(body.startswith("DEV-1 was set aside, and this is what its 1 model attempt and the runner's "
                                        "checks used until then;"))
        self.assertEqual(self.table(body)[2:], [
            "| Implement (blocked) | astra | medium | 100 (40) | 5 | 4 (1 failed) | 30 s |",
            "| Checks | — | — | — | — | — | — |",
            "| **Total** |  |  | **100 (40)** | **5** | **4 (1 failed)** | **30 s** |"])
        event = next(e for e in self.state()["events"].values() if e["kind"] == "run-summary")
        self.assertEqual(event["dedupe"], "deferred:DEV-1#1")


REPAIRED_CHECK = [{"name": "output", "kind": "code", "tier": "default", "inputs": ["result.txt"], "cwd": ".",
                   "command": ["${python}", "-c", "from pathlib import Path; "
                                                 "assert Path('result.txt').read_text() == 'fixed'"]}]


class DoneCommentTests(Recorded):
    BATCH = {"issues": ["DEV-1"], "terminal_issue": "DEV-1"}
    PROJECT = {"checks": REPAIRED_CHECK}

    def setUp(self):
        super().setUp()
        self.hooks[("DEV-1", "repair")] = lambda result: (self.repo / "result.txt").write_text("fixed")

    def test_done_is_followed_by_the_run_summary_with_a_checks_row(self):
        self.assertEqual(self.launch()["started"]["outcome"], "complete")
        self.assertEqual(self.linear.kinds("DEV-1"), ["claim", "ready", "validation", "ready", "validation", "review",
                                                      "done", "run-summary", "batch-finished"])
        body = self.linear.last("DEV-1", "run-summary")
        self.assertTrue(body.startswith("DEV-1 is Done, and this is what its 3 model attempts and the runner's checks "
                                        "used; no action is needed."))
        rows = self.table(body)
        # Implement (1st call, 100 input), a failed validation, Repair 1 resuming that session (counter 200, so
        # 100 more), a passing validation and Review (a new session, 300).
        self.assertEqual(rows[2:5], ["| Implement | astra | medium | 100 (40) | 5 | 4 (1 failed) | 30 s |",
                                     "| Repair 1 | luna | max | 100 (40) | 5 | 4 (1 failed) | 30 s |",
                                     "| Review | astra | medium | 300 (120) | 15 | 4 (1 failed) | 30 s |"])
        self.assertRegex(rows[5], r"^\| Checks \| — \| — \| — \| — \| — \| \d+ s \|$")
        self.assertRegex(rows[6], r"^\| \*\*Total\*\* \|  \|  \| \*\*500 \(200\)\*\* \| \*\*25\*\* \| "
                                  r"\*\*12 \(3 failed\)\*\* \| \*\*(1|2) min\*\* \|$")
        self.assertEqual(updates.lint(body.rsplit("\n\n<!--", 1)[0], kind="runner", limits=RUNNER_LIMITS,
                                      allow_tables=True), [])
        # The same records through `runner.py report` give the same totals.
        run = Path(self.state()["history"][0]["run_dir"])
        payload, _ = report_json(run)
        self.assertEqual(payload["summaries"][0]["totals"]["input_tokens"], {"value": 500, "bound": "exact"})
        self.assertEqual(payload["summaries"][0]["totals"]["tool_calls"], {"value": 12, "bound": "exact"})

    def test_a_failed_summary_post_never_blocks_done_and_is_retried_once(self):
        original = self.linear.post_comment
        def flaky(issue, body, marker, **kwargs):
            if "/run-summary/" in marker:
                raise RuntimeError("Linear offline during the run summary post")
            return original(issue, body, marker, **kwargs)
        self.linear.post_comment = flaky
        self.assertEqual(self.launch()["started"]["outcome"], "complete")
        state = self.state()
        # Done was published, read back and recorded; the lifecycle read-back ran.
        self.assertEqual((self.done(), self.linear.data["statusType"]), (["DEV-1"], "completed"))
        self.assertTrue(state["lifecycle"]["DEV-1"]["synced_at"])
        self.assertEqual(state["events"]["DEV-1/run-summary/1"]["status"], "pending")
        self.assertNotIn("run-summary", self.linear.kinds("DEV-1"))
        # The next reconcile (any later launch or phase) posts the recorded body, once.
        self.linear.post_comment = original
        runner = self.make_runner()
        runner.reconcile_events()
        runner.reconcile_events()
        self.assertEqual(self.linear.kinds("DEV-1").count("run-summary"), 1)
        self.assertEqual(self.linear.last("DEV-1", "run-summary").split("\n\n<!--")[0],
                         state["events"]["DEV-1/run-summary/1"]["body"].split("\n\n<!--")[0])

    def test_a_rendering_failure_is_logged_and_done_still_completes(self):
        with unittest.mock.patch.object(trajectory, "run_summary", side_effect=ValueError("broken records")):
            self.assertEqual(self.launch()["started"]["outcome"], "complete")
        self.assertEqual(self.done(), ["DEV-1"])
        self.assertNotIn("run-summary", self.linear.kinds("DEV-1"))


class ReviewRepairSummaryTests(Recorded):
    BATCH = {"issues": ["DEV-1"], "terminal_issue": "DEV-1"}

    def test_the_run_summary_and_the_report_label_the_repair_from_review(self):
        reviews = []
        def review(result):
            reviews.append(1)
            if len(reviews) == 1:
                result.update(status="blocked", summary="The notes are incomplete.")
                for entry in result["acceptance"]:
                    entry.update(satisfied=False, evidence="notes.md stops after the CSV section")
        self.hooks[("DEV-1", "review")] = review
        self.assertEqual(self.launch()["started"]["outcome"], "blocked")
        self.recover("repair")
        self.assertEqual(self.launch()["started"]["outcome"], "complete")
        rows = self.table(self.linear.last("DEV-1", "run-summary"))
        # Implement (100), the blocked review (a new session, 200), the repair resuming the implement
        # session (counter 300, so 200 more) and the fresh review (a new session, 400).
        self.assertEqual(rows[2:6], ["| Implement | astra | medium | 100 (40) | 5 | 4 (1 failed) | 30 s |",
                                     "| Review 1 (blocked) | astra | medium | 200 (80) | 10 | 4 (1 failed) | 30 s |",
                                     "| Repair 1 (from review) | luna | max | 200 (80) | 10 | 4 (1 failed) | 30 s |",
                                     "| Review 2 | astra | medium | 400 (160) | 20 | 4 (1 failed) | 30 s |"])
        self.assertRegex(rows[7], r"^\| \*\*Total\*\* \|  \|  \| \*\*900 \(360\)\*\* \| \*\*45\*\* \| ")
        run = Path(self.state()["history"][0]["run_dir"])
        payload, markdown = report_json(run)
        self.assertIn("| Repair 1 (from review) |", markdown)
        self.assertEqual([a["repair_source"] for a in payload["attempts"]], [None, None, "review", None])
        # Accounting is unchanged: the same totals as the comment.
        self.assertEqual(payload["summaries"][0]["totals"]["input_tokens"], {"value": 900, "bound": "exact"})
        self.assertEqual(payload["summaries"][0]["repairs"], 1)


class StopCommentTests(Recorded):
    def test_a_stop_has_no_table_and_an_owner_set_aside_gets_one(self):
        self.hooks[("DEV-2", "implement")] = self.blocked()
        self.assertEqual(self.launch()["started"]["outcome"], "blocked")
        self.assertEqual(self.linear.kinds("DEV-2"), ["claim", "blocked"])
        self.assertFalse(self.table(self.linear.last("DEV-2", "blocked")))
        self.assertFalse(self.table(self.linear.last("DEV-3", "batch-paused")))
        # The owner sets the paused issue aside (`recover defer`): its recovery comment is followed by the table.
        record = self.recover("defer", issue="DEV-2", restore_worktree=True)
        self.launch()
        self.assertEqual(self.linear.kinds("DEV-2"), ["claim", "blocked", "recovery", "run-summary"])
        body = self.linear.last("DEV-2", "run-summary")
        self.assertTrue(body.startswith("DEV-2 was set aside by its owner, and this is what its 1 model attempt"))
        self.assertIn("| Implement (blocked) | astra | medium |", body)
        event = next(e for e in self.state()["events"].values() if e["issue"] == "DEV-2" and e["kind"] == "run-summary")
        self.assertEqual(event["dedupe"], "set-aside:" + record["id"])
        rows = self.table(self.linear.last("DEV-3", "batch-finished"))
        self.assertTrue(rows[3].startswith("| DEV-2 | Set aside | 1 | "))


if __name__ == "__main__":
    unittest.main()
