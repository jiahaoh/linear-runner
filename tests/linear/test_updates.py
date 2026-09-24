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
import unittest.mock

from linear_runner.linear import attention
from linear_runner.config import load_config, pin_resolution
from tests.fixtures import CHECKOUT, FakeLinear, make_home
from linear_runner.linear import messages
from linear_runner.reporting import render_samples
from linear_runner.engine.runner import IssueBlocked, Runner, git, write_json
from tests.supervision.test_supervisor import Harness
from linear_runner.linear import updates
from linear_runner.supervision import watchdog

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

    def test_command_blocks_are_allowed_only_in_runner_comments(self):
        body = "TEAM-1 is paused.\n\n**To continue**\nStart it:\n\n```bash\npython3 runner.py launch --batch b\n```\n"
        self.assertEqual(updates.lint(body, kind="runner", limits=LIMITS, allow_commands=True), [])
        self.assertIn("line 6: code blocks are not allowed", updates.lint(body, kind="runner", limits=LIMITS))
        two = body.replace("launch --batch b", "launch --batch b\npython3 runner.py status --batch b")
        self.assertIn("line 6: a command block must hold exactly one command line",
                      updates.lint(two, kind="runner", limits=LIMITS, allow_commands=True))

    def test_render_drops_empty_optional_sections(self):
        body = updates.render("ready", {"issue": "TEAM-1", "summary": "> done", "criteria": "", "limitations": "",
                                        "evidence": ""})
        self.assertEqual(body, "The worker reports TEAM-1 ready for validation; no action is needed.\n\n"
                               "**Worker summary**\n> done\n")

    def test_every_sample_passes_lint_and_the_samples_file_is_current(self):
        runner_limits = dict(LIMITS, max_chars=3500, max_lines=60)
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
                    self.assertEqual(updates.lint(text, kind="runner", limits=runner_limits, allow_commands=True), [])
                    self.assertNotIn("- `", text)  # commands are never inline code in bullets
        for path in updates.TEMPLATE_DIR.glob("*.md"):
            self.assertNotIn("DRAFT", path.read_text())
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
        result, _, _ = runner.run_session("prompt", attempt, phase="implement", model="astra", effort="medium",
                                    writable=True, watch=lambda: runner.poll_outbox(active, "implement", attempt))
        self.assertEqual(seen, [("DEV-1", True)])  # posted while the Codex process was still running
        self.assertEqual(result, {"released": True})
        body = self.linear.bodies("DEV-1")[0]
        self.assertEqual(first_line(body), "The QC report now renders for all tiles; no action is needed.")
        self.assertIn("_Written by the worker (implement phase); posted by the runner._", body)
        self.assertEqual(runner.state["drafts"][str(attempt / "outbox" / "001-progress.md")]["status"], "posted")


# --- Batch scenarios: the three silent-stop cases, fallback, needs-input ------------------------

class AttentionHarness(Harness):
    ATTENTION = {}
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
        self.assertEqual(first_line(body), "DEV-2 is paused because the worker reported that it cannot finish, "
                                           "and it needs your decision to continue.")
        self.assertIn("> " + WORKER_EXPLANATION, body)
        self.assertIn("Record the recovery (you can add --note-file with a note for the worker):\n\n```bash\n"
                      f"python3 {CHECKOUT / 'runner.py'} recover resume --batch fixture --home ", body)
        self.assertNotIn('{"', body)
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
        self.assertEqual(first_line(body), "DEV-1 is paused because the independent review did not accept it, "
                                           "and it needs your decision to continue.")
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
                                           "working on DEV-2, and it needs you to check the host and relaunch.")
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
        self.assertIn("it needs your decision", first_line(self.linear.last("DEV-1", "blocked")))
        self.finish()  # launch preflight accepts the runner's own needs-input state
        # Restored by the recovery, then re-confirmed by the resumed implement step.
        self.assertEqual(self.linear.writes, ["In Progress", "Blocked", "In Progress", "In Progress", "In Review", "Done"])

    def test_mention_only_changes_nothing_on_the_issue(self):
        self.ATTENTION = {"owner_mention": OWNER}
        self.setUp()
        issue = self.block_then_recover()
        self.assertEqual(self.linear.label_writes, [])
        self.assertEqual(issue["status"], "In Progress")
        blocked = self.linear.last("DEV-1", "blocked")
        self.assertEqual(blocked.split("\n\n")[1], OWNER)  # an optional mention is its own paragraph
        self.assertEqual(self.state()["needs_input"]["DEV-1"], {"mechanism": "mention", "issue": "DEV-1", "applied": False})
        self.finish()

    def test_config_rejects_unknown_mechanism_and_empty_command(self):
        from linear_runner.config import ConfigError
        with self.assertRaisesRegex(ConfigError, "must be one of"):
            make_home(self.root, self.repo, workspace={"attention": {"needs_input": {"mechanism": "email"}}})
            load_config(self.batch, self.home)
        make_home(self.root, self.repo, site={"attention": {"notifier": {"backend": "command"}}})
        with self.assertRaisesRegex(ConfigError, "non-empty command"):
            load_config(self.batch, self.home)


