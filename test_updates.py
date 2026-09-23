"""Human-review Linear updates: templates, lint, outbox, exactly-once events, stops, watchdog.

Fake Linear, Codex, notifier and process boundaries only: no network, no systemd, no mail.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import attention
from config import load_config, pin_resolution
from fixtures import FakeLinear, make_home
import messages
import render_samples
from runner import IssueBlocked, Runner, git, write_json
from test_supervisor import Harness
import updates
import watchdog

LIMITS = {"max_chars": 1500, "max_lines": 30, "max_first_sentence_chars": 240}
GOOD_PROGRESS = """The QC report now renders for all tiles; no action is needed.

**What changed**
I added per-tile spot counts and a helper for error bars.

**Next**
Wire in the calibration table.

Evidence: /absolute/path/to/runs/TEAM-1/qc_report.html
"""
WORKER_EXPLANATION = "I could not find the calibration table the criterion names; the data folder has only raw tiles."
OWNER = "@owner-handle"


def first_line(body):
    return body.splitlines()[0]


class NotifierFake:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get("input")))
        return subprocess.CompletedProcess(argv, 0, "", "")


# --- Templates and lint ------------------------------------------------------------------

class LintTests(unittest.TestCase):
    def lint(self, text, kind="progress"):
        template = updates.draft_template(kind)
        return updates.lint(text, kind=kind, limits=LIMITS, sections=template["sections"], required=template["required"])

    def test_valid_draft_is_accepted(self):
        self.assertEqual(self.lint(GOOD_PROGRESS), [])
        self.assertEqual(updates.draft_template("progress")["required"], ["What changed"])
        self.assertEqual(updates.draft_template("blocked")["required"], ["What is blocking", "What is needed"])

    def test_each_rule_rejects(self):
        cases = {
            "**What changed**\nFirst line is a heading.": "first line must be one plain sentence",
            "Did one thing. Then another.\n\n**What changed**\nx.": "exactly one sentence",
            "No full stop here\n\n**What changed**\nx.": "exactly one sentence",
            "Fine opening sentence.\n\n**Next**\nOnly next.": "missing required section(s): What changed",
            "Fine opening sentence.\n\n**What changed**\nx.\n\n**Details**\ny.": "unknown section 'Details'",
            "Fine opening sentence.\n\n**What changed**\n```\ncode\n```": "code blocks are not allowed",
            "Fine opening sentence.\n\n**What changed**\n| a | b |\n| --- | --- |": "tables are not allowed",
            'Fine opening sentence.\n\n**What changed**\n{"status": "ready"}': "JSON or raw records",
            "Fine opening sentence.\n\n**What changed**\nsha " + "a" * 64: "long hashes",
            "Fine opening sentence.\n\n**What changed**\n<Two to four plain sentences.>": "placeholder was left in",
            "Fine opening sentence.\n\n**What changed**\n<!-- hidden -->": "HTML comments",
            "Fine opening sentence.\n\nEvidence: /a\n\n**What changed**\nx.": "must be the last line",
            "Fine opening sentence.\n\n**What changed**\nx.\nEvidence: /a\nEvidence: /b": "at most one 'Evidence:'",
            "Fine opening sentence.\n\n**What changed**\n" + "word " * 400: "too long",
            "": "the draft is empty",
        }
        for text, problem in cases.items():
            with self.subTest(problem=problem):
                self.assertTrue(any(problem in p for p in self.lint(text)), self.lint(text))

    def test_file_name_and_phase_kinds(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Path(directory)
            (outbox / "001-progress.md").write_text(GOOD_PROGRESS)
            (outbox / "002-review.md").write_text(GOOD_PROGRESS)
            (outbox / "notes.md").write_text(GOOD_PROGRESS)
            self.assertEqual(updates.lint_draft(outbox / "001-progress.md", "implement", LIMITS)[2], [])
            self.assertIn("not allowed in the implement phase", updates.lint_draft(outbox / "002-review.md", "implement", LIMITS)[2][0])
            self.assertIn("not allowed in the review phase", updates.lint_draft(outbox / "001-progress.md", "review", LIMITS)[2][0])
            self.assertIn("is not NNN-<kind>.md", updates.lint_draft(outbox / "notes.md", "implement", LIMITS)[2][0])

    def test_render_drops_empty_optional_sections(self):
        body = updates.render("ready", {"issue": "TEAM-1", "summary": "> done", "criteria": "", "limitations": "",
                                        "evidence": ""})
        self.assertEqual(body, "The worker reports TEAM-1 ready for validation; no action is needed.\n\n"
                               "**Worker summary**\n> done\n")

    def test_every_sample_passes_lint_and_the_samples_file_is_current(self):
        runner_limits = dict(LIMITS, max_chars=3500, max_lines=40)
        for title, template, author, _, body in render_samples.samples():
            with self.subTest(sample=title):
                text = body.rsplit("\n\n<!-- linear-runner ", 1)[0]
                self.assertEqual(body.count("<!--"), 1)
                self.assertTrue(body.rstrip().endswith("-->"))
                self.assertNotIn("TEAM-12".replace("TEAM", "W"), body)
                if author in ("worker", "reviewer"):
                    kind = template.split("-", 1)[1].removesuffix(".md")
                    draft = text.rsplit("\n\n_Written by", 1)[0]
                    self.assertEqual(TestDraftLint.lint(kind, draft), [])
                else:
                    self.assertEqual(updates.lint(text, kind="runner", limits=runner_limits), [])
        for name in ("draft-progress", "draft-ready", "draft-blocked", "draft-review"):
            self.assertEqual(updates.load_template(name)["meta"]["status"], "DRAFT")
        self.assertEqual(render_samples.main(["--check"]), 0, "run python3 render_samples.py")


class TestDraftLint:
    @staticmethod
    def lint(kind, text):
        template = updates.draft_template(kind)
        return updates.lint(text, kind=kind, limits=LIMITS, sections=template["sections"], required=template["required"])


# --- Classification and notifier -------------------------------------------------------------

class ClassificationTests(unittest.TestCase):
    def test_stop_classes(self):
        cases = [
            (IssueBlocked("x", "worker_blocked"), "needs-decision"),
            (IssueBlocked("x", "review_blocked"), "needs-decision"),
            (IssueBlocked("x", "budget_exceeded"), "needs-decision"),
            (IssueBlocked("x", "checks_failed"), "technical-block"),
            (IssueBlocked("x", "delivery_failed"), "technical-block"),
            (KeyboardInterrupt("Signal 15"), "environment"),
            (OSError("No space left on device"), "environment"),
            (TimeoutError("Codex exceeded 5400 seconds"), "environment"),
            (RuntimeError("Linear OAuth expired; refresh with the owning CLI and resume"), "environment"),
            (RuntimeError("Linear HTTP 502; reconcile authentication/write outcome before resume"), "environment"),
            (RuntimeError("Codex failed or did not finish a turn; see /x"), "environment"),
            (RuntimeError("Issue state changed outside this execution; reconcile ownership"), "needs-decision"),
            (RuntimeError("Human approval evidence is not confirmed for GATE-1"), "needs-decision"),
            (RuntimeError("Required readiness issue READY-1 is not Done"), "needs-decision"),
            (RuntimeError("Repair interrupted; inspect its recorded result before explicit recovery"), "needs-decision"),
            (RuntimeError("No applicable validation checks"), "technical-block"),
            (KeyError("active"), "runner-defect"),
            (TypeError("unsupported operand"), "runner-defect"),
            (AssertionError("invariant"), "runner-defect"),
        ]
        for error, expected in cases:
            with self.subTest(error=repr(error)):
                self.assertEqual(attention.classify_stop(error), expected)
        self.assertEqual(set(attention.EVENT_CLASSES.values()) | {"runner-defect", "environment"},
                         set(attention.STOP_CLASSES))

    def test_notifier_backends(self):
        fake = NotifierFake()
        self.assertFalse(attention.notify({"backend": "none"}, "s", "m", run=fake)["sent"])
        self.assertIn("mentions the owner", attention.notify({"backend": "linear-mention-only"}, "s", "m", run=fake)["note"])
        self.assertEqual(fake.calls, [])
        record = attention.notify({"backend": "command", "command": ["/usr/bin/mail", "-s", "[runner] {subject}", "owner@example.invalid"],
                                   "timeout_seconds": 5}, "TEAM-1 is paused.", "full message", run=fake)
        self.assertTrue(record["sent"])
        self.assertEqual(fake.calls, [(["/usr/bin/mail", "-s", "[runner] TEAM-1 is paused.", "owner@example.invalid"],
                                       "full message")])
        failing = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "no MTA")
        self.assertFalse(attention.notify({"backend": "command", "command": ["x"]}, "s", "m", run=failing)["sent"])
        def broken(argv, **kwargs):
            raise FileNotFoundError("x")
        self.assertIn("x", attention.notify({"backend": "command", "command": ["x"]}, "s", "m", run=broken)["error"])


# --- Exactly-once events, read-back and the real-time outbox ------------------------------------

class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q"); git(self.repo, "checkout", "-q", "-b", "codex/test")
        self.home, self.batch = make_home(self.root, self.repo)
        self.linear = FakeLinear()
        self.config, _ = pin_resolution(load_config(self.batch, self.home), self.linear)

    def runner(self):
        return Runner(copy.deepcopy(self.config), self.linear)

    def test_lost_response_is_reconciled_without_a_second_comment(self):
        self.linear.lose_responses = 1
        runner = self.runner()
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            runner.emit("DEV-1", "blocked", "DEV-1 is paused.", dedupe="S-1")
        self.assertEqual(runner.state["events"]["DEV-1/blocked/1"]["status"], "pending")
        again = self.runner()  # a new process reads the pending event from disk
        record = again.emit("DEV-1", "blocked", "DEV-1 is paused.", dedupe="S-1")
        self.assertEqual((record["status"], record["comment_id"]), ("posted", "comment-1"))
        self.assertEqual(len(self.linear.posts), 1)
        again.reconcile_events()
        self.assertEqual(len(self.linear.posts), 1)

    def test_crash_between_write_and_save_posts_exactly_once(self):
        runner = self.runner()
        def die(issue, text):
            raise SystemExit("process killed right after Linear accepted the comment")
        self.linear.on_post = die
        with self.assertRaises(SystemExit):
            runner.emit("DEV-1", "done", "DEV-1 is Done.", dedupe="done:run")
        self.linear.on_post = None
        saved = json.loads((runner.root / "state.json").read_text())["events"]["DEV-1/done/1"]
        self.assertEqual((saved["status"], saved["attempts"]), ("pending", 1))
        restarted = self.runner()
        restarted.reconcile_events()
        restarted.emit("DEV-1", "done", "DEV-1 is Done.", dedupe="done:run")  # the step re-runs after restart
        self.assertEqual(len(self.linear.posts), 1)
        self.assertEqual(restarted.state["events"]["DEV-1/done/1"]["status"], "posted")
        # A later, different event gets the next sequence and a new comment.
        restarted.emit("DEV-1", "done", "Again.", dedupe="done:other")
        self.assertEqual(sorted(restarted.state["events"]), ["DEV-1/done/1", "DEV-1/done/2"])
        self.assertIn("<!-- linear-runner fixture/DEV-1/done/2 -->", self.linear.bodies("DEV-1")[1])

    def test_posted_comment_is_read_back(self):
        runner = self.runner()
        def normalized(issue, text):
            self.linear.comment_store[issue][-1]["body"] = text.replace("paused", "PAUSED")
        self.linear.on_post = normalized
        with self.assertRaisesRegex(RuntimeError, "read-back failed"):
            runner.emit("DEV-1", "blocked", "DEV-1 is paused.", dedupe="S-1")
        self.assertEqual(runner.state["events"]["DEV-1/blocked/1"]["status"], "pending")

    def test_progress_draft_is_posted_while_the_session_is_still_running(self):
        draft = self.root / "draft.md"; draft.write_text(GOOD_PROGRESS)
        fake = self.root / "fake-codex"
        fake.write_text(f"#!{sys.executable}\n" + f"""import json, pathlib, sys, time
