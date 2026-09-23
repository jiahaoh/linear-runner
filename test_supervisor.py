"""Launch, generic supervisor, recovery commands, decision rules and delivery integrity.

Real Git, checks and state files; fake Codex, Linear and launcher boundaries. No network,
no systemd-run and no live batch.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from config import load_config, pin_resolution, write_resolved
from fixtures import TEST_REGISTRY, FakeLinear, make_home
from launcher import ForegroundBackend, LaunchError, SystemdUserBackend, launch, preflight
import recovery
from recovery import RecoveryError, verify_log
from rules import RuleError, evaluate, parse_rules
import runner as runner_module
from runner import Runner, git, main, project_lock, review_criteria, write_json
from supervisor import SupervisorRefused, supervise

RULES = "# defer this issue instead of stopping everything\ndefer issue when worker blocked 2 times on the same criterion"


def rule_block(text=RULES):
    return "\n\n```linear-runner-rules\n" + text + "\n```\n"


class Harness(unittest.TestCase):
    """Three-issue batch on a real Git worktree with fake Codex and Linear."""
    BATCH = {}
    SITE = {}
    PROJECT = {}
    REGISTRY = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q"); git(self.repo, "checkout", "-q", "-b", "codex/test")
        git(self.repo, "config", "user.name", "Test"); git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("fixture")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "baseline")
        batch = dict({"issues": ["DEV-1", "DEV-2", "DEV-3"], "terminal_issue": "DEV-3"}, **self.BATCH)
        self.home, self.batch = make_home(self.root, self.repo, batch=batch, site=self.SITE or None,
                                          project=self.PROJECT or None, registry=self.REGISTRY)
        self.linear = FakeLinear()
        self.linear.add_issue("DEV-2"); self.linear.add_issue("DEV-3")
        self.calls, self.prompts, self.hooks, self.resumed = [], [], {}, []

    def codex(self, prompt, directory, **kwargs):
        directory = Path(directory)
        phase, issue = directory.name.split("-")[0], directory.parent.parent.name
        self.calls.append((issue, phase)); self.prompts.append(prompt); self.resumed.append(kwargs.get("resume"))
        directory.mkdir(parents=True)
        if phase in ("implement", "repair"):
            (self.repo / "result.txt").write_text("ready")
            (self.repo / f"{issue}.txt").write_text(f"{issue} attempt {len(self.calls)}")
        session = kwargs.get("resume") or f"{issue}-{phase}-{len(self.calls)}"
        write_json(directory / "session.json", {"session_id": session, "requested_model": kwargs["model"],
                   "requested_reasoning_effort": kwargs["effort"], "exit_code": 0,
                   "execution_evidence": {"usage_events": [{"usage": {"input_tokens": 100, "output_tokens": 5}}]}})
        criteria = review_criteria(self.linear.issue(issue))
        result = {"issue_id": issue, "status": "ready", "commit": git(self.repo, "rev-parse", "HEAD"), "summary": "Ready",
                  "acceptance": [{"criterion": c, "satisfied": True, "evidence": "fixture evidence"} for c in criteria],
                  "limitations": []}
        hook = self.hooks.get((issue, phase))
        if hook:
            hook(result)
        return result, [], session

    def blocked(self, times=None):
        """Hook: the model reports every criterion unsatisfied (``times`` attempts, or always)."""
        state = {"left": times}
        def hook(result):
            if state["left"] is None or state["left"] > 0:
                result["status"] = "blocked"
                for entry in result["acceptance"]:
                    entry.update(satisfied=False, evidence="not yet")
                if state["left"] is not None:
                    state["left"] -= 1
        return hook

    def make_runner(self):
        config, fresh = pin_resolution(load_config(self.batch, self.home), self.linear)
        if fresh:
            write_resolved(config)
        runner = Runner(config, self.linear)
        runner.codex = self.codex
        return runner

    def launch(self, **kwargs):
        runner = self.make_runner()
        def run(spec):
            return supervise(runner.config, self.linear, launch_id=spec["launch_id"], stop_after=spec["stop_after"],
                             scope=spec["scope"], runner=self.make_runner())
        self.output = []
        return launch(runner.config, self.linear, backend=ForegroundBackend(run), runner=runner,
                      out=self.output.append, **kwargs)

    def recover(self, kind, **kwargs):
        runner = self.make_runner()
        kwargs.setdefault("reason", "fixture reason")
        kwargs.setdefault("authorized_by", "Owner")
        with project_lock(runner.root / "controller.lock"):
            return getattr(recovery, f"recover_{kind}")(runner, **kwargs)

    @property
    def state_dir(self):
        return self.root / "state" / "fixture"

    def state(self):
        return json.loads((self.state_dir / "state.json").read_text())

    def done(self):
        return [h["issue_id"] for h in self.state()["history"]]


class LaunchAndSupervisorTests(Harness):
    def test_launch_runs_queue_with_lifecycle_readbacks_and_reports(self):
        entry = self.launch()
        self.assertEqual(entry["started"], {"pid": os.getpid(), "exit_code": 0, "outcome": "complete"})
        self.assertEqual([c for c in self.calls], [(i, p) for i in ("DEV-1", "DEV-2", "DEV-3") for p in ("implement", "review")])
        state = self.state()
        self.assertEqual(state["phase"], "queue_complete")
        for issue in ("DEV-1", "DEV-2", "DEV-3"):
            record = json.loads((self.state_dir / "lifecycle" / issue / "readback.json").read_text())
            run = Path(record["run_dir"])
            self.assertEqual(record["live"]["statusType"], "completed")
            self.assertEqual(record["files_sha256"]["final-result.json"],
                             runner_module.hashlib.sha256((run / "final-result.json").read_bytes()).hexdigest())
            self.assertEqual(record["destinations"], [issue, "DEV-3"] if issue != "DEV-3" else ["DEV-3"])
            self.assertTrue(state["lifecycle"][issue]["synced_at"])
        posted = [(issue, marker) for issue, marker, _ in self.linear.posts if "lifecycle" in marker]
        self.assertEqual(len(posted), 5)
        self.assertTrue((self.state_dir / "STOP").exists())  # durable hold on exit
        status = json.loads((self.state_dir / "supervisor.json").read_text())
        self.assertEqual((status["launch_id"], status["status"], status["outcome"]), (entry["launch_id"], "exited", "complete"))
        self.assertTrue((self.state_dir / "launches" / f"{entry['launch_id']}.json").is_file())
        self.assertTrue(json.loads((self.state_dir / "preflight.json").read_text())["passed"])
        summary = json.loads(self.output[0])
        self.assertEqual(summary["state_dir"], str(self.state_dir))
        self.assertEqual(summary["pid"], os.getpid())

    def test_planned_checkpoint_and_continuation(self):
        entry = self.launch(stop_after=["DEV-1"])
        self.assertEqual(entry["started"]["outcome"], "checkpoint")
        self.assertEqual(self.done(), ["DEV-1"])
        self.assertIn("Planned checkpoint after DEV-1", (self.state_dir / "STOP").read_text())
        self.assertEqual(self.linear.others["DEV-2"]["statusType"], "unstarted")
        with self.assertRaisesRegex(LaunchError, "STOP marker present"):
            self.launch()  # a bare continuation (no pending recovery) needs --clear-stop
        # Continuation after the checkpoint: one command, a new CLI checkpoint after DEV-2.
        entry = self.launch(stop_after=["DEV-2"], clear_stop=True)
        self.assertEqual((entry["started"]["outcome"], self.done()), ("checkpoint", ["DEV-1", "DEV-2"]))
        cleared = json.loads((self.state_dir / "launches" / f"{entry['launch_id']}.json").read_text())["cleared_stop"]
        self.assertEqual((cleared["text"].split(" (")[0], cleared["by"]), ("Planned checkpoint after DEV-1", "--clear-stop"))
        entry = self.launch(clear_stop=True)
        self.assertEqual((entry["started"]["outcome"], self.done()), ("complete", ["DEV-1", "DEV-2", "DEV-3"]))
        self.assertEqual([c["issue"] for c in self.state()["checkpoints_reached"]], ["DEV-1", "DEV-2"])
        self.assertEqual(self.calls.count(("DEV-1", "implement")), 1)

    def test_batch_planned_checkpoint_field(self):
        home, batch = make_home(self.root, self.repo, batch={"issues": ["DEV-1", "DEV-2", "DEV-3"], "terminal_issue": "DEV-3",
                                                              "supervision": {"stop_after": ["DEV-2"]}})
        self.batch = batch
        self.assertEqual(self.launch()["started"]["outcome"], "checkpoint")
        self.assertEqual(self.done(), ["DEV-1", "DEV-2"])

    def test_preflight_records_reuse_and_rerun_reasons(self):
        self.launch(stop_after=["DEV-1"])
        first = json.loads((self.state_dir / "preflight.json").read_text())
        self.assertEqual({n: s["reason"] for n, s in first["steps"].items()},
                         {"config": "no previous preflight result", "worktree": "no previous preflight result",
                          "model_catalog": "no previous preflight result", "linear": "live state: always re-read"})
        self.launch(stop_after=["DEV-2"], clear_stop=True)
        second = json.loads((self.state_dir / "preflight.json").read_text())["steps"]
        self.assertTrue(second["config"]["reused"]); self.assertTrue(second["model_catalog"]["reused"])
        self.assertEqual(second["config"]["reused_from"], first["launch_id"])
        self.assertEqual(second["worktree"]["reason"], "changed: source")
        self.assertFalse(second["linear"]["reused"])
        catalog = self.root / "models.json"
        catalog.write_text(catalog.read_text() + "\n")
        self.launch(stop_after=["DEV-3"], clear_stop=True)
        third = json.loads((self.state_dir / "preflight.json").read_text())["steps"]
        self.assertEqual(third["model_catalog"]["reason"], "changed: model_catalog")
        self.assertTrue(third["config"]["reused"])

    def test_invalid_state_or_marker_is_refused_before_any_linear_write(self):
        (self.state_dir).mkdir(parents=True)
        (self.state_dir / "STOP").write_text("held")
        with self.assertRaisesRegex(LaunchError, "STOP"):
            self.launch(clear_stop=False)
        self.assertFalse(self.calls); self.assertFalse(self.linear.posts); self.assertFalse(self.linear.writes)
        # A supervisor cannot run without the launch preflight bound to its launch ID.
        runner = self.make_runner()
        (self.state_dir / "STOP").unlink()
        with self.assertRaisesRegex(SupervisorRefused, "No launch preflight"):
            supervise(runner.config, self.linear, launch_id="L-unknown", runner=runner)
        self.assertFalse((self.state_dir / "STOP").exists())

    def test_supervisor_refuses_changed_source_after_preflight(self):
        runner = self.make_runner()
        record = preflight(runner.config, runner, launch_id="L-fixture")
        self.assertTrue(record["passed"])
        (self.repo / "README.md").write_text("changed"); git(self.repo, "commit", "-qam", "outside")
        with self.assertRaisesRegex(SupervisorRefused, "Source revision changed|branch moved"):
            supervise(runner.config, self.linear, launch_id="L-fixture", runner=self.make_runner())

    def test_gate_failure_is_batch_level_and_pauses(self):
        home, batch = make_home(self.root, self.repo, batch={"issues": ["DEV-1", "DEV-2", "DEV-3"], "terminal_issue": "DEV-3",
                                                              "required_done": ["READY-1"]})
        self.batch = batch
        self.linear.others["READY-1"] = {"id": "READY-1", "statusType": "started"}
        with self.assertRaisesRegex(LaunchError, "READY-1 is not Done"):
            self.launch()
        self.assertFalse(self.calls)


class SystemdBackendTests(Harness):
    SITE = {"launcher": {"backend": "systemd-user", "python": "${python}", "cpu_list": "0",
                         "environment": {"PATH": "/usr/bin:/bin"}, "startup_timeout_seconds": 5}}

    def fake(self, unit_state="active", start_supervisor=True):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "systemd-run":
                if start_supervisor:
                    launch_id = argv[argv.index("--launch-id") + 1]
                    write_json(self.state_dir / "supervisor.json", {"launch_id": launch_id, "pid": 4242, "status": "running"})
                return subprocess.CompletedProcess(argv, 0, "", "Running as unit: fixture.service\n")
            return subprocess.CompletedProcess(argv, 0, f"MainPID=4242\nActiveState={unit_state}\nSubState=running\n"
                                                        "ExecMainStatus=0\nResult=success\n", "")
        clock = iter(range(0, 100))
        return SystemdUserBackend(run=run, sleep=lambda s: None, clock=lambda: next(clock)), calls

    def test_systemd_argv_inherits_credentials_by_name_and_confirms_startup(self):
        backend, calls = self.fake()
        runner = self.make_runner()
        with patch.dict(os.environ, {"TEST_LINEAR_TOKEN": "secret-token-value"}):
            entry = launch(runner.config, self.linear, backend=backend, runner=runner, out=lambda text: None,
                           stop_after=["DEV-1"])
        argv = calls[0]
        self.assertEqual(argv[:2], ["systemd-run", "--user"])
        unit = argv[2].split("=", 1)[1]
        self.assertTrue(unit.startswith("linear-runner-fixture-L-") and unit.endswith(".service"))
        for expected in ("--property=Restart=no", "--property=KillMode=control-group",
                         f"--property=StandardOutput=append:{self.state_dir / 'supervisor.log'}",
                         f"--property=WorkingDirectory={Path(runner_module.__file__).parent}",
                         "--setenv=PATH=/usr/bin:/bin", "--setenv=TEST_LINEAR_TOKEN"):
            self.assertIn(expected, argv)
        self.assertTrue(any(a.startswith("--property=ExecStopPost=") and a.endswith(str(self.state_dir / "STOP")) for a in argv))
        command = argv[argv.index("-c") - 1:]
        self.assertTrue(command[0].endswith("taskset")); self.assertEqual(command[1:3], ["-c", "0"])
        self.assertEqual(command[3:6], [sys.executable, str(Path(runner_module.__file__).parent / "runner.py"), "supervise"])
        self.assertEqual(command[-2:], ["--stop-after", "DEV-1"])
        self.assertEqual(calls[1][:3], ["systemctl", "--user", "show"])
        self.assertEqual(entry["confirmation"]["supervisor"]["pid"], 4242)
        record = (self.state_dir / "launches" / f"{entry['launch_id']}.json").read_text()
        self.assertNotIn("secret-token-value", record)
        self.assertFalse(self.calls)  # nothing was dispatched by the launcher itself

    def test_supervisor_refusal_after_startup_fails_the_launch(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "systemd-run":
                launch_id = argv[argv.index("--launch-id") + 1]
                write_json(self.state_dir / "supervisor.json", {"launch_id": launch_id, "pid": 4242, "status": "refused",
                                                                "error": "STOP marker present"})
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "ActiveState=inactive\n", "")
        runner = self.make_runner()
        with self.assertRaisesRegex(LaunchError, "refused to start: STOP marker present"):
            launch(runner.config, self.linear, backend=SystemdUserBackend(run=run, sleep=lambda s: None),
                   runner=runner, out=lambda text: None)

    def test_failed_unit_is_reported_and_holds_the_batch(self):
        backend, _ = self.fake(unit_state="failed", start_supervisor=False)
        runner = self.make_runner()
        with self.assertRaisesRegex(LaunchError, "stopped before the supervisor started"):
            launch(runner.config, self.linear, backend=backend, runner=runner, out=lambda text: None)
        self.assertIn("failed", (self.state_dir / "STOP").read_text())
        record = json.loads(next((self.state_dir / "launches").glob("*.json")).read_text())
        self.assertIn("stopped before", record["error"])


class RecoveryScenarioTests(Harness):
    BATCH = {"supervision": {"on_block": "stop"}}

    def test_review_only_recovery_with_redelivery(self):
        self.hooks[("DEV-1", "review")] = self.blocked(times=1)
        entry = self.launch()
        self.assertEqual(entry["started"]["exit_code"], 1)
        state = self.state()
        self.assertEqual((state["phase"], state["active"]["step"]), ("paused", "review"))
        self.assertEqual(state["blocks"]["DEV-1"][0]["event"], "review_blocked")
        with self.assertRaisesRegex(LaunchError, "STOP marker present"):
            self.launch()
        with self.assertRaisesRegex(LaunchError, "paused"):
            self.launch(clear_stop=True)  # clearing STOP alone never resumes a paused batch
        record = self.recover("review", redeliver=True)
        self.assertEqual(record["then"], "continue")
        self.assertEqual(record["details"]["prior_reviews"][0].split("-")[0], "review")
        entry = self.launch()  # no --clear-stop: the recovery was recorded against this marker
        self.assertEqual(entry["started"]["outcome"], "complete")
        self.assertEqual(self.calls[:3], [("DEV-1", "implement"), ("DEV-1", "review"), ("DEV-1", "review")])
        self.assertEqual(self.done(), ["DEV-1", "DEV-2", "DEV-3"])
        launch_record = json.loads((self.state_dir / "launches" / f"{entry['launch_id']}.json").read_text())
        self.assertEqual(launch_record["cleared_stop"]["by"], f"pending recovery {record['id']}")
        run = Path(self.state()["history"][0]["run_dir"])
        self.assertEqual(len(list(run.glob("delivery-superseded-*"))), 1)
        self.assertTrue((run / "delivery" / "context.json").is_file())
        consumed = self.state()["recoveries"][0]["consumed"]
        self.assertEqual(consumed["launch_id"], entry["launch_id"])
        events = [e["event"] for e in verify_log(self.state_dir)]
        self.assertEqual(events, ["recorded", "consumed"])

    def test_stop_written_after_the_recovery_still_needs_clear_stop(self):
        self.hooks[("DEV-1", "review")] = self.blocked(times=1)
        self.launch()
        self.recover("review", then="stop")
        (self.state_dir / "STOP").write_text("Operator hold written after the recovery.")
        with self.assertRaisesRegex(LaunchError, "changed after recovery R-"):
            self.launch()
        entry = self.launch(clear_stop=True)
        self.assertEqual((entry["started"]["outcome"], self.done()), ("checkpoint", ["DEV-1"]))  # --then stop

    def test_review_recovery_repins_an_authorized_criterion_clarification(self):
        self.hooks[("DEV-1", "review")] = self.blocked(times=1)
        self.launch()
        self.linear.data["description"] = "- [ ] Produce validated output (lifecycle evidence is checked after review)"
        self.recover("review")
        with self.assertRaisesRegex(LaunchError, "changed since intake"):
            self.launch()
        with self.assertRaisesRegex(RecoveryError, "--authorized-by"):
            self.recover("cancel", authorized_by="")
        self.recover("cancel", reason="recorded without the clarified criterion")
        self.assertEqual(self.state()["recoveries"][0]["cancelled"]["reason"], "recorded without the clarified criterion")
        record = self.recover("review", repin=True)
        self.assertEqual(record["details"]["contract"]["new_criteria"],
                         ["Produce validated output (lifecycle evidence is checked after review)"])
        self.assertEqual(self.launch()["started"]["outcome"], "complete")
        run = Path(self.state()["history"][0]["run_dir"])
        self.assertEqual(len(list(run.glob("intake-before-R-*.json"))), 1)
        self.assertTrue((self.state_dir / "lifecycle" / "DEV-1" / "readback.json").is_file())
        self.assertEqual(self.linear.data["description"], "- [x] Produce validated output (lifecycle evidence is checked after review)")
        self.assertEqual([e["event"] for e in verify_log(self.state_dir)], ["recorded", "cancelled", "recorded", "consumed"])

    def test_review_recovery_cannot_start_other_model_phases(self):
        runner = self.make_runner()
        runner.allowed_phases = {"review"}
        with self.assertRaisesRegex(RuntimeError, "does not authorize a implement"):
            runner.model_phase({"issue": self.linear.data}, "implement", "prompt")

    def test_soft_budget_reconciliation_with_recorded_allowance(self):
        registry = copy.deepcopy(TEST_REGISTRY)
        registry["phases"]["phases"]["implement"]["budget"]["input_tokens"] = 50
        self.home, self.batch = make_home(self.root, self.repo, registry=registry,
                                          batch={"issues": ["DEV-1", "DEV-2", "DEV-3"], "terminal_issue": "DEV-3",
                                                 "supervision": {"on_block": "stop"}})
        self.launch()
        active = self.state()["active"]
        self.assertEqual(active["budget_exceeded"]["observed"]["input_tokens"], 100)
        with self.assertRaisesRegex(RecoveryError, "recover budget"):
            self.recover("resume")
        with self.assertRaisesRegex(RecoveryError, "for phase 'implement'"):
            self.recover("budget", phase="review", limits={"input_tokens": 500, "output_tokens": 50, "tool_calls": 10})
        with self.assertRaisesRegex(RecoveryError, "explicitly"):
            self.recover("budget", phase="implement", limits={"input_tokens": 500})
        record = self.recover("budget", phase="implement", then="stop",
                              limits={"input_tokens": 500, "output_tokens": 50, "tool_calls": 10})
        self.assertEqual(record["details"]["previous_limits"]["input_tokens"], 50)
        self.assertEqual(record["details"]["observed"]["input_tokens"], 100)
        active = self.state()["active"]
        self.assertNotIn("budget_exceeded", active)
        self.assertEqual(active["budget_reconciliations"][0]["checkpoint"]["observed"]["input_tokens"], 100)
        self.assertEqual(self.launch()["started"]["outcome"], "checkpoint")
        self.assertEqual(self.calls, [("DEV-1", "implement"), ("DEV-1", "implement"), ("DEV-1", "review")])
        self.assertEqual(self.resumed[1], "DEV-1-implement-1")  # same session, usage not reset
        run = Path(self.state()["history"][0]["run_dir"])
        self.assertEqual(len(list(run.glob("implement-*/session.json"))), 2)

    def test_publish_only_recovery_runs_no_model(self):
        original = self.linear.call
        def interrupted(name, **args):
            if args.get("state") == "Done":
                raise RuntimeError("interrupted write")
            return original(name, **args)
        self.linear.call = interrupted
        self.launch()
        self.assertEqual(self.state()["active"]["step"], "publish")
        self.linear.call = original
        path = self.state_dir / "state.json"
        saved = path.read_text()
        tampered = json.loads(saved)
        tampered["active"]["accepted_result"]["acceptance"][0]["evidence"] = " "
        path.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(RecoveryError, "does not validate"):
            self.recover("publish")
        path.write_text(saved)
        self.recover("publish", then="stop")
        before = list(self.calls)
        entry = self.launch()
        self.assertEqual((entry["started"]["outcome"], self.calls), ("checkpoint", before))
        self.assertEqual(self.linear.data["statusType"], "completed")
        self.assertTrue((self.state_dir / "lifecycle" / "DEV-1" / "readback.json").is_file())

    def test_resume_blocked_worker_with_recorded_note(self):
        self.hooks[("DEV-1", "implement")] = self.blocked(times=1)
        self.launch()
        self.assertEqual(self.state()["blocks"]["DEV-1"][0]["unsatisfied"], ["Produce validated output"])
        note = self.root / "note.md"; note.write_text("Try the simple adapter first; record anything deferred.")
        with self.assertRaisesRegex(RecoveryError, "--reason"):
            self.recover("resume", reason="  ", note_file=str(note))
        self.assertIsNone(self.state().get("pending_recovery"))
        record = self.recover("resume", note_file=str(note), then="continue")
        self.assertEqual(record["authorized_by"], "Owner")
        with self.assertRaisesRegex(RecoveryError, "already pending"):
            self.recover("resume")
        entry = self.launch()
        self.assertEqual(entry["started"]["outcome"], "complete")
        self.assertEqual(self.calls[:3], [("DEV-1", "implement"), ("DEV-1", "implement"), ("DEV-1", "review")])
        self.assertEqual(self.resumed[1], "DEV-1-implement-1")
        self.assertIn("Try the simple adapter first", self.prompts[1])
        self.assertIn("authorized by Owner", self.prompts[2])  # the reviewer sees the recorded note too
        run = Path(self.state()["history"][0]["run_dir"])
        self.assertEqual(json.loads((run / "intake.json").read_text())["operator_notes"][0]["authorized_by"], "Owner")

    def test_defer_active_issue_and_continue_with_independent_issue(self):
        self.hooks[("DEV-2", "implement")] = self.blocked()
        self.launch()
        self.assertEqual((self.done(), self.state()["active"]["issue_id"]), (["DEV-1"], "DEV-2"))
        self.assertTrue(git(self.repo, "status", "--porcelain"))
        with self.assertRaisesRegex(RecoveryError, "uncommitted"):
            self.recover("defer", issue="DEV-2")
        record = self.recover("defer", issue="DEV-2", restore_worktree=True)
        ref = record["details"]["park"]["parked_ref"]
        self.assertIn("DEV-2.txt", git(self.repo, "show", "--stat", ref))
        self.assertFalse(git(self.repo, "status", "--porcelain"))
        entry = self.launch()
        self.assertEqual(entry["started"]["outcome"], "partial")
        self.assertEqual(self.done(), ["DEV-1", "DEV-3"])
        self.assertEqual(self.linear.others["DEV-2"]["statusType"], "started")  # never accepted or reset
        report = json.loads((self.state_dir / "terminal-report.json").read_text())
        self.assertEqual((report["outcome"], list(report["deferred"])), ("partial", ["DEV-2"]))

    def test_defer_refuses_while_another_issue_is_active(self):
        self.hooks[("DEV-1", "implement")] = self.blocked(times=1)
        self.launch()
        with self.assertRaisesRegex(RecoveryError, "DEV-1 is active"):
            self.recover("defer", issue="DEV-2")
        self.assertNotIn("deferred", self.state())
        self.assertIsNone(self.state().get("pending_recovery"))

    def test_dependents_of_a_deferred_issue_wait(self):
        self.linear.others["DEV-3"]["relations"] = {"blockedBy": [{"id": "DEV-2"}]}
        self.hooks[("DEV-2", "implement")] = self.blocked()
        self.launch()
        self.recover("defer", issue="DEV-2", restore_worktree=True)
        self.launch()
        report = json.loads((self.state_dir / "terminal-report.json").read_text())
        self.assertEqual(report["waiting"], {"DEV-3": ["DEV-2"]})
        self.assertNotIn(("DEV-3", "implement"), self.calls)

    def test_parked_issue_can_be_restored_and_resumed(self):
        self.linear.others["DEV-2"]["relations"] = {"blockedBy": [{"id": "DEV-1"}]}
        self.linear.others["DEV-3"]["relations"] = {"blockedBy": [{"id": "DEV-1"}]}
        self.hooks[("DEV-1", "implement")] = self.blocked(times=1)
        self.launch()
        worktree = (self.repo / "DEV-1.txt").read_text()
        self.recover("defer", issue="DEV-1", restore_worktree=True)
        self.assertFalse((self.repo / "DEV-1.txt").exists())
        self.assertEqual(self.launch()["started"]["outcome"], "partial")
        self.recover("resume", issue="DEV-1", then="continue")
        self.assertEqual((self.repo / "DEV-1.txt").read_text(), worktree)
        self.assertEqual(self.launch()["started"]["outcome"], "complete")
        self.assertEqual(self.done(), ["DEV-1", "DEV-2", "DEV-3"])

    def test_interrupted_lifecycle_sync_is_reconciled_without_model_work(self):
        original = self.linear.summary
        def flaky(issue, marker, body):
            if "lifecycle" in marker:
                raise RuntimeError("Linear offline during lifecycle post")
            return original(issue, marker, body)
        self.linear.summary = flaky
        self.launch()
        state = self.state()
        self.assertEqual((self.done(), state["phase"], state["lifecycle"]["DEV-1"]["synced_at"]), (["DEV-1"], "paused", None))
        self.linear.summary = original
        self.recover("resume", then="continue")
        calls = list(self.calls)
        self.launch(stop_after=["DEV-2"])
        self.assertEqual(self.calls[:len(calls)], calls)
        self.assertEqual(len(list((self.state_dir / "lifecycle" / "DEV-1").glob("readback-superseded-*.json"))), 1)
        self.assertTrue(self.state()["lifecycle"]["DEV-1"]["synced_at"])

    def test_expected_state_mismatch_refuses_the_recovery(self):
        self.hooks[("DEV-1", "implement")] = self.blocked(times=1)
        self.launch()
        self.recover("resume")
        path = self.state_dir / "state.json"
        state = json.loads(path.read_text()); state["history"].append({"issue_id": "DEV-9"}); path.write_text(json.dumps(state))
        with self.assertRaisesRegex(LaunchError, "State changed since recovery"):
            self.launch()

    def test_recovery_log_is_hash_chained(self):
        self.hooks[("DEV-1", "implement")] = self.blocked(times=1)
        self.launch()
        self.recover("resume")
        self.launch()
        entries = verify_log(self.state_dir)
        self.assertEqual([e["event"] for e in entries], ["recorded", "consumed"])
        self.assertEqual(entries[0]["reason"], "fixture reason")
        log = self.state_dir / recovery.LOG_NAME
        log.write_text(log.read_text().replace("fixture reason", "edited reason"))
        with self.assertRaisesRegex(RecoveryError, "line 2 does not continue the hash chain"):
            verify_log(self.state_dir)


class OnBlockPolicyTests(Harness):
    BATCH = {"supervision": {"report_issues": ["TRACK-1"]}}  # on_block defaults to continue_independent

    def test_block_defers_and_independent_issues_continue(self):
        self.linear.others["DEV-3"]["relations"] = {"blockedBy": [{"id": "DEV-1"}]}
        self.hooks[("DEV-1", "implement")] = self.blocked()
        entry = self.launch()
        self.assertEqual(entry["started"]["outcome"], "partial")
        self.assertEqual(self.done(), ["DEV-2"])
        state = self.state()
        self.assertEqual(state["deferred"]["DEV-1"]["policy"], "on_block=continue_independent")
        self.assertTrue(state["deferred"]["DEV-1"]["park"]["parked_ref"].startswith("refs/linear-runner/parked/fixture/DEV-1/"))
        self.assertNotIn("rule_applications", state)
        self.assertEqual([e["event"] for e in verify_log(self.state_dir)], ["deferred"])
        targets = {issue for issue, marker, _ in self.linear.posts if "lifecycle" in marker}
        self.assertEqual(targets, {"DEV-2", "DEV-3", "TRACK-1"})

    def test_batch_level_failures_still_stop(self):
        self.linear.others["DEV-2"]["assigneeId"] = "someone-else"
        entry = self.launch()
        self.assertEqual(entry["started"]["exit_code"], 1)
        self.assertEqual(self.done(), ["DEV-1"])
        self.assertEqual(self.state()["phase"], "paused")


class DecisionRuleTests(Harness):
    BATCH = {"supervision": {"on_block": "stop"}}

    def test_rule_defers_after_repeated_block_on_same_criterion(self):
        self.linear.data["description"] += rule_block()
        self.hooks[("DEV-1", "implement")] = self.blocked()
        self.launch()
        self.assertEqual(self.state()["phase"], "paused")  # one block does not match min_count 2
        self.recover("resume", then="continue")
        entry = self.launch()
        self.assertEqual(entry["started"]["outcome"], "partial")
        state = self.state()
        application = state["rule_applications"][0]
        rule = parse_rules(rule_block())[0]
        self.assertTrue(rule["id"].startswith("rule-"))
        self.assertEqual((application["rule"]["text"], application["blocks"], application["matched_criteria"]),
                         ("defer issue when worker blocked 2 times on the same criterion", ["DEV-1#1", "DEV-1#2"],
                          ["Produce validated output"]))
        self.assertEqual((state["deferred"]["DEV-1"]["rule"], state["deferred"]["DEV-1"]["rule_text"]),
                         (rule["id"], rule["text"]))
        self.assertEqual(self.done(), ["DEV-2", "DEV-3"])
        self.assertEqual([e["event"] for e in verify_log(self.state_dir)],
                         ["recorded", "consumed", "rule_applied", "deferred"])

    def test_stop_batch_rule_overrides_the_continue_default(self):
        self.linear.data["description"] += rule_block("stop batch when worker blocked 1 time")
        self.hooks[("DEV-1", "implement")] = self.blocked()
        home, batch = make_home(self.root, self.repo, batch={"issues": ["DEV-1", "DEV-2", "DEV-3"], "terminal_issue": "DEV-3"})
        self.batch = batch  # default on_block (continue_independent) would otherwise defer DEV-1
        self.assertEqual(self.launch()["started"]["exit_code"], 1)
        self.assertEqual((self.state()["phase"], self.done()), ("paused", []))
        self.assertEqual(self.state()["rule_applications"][0]["rule"]["text"], "stop batch when worker blocked 1 time")

    def test_invalid_rule_block_fails_preflight(self):
        self.linear.others["DEV-2"]["description"] += rule_block("# fine\naccept issue when worker blocked 1 time")
        with self.assertRaisesRegex(LaunchError, "invalid decision rules: linear-runner-rules line 2: "
                                                 "'accept issue when worker blocked 1 time' is not a rule"):
            self.launch()

    def test_parse_valid_lines(self):
        text = ("# comments and blank lines are ignored\n\n"
                "Defer Issue when Worker Blocked 2 times on the same criterion\n"
                "stop batch  when review blocked 1 time\n"
                'defer issue when review blocked 3 times on "A <issue id="x">DEV-2</issue>"\n'
                "defer issue when worker blocked 1 time")
        rules = parse_rules("- [ ] A <issue id=\"x\">DEV-2</issue>" + rule_block(text), ['A <issue id="x">DEV-2</issue>'])
        self.assertEqual([r["text"] for r in rules], [
            "defer issue when worker blocked 2 times on the same criterion",
            "stop batch when review blocked 1 time",
            'defer issue when review blocked 3 times on "A <issue id="x">DEV-2</issue>"',
            "defer issue when worker blocked 1 time"])
        self.assertEqual([(r["when"], r["then"]["action"]) for r in rules[:3]], [
            ({"event": "worker_blocked", "min_count": 2, "same_criterion": True}, "defer_issue"),
            ({"event": "review_blocked", "min_count": 1}, "stop_batch"),
            ({"event": "review_blocked", "min_count": 3, "criterion": 'A <issue id="x">DEV-2</issue>'}, "defer_issue")])
        # The id depends only on the normalized text, so it is stable across spacing and case.
        self.assertEqual(parse_rules(rule_block("DEFER ISSUE  when worker blocked 2 TIMES on the same criterion"))[0]["id"],
                         rules[0]["id"])
        self.assertEqual(parse_rules("No rules here"), [])

    def test_each_invalid_form_names_the_line(self):
        cases = [("accept issue when worker blocked 1 time", "line 1: 'accept issue when worker blocked 1 time' is not a rule"),
                 ("defer issue when tests failed 1 time", "is not a rule; expected: <action> when <event>"),
                 ("defer issue when worker blocked twice", "is not a rule"),
                 ("defer issue when worker blocked 0 times", "the count must be at least 1"),
                 ("defer issue when worker blocked 2 time", "write '1 time' or '2 times'"),
                 ("defer issue when worker blocked 1 times", "write '1 time' or '2 times'"),
                 ("defer issue when worker blocked 1 time on the same criteria", "is not a rule"),
                 ("defer issue when worker blocked 1 time on A", "is not a rule"),
                 ('defer issue when worker blocked 1 time on "a"', '"a" is not an unchecked criterion'),
                 ("defer issue when worker blocked 1 time\ndefer issue when worker blocked 1 time", "line 2: .* repeats an earlier rule"),
                 ('{"version": 1}', "line 1: '{\"version\": 1}' is not a rule")]
        for text, error in cases:
            with self.subTest(text=text), self.assertRaisesRegex(RuleError, error):
                parse_rules(rule_block(text), ["A"])
        with self.assertRaisesRegex(RuleError, "more than one"):
            parse_rules(rule_block() + rule_block())
        with self.assertRaisesRegex(RuleError, "no well-formed"):
            parse_rules("see linear-runner-rules below")

    def test_evaluate_exact_matches(self):
        rules = parse_rules(rule_block(RULES), ["A", "B"])
        blocks = [{"id": "1", "event": "worker_blocked", "unsatisfied": ["A", "B"]},
                  {"id": "2", "event": "review_blocked", "unsatisfied": ["A"]},
                  {"id": "3", "event": "worker_blocked", "unsatisfied": ["B"]}]
        self.assertIsNone(evaluate(rules, blocks[:2]))
        self.assertEqual(evaluate(rules, blocks)["matched_criteria"], ["B"])
        self.assertIsNone(evaluate(rules, [dict(blocks[0], unsatisfied=["A"]), dict(blocks[2])]))
        exact = parse_rules(rule_block('stop batch when review blocked 1 time on "A"'), ["A"])
        self.assertEqual(evaluate(exact, blocks)["rule"]["then"]["action"], "stop_batch")
        self.assertIsNone(evaluate(parse_rules(rule_block('stop batch when review blocked 1 time on "B"'), ["B"]), blocks))


RENDERER = """
import hashlib, json, os, pathlib
context = pathlib.Path(os.environ['RUNNER_DELIVERY_CONTEXT']); data = json.loads(context.read_text())
packet = context.parent / 'packet'; packet.mkdir()
(packet / 'review.html').write_text('<p>report</p>')
commit = data['commit'] if os.environ.get('FIXTURE_REVISION') != 'wrong' else 'f' * 40
(packet / 'manifest.json').write_text(json.dumps({'code_commit': commit,
    'report_sha256': hashlib.sha256((packet / 'review.html').read_bytes()).hexdigest()}))