class MessageTests(unittest.TestCase):
    def test_commands_are_short_blocks(self):
        ctx = dict(render_samples.CTX, home="/absolute/path/to/home with space")
        body = messages.blocked(ctx, issue="TEAM-1", classification="technical-block", event="checks_failed",
                                error="Repeated unchanged failure or repair limit exhausted; failing: regression",
                                step="validate", result={"summary": "Fixed the parser.", "acceptance": []})
        self.assertEqual(first_line(body), "TEAM-1 is paused because checks still fail after the allowed repairs, and it "
                                           "needs you to choose a recovery.")
        self.assertIn("Then start the batch again:\n\n```bash\npython3 /absolute/path/to/linear-runner/runner.py launch "
                      "--batch demo-batch --home '/absolute/path/to/home with space'\n```", body)
        self.assertIn("failing: regression", body)
        repair = messages.recovery_steps(ctx, issue="TEAM-1", step="repair")
        self.assertIn("An interrupted repair cannot be resumed", repair)
        self.assertIn("recover revalidate", repair)
        self.assertNotIn("--note-file", repair)
        finished = messages.recovery_steps(ctx, issue="TEAM-1", step="repair", event="worker_blocked", repairs=1)
        self.assertNotIn("interrupted", finished)
        self.assertEqual([line.split(" --batch")[0].split("runner.py ")[1] for line in finished.splitlines()
                          if "runner.py" in line],
                         ["recover revalidate", "recover resume --note-file <note file>", "launch",
                          "recover defer --issue TEAM-1 --restore-worktree"])
        exhausted = messages.recovery_steps(ctx, issue="TEAM-1", step="repair", event="worker_blocked", repairs=2)
        self.assertNotIn("--note-file", exhausted)
        failing = messages.recovery_steps(ctx, issue="TEAM-1", step="validate", event="checks_failed")
        self.assertIn("recover revalidate", failing)
        batch = messages.blocked(ctx, issue=None, classification="environment", error="Linear HTTP 502", step=None)
        self.assertTrue(first_line(batch).startswith("Batch demo-batch is paused by a host or service problem"))


if __name__ == "__main__":
    unittest.main()


class BatchIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        self.home, self.batch = make_home(self.root, self.repo)

    def test_bare_id_resolves_in_the_home_and_unknown_id_is_an_error(self):
        from linear_runner.config import ConfigError, batch_argument, resolve_batch
        by_id = load_config("fixture", self.home)
        self.assertEqual(by_id["batch_id"], "fixture")
        self.assertEqual(by_id["_layers"]["batch fixture"], str(self.batch.resolve()))
        self.assertEqual(load_config(str(self.batch), self.home)["_layers"], by_id["_layers"])
        self.assertEqual(batch_argument(by_id), "fixture")
        with self.assertRaisesRegex(ConfigError, "Unknown batch id 'nope': .*batches/nope.json does not exist"):
            load_config("nope", self.home)
        with self.assertRaisesRegex(ConfigError, "neither a batch file path nor a batch id"):
            resolve_batch("bad id", self.home)
        (self.home / "batches" / "renamed.json").write_text(self.batch.read_text())  # its id is still "fixture"
        with self.assertRaisesRegex(ConfigError, "has id 'fixture'"):
            load_config("renamed", self.home)
        # A batch file outside <home>/batches is named by its path in comments.
        outside = self.root / "elsewhere" / "fixture.json"
        outside.parent.mkdir(); outside.write_text(self.batch.read_text())
        self.assertEqual(batch_argument(load_config(str(outside), self.home)), str(outside.resolve()))

    def test_cli_accepts_an_id_and_rejects_an_unknown_one(self):
        from linear_runner.cli import main
        with unittest.mock.patch("sys.stdout") as stdout:
            main(["validate-config", "--batch", "fixture", "--home", str(self.home)])
        self.assertEqual(json.loads("".join(c.args[0] for c in stdout.write.call_args_list))["batch"], "fixture")
        with unittest.mock.patch("sys.stderr") as stderr, self.assertRaises(SystemExit) as raised:
            main(["status", "--batch", "missing-batch", "--home", str(self.home)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Unknown batch id 'missing-batch'", "".join(c.args[0] for c in stderr.write.call_args_list))

    def test_command_prefix_is_a_resolved_site_setting(self):
        self.assertEqual(load_config("fixture", self.home)["attention"]["command_prefix"],
                         f"python3 {CHECKOUT}/runner.py")
        # The default prefix names the checkout's runner.py, which works from any directory.
        shown = subprocess.run([sys.executable, str(CHECKOUT / "runner.py"), "validate-config",
                                "--batch", "fixture", "--home", str(self.home)], cwd=self.root, check=True,
                               capture_output=True, text=True).stdout
        self.assertEqual(json.loads(shown)["batch"], "fixture")
        make_home(self.root, self.repo, site={"attention": {"command_prefix": "${python} -m runner"}})
        config = load_config("fixture", self.home)
        self.assertEqual(messages.command(messages.context(config), "launch"),
                         f"{sys.executable} -m runner launch --batch fixture --home {self.home}")


class DeliverablesTests(AttentionHarness):
    def test_listed_deliverables_are_validated_shown_to_the_reviewer_and_listed_at_done(self):
        (self.root / "outside.html").write_text("not in the worktree")
        def listing(result):
            result["deliverables"] = [{"path": "DEV-1.txt", "description": "The rendered DEV-1 output"},
                                      {"path": "reports/missing.html", "description": "never written"},
                                      {"path": str(self.root / "outside.html"), "description": "outside"}]
        self.hooks[("DEV-1", "implement")] = listing
        self.launch(stop_after=["DEV-1"])
        path = self.repo / "DEV-1.txt"
        ready = self.linear.last("DEV-1", "ready")
        self.assertIn(f"**Deliverables to review**\n- {path} — The rendered DEV-1 output", ready)
        self.assertIn(f"Listed deliverables that were not found: reports/missing.html and {self.root / 'outside.html'}.", ready)
        review_prompt = self.prompts[1]
        self.assertIn(f"open and assess them as part of the review: {path} (The rendered DEV-1 output)", review_prompt)
        done = self.linear.last("DEV-1", "done")
        self.assertIn("the deliverables below are ready for your review.", first_line(done))
        self.assertIn(f"**Deliverables to review**\n- {path} — The rendered DEV-1 output\n\n**Delivered**", done)
        self.assertNotIn("missing.html", done)
        # Nothing listed: no section, and the headline says no action is needed.
        self.assertNotIn("Deliverables", self.linear.last("DEV-1", "claim"))

    def test_schemas_carry_deliverables_for_the_worker_only(self):
        from linear_runner.engine.runner import RESULT_SCHEMA, review_schema
        self.assertIn("deliverables", RESULT_SCHEMA["required"])
        self.assertEqual(RESULT_SCHEMA["properties"]["deliverables"]["items"]["required"], ["path", "description"])
        schema = review_schema(self.linear.data, "sha")
        self.assertNotIn("deliverables", schema["properties"]); self.assertNotIn("deliverables", schema["required"])
        self.assertIn("deliverables", RESULT_SCHEMA["properties"])  # the copy did not change the worker schema
        self.launch(stop_after=["DEV-2"])
        done = self.linear.last("DEV-2", "done")
        self.assertTrue(first_line(done).endswith("so no action is needed."))
        self.assertNotIn("Deliverables", done)


class LabelTests(unittest.TestCase):
    def test_label_writes_preserve_other_labels_and_read_back(self):
        linear = FakeLinear()
        linear.data["labels"] = [{"name": "Implementation"}, {"name": "Standard"}, "Bug"]
        settings = {"mechanism": "label", "label": "Needs input"}
        mark = attention.mark_needs_input(linear, "DEV-1", settings)
        self.assertEqual(linear.label_writes, [("DEV-1", ["Implementation", "Standard", "Bug", "Needs input"])])
        attention.mark_needs_input(linear, "DEV-1", settings)  # already set: no second write
        self.assertEqual(len(linear.label_writes), 1)
        attention.clear_needs_input(linear, mark)
        self.assertEqual(linear.label_writes[-1], ("DEV-1", ["Implementation", "Standard", "Bug"]))
        original = linear.call
        linear.call = lambda name, **args: copy.deepcopy(linear.data)  # write acknowledged but not applied
        with self.assertRaisesRegex(RuntimeError, "read-back does not show the 'Needs input' label"):
            attention.mark_needs_input(linear, "DEV-1", settings)
        linear.call = original


class WatchdogLabelAndTimerTests(AttentionHarness):
    ATTENTION = {"needs_input": {"mechanism": "label", "label": "Needs input"}}

    def running(self, **fields):
        path = self.state_dir / "supervisor.json"
        write_json(path, dict(json.loads(path.read_text()), **dict({"status": "running"}, **fields)))

    def test_alert_labels_the_target_and_the_next_launch_removes_it(self):
        self.launch(stop_after=["DEV-1"])
        config = self.make_runner().config
        self.running()
        later = os.path.getmtime(self.state_dir / "state.json") + 3 * 3600
        result = watchdog.check(config, self.linear, clock=lambda: later, alive=lambda pid: True,
                                notify_run=self.notifier, log=lambda m: None)
        self.assertEqual((result["condition"], result["issue"]), ("stalled", "DEV-3"))
        self.assertIn("Needs input", self.linear.others["DEV-3"]["labels"])
        self.running(status="exited")
        self.launch(stop_after=["DEV-2"], clear_stop=True)
        self.assertNotIn("Needs input", self.linear.others["DEV-3"]["labels"])
        self.assertEqual(json.loads((self.state_dir / "watchdog.json").read_text())["needs_input"], {})

    def test_the_watchdog_stops_its_own_timer_only_when_done(self):
        entry = self.launch(stop_after=["DEV-1"])
        config = self.make_runner().config
        calls = []
        def systemctl(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        timer = "linear-runner-fixture-x-watchdog.timer"
        check = lambda **kw: watchdog.check(config, self.linear, notify_run=self.notifier, log=lambda m: None,
                                            launch_id=entry["launch_id"], timer=timer, systemctl=systemctl, **kw)
        self.running()
        self.assertEqual(check(alive=lambda pid: True)["status"], "ok")
        self.assertEqual(calls, [])  # still watching a running supervisor
        # The supervisor finished, but an alert is still unposted: keep the timer to retry.
        self.running(status="exited")
        record = json.loads((self.state_dir / "watchdog.json").read_text()) if (self.state_dir / "watchdog.json").exists() else {}
        record.setdefault("events", {})["DEV-3/watchdog/9"] = {"key": "DEV-3/watchdog/9", "issue": "DEV-3", "kind": "watchdog",
                                                               "status": "pending", "attempts": 0, "body": "x", "seq": 9}
        write_json(self.state_dir / "watchdog.json", record)
        self.linear.fail_posts = True
        self.assertFalse(check(alive=lambda pid: False)["timer_stopped"])
        self.linear.fail_posts = False
        self.assertTrue(check(alive=lambda pid: False)["timer_stopped"])
        self.assertEqual(calls, [["systemctl", "--user", "stop", timer]])
        self.assertFalse(check(alive=lambda pid: False)["timer_stopped"])  # recorded once
        # A timer whose launch was replaced stops itself.
        other = watchdog.check(config, self.linear, log=lambda m: None, launch_id="L-older", timer="old.timer",
                               systemctl=systemctl)
        self.assertEqual((other["status"], calls[-1]), ("superseded", ["systemctl", "--user", "stop", "old.timer"]))

    def test_gone_alert_stops_the_timer_after_posting(self):
        entry = self.launch(stop_after=["DEV-1"])
        config = self.make_runner().config
        self.running()
        calls = []
        result = watchdog.check(config, self.linear, alive=lambda pid: False, notify_run=self.notifier, log=lambda m: None,
                                launch_id=entry["launch_id"], timer="t.timer",
                                systemctl=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
        self.assertEqual((result["condition"], result["posted"], result["timer_stopped"]), ("gone", True, True))
        self.assertEqual(calls, [["systemctl", "--user", "stop", "t.timer"]])

    def test_stop_command_stops_the_timer_unless_a_supervisor_is_running(self):
        from linear_runner.cli import stop_watchdog_timer
        entry = self.launch(stop_after=["DEV-1"])
        record_path = self.state_dir / "launches" / f"{entry['launch_id']}.json"
        write_json(record_path, dict(json.loads(record_path.read_text()), watchdog_timer={"timer": "w.timer"}))
        calls = []
        run = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        self.running(pid=os.getpid())
        self.assertEqual(stop_watchdog_timer(self.state_dir, run)["state"],
                         "left running until the supervisor exits between issues")
        self.running(status="exited")
        self.assertEqual(stop_watchdog_timer(self.state_dir, run)["state"], "stopped")
        self.assertEqual(calls, [["systemctl", "--user", "stop", "w.timer"]])
        self.assertEqual(stop_watchdog_timer(self.state_dir, run)["state"], "already stopped")
