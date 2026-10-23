"""``status --board`` (W-442): the compact record a progress board reads.

The fixture is a batch of the supervisor harness, run twice: DEV-1 is accepted after a blocked
review and a repair of its findings, DEV-2 is active in a repair whose session was cut off (a
start and no finish), and DEV-3 is queued. ``testdata/board/status-board.json`` is the record of
that batch with its times, commits and identifiers replaced by fixed ones. The board mod's tests
(``mods/runner-board``, run by ``claude plugin test``) read the same record: a mod's test can import
only code files of the mod's own folder, so the record is also kept there as a module, and the test
below holds both files to it.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from linear_runner import cli as cli_module
from linear_runner.cli import main
from linear_runner.engine.runner import git, write_json
from linear_runner.reporting import board, records
from tests.fixtures import CHECKOUT
from tests.linear.test_run_summary import Recorded, report_json

EXAMPLE = CHECKOUT / "testdata" / "board" / "status-board.json"
# The same record where the board mod's tests can import it (W-444): a comment, then the JSON as a module.
MOD_EXAMPLE = CHECKOUT / "mods" / "runner-board" / "hooks" / "status-board.fixture.ts"
MOD_HEADER = ("// What `runner.py status --board` prints for the runner's own test fixture: the record of\n"
              "// testdata/board/status-board.json, as a module, because a mod's tests can import only code files of the\n"
              "// mod's own folder. tests/reporting/test_board.py writes both files and fails when either differs from\n"
              "// the record, so do not edit this one by hand (see testdata/board/README.md).\n")
UPDATE = "LINEAR_RUNNER_UPDATE_BOARD_EXAMPLE"  # set to rewrite both files from the fixture
ACCEPTED = "The table is read and written losslessly; the notes cover both formats."
LINEAGE = {"purpose": "Repair what the review of the first batch found.", "follows": "fixture-first",
           "reason": "The review found two defects, and the owner asked for both to be repaired.",
           "from_issues": ["DEV-7"]}
TITLES = {"DEV-1": "Read and write the table", "DEV-2": "Plot the table", "DEV-3": "Write the report"}
# DEV-2's own file must say "repaired": its implementation fails the check, so a repair starts.
CHECK = {"name": "output", "kind": "code", "tier": "default", "inputs": ["*.txt"], "cwd": ".",
         "command": ["${python}", "-c", "from pathlib import Path; p = Path('DEV-2.txt'); "
                                        "assert not p.exists() or 'repaired' in p.read_text()"]}
CUT_OFF = "2026-01-01T00:40:00+00:00"


def stable(record):
    """``record`` with what differs from run to run replaced: each time by a fixed one that keeps
    their order (the first is 01:00, the next 02:00, ...), launch identifiers and commits by fixed
    ones. Elapsed times and tokens come from the fake sessions and stay."""
    text = json.dumps(record)
    moment = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+00:00"
    order = {found: f"2026-01-01T{index + 1:02d}:00:00+00:00"
             for index, found in enumerate(sorted(set(re.findall(moment, text))))}
    text = re.sub(moment, lambda match: order[match.group(0)], text)
    text = re.sub(r"\d{8}T\d{6}Z-[0-9a-f]{8}", "20260101T000000Z-00000000", text)
    return json.loads(re.sub(r"\b[0-9a-f]{40}\b", "0" * 40, text))


class BoardTests(Recorded):
    BATCH = LINEAGE
    PROJECT = {"checks": [CHECK]}

    def setUp(self):
        super().setUp()
        for identifier, title in TITLES.items():
            issue = self.linear.data if identifier == "DEV-1" else self.linear.others[identifier]
            issue.update(title=title, url=f"https://linear.app/test/issue/{identifier}/{title.lower().replace(' ', '-')}")
        reviews = []

        def review(result):
            reviews.append(1)
            if len(reviews) == 1:
                result.update(status="blocked", summary="The notes are incomplete.")
                for entry in result["acceptance"]:
                    entry.update(satisfied=False, evidence="notes.md stops after the CSV section")
            else:
                result["summary"] = ACCEPTED
        self.hooks[("DEV-1", "review")] = review
        self.hooks[("DEV-1", "implement")] = lambda result: result.update(
            deliverables=[{"path": "DEV-1.txt", "description": "The table"}])
        self.hooks[("DEV-2", "implement")] = lambda result: result.update(summary="The plot is drawn.")
        self.assertEqual(self.launch()["started"]["outcome"], "blocked")  # DEV-1's first review
        self.recover("repair")
        self.assertEqual(self.launch()["started"]["outcome"], "blocked")  # DEV-2's repair is cut off
        self.args = ["--batch", str(self.batch), "--home", str(self.home)]

    def codex(self, prompt, directory, **kwargs):
        directory = Path(directory)
        if (directory.parent.parent.name, directory.name.split("-")[0]) == ("DEV-2", "repair"):
            # A session that started and never finished: its record has no finish time.
            self.calls.append(("DEV-2", "repair"))
            write_json(directory / "session.json", {"session_id": kwargs.get("resume"), "started_at": CUT_OFF,
                                                    "requested_model": kwargs["model"],
                                                    "requested_reasoning_effort": kwargs["effort"]})
            raise RuntimeError("The repair session was cut off")
        return super().codex(prompt, directory, **kwargs)

    def board(self):
        """``status --board`` as it prints: (record, printed text). Any Linear client it built
        would be the harness's fake, whose calls the tests count."""
        out = io.StringIO()
        with patch.object(cli_module, "LinearClient", return_value=self.linear), contextlib.redirect_stdout(out):
            main(["status", "--board", *self.args])
        return json.loads(out.getvalue()), out.getvalue()

    def test_the_record_is_complete_and_small(self):
        record, printed = self.board()
        state = self.state()
        self.assertEqual(self.calls, [("DEV-1", "implement"), ("DEV-1", "review"), ("DEV-1", "repair"),
                                      ("DEV-1", "review"), ("DEV-2", "implement"), ("DEV-2", "repair")])
        launches = sorted(json.loads(p.read_text())["at"] for p in (self.state_dir / "launches").glob("*.json"))
        self.assertEqual(len(launches), 2)
        self.assertLess(launches[0], launches[1])
        self.assertEqual(record["schema"], board.SCHEMA)
        self.assertEqual(record["batch"], {
            "id": "fixture", "phase": "paused", "supervisor": {"status": "exited", "outcome": "blocked"},
            "first_launch_at": launches[0], "last_launch_at": launches[1],
            "stop_marker": (self.state_dir / "STOP").read_text().strip(), "pending_recovery": None,
            "error": "The repair session was cut off", "lineage": LINEAGE})
        self.assertTrue(record["batch"]["stop_marker"])
        [done] = state["history"]
        unknown = {"value": None, "bound": None}
        first, second, third = record["issues"]
        # DEV-1: implement (counter 100), the blocked review (a new session, 200), the repair resuming
        # the implement session (300, so 200 more) and the accepting review (a new session, 400).
        self.assertEqual(first, {
            "id": "DEV-1", "title": TITLES["DEV-1"], "url": self.linear.data["url"], "stage": "done", "repairs": 1,
            "blocked": {"worker": 0, "review": 1}, "summary": ACCEPTED, "accepted_at": done["completed_at"],
            "commit": git(self.repo, "rev-parse", "HEAD"), "files": ["DEV-1.txt"], "elapsed_seconds": 210.0,
            "tokens": {"input_tokens": {"value": 900, "bound": "exact"},
                       "cached_input_tokens": {"value": 360, "bound": "exact"},
                       "output_tokens": {"value": 45, "bound": "exact"}, "reasoning_output_tokens": unknown},
            "pending_invocations": 0})
        self.assertEqual((done["summary"], done["commit"]), (ACCEPTED, first["commit"]))
        # DEV-2: its implementation finished (the 5th session, 30 s); the cut-off repair is left out.
        self.assertEqual(second, {
            "id": "DEV-2", "title": TITLES["DEV-2"], "url": self.linear.others["DEV-2"]["url"], "stage": "repair",
            "repairs": 1, "blocked": {"worker": 0, "review": 0}, "summary": "The plot is drawn.",
            "accepted_at": None, "commit": None, "files": [], "elapsed_seconds": 30.0,
            "tokens": {"input_tokens": {"value": 500, "bound": "exact"},
                       "cached_input_tokens": {"value": 200, "bound": "exact"},
                       "output_tokens": {"value": 25, "bound": "exact"}, "reasoning_output_tokens": unknown},
            "pending_invocations": 1})
        self.assertEqual((state["active"]["issue_id"], state["active"]["step"], state["active"]["repairs"]),
                         ("DEV-2", "repair", 1))
        # DEV-3 was never claimed: the state has no record of it, and the launch kept its title.
        self.assertNotIn("DEV-3", state["issue_cache"])
        self.assertEqual(third, {
            "id": "DEV-3", "title": TITLES["DEV-3"], "url": self.linear.others["DEV-3"]["url"], "stage": "queued",
            "repairs": 0, "blocked": {"worker": 0, "review": 0}, "summary": None, "accepted_at": None, "commit": None,
            "files": [], "elapsed_seconds": None, "tokens": dict.fromkeys(records.USAGE_KEYS, unknown),
            "pending_invocations": 0})
        # Small: no issue description, no event list, no path of this host.
        self.assertLess(len(printed.encode()), 10_000)
        self.assertEqual(len(printed.splitlines()), 1)
        for absent in ("Produce", "description", "events", "issue_cache", str(self.root)):
            self.assertNotIn(absent, printed)
        self.assertIn("Produce DEV-2 output", json.dumps(state))  # the state does hold the descriptions

    def test_time_and_tokens_agree_with_the_report(self):
        record, _ = self.board()
        state = self.state()
        runs = [state["history"][0]["run_dir"], state["active"]["run_dir"]]
        report, _ = report_json(*runs)
        summaries = {s["issue"]: s for s in report["summaries"]}
        self.assertEqual(sorted(summaries), ["DEV-1", "DEV-2"])
        for issue in record["issues"][:2]:
            summary = summaries[issue["id"]]
            with self.subTest(issue=issue["id"]):
                self.assertAlmostEqual(issue["elapsed_seconds"], summary["issue_elapsed_seconds"], places=6)
                self.assertEqual(issue["tokens"], {key: summary["totals"][key] for key in records.USAGE_KEYS})
                self.assertEqual(issue["pending_invocations"], summary["pending"])
        # The repair without a finish is pending in the report and in neither's totals.
        self.assertEqual([(p["issue"], p["phase"], p["pending_reason"]) for p in report["pending"]],
                         [("DEV-2", "repair", "no recorded finish")])
        self.assertEqual((summaries["DEV-2"]["attempts"], summaries["DEV-2"]["repairs"]), (1, 0))
        self.assertEqual(summaries["DEV-1"]["repairs"], record["issues"][0]["repairs"])

    def test_nothing_is_written_and_linear_is_not_called(self):
        def files():
            return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in sorted(self.root.rglob("*")) if p.is_file() and ".git" not in p.parts}
        before = files()
        self.assertTrue(any(name.startswith("state/fixture/") for name in before))
        calls = lambda: (self.linear.reads, len(self.linear.posts), list(self.linear.writes),
                         list(self.linear.label_writes), list(self.linear.resolutions))
        counted = calls()
        with patch.object(cli_module.LinearClient, "call", side_effect=AssertionError("no Linear access")):
            self.board()
        self.assertEqual(files(), before)
        self.assertEqual(calls(), counted)

    def test_a_state_from_before_the_change_reads(self):
        record, _ = self.board()
        state = self.state()
        self.assertEqual([entry.pop("summary") for entry in state["history"]], [ACCEPTED])
        write_json(self.state_dir / "state.json", state)
        older, _ = self.board()
        self.assertIsNone(older["issues"][0]["summary"])
        older["issues"][0]["summary"] = ACCEPTED
        self.assertEqual(older, record)
        # Nor did such a batch keep its titles: a claimed issue has the title the state saved, and
        # an issue it never claimed has none until the next dry-run or launch.
        (self.state_dir / "titles.json").unlink()
        untitled, _ = self.board()
        self.assertEqual([issue["title"] for issue in untitled["issues"]], [TITLES["DEV-1"], TITLES["DEV-2"], None])
        self.assertEqual(untitled["issues"][2]["url"], "https://linear.app/test/issue/DEV-3")

    def test_an_issue_set_aside_keeps_its_summary_and_its_counts(self):
        before, _ = self.board()
        state = self.state()
        active = state["active"]
        state.update(active=None, parked={"DEV-2": {"active": active, "park": {}}},
                     deferred={"DEV-2": {"policy": "on_block=continue_independent"}},
                     pending_recovery={"id": "R-fixture", "kind": "resume", "expected": {}},
                     blocks=dict(state["blocks"], **{"DEV-2": [{"event": "worker_blocked"}, {"event": "checks_failed"},
                                                               {"event": "worker_blocked"}]}))
        write_json(self.state_dir / "state.json", state)
        record, _ = self.board()
        self.assertEqual(record["batch"]["pending_recovery"], {"id": "R-fixture", "kind": "resume"})
        expected = dict(before["issues"][1], stage="deferred", blocked={"worker": 2, "review": 0})
        self.assertEqual(record["issues"], [before["issues"][0], expected, before["issues"][2]])
        # Deferred before it was claimed: nothing but its place in the queue.
        state["deferred"]["DEV-3"] = {"policy": "recover defer"}
        write_json(self.state_dir / "state.json", state)
        self.assertEqual(self.board()[0]["issues"][2], dict(before["issues"][2], stage="deferred"))

    def test_the_example_the_board_mod_reads_is_this_record(self):
        record = stable(self.board()[0])
        text = json.dumps(record, indent=2) + "\n"
        if os.environ.get(UPDATE):
            EXAMPLE.parent.mkdir(parents=True, exist_ok=True)
            EXAMPLE.write_text(text)
            MOD_EXAMPLE.write_text(MOD_HEADER + "export default " + text)
        hint = f"rewrite it with {UPDATE}=1 python3 -m unittest tests.reporting.test_board"
        self.assertEqual(json.loads(EXAMPLE.read_text()), record, hint)
        # The module the mod's tests import is that record and nothing else.
        module = MOD_EXAMPLE.read_text()
        self.assertTrue(module.startswith(MOD_HEADER + "export default {"), hint)
        self.assertEqual(json.loads(module[len(MOD_HEADER + "export default "):]), record, hint)

    def test_launches_lists_every_started_launch_of_the_home(self):
        def launches(*since):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["launches", "--home", str(self.home), *since])
            self.assertEqual(out.getvalue().count("\n"), 1)  # one line, for a program to read
            printed = json.loads(out.getvalue())
            self.assertEqual(printed["schema"], board.LAUNCHES_SCHEMA)
            return printed["launches"]
        first, second = launches()
        self.assertEqual([first["batch"], second["batch"]], [self.state_dir.name] * 2)
        self.assertLess(first["at"], second["at"])
        self.assertEqual(launches("--since", first["at"]), [second])
        self.assertEqual(launches("--since", second["at"].replace("+00:00", "Z")), [])
        # A launch whose supervisor did not start puts no batch on a board.
        path = self.state_dir / "launches" / f"{second['launch_id']}.json"
        write_json(path, dict(records.read_json(path), error="Supervisor refused to start"))
        self.assertEqual(launches(), [first])


