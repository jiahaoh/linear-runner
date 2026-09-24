"""W-181 context-cost controls: compact intake and shared contract, bounded worker sessions and
risk-based review routing. Real Git, checks and state files; fake Codex and Linear only."""
import copy
import json
from pathlib import Path
import sys
import unittest

import intake
import measure
from fixtures import FakeLinear, TEST_REGISTRY, make_home
from config import load_config, pin_resolution
from runner import Runner, git, usage_totals, write_json
import tempfile
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
    registry["phases"]["bounded_sessions"] = dict({"input_threshold_tokens": 1000,
                                                    "handoff_after_implement": True, "max_handoff_bytes": 4000},
                                                   **settings)
    return registry


class BoundedSessionTests(Harness):
    """Checks need 'fixed'; implement writes 'ready', so every issue needs at least one repair."""
    PROJECT = {"checks": [{"name": "output", "kind": "code", "tier": "default", "inputs": ["result.txt"], "cwd": ".",
                           "command": [sys.executable, "-c", "from pathlib import Path; "
                                       "assert Path('result.txt').read_text() == 'fixed'"]}]}
    REGISTRY = bounded_registry()
    BATCH = {"context_controls": {"bounded_sessions": True}}
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
    # Thresholds are in the registry, but this batch does not opt in.
    REGISTRY = bounded_registry(input_threshold_tokens=50)
    BATCH = {}

    def test_default_off_resumes_and_asks_for_no_handoff(self):
        runner = self.run_issue()
        self.assertEqual(runner.config["context_controls"], {"bounded_sessions": False, "low_risk_review": False,
                                                      "compact_token_limit": None})
        self.assertEqual(self.phases(), [("implement", None), ("repair", "DEV-1-implement-1"), ("review", None)])
        self.assertNotIn("handoff.json", self.prompts[0])


def risk_registry(**settings):
    registry = copy.deepcopy(TEST_REGISTRY)
    registry["profiles"]["review_routing"] = {"light_review": dict({
        "profile": "Economy", "issue_profiles": ["Economy", "Standard"],
        "task_kinds": ["Maintenance", "Implementation"], "max_changed_files": 8, "max_changed_lines": 300,
        "max_repairs": 0, "opt_out_labels": ["Full review"], "gate_labels": ["Human gate"]}, **settings)}
    return registry


class RiskBase(Harness):
    REGISTRY = risk_registry()
    BATCH = {"context_controls": {"low_risk_review": True}}

    def review_selection(self, labels=None):
        if labels is not None:
            self.linear.data["labels"] = labels
        runner = self.make_runner()
        runner.execute(limit=1)
        run = next(p for p in (self.root / "runs" / "DEV-1").iterdir() if p.is_dir())
        meta = json.loads(next(run.glob("review-*/session.json")).read_text())
        return meta["selection"], json.loads((run / "review-risk.json").read_text())


class RiskRoutingTests(RiskBase):
    def test_low_risk_issue_gets_the_lighter_review(self):
        selection, risk = self.review_selection()
        self.assertEqual((selection["profile"], selection["selection_source"]), ("Economy", "low-risk review rule"))
        self.assertEqual((risk["eligible"], risk["diff"]["files"]), (True, 2))
        self.assertEqual(self.linear.data["statusType"], "completed")

    def test_research_validation_and_deep_floors_still_hold(self):
        for labels, expected in ((["Research", "Standard"], "Deep"), (["Validation", "Economy"], "Deep"),
                                 (["Implementation", "Deep"], "Deep")):
            with self.subTest(labels=labels):
                self.setUp()
                selection, _ = self.review_selection(labels)
                self.assertEqual(selection["profile"], expected)

    def test_opt_out_and_gate_labels_keep_the_normal_review(self):
        for label in ("Full review", "Human gate"):
            with self.subTest(label=label):
                self.setUp()
                selection, risk = self.review_selection(["Implementation", "Standard", label])
                self.assertEqual(selection["profile"], "Standard")
                self.assertIn(f"label {label!r}", risk["failed"])


class RiskFloorWithBroadRuleTests(RiskBase):
    # Even a rule that names Research cannot lower the Research review floor.
    REGISTRY = risk_registry(task_kinds=["Research", "Implementation"])

    def test_floor_wins_over_a_broad_rule(self):
        selection, risk = self.review_selection(["Research", "Standard"])
        self.assertTrue(risk["eligible"])
        self.assertEqual((selection["profile"], selection["selection_source"]),
                         ("Deep", "approved phase override/review floor"))


class RiskLargeDiffTests(RiskBase):
    REGISTRY = risk_registry(max_changed_lines=1)

    def test_large_diff_keeps_the_normal_review(self):
        selection, risk = self.review_selection()
        self.assertEqual(selection["profile"], "Standard")
        self.assertIn("2 changed lines > 1", risk["failed"])