(packet / 'browser.json').write_text(json.dumps({'passed': True}))
"""
INTEGRITY = {"required_checks": ["output"], "manifest": "packet/manifest.json", "revision_field": "code_commit",
             "file_hashes": {"report_sha256": "review.html"}, "true_fields": [{"file": "browser.json", "field": "passed"}]}


class DeliveryIntegrityTests(Harness):
    PROJECT = {"delivery_checks": [{"cwd": ".", "command": ["${python}", "-c", RENDERER]}], "delivery_integrity": INTEGRITY}

    def test_generic_integrity_passes_and_is_hashed_into_lifecycle(self):
        self.launch(stop_after=["DEV-1"])
        run = Path(self.state()["history"][0]["run_dir"])
        integrity = json.loads((run / "delivery" / "integrity.json").read_text())
        self.assertTrue(integrity["passed"])
        self.assertEqual(integrity["validation_checks"], ["output"])
        self.assertIn("review.html", integrity["files_sha256"])
        readback = json.loads((self.state_dir / "lifecycle" / "DEV-1" / "readback.json").read_text())
        self.assertIn("delivery/integrity.json", readback["files_sha256"])
        context = json.loads((run / "delivery" / "context.json").read_text())
        self.assertEqual(context["issue_run_dir"], str(run))

    def test_wrong_revision_blocks_before_review(self):
        with patch.dict(os.environ, {"FIXTURE_REVISION": "wrong"}):
            self.launch()
        state = self.state()
        self.assertEqual((state["phase"], state["active"]["step"]), ("paused", "delivery"))
        self.assertEqual(state["blocks"]["DEV-1"][0]["event"], "delivery_failed")
        self.assertNotIn(("DEV-1", "review"), self.calls)

    def test_verify_delivery_rejects_each_integrity_failure(self):
        from delivery import DeliveryError, sha256, verify_delivery
        directory = self.root / "delivery"; (directory / "packet").mkdir(parents=True)
        log = self.root / "check.log"; log.write_text("ok")
        records = [{"name": "output", "exit_code": 0, "log": str(log), "sha256": sha256(log)}]
        packet = directory / "packet"
        (packet / "review.html").write_text("x")
        write_json(packet / "browser.json", {"passed": True})
        def manifest(**changes):
            write_json(packet / "manifest.json", dict({"code_commit": "c" * 40, "report_sha256": sha256(packet / "review.html")}, **changes))
        manifest()
        self.assertEqual(verify_delivery(INTEGRITY, directory, "c" * 40, records)["commit"], "c" * 40)
        cases = [
            (lambda: None, dict(INTEGRITY, required_checks=["docs"]), "required check"),
            (lambda: log.write_text("edited"), INTEGRITY, "log is missing or altered"),
            (lambda: manifest(code_commit="d" * 40), INTEGRITY, "committed revision"),
            (lambda: manifest(report_sha256="0"), INTEGRITY, "SHA-256"),
            (lambda: write_json(packet / "browser.json", {"passed": "yes"}), INTEGRITY, "is not true"),
            (lambda: (packet / "manifest.json").unlink(), INTEGRITY, "manifest is missing"),
            (lambda: None, dict(INTEGRITY, manifest="../outside.json"), "escapes"),
        ]
        for mutate, spec, error in cases:
            with self.subTest(error=error):
                log.write_text("ok"); manifest(); write_json(packet / "browser.json", {"passed": True})
                mutate()
                with self.assertRaisesRegex(DeliveryError, error):
                    verify_delivery(spec, directory, "c" * 40, records)


class PreflightBaselineTests(Harness):
    BATCH = {"supervision": {"baseline_checks": True}}
    PROJECT = {"identity_files": ["../fixture.lock"]}

    def test_baseline_checks_are_reused_only_for_unchanged_identities(self):
        (self.root / "home" / "fixture.lock").write_text("v1")
        (self.repo / "result.txt").write_text("ready"); git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "fixture")
        runner = self.make_runner()
        first = preflight(runner.config, runner, launch_id="L-one")["steps"]["baseline_checks"]
        self.assertEqual((first["status"], first["reused"]), ("passed", False))
        second = preflight(runner.config, runner, launch_id="L-two")["steps"]["baseline_checks"]
        self.assertTrue(second["reused"]); self.assertEqual(second["reused_from"], "L-one")
        (self.root / "home" / "fixture.lock").write_text("v2")
        third = preflight(runner.config, runner, launch_id="L-three")["steps"]["baseline_checks"]
        self.assertEqual((third["reused"], third["reason"]), (False, "changed: fixtures"))
        (self.repo / "result.txt").write_text("broken"); git(self.repo, "commit", "-qam", "break")
        runner = self.make_runner()
        runner.state["last_commit"] = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(LaunchError, "Baseline check 'output' failed"):
            preflight(runner.config, runner, launch_id="L-four")
        record = json.loads((self.state_dir / "preflight.json").read_text())
        self.assertEqual(record["steps"]["baseline_checks"]["reason"], "changed: source")
        self.assertFalse(record["passed"])


class CommandLineTests(Harness):
    def test_recover_requires_reason_and_authorizer(self):
        args = ["--batch", str(self.batch), "--home", str(self.home)]
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["recover", "publish", *args, "--authorized-by", "Owner"])
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["recover", "defer", *args, "--reason", "x", "--authorized-by", "Owner"])

    def test_launch_refusal_writes_nothing_to_linear(self):
        args = ["--batch", str(self.batch), "--home", str(self.home)]
        self.state_dir.mkdir(parents=True); (self.state_dir / "STOP").write_text("held")
        fake = FakeLinear()
        with patch.object(runner_module, "pin_resolution", side_effect=lambda c, l: pin_resolution(c, fake)), \
                patch.object(runner_module.LinearClient, "call", side_effect=AssertionError("no Linear")), \
                patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            main(["launch", *args, "--backend", "foreground"])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(fake.posts)

    def test_launcher_change_does_not_block_resume_and_is_recorded(self):
        self.launch(stop_after=["DEV-1"])
        before = json.loads((self.state_dir / "resolved-config.json").read_text())["config_sha256"]
        site = json.loads((self.home / "site.json").read_text())
        site["launcher"] = {"cpu_list": "0", "environment": {"PATH": "/usr/bin:/bin"}, "startup_timeout_seconds": 9}
        (self.home / "site.json").write_text(json.dumps(site))
        entry = self.launch(clear_stop=True)
        self.assertEqual(entry["started"]["outcome"], "complete")
        self.assertEqual(self.make_runner().state["config_sha256"], before)
        self.assertEqual((entry["launcher"]["cpu_list"], entry["launcher"]["startup_timeout_seconds"]), ("0", 9))
        with patch("sys.stdout") as stdout:
            main(["status", "--batch", str(self.batch), "--home", str(self.home)])
        report = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertEqual(report["launch"]["launcher"]["environment"], {"PATH": "/usr/bin:/bin"})
        self.assertEqual(report["launch"]["launch_id"], entry["launch_id"])

    def test_status_reports_supervisor_and_marker(self):
        self.launch(stop_after=["DEV-1"])
        with patch("sys.stdout") as stdout:
            main(["status", "--batch", str(self.batch), "--home", str(self.home)])
        report = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertEqual(report["supervisor"]["outcome"], "checkpoint")
        self.assertIn("Planned checkpoint", report["stop_marker"])


if __name__ == "__main__":
    unittest.main()