class UnclaimedBatchTests(Recorded):
    """A batch that has no state yet: every issue is queued, and a dry-run gives the titles."""

    def test_a_dry_run_keeps_the_titles_of_the_whole_allowlist(self):
        args = ["--batch", str(self.batch), "--home", str(self.home)]
        for identifier, title in TITLES.items():
            (self.linear.data if identifier == "DEV-1" else self.linear.others[identifier])["title"] = title

        def board():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["status", "--board", *args])
            return json.loads(out.getvalue())
        record = board()
        self.assertFalse(self.state_dir.exists())  # reading a batch that never ran creates nothing
        self.assertEqual((record["batch"]["phase"], record["batch"]["first_launch_at"], record["batch"]["lineage"]),
                         ("not started", None, {}))
        self.assertEqual([(i["id"], i["title"], i["stage"]) for i in record["issues"]],
                         [("DEV-1", None, "queued"), ("DEV-2", None, "queued"), ("DEV-3", None, "queued")])
        with patch.object(cli_module, "LinearClient", return_value=self.linear), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main(["dry-run", *args])
        self.assertFalse((self.state_dir / "state.json").exists())  # still a batch that has claimed nothing
        self.assertEqual([(i["id"], i["title"], i["stage"]) for i in board()["issues"]],
                         [(identifier, title, "queued") for identifier, title in TITLES.items()])

    def test_json_and_board_are_two_views(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as refused:
            main(["status", "--json", "--board", "--batch", str(self.batch), "--home", str(self.home)])
        self.assertIn("not allowed with argument", refused.getvalue())


if __name__ == "__main__":
    unittest.main()