class RiskAfterRepairTests(BoundedSessionTests):
    REGISTRY = risk_registry()
    BATCH = {"context_controls": {"low_risk_review": True}}

    def test_repaired_issue_keeps_the_normal_review(self):
        self.run_issue()
        run = self.run_dir()
        risk = json.loads((run / "review-risk.json").read_text())
        self.assertIn("1 repair(s) > 0", risk["failed"])
        meta = json.loads(next(run.glob("review-*/session.json")).read_text())
        self.assertEqual(meta["selection"]["profile"], "Standard")


class RiskGateRelationTests(RiskBase):
    BATCH = dict(RiskBase.BATCH, human_gates=[{"issue_id": "GATE-1", "comment_id": "c1", "author_id": "owner",
                                               "approval_text": "approved"}])

    def test_issue_blocked_by_a_human_gate_keeps_the_normal_review(self):
        self.linear.others["GATE-1"] = {"id": "GATE-1", "statusType": "completed"}
        self.linear.comments = lambda issue: [{"id": "c1", "author": {"id": "owner"}, "body": "approved"}]
        self.linear.data["relations"] = {"blockedBy": [{"id": "GATE-1"}]}
        selection, risk = self.review_selection()
        self.assertEqual(selection["profile"], "Standard")
        self.assertIn("blocked by human gate ['GATE-1']", risk["failed"])


class RiskDefaultOffTests(Harness):
    def test_rule_is_in_the_registry_but_off_unless_the_batch_opts_in(self):
        rule = json.loads((Path(__file__).resolve().parent / "registry" / "profiles.json").read_text())
        self.assertEqual(rule["review_routing"]["light_review"]["profile"], "Economy")
        self.make_runner().execute(limit=1)
        run = next(p for p in (self.root / "runs" / "DEV-1").iterdir() if p.is_dir())
        self.assertEqual(json.loads(next(run.glob("review-*/session.json")).read_text())["selection"]["profile"],
                         "Standard")
        self.assertEqual(json.loads((run / "review-risk.json").read_text())["failed"], ["not enabled for this batch"])


class ContextControlsConfigTests(Harness):
    def test_controls_are_validated_and_fingerprinted(self):
        from config import ConfigError, config_fingerprint, load_config
        base = load_config(self.batch, self.home)
        self.assertEqual(base["context_controls"], {"bounded_sessions": False, "low_risk_review": False,
                                                      "compact_token_limit": None})
        batch = json.loads(self.batch.read_text())
        self.batch.write_text(json.dumps(dict(batch, context_controls={"low_risk_review": True})))
        enabled = load_config(self.batch, self.home)
        self.assertEqual(enabled["_sources"]["context_controls.low_risk_review"], "batch fixture")
        self.assertNotEqual(config_fingerprint(enabled), config_fingerprint(base))
        for bad in ({"bounded": True}, {"bounded_sessions": "yes"}):
            self.batch.write_text(json.dumps(dict(batch, context_controls=bad)))
            with self.assertRaises(ConfigError):
                load_config(self.batch, self.home)


class TerminalTrajectoryTests(Harness):
    def codex(self, prompt, directory, **kwargs):
        value = super().codex(prompt, directory, **kwargs)
        meta_path = Path(directory) / "session.json"
        meta = json.loads(meta_path.read_text())
        stamp = f"2026-01-01T00:{len(self.calls):02d}:00+00:00"
        write_json(meta_path, dict(meta, started_at=stamp, finished_at=stamp, wall_seconds=60.0))
        return value

    def test_supervised_terminal_step_writes_the_trajectory_report(self):
        self.launch(stop_after=["DEV-1"])
        root = self.root / "state" / "fixture"
        result = json.loads((root / "terminal-trajectory.json").read_text())
        summary = {s["issue"]: s for s in result["summaries"]}
        self.assertEqual((summary["DEV-1"]["attempts"], summary["DEV-1"]["usage"]["input_tokens"]), (2, 200))
        self.assertEqual(summary["DEV-2"]["attempts"], 0)
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["comparison"][0]["batch"], "fixture")
        self.assertTrue((root / "terminal-trajectory.md").is_file())
        delivery = json.loads((root / "terminal-delivery.json").read_text())
        self.assertEqual(delivery["trajectory"]["html"], str(root / "terminal-trajectory.html"))
        self.assertIn("terminal-trajectory.html", self.linear.last("DEV-3", "batch-finished"))

    def test_renderer_failure_never_blocks_the_terminal_report(self):
        import trajectory as module
        original = module.from_roots
        module.from_roots = lambda *a, **k: (_ for _ in ()).throw(ValueError("broken records"))
        self.addCleanup(setattr, module, "from_roots", original)
        self.launch(stop_after=["DEV-1"])
        root = self.root / "state" / "fixture"
        self.assertTrue((root / "terminal-report.json").is_file())
        self.assertEqual(json.loads((root / "terminal-delivery.json").read_text())["trajectory"],
                         {"error": "broken records"})