sys.stdin.read()
result = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
outbox = result.parent / 'outbox'
print(json.dumps({{'type': 'thread.started', 'thread_id': 'fixture-session'}}), flush=True)
(outbox / '001-progress.md.tmp').write_text(pathlib.Path({str(draft)!r}).read_text())
(outbox / '001-progress.md.tmp').rename(outbox / '001-progress.md')
deadline = time.time() + 20  # wait until the runner has posted the draft
while not (result.parent / 'released').exists() and time.time() < deadline:
    time.sleep(0.05)
result.write_text(json.dumps({{'released': (result.parent / 'released').exists()}}))
print(json.dumps({{'type': 'turn.completed', 'usage': {{'input_tokens': 1, 'output_tokens': 1}}}}), flush=True)
""")
        fake.chmod(0o755)
        config = copy.deepcopy(self.config)
        config["codex"] = str(fake)
        config["attention"]["outbox"] = {"poll_seconds": 0, "settle_seconds": 0}
        runner = Runner(config, self.linear)
        attempt = self.root / "runs" / "DEV-1" / "run" / "implement-1"
        active = {"issue_id": "DEV-1", "run_dir": str(attempt.parent)}
        seen = []
        def released(issue, text):
            seen.append((issue, runner.child is not None and runner.child.poll() is None))
            (attempt / "released").write_text("posted")
        self.linear.on_post = released
        result, _, _ = runner.codex("prompt", attempt, phase="implement", model="astra", effort="medium",
                                    writable=True, watch=lambda: runner.poll_outbox(active, "implement", attempt))
        self.assertEqual(seen, [("DEV-1", True)])  # posted while the Codex process was still running
        self.assertEqual(result, {"released": True})
        body = self.linear.bodies("DEV-1")[0]
        self.assertEqual(first_line(body), "The QC report now renders for all tiles; no action is needed.")
        self.assertIn("_Written by the worker (implement phase); posted by the runner._", body)
        self.assertEqual(runner.state["drafts"][str(attempt / "outbox" / "001-progress.md")]["status"], "posted")


# --- Batch scenarios: the three silent-stop cases, fallback, needs-input ------------------------

class AttentionHarness(Harness):
    ATTENTION = {"owner_mention": OWNER}
    SITE_ATTENTION = {"notifier": {"backend": "command", "command": ["/usr/bin/notify-send", "{subject}"]},
                      "outbox": {"poll_seconds": 0, "settle_seconds": 0}}

    def setUp(self):
        super().setUp()
        self.home, self.batch = make_home(self.root, self.repo, batch={"issues": ["DEV-1", "DEV-2", "DEV-3"],
                                                                       "terminal_issue": "DEV-3", **self.BATCH},
                                          workspace={"attention": self.ATTENTION},
                                          site={"attention": self.SITE_ATTENTION})
        self.notifier = NotifierFake()
        self.drafts = {}

    def make_runner(self):
        runner = super().make_runner()
        runner.notify_run = self.notifier
        return runner

    def codex(self, prompt, directory, **kwargs):
        """The fake session writes its outbox drafts, whether it then finishes or dies."""
        directory = Path(directory)
        issue, phase = directory.parent.parent.name, directory.name.split("-")[0]
        try:
            return super().codex(prompt, directory, **kwargs)
        finally:
            for name, text in self.drafts.get((issue, phase), {}).items():
                (directory / "outbox").mkdir(exist_ok=True)
                (directory / "outbox" / name).write_text(text)

    def worker_blocks(self, issue, summary=WORKER_EXPLANATION, phase="implement"):
        def hook(result):
            result.update(status="blocked", summary=summary)
            for entry in result["acceptance"]:
                entry.update(satisfied=False, evidence="calibration table missing from the data folder")
        self.hooks[(issue, phase)] = hook


class SilentStopTests(AttentionHarness):
    def test_block_right_after_a_previous_success_posts_new_comment_on_the_blocked_issue(self):
        self.worker_blocks("DEV-2")
        self.launch()
        done_comments = self.linear.bodies("DEV-1")
        self.assertEqual(self.linear.kinds("DEV-1"), ["claim", "ready", "validation", "review", "done"])
        self.assertEqual(self.linear.kinds("DEV-2"), ["claim", "blocked"])
        body = self.linear.last("DEV-2", "blocked")
        self.assertEqual(first_line(body), "DEV-2 is paused because the worker reported that it cannot finish; "
                                           f"{OWNER} needs to decide how to continue.")
        self.assertIn("> " + WORKER_EXPLANATION, body)
        self.assertIn("recover resume --batch", body)
        self.assertNotIn("```", body); self.assertNotIn('{"', body)
        # The success comments on DEV-1 were not touched; the batch summary is a new comment.
        self.assertEqual(self.linear.bodies("DEV-1"), done_comments)
        self.assertEqual(self.linear.kinds("DEV-3"), ["batch-paused"])
        self.assertIn("details are in the comment on DEV-2", self.linear.last("DEV-3", "batch-paused"))
        stop = self.state()["stops"][0]
        self.assertEqual((stop["class"], stop["event"], stop["issue"]), ("needs-decision", "worker_blocked", "DEV-2"))
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(self.notifier.calls[0][0][1], first_line(body))

    def test_blocked_review_quotes_the_reviewer(self):
        reviewer = "The report has no error bars on the per-tile counts, which the first criterion requires."
        def hook(result):
            result.update(status="blocked", summary=reviewer)
            for entry in result["acceptance"]:
                entry.update(satisfied=False, evidence="no error bars in the report")
        self.hooks[("DEV-1", "review")] = hook
        self.launch()
        body = self.linear.last("DEV-1", "blocked")
        self.assertEqual(first_line(body), "DEV-1 is paused because the independent review did not accept it; "
                                           f"{OWNER} needs to decide how to continue.")
        self.assertIn("The reviewer wrote:\n\n> " + reviewer, body)
        self.assertIn("recover review --batch", body)
        self.assertEqual(self.linear.kinds("DEV-1"), ["claim", "ready", "validation", "blocked"])
        run = Path(self.state()["active"]["run_dir"])
        self.assertTrue(list(run.glob("review-*/outbox/001-review.md")))  # the reviewer's prose is kept on disk

    def test_supervisor_exit_without_terminal_event_is_caught_by_the_watchdog(self):
        self.drafts[("DEV-2", "implement")] = {"001-progress.md": GOOD_PROGRESS}
        def killed(result):
            raise SystemExit("supervisor killed")  # not handled: no terminal outcome is written
        self.hooks[("DEV-2", "implement")] = killed
        with self.assertRaises(SystemExit):
            self.launch()
        status = json.loads((self.state_dir / "supervisor.json").read_text())
        self.assertEqual(status["status"], "running")
        self.assertEqual(self.linear.kinds("DEV-2"), ["claim", "progress"])  # posted after the session ended
        config = self.make_runner().config
        result = watchdog.check(config, self.linear, alive=lambda pid: False, notify_run=self.notifier, log=lambda m: None)
        self.assertEqual((result["status"], result["condition"], result["issue"]), ("alerted", "gone", "DEV-2"))
        body = self.linear.last("DEV-2", "watchdog")
        self.assertEqual(first_line(body), "The supervisor for batch fixture stopped without reporting an outcome while "
                                           f"working on DEV-2; {OWNER} needs to check the host and relaunch.")
        self.assertIn("From the progress comment: The QC report now renders for all tiles", body)
        again = watchdog.check(config, self.linear, alive=lambda pid: False, notify_run=self.notifier, log=lambda m: None)
        self.assertEqual(again["status"], "already-alerted")
        self.assertEqual(self.linear.kinds("DEV-2").count("watchdog"), 1)
        self.assertEqual(len(self.notifier.calls), 1)


class WatchdogStallTests(AttentionHarness):
    def test_stalled_supervisor_alerts_once_per_stall(self):
        self.launch(stop_after=["DEV-1"])
        config = self.make_runner().config
        status_path = self.state_dir / "supervisor.json"
        status = json.loads(status_path.read_text()); status["status"] = "running"; write_json(status_path, status)
        clock = [os.path.getmtime(self.state_dir / "state.json") + 60]
        check = lambda: watchdog.check(config, self.linear, clock=lambda: clock[0], alive=lambda pid: True,
                                       notify_run=self.notifier, log=lambda m: None)
        self.assertEqual(check()["status"], "ok")
        clock[0] += 3 * 3600
        first = check()
        self.assertEqual((first["status"], first["condition"], first["issue"]), ("alerted", "stalled", "DEV-3"))
        self.assertTrue(first_line(self.linear.last("DEV-3", "watchdog")).startswith(
            "Batch fixture has recorded no progress on batch fixture for "))
        self.assertEqual(check()["status"], "already-alerted")
        # Progress resumes, then stalls again: that is a new condition and alerts once more.
        state = json.loads((self.state_dir / "state.json").read_text())
        state["updated_at"] = __import__("datetime").datetime.fromtimestamp(clock[0], __import__("datetime").timezone.utc).isoformat()
        write_json(self.state_dir / "state.json", state)
        self.assertEqual(check()["status"], "ok")
        clock[0] += 3 * 3600
        self.assertEqual(check()["status"], "alerted")
        self.assertEqual(self.linear.kinds("DEV-3").count("watchdog"), 2)
        self.assertEqual(len(self.notifier.calls), 2)
        ledger = json.loads((self.state_dir / "watchdog.json").read_text())
        self.assertEqual(len(ledger["alerts"]), 2)

    def test_exited_supervisor_and_other_hosts_are_not_alerts(self):
        self.launch(stop_after=["DEV-1"])
        config = self.make_runner().config
        self.assertEqual(watchdog.check(config, self.linear, alive=lambda pid: False, log=lambda m: None)["status"], "ok")
        status_path = self.state_dir / "supervisor.json"
        status = json.loads(status_path.read_text()); status.update(status="running", host="elsewhere")
        write_json(status_path, status)
        self.assertEqual(watchdog.check(config, self.linear, alive=lambda pid: False, log=lambda m: None)["status"], "skipped")


class OutboxFallbackTests(AttentionHarness):
    def test_valid_drafts_are_posted_and_invalid_ones_fall_back(self):
        bad_ready = "**What was done**\nEverything.\n\n```json\n{\"status\": \"ready\"}\n```\n"
        good_ready = GOOD_PROGRESS.replace("**What changed**", "**What was done**").replace(
            "**Next**\nWire in the calibration table.", "**How it was checked**\nFocused tests passed.")
        self.drafts[("DEV-1", "implement")] = {"001-progress.md": GOOD_PROGRESS, "002-ready.md": bad_ready}
        self.drafts[("DEV-2", "implement")] = {"001-ready.md": good_ready}
        self.launch(stop_after=["DEV-2"])
        # DEV-1: progress posted as written, the invalid ready draft replaced by the runner's fallback.
        self.assertEqual(self.linear.kinds("DEV-1")[:3], ["claim", "progress", "ready"])
        ready = self.linear.last("DEV-1", "ready")
        self.assertEqual(first_line(ready), "The worker reports DEV-1 ready for validation; no action is needed.")
        self.assertIn("The worker's own ready note was not posted because the first line must be one plain sentence", ready)
        drafts = self.state()["drafts"]
        rejected = [(Path(p).name, r["status"]) for p, r in drafts.items() if r["issue"] == "DEV-1"]
        self.assertIn(("002-ready.md", "rejected"), rejected)
        path = next(Path(p) for p in drafts if p.endswith("002-ready.md"))
        self.assertEqual(path.read_text(), bad_ready)  # kept on disk, never posted
        lint = json.loads((path.parent.parent / "outbox-lint.json").read_text())
        self.assertTrue(any("code blocks are not allowed" in p for p in lint[str(path)]["problems"]))
        self.assertFalse(any("```" in b for b in self.linear.bodies("DEV-1")))
        # DEV-2: the worker's own valid ready draft is posted unchanged.
        self.assertTrue(self.linear.last("DEV-2", "ready").startswith(good_ready.strip()))

    def test_invalid_blocked_draft_falls_back_to_the_worker_result(self):
        self.worker_blocks("DEV-1")
        self.drafts[("DEV-1", "implement")] = {"001-blocked.md": "| table | only |\n| --- | --- |\n"}
        self.launch()
        body = self.linear.last("DEV-1", "blocked")
        self.assertIn("> " + WORKER_EXPLANATION, body)
        self.assertIn("The worker's blocked note was not posted because", body)
        self.assertNotIn("| table |", body)

    def test_valid_blocked_draft_is_quoted_in_the_blocked_comment(self):
        self.worker_blocks("DEV-1")
        note = render_samples.BLOCKED_DRAFT.replace("TEAM-12", "DEV-1")
        self.drafts[("DEV-1", "implement")] = {"001-blocked.md": note}
        self.launch()
        body = self.linear.last("DEV-1", "blocked")
        self.assertIn("> I cannot finish DEV-1 because the calibration table", body)
        self.assertIn("> **What is needed**", body)
        self.assertEqual(self.linear.kinds("DEV-1"), ["claim", "blocked"])


class NeedsInputTests(AttentionHarness):
    def block_then_recover(self):
        self.worker_blocks("DEV-1")
        self.launch()
        self.hooks.clear()
        return self.linear.data

    def finish(self):
        self.recover("resume", then="stop")
        entry = self.launch()
        self.assertEqual(entry["started"]["outcome"], "checkpoint")
        self.assertEqual(self.linear.data["status"], "Done")
        kinds = self.linear.kinds("DEV-1")
        self.assertLess(kinds.index("blocked"), kinds.index("recovery"))
        recovery = self.linear.last("DEV-1", "recovery")
        self.assertEqual(first_line(recovery), "A resume recovery for DEV-1 recorded by Owner is being carried out now; "
                                               "no action is needed.")

    def test_label_is_added_on_stop_and_removed_on_recovery(self):
        self.ATTENTION = {"owner_mention": OWNER, "needs_input": {"mechanism": "label", "label": "Needs input"}}
        self.setUp()
        issue = self.block_then_recover()
        self.assertIn("Needs input", issue["labels"])
        self.assertEqual(self.state()["needs_input"]["DEV-1"]["mechanism"], "label")
        self.finish()
        self.assertNotIn("Needs input", self.linear.data["labels"])
        self.assertEqual(self.linear.label_writes, [("DEV-1", ["Implementation", "Standard", "Needs input"]),
                                                    ("DEV-1", ["Implementation", "Standard"])])
        self.assertEqual(self.state()["needs_input"], {})

    def test_state_is_set_on_stop_and_restored_on_recovery(self):
        self.ATTENTION = {"needs_input": {"mechanism": "state", "state": "Blocked"}}
        self.setUp()
        issue = self.block_then_recover()
        self.assertEqual(issue["status"], "Blocked")
        self.assertIn("the owner needs to decide", first_line(self.linear.last("DEV-1", "blocked")))
        self.finish()  # launch preflight accepts the runner's own needs-input state
        # Restored by the recovery, then re-confirmed by the resumed implement step.
        self.assertEqual(self.linear.writes, ["In Progress", "Blocked", "In Progress", "In Progress", "In Review", "Done"])

    def test_mention_only_changes_nothing_on_the_issue(self):
        issue = self.block_then_recover()
        self.assertEqual(self.linear.label_writes, [])
        self.assertEqual(issue["status"], "In Progress")
        self.assertIn(OWNER, first_line(self.linear.last("DEV-1", "blocked")))
        self.assertEqual(self.state()["needs_input"]["DEV-1"], {"mechanism": "mention", "issue": "DEV-1", "applied": False})
        self.finish()

    def test_config_rejects_unknown_mechanism_and_empty_command(self):
        from config import ConfigError
        with self.assertRaisesRegex(ConfigError, "must be one of"):
            make_home(self.root, self.repo, workspace={"attention": {"needs_input": {"mechanism": "email"}}})
            load_config(self.batch, self.home)
        make_home(self.root, self.repo, site={"attention": {"notifier": {"backend": "command"}}})
        with self.assertRaisesRegex(ConfigError, "non-empty command"):
            load_config(self.batch, self.home)


class MessageTests(unittest.TestCase):
    def test_commands_and_owner_fallback(self):
        ctx = dict(render_samples.CTX, owner="the owner", home="/absolute/path/to/home with space")
        body = messages.blocked(ctx, issue="TEAM-1", classification="technical-block", event="checks_failed",
                                error="Repeated unchanged failure or repair limit exhausted; failing: regression",
                                step="validate", result={"summary": "Fixed the parser.", "acceptance": []})
        self.assertEqual(first_line(body), "TEAM-1 is paused because checks still fail after the allowed repairs; the owner "
                                           "needs to choose a recovery.")
        self.assertIn("--home '/absolute/path/to/home with space'", body)
        self.assertIn("failing: regression", body)
        repair = messages.recovery_steps(ctx, issue="TEAM-1", step="repair")
        self.assertIn("An interrupted repair cannot be resumed", repair)
        batch = messages.blocked(ctx, issue=None, classification="environment", error="Linear HTTP 502", step=None)
        self.assertTrue(first_line(batch).startswith("Batch demo-batch is paused by a host or service problem"))


if __name__ == "__main__":
    unittest.main()