class CompactionArgvTests(unittest.TestCase):
    """The real subprocess argv with a fake Codex executable; no model is called."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fake = self.root / "fake-codex"
        fake.write_text(f"#!{sys.executable}\n" +
                        "import json, pathlib, sys\n"
                        "sys.stdin.read()\n"
                        "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'argv': sys.argv[1:]}))\n"
                        "print(json.dumps({'type':'thread.started','thread_id':'fixture-session'}),flush=True)\n"
                        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}),flush=True)\n")
        fake.chmod(0o755)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.home, self.batch = make_home(self.root, self.repo,
                                          site={"executables": {"codex": str(fake), "python": sys.executable}})

    def runner(self, controls=None, registry=None):
        if controls is not None:
            batch = json.loads(self.batch.read_text())
            self.batch.write_text(json.dumps(dict(batch, context_controls=controls)))
        if registry is not None:
            write_json(self.home / "registry" / "phases.json", registry)
        config, _ = pin_resolution(load_config(self.batch, self.home), FakeLinear())
        return Runner(config, FakeLinear())

    def argv(self, runner, phase, **kwargs):
        directory = self.root / f"{phase}-{len(list(self.root.iterdir()))}"
        result, _, _ = runner.codex("test only", directory, phase=phase, model="astra", effort="high",
                                    compact_limit=runner.compact_limit(phase), **kwargs)
        meta = json.loads((directory / "session.json").read_text())
        return result["argv"], meta["compact_token_limit"]

    def test_limit_is_passed_on_fresh_and_resumed_calls_only_when_set(self):
        runner = self.runner()
        for kwargs in ({"writable": True}, {"writable": True, "resume": "s-1"}, {"writable": False}):
            argv, recorded = self.argv(runner, "implement", **kwargs)
            self.assertFalse([a for a in argv if "auto_compact" in a])
            self.assertIsNone(recorded)
        runner = self.runner({"compact_token_limit": 150000})
        for kwargs in ({"writable": True}, {"writable": True, "resume": "s-1"}, {"writable": False}):
            with self.subTest(**kwargs):
                argv, recorded = self.argv(runner, "review", **kwargs)
                index = argv.index("model_auto_compact_token_limit=150000")
                self.assertEqual(argv[index - 1], "-c")
                if kwargs.get("resume"):
                    self.assertGreater(index, argv.index("resume"))
                self.assertEqual(recorded, 150000)

    def test_registry_phase_value_and_batch_override(self):
        registry = copy.deepcopy(TEST_REGISTRY["phases"])
        registry["phases"]["implement"]["compact_token_limit"] = 120000
        runner = self.runner(registry=registry)
        self.assertEqual((runner.compact_limit("implement"), runner.compact_limit("review")), (120000, None))
        self.assertIn("model_auto_compact_token_limit=120000", self.argv(runner, "implement", writable=True)[0])
        runner = self.runner({"compact_token_limit": 90000})
        self.assertEqual((runner.compact_limit("implement"), runner.compact_limit("review")), (90000, 90000))

    def test_schema_rejects_a_tiny_or_non_integer_limit(self):
        from config import ConfigError
        for bad in (10, "150000"):
            with self.assertRaises(ConfigError):
                self.runner({"compact_token_limit": bad})


class CompactionRecordTests(Harness):
    BATCH = {"context_controls": {"compact_token_limit": 150000}}

    def test_limit_reaches_codex_and_the_session_and_measurement_records(self):
        seen = []
        original = self.codex
        def codex(prompt, directory, **kwargs):
            seen.append(kwargs.get("compact_limit"))
            value = original(prompt, directory, **kwargs)
            meta_path = Path(directory) / "session.json"
            meta = json.loads(meta_path.read_text())
            write_json(meta_path, dict(meta, started_at="2026-01-01T00:00:00+00:00",
                                       finished_at="2026-01-01T00:01:00+00:00", wall_seconds=60.0))
            return value
        runner = self.make_runner()
        runner.codex = codex
        runner.execute(limit=1)
        self.assertEqual(seen, [150000, 150000])
        run = next(p for p in (self.root / "runs" / "DEV-1").iterdir() if p.is_dir())
        self.assertEqual({json.loads(p.read_text())["compact_token_limit"] for p in run.glob("*/session.json")},
                         {150000})
        report = measure.measure([run])
        self.assertEqual([r["compact_token_limit"] for r in report["issues"]["DEV-1"]["invocations"]],
                         [150000, 150000])
        table = measure.render_markdown(report)
        header = next(line for line in table.splitlines() if line.startswith("| Issue | Attempt"))
        self.assertEqual(header.count("|"), table.splitlines()[table.splitlines().index(header) + 1].count("|"))
        self.assertIn("| 150,000 |", table)


if __name__ == "__main__":
    unittest.main()
