"""Engine behavior: real Git and checks, fake boundary for Codex and Linear I/O."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from linear_runner.config import load_config, pin_resolution
from tests.fixtures import FakeLinear, make_home
from linear_runner.linear.client import LinearClient
from linear_runner import cli as cli_module
from linear_runner.engine import runner as runner_module
from linear_runner.cli import main
from linear_runner.backends.codex import execution_evidence
from linear_runner.engine.runner import (RESULT_SCHEMA, Runner, fingerprint, git, issue_contract, project_lock,
                                         published_contract_matches, published_issue, resolve_profile, review_criteria,
                                         review_schema, usage_totals, write_json)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q"); git(self.repo, "checkout", "-q", "-b", "codex/test")
        git(self.repo, "config", "user.name", "Test"); git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("fixture")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "baseline")
        self.home, self.batch = make_home(self.root, self.repo)
        self.linear = FakeLinear()
        self.config, _ = pin_resolution(load_config(self.batch, self.home), self.linear)
        self.policy = self.config["policy"]
        self.runner = Runner(self.config, self.linear)
        self.calls = []
        def codex(prompt, directory, **kwargs):
            phase = Path(directory).name.split('-')[0]; self.calls.append(phase)
            Path(directory).mkdir(parents=True)
            if phase in ("implement", "repair"):
                (self.repo / "result.txt").write_text("ready")
            session = kwargs.get("resume") or phase
            write_json(Path(directory) / "session.json", {"session_id": session, "requested_model": kwargs["model"],
                       "requested_reasoning_effort": kwargs["effort"], "exit_code": 0,
                       "execution_evidence": {"usage_events": [{"usage": {"input_tokens": 100, "output_tokens": 5}}]}})
            result = {"issue_id": "DEV-1", "status": "ready", "commit": git(self.repo, "rev-parse", "HEAD"),
                      "summary": "Ready", "acceptance": [{"criterion": "Produce validated output", "satisfied": True, "evidence": "result.txt and successful output check"}],
                      "limitations": []}
            return result, [], session
        self.runner.run_session = codex

    # --- End-to-end lifecycle and controller-owned commit ------------------------

    def test_end_to_end_only_implementation_and_review_use_models(self):
        start = git(self.repo, "rev-parse", "HEAD")
        self.runner.execute(limit=1)
        self.assertEqual(self.calls, ["implement", "review"])
        self.assertEqual(self.linear.data["statusType"], "completed")
        self.assertEqual(self.runner.state["phase"], "queue_complete")
        self.assertFalse(git(self.repo, "status", "--porcelain"))
        # The controller, not the worker, made exactly one commit on top of the baseline.
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), start)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%B"), "feat(dev-1): implement validated issue deliverables")
        self.runner.execute(limit=1)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue((self.runner.root / "terminal-report.json").is_file())
        manifest = json.loads((Path(self.runner.state["history"][0]["run_dir"]) / "manifest.json").read_text())
        self.assertEqual(manifest["issue_url"], "https://linear.app/test/issue/DEV-1")
        # Stage comments name what ran: the fixture's Standard pools are astra medium on Codex.
        self.assertIn("Implementation runs with astra (medium effort, Codex).", self.linear.last("DEV-1", "claim"))
        self.assertIn("Repairs, if needed, run with luna (max effort, Codex).", self.linear.last("DEV-1", "claim"))
        self.assertIn("It was implemented with astra (medium effort, Codex) and reviewed with astra (medium effort, "
                      "Codex).", self.linear.last("DEV-1", "done"))
        self.assertNotIn("_sources", manifest["config"])

    def test_worker_commit_is_rejected_before_controller_commit(self):
        original = self.runner.run_session
        def committing(prompt, directory, **kwargs):
            value = original(prompt, directory, **kwargs)
            if kwargs.get("writable"):
                git(self.repo, "add", "--all"); git(self.repo, "commit", "-qm", "worker commit")
            return value
        self.runner.run_session = committing
        with self.assertRaisesRegex(RuntimeError, "changed Git history"):
            self.runner.execute(limit=1)
        self.assertNotEqual(self.linear.data["statusType"], "completed")

    def test_missing_labels_and_changed_dependency_fail_before_model(self):
        self.linear.data["labels"] = ["Implementation"]
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            self.runner.execute(limit=1)
        self.assertFalse(self.calls)
        self.linear.data["labels"] = ["Implementation", "Standard"]
        self.linear.data["relations"]["blockedBy"] = [{"id": "OTHER-1"}]
        self.linear.others["OTHER-1"] = {"id": "OTHER-1", "statusType": "started"}
        with self.assertRaisesRegex(RuntimeError, "prerequisite"):
            self.runner.execute(limit=1)
        self.assertFalse(self.calls)

    # --- Linear workflow states ---------------------------------------------------

    def test_review_phase_moves_issue_to_in_review_with_readback(self):
        original = self.runner.run_session
        seen = []
        def observe(prompt, directory, **kwargs):
            seen.append((Path(directory).name.split("-")[0], self.linear.data["status"]))
            return original(prompt, directory, **kwargs)
        self.runner.run_session = observe
        self.runner.execute(limit=1)
        self.assertEqual(self.linear.writes, ["In Progress", "In Review", "Done"])
        self.assertEqual(seen, [("implement", "In Progress"), ("review", "In Review")])
        self.assertEqual(self.linear.data["status"], "Done")

    def test_repairs_and_validation_stay_in_progress(self):
        self.runner.config["checks"][0]["command"] = [sys.executable, "-c", "import sys; from pathlib import Path; "
                                                      "sys.exit(0 if Path('result.txt').read_text() == 'fixed' else 1)"]
        original = self.runner.run_session
        statuses = []
        def repairing(prompt, directory, **kwargs):
            value = original(prompt, directory, **kwargs)
            statuses.append(self.linear.data["status"])
            if Path(directory).name.startswith("repair"):
                (self.repo / "result.txt").write_text("fixed")
            return value
        self.runner.run_session = repairing
        self.runner.execute(limit=1)
        self.assertEqual(statuses, ["In Progress", "In Progress", "In Review"])
        self.assertEqual(self.linear.writes, ["In Progress", "In Review", "Done"])

    def test_in_review_readback_failure_stops_before_review(self):
        original = self.linear.call
        def ignored(name, **args):
            if args.get("state") == "In Review":
                self.linear.writes.append("In Review (not applied)")
                return copy.deepcopy(self.linear.data)
            return original(name, **args)
        self.linear.call = ignored
        with self.assertRaisesRegex(RuntimeError, "read-back does not show In Review"):
            self.runner.execute(limit=1)
        self.assertEqual(self.calls, ["implement"])
        self.assertEqual(self.runner.state["active"]["step"], "review")
        self.linear.call = original
        self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.calls, ["implement", "review"])
        self.assertEqual(self.linear.data["status"], "Done")

    def test_interrupted_in_review_write_resumes_without_rewrite(self):
        original = self.linear.call
        def lost(name, **args):
            result = original(name, **args)
            if args.get("state") == "In Review":
                raise RuntimeError("response lost after In Review write")
            return result
        self.linear.call = lost
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            self.runner.execute(limit=1)
        self.assertEqual(self.linear.data["status"], "In Review")
        self.assertEqual(self.calls, ["implement"])
        self.linear.call = original
        self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.linear.writes, ["In Progress", "In Review", "Done"])
        self.assertEqual(self.calls, ["implement", "review"])

    def test_failed_review_resumes_from_in_review(self):
        original = self.runner.run_session
        def blocked(prompt, directory, **kwargs):
            result, events, session = original(prompt, directory, **kwargs)
            if not kwargs.get("writable"):
                result["status"] = "blocked"
            return result, events, session
        self.runner.run_session = blocked
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.runner.execute(limit=1)
        self.assertEqual(self.linear.data["status"], "In Review")
        self.runner.run_session = original
        self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.linear.writes, ["In Progress", "In Review", "Done"])
        self.assertEqual(self.calls, ["implement", "review", "review"])

    def test_foreign_live_states_block_each_step(self):
        cases = [("validate", "In Review", "started"), ("review", "Blocked", "started"), ("publish", "In Progress", "started"),
                 ("publish", "Canceled", "canceled"), ("done", "In Review", "started")]
        for step, status, kind in cases:
            with self.subTest(step=step, status=status):
                with self.assertRaisesRegex(RuntimeError, "state changed outside"):
                    self.runner.verify_live_state({"status": status, "statusType": kind}, step)
        for step, status, kind in [("implement", "Todo", "unstarted"), ("implement", "In Progress", "started"),
                                   ("review", "In Progress", "started"), ("review", "In Review", "started"),
                                   ("publish", "In Review", "started"), ("publish", "Done", "completed"), ("done", "Done", "completed")]:
            self.runner.verify_live_state({"status": status, "statusType": kind}, step)

    def test_renamed_workspace_states_are_used(self):
        self.runner.config["states"] = {"in_progress": "Doing", "review": "Checking", "done": "Shipped"}
        original = self.linear.call
        def mapped(name, **args):
            result = original(name, **args)
            if name == "save_issue":
                self.linear.data["statusType"] = {"Doing": "started", "Checking": "started", "Shipped": "completed"}[args["state"]]
            return result
        self.linear.call = mapped
        self.runner.execute(limit=1)
        self.assertEqual(self.linear.writes, ["Doing", "Checking", "Shipped"])

    # --- Gates -------------------------------------------------------------------

    def test_human_gate_requires_pinned_done_comment_author_and_text(self):
        gate = {"issue_id": "GATE-1", "comment_id": "approval", "author_id": "owner", "approval_text": "approved"}
        self.runner.config["human_gates"] = [gate]
        self.linear.others["GATE-1"] = {"id": "GATE-1", "statusType": "completed"}
        good = {"id": "approval", "author": {"id": "owner"}, "body": "Scope approved."}
        cases = [([], "completed"), ([dict(good, id="other")], "completed"), ([dict(good, author={"id": "someone"})], "completed"),
                 ([dict(good, body="Scope pending.")], "completed"), ([good, good], "completed"), ([good], "started")]
        for comments, state in cases:
            with self.subTest(comments=comments, state=state):
                self.linear.comments = lambda identity, c=comments: copy.deepcopy(c)
                self.linear.others["GATE-1"]["statusType"] = state
                with self.assertRaisesRegex(RuntimeError, "Human approval"):
                    self.runner.execute(limit=1)
                self.assertFalse(self.calls)
        self.linear.comments = lambda identity: [copy.deepcopy(good)]
        self.linear.others["GATE-1"]["statusType"] = "completed"
        self.runner.execute(limit=1)
        self.assertEqual(self.linear.data["statusType"], "completed")

    def test_required_done_gate_blocks_dispatch(self):
        self.runner.config["required_done"] = ["READY-1"]
        self.linear.others["READY-1"] = {"id": "READY-1", "statusType": "started"}
        with self.assertRaisesRegex(RuntimeError, "READY-1 is not Done"):
            self.runner.execute(limit=1)
        self.assertFalse(self.calls)
        self.assertEqual(self.linear.data["statusType"], "unstarted")

    # --- Routing, budgets and repairs -----------------------------------------

    def test_review_floor_and_labels_pin(self):
        self.linear.data["labels"] = ["Validation", "Economy"]
        self.assertEqual(resolve_profile(self.config, self.linear.data, "review")["profile"], "Deep")
        self.assertEqual(resolve_profile(self.config, self.linear.data, "repair")["profile"], "Economy")
        self.linear.data["labels"] = ["Maintenance", "Economy"]
        self.assertEqual(resolve_profile(self.config, self.linear.data, "review")["profile"], "Standard")
        self.assertEqual(resolve_profile(self.config, self.linear.data, "implement")["selection_source"], "issue labels")
        self.linear.data["labels"] = ["Implementation", {"name": "Deep"}]
        self.assertEqual(resolve_profile(self.config, self.linear.data, "review")["model"], "astra")
        self.assertEqual(resolve_profile(self.config, self.linear.data, "review")["effort"], "high")

    def test_soft_budget_is_persistent_checkpoint(self):
        self.policy["phases"]["phases"]["implement"]["budget"]["input_tokens"] = 1
        with self.assertRaisesRegex(RuntimeError, "soft budget"):
            self.runner.execute(limit=1)
        with self.assertRaisesRegex(RuntimeError, "reconciliation"):
            self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.calls, ["implement"])

    def test_repair_limits_survive_resume(self):
        self.runner.config["checks"][0]["command"] = [sys.executable, "-c", "raise SystemExit(1)"]
        with self.assertRaisesRegex(RuntimeError, "Repeated unchanged failure|repair limit"):
            self.runner.execute(limit=1)
        repairs = self.runner.state["active"]["repairs"]
        with self.assertRaisesRegex(RuntimeError, "Repeated unchanged failure|repair limit"):
            self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.runner.state["active"]["repairs"], repairs)

    def test_two_repairs_share_one_escalation_and_survive_resume(self):
        self.linear.data["labels"] = ["Maintenance", "Economy"]
        self.config["checks"][0]["command"] = [sys.executable, "-c", "raise SystemExit(1)"]
        original = self.runner.run_session
        models = []
        def changing(*args, **kwargs):
            value = original(*args, **kwargs)
            models.append(kwargs["model"])
            if kwargs.get("writable"):
                (self.repo / "result.txt").write_text(str(len(self.calls)))
            return value
        self.runner.run_session = changing
        with self.assertRaisesRegex(RuntimeError, "repair limit"):
            self.runner.execute(limit=1)
        active = self.runner.state["active"]
        self.assertEqual(active["repairs"], 2)
        self.assertEqual(active["escalation"], "Deep")
        self.assertEqual(self.calls, ["implement", "repair", "repair"])
        self.assertEqual(models, ["luna", "luna", "astra"])
        with self.assertRaisesRegex(RuntimeError, "repair limit|Repeated"):
            self.runner.execute(limit=1, resume=True)
        self.assertEqual(len(self.calls), 3)

    def test_unavailable_model_stops_before_claim_or_dispatch(self):
        for phases in self.policy["pools"]["pools"]["*"]["Standard"].values():
            phases[0]["model"] = "unavailable"
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.runner.execute(limit=1)
        self.assertEqual(self.linear.data["statusType"], "unstarted")
        self.assertFalse(self.calls)
        # A listed model without the requested effort is also unavailable (fresh state: config changed).
        config = copy.deepcopy(self.config); config["state_dir"] = str(self.root / "state-effort")
        config["policy"]["pools"]["pools"]["*"]["Standard"] = {
            phase: [{"backend": "codex", "model": "luna", "effort": "medium"}] for phase in ("implement", "repair", "review")}
        runner = Runner(config, self.linear); runner.run_session = self.runner.run_session
        write_json(self.root / "models.json", {"models": [{"slug": "luna", "supported_reasoning_levels": [{"effort": "max"}]}]})
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            runner.execute(limit=1)
        self.assertFalse(self.calls)

    # --- Check evidence reuse and frozen source ------------------------------------

    def test_evidence_reuse_and_invalidation(self):
        self.runner.execute(limit=1)
        run = Path(self.runner.state["history"][0]["run_dir"])
        active = {"run_dir": str(run), "issue_id": "DEV-1", "starting_commit": git(self.repo, "rev-parse", "HEAD")}
        self.assertTrue(self.runner.run_checks(active))
        records = json.loads((Path(active["validation_dir"]) / "checks.json").read_text())
        self.assertTrue(records[0]["reused"])
        (self.repo / "result.txt").write_text("bad")
        self.assertFalse(self.runner.run_checks(active))
        (self.repo / "result.txt").write_text("ready")
        self.assertTrue(self.runner.run_checks(active))

    def test_failed_checks_are_never_reused(self):
        active = {"run_dir": str(self.root / "runs" / "manual"), "issue_id": "DEV-1", "starting_commit": git(self.repo, "rev-parse", "HEAD")}
        Path(active["run_dir"]).mkdir(parents=True)
        (self.repo / "result.txt").write_text("bad")
        for _ in range(2):
            self.assertFalse(self.runner.run_checks(active))
            records = json.loads((Path(active["validation_dir"]) / "checks.json").read_text())
            self.assertFalse(records[0]["reused"])
            self.assertNotEqual(records[0]["exit_code"], 0)
        # Tampered successful evidence is also rerun, not trusted.
        (self.repo / "result.txt").write_text("ready")
        self.assertTrue(self.runner.run_checks(active))
        cache = json.loads((self.runner.root / "check-cache.json").read_text())
        Path(cache["output"]["log"]).write_text("edited")
        self.assertTrue(self.runner.run_checks(active))
        self.assertFalse(json.loads((Path(active["validation_dir"]) / "checks.json").read_text())[0]["reused"])

    def test_allowed_empty_selection_counts_as_passing_and_is_recorded(self):
        empty = [sys.executable, "-c", "raise SystemExit(5)"]
        self.config["checks"].append({"name": "pytest-extended", "kind": "code", "tier": "default", "inputs": ["*"],
                                      "cwd": ".", "command": empty, "allow_empty": True})
        self.runner.execute(limit=1)
        self.assertEqual(self.calls, ["implement", "review"])  # no repair
        self.assertEqual(self.linear.data["statusType"], "completed")
        run = Path(self.runner.state["history"][0]["run_dir"])
        records = json.loads(next(run.glob("validation-*/checks.json")).read_text())
        record = next(r for r in records if r["name"] == "pytest-extended")
        self.assertEqual((record["exit_code"], record["status"], record["allow_empty"], record["note"]),
                         (5, "empty", True, "no tests selected; not applicable"))
        self.assertEqual(records[0]["status"], "passed")
        comment = self.linear.last("DEV-1", "validation")
        self.assertTrue(comment.startswith("Checks passed for DEV-1 (2 checks)"))
        self.assertIn("**Not applicable**\npytest-extended selected no tests (allowed).", comment)
        # An empty outcome is passing evidence, so it is reused while its identity is unchanged.
        active = {"run_dir": str(run), "issue_id": "DEV-1", "starting_commit": git(self.repo, "rev-parse", "HEAD")}
        self.assertTrue(self.runner.run_checks(active))
        reused = json.loads((Path(active["validation_dir"]) / "checks.json").read_text())
        self.assertEqual([(r["status"], r["reused"]) for r in reused], [("passed", True), ("empty", True)])

    def test_empty_selection_without_allow_empty_and_other_exits_still_fail(self):
        active = {"run_dir": str(self.root / "runs" / "manual"), "issue_id": "DEV-1",
                  "starting_commit": git(self.repo, "rev-parse", "HEAD")}
        Path(active["run_dir"]).mkdir(parents=True)
        (self.repo / "result.txt").write_text("ready")
        spec = {"name": "pytest-extended", "kind": "code", "tier": "default", "inputs": ["*"], "cwd": "."}
        cases = [(5, False, "failed"), (1, True, "failed"), (4, True, "failed"), (5, True, "empty"), (0, True, "passed")]
        keys = {}
        for code, allow, status in cases:
            with self.subTest(code=code, allow_empty=allow):
                check = dict(spec, command=[sys.executable, "-c", f"raise SystemExit({code})"])
                if allow:
                    check["allow_empty"] = True
                self.config["checks"] = [check]
                self.assertEqual(self.runner.run_checks(active), status != "failed")
                record = json.loads((Path(active["validation_dir"]) / "checks.json").read_text())[0]
                self.assertEqual((record["exit_code"], record["status"], record["reused"]), (code, status, False))
                self.assertEqual("note" in record, status == "empty")
                keys[(code, allow)] = record["key"]
        # allow_empty is part of the check definition: it changes the reuse identity.
        self.assertNotEqual(keys[(5, False)], keys[(5, True)])

    def test_allow_empty_is_part_of_the_configuration_fingerprint(self):
        from linear_runner.config import config_fingerprint
        project = json.loads((self.home / "projects" / "fixture.json").read_text())
        project["checks"][0]["allow_empty"] = True
        (self.home / "projects" / "fixture.json").write_text(json.dumps(project))
        changed = load_config(self.batch, self.home)
        self.assertIs(changed["checks"][0]["allow_empty"], True)
        self.assertNotEqual(config_fingerprint(dict(changed, project_id="p", assignee_id="owner")),
                            config_fingerprint(self.config))
        project["checks"][0]["allow_empty"] = "yes"
        (self.home / "projects" / "fixture.json").write_text(json.dumps(project))
        from linear_runner.config import ConfigError
        with self.assertRaisesRegex(ConfigError, "allow_empty: expected boolean"):
            load_config(self.batch, self.home)

    def test_delivery_integrity_accepts_only_evidenced_empty_outcomes(self):
        from linear_runner.engine.delivery import check_outcome, check_passed
        self.assertEqual([check_outcome(0), check_outcome(5), check_outcome(5, True), check_outcome(1, True)],
                         ["passed", "failed", "empty", "failed"])
        self.assertTrue(check_passed({"exit_code": 0}))  # records written before the status field
        self.assertTrue(check_passed({"exit_code": 5, "status": "empty", "allow_empty": True}))
        self.assertFalse(check_passed({"exit_code": 5, "status": "empty", "allow_empty": False}))
        self.assertFalse(check_passed({"exit_code": 5, "status": "failed", "allow_empty": True}))
        self.assertFalse(check_passed({"exit_code": 1, "status": "empty", "allow_empty": True}))

    def test_report_and_measure_name_empty_outcomes(self):
        from linear_runner.reporting import measure
        from linear_runner.reporting import trajectory
        run = self.root / "runs" / "DEV-1" / "20260101T000000Z-0000000a"
        validation = run / "validation-20260101T000100Z-0000000b"
        validation.mkdir(parents=True)
        log = validation / "check-0.log"; log.write_text("collected 0 items / 3 deselected")
        write_json(validation / "checks.json", [
            {"name": "pytest-extended", "exit_code": 5, "status": "empty", "allow_empty": True, "reused": False,
             "log": str(log), "sha256": runner_module.hashlib.sha256(log.read_bytes()).hexdigest(),
             "started_at": "2026-01-01T00:01:00+00:00", "finished_at": "2026-01-01T00:01:02+00:00"}])
        result = trajectory.from_roots([self.root / "runs"], captured_at="2026-01-02T00:00:00+00:00")
        self.assertEqual(result["validation_audit"][0]["status"], "empty")
        self.assertIn("| DEV-1 | validation-20260101T000100Z-0000000b | pytest-extended | 5 | empty (no tests "
                      "selected; allowed) |", trajectory.render_markdown(result))
        report = measure.measure([self.root / "runs"])
        self.assertEqual(report["issues"]["DEV-1"]["checks"][0]["status"], "empty")
        self.assertIn("| pytest-extended | 5 | empty (no tests selected; allowed) | no |", measure.render_markdown(report))

    def test_review_cannot_change_validated_source(self):
        original = self.runner.run_session
        def corrupt(prompt, directory, **kwargs):
            result = original(prompt, directory, **kwargs)
            if not kwargs.get("writable"):
                (self.repo / "result.txt").write_text("unvalidated")
            return result
        self.runner.run_session = corrupt
        with self.assertRaisesRegex(RuntimeError, "Frozen"):
            self.runner.execute(limit=1)
        self.assertNotEqual(self.linear.data["statusType"], "completed")

    # --- Criterion-level review --------------------------------------------------

    def test_checklist_omission_blocks_done(self):
        original = self.runner.run_session
        def omission(*args, **kwargs):
            result, events, session = original(*args, **kwargs)
            if not kwargs.get("writable"):
                result["acceptance"][0]["criterion"] = "Some other criterion"
            return result, events, session
        self.runner.run_session = omission
        with self.assertRaisesRegex(RuntimeError, "omitted"):
            self.runner.execute(limit=1)
        self.assertEqual(self.linear.data["statusType"], "started")

    def test_review_schema_pins_identity_and_count_without_rewriting_markup(self):
        criterion = 'Inspect <issue id="fixture">DEV-2</issue> and preserve units'
        issue = dict(self.linear.data, description='- [ ] Produce validated output\n- [ ] ' + criterion)
        schema = review_schema(issue, "committed-sha")
        self.assertEqual(schema["properties"]["issue_id"]["enum"], ["DEV-1"])
        self.assertEqual(schema["properties"]["commit"]["enum"], ["committed-sha"])
        entries = schema["properties"]["acceptance"]
        self.assertEqual((entries["minItems"], entries["maxItems"]), (2, 2))
        self.assertNotIn("enum", entries["items"]["properties"]["criterion"])
        self.assertEqual(review_criteria(issue), ["Produce validated output", criterion])
        self.assertNotIn("enum", RESULT_SCHEMA["properties"]["issue_id"])
        self.assertNotIn("minItems", RESULT_SCHEMA["properties"]["acceptance"])
        self.assertEqual(review_schema(dict(issue, description="No checkbox"), "sha")["properties"]["acceptance"]["minItems"], 1)

    def test_invalid_review_results_never_publish_and_resume_without_reimplementation(self):
        original = self.runner.run_session
        cases = [
            ({"issue_id": "invalid generated identity", "acceptance": []}, "identity"),
            ({"commit": "wrong-revision"}, "identity"),
            ({"acceptance": []}, "omitted"),
            ({"acceptance": [None]}, "malformed"),
            ({"acceptance": [{"criterion": "Produce validated output", "satisfied": True, "evidence": "checked"}] * 2}, "repeated"),
            ({"acceptance": [{"criterion": "Produce validated output", "satisfied": True, "evidence": "checked"},
                             {"criterion": "Unrequested claim", "satisfied": True, "evidence": "checked"}]}, "unexpected"),
            ({"acceptance": [{"criterion": "Produce validated output", "satisfied": True, "evidence": "  "}]}, "incomplete"),
            ({"acceptance": [{"criterion": "Produce validated output", "satisfied": False, "evidence": "missing runtime"}]}, "incomplete"),
            ({"status": "blocked"}, "incomplete"),
        ]
        for index, (changes, error) in enumerate(cases):
            def invalid(prompt, directory, **kwargs):
                result, events, session = original(prompt, directory, **kwargs)
                if not kwargs.get("writable"):
                    self.assertIsInstance(kwargs["schema"], dict)
                    self.assertEqual(kwargs["schema"]["properties"]["issue_id"]["enum"], ["DEV-1"])
                    self.assertIn("Exact required criteria:", prompt)
                    result.update(changes)
                return result, events, session
            self.runner.run_session = invalid
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, error):
                self.runner.execute(limit=1, resume=index > 0)
            self.assertEqual(self.linear.data["statusType"], "started")
            self.assertEqual(self.runner.state["active"]["step"], "review")
            self.assertEqual(self.runner.state["history"], [])
        self.runner.run_session = original
        self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.calls.count("implement"), 1)
        self.assertEqual(self.linear.data["statusType"], "completed")

    # --- Publication, [x]/[X] read-back and interrupted writes ----------------------

    def test_linear_uppercase_checked_markers_complete(self):
        original = self.linear.call
        def serialize(name, **args):
            result = original(name, **args)
            if name == "save_issue" and "description" in args:
                self.linear.data["description"] = args["description"].replace("[x]", "[X]")
            return result
        self.linear.call = serialize
        self.runner.execute(limit=1)
        self.assertEqual(self.runner.state["phase"], "queue_complete")
        self.assertEqual(self.linear.data["description"], "- [X] Produce validated output")
        self.assertEqual(self.calls, ["implement", "review"])

    def test_publication_comparison_preserves_contract_content(self):
        original = copy.deepcopy(self.linear.data)
        live = published_issue(original)
        live["description"] = live["description"].replace("[x]", "[X]")
        self.assertTrue(published_contract_matches(live, original))
        self.assertTrue(published_contract_matches(published_issue(original), original))
        changes = {"description": ["- [ ] Produce validated output", "- [X] Changed output",
                                    "- [X] Produce validated output\n- [X] Added criterion"],
                   "id": ["OTHER-1"], "assigneeId": ["someone"], "projectId": ["other"],
                   "projectMilestone": [{"id": "new"}], "relations": [{"blockedBy": [{"id": "NEW-1"}]}]}
        for field, values in changes.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    altered = dict(live, **{field: value})
                    self.assertFalse(published_contract_matches(altered, original))
        # Do not lowercase arbitrary prose or inline checkbox examples.
        for before, after in [("Prose X", "Prose x"), ("Example `[X]`", "Example `[x]`")]:
            self.assertFalse(published_contract_matches(dict(live, description=after), dict(original, description=before)))

    # --- The issue contract: scope fields only, related links excluded -------------

    RELATED = [{"id": "OTHER-7", "title": "An unrelated issue that mentions this one"}]

    def test_related_links_are_not_part_of_the_contract(self):
        issue = dict(copy.deepcopy(self.linear.data), relations={"blocks": [], "blockedBy": [{"id": "PRE-1"}],
                                                                 "duplicateOf": None})
        pinned = issue_contract(issue)
        for related in (self.RELATED, self.RELATED + [{"id": "OTHER-8", "title": "a comment mention"}], []):
            with self.subTest(related=related):
                self.assertEqual(issue_contract(dict(issue, relations=dict(issue["relations"], relatedTo=related))),
                                 pinned)
        removed = dict(issue, relations={k: v for k, v in issue["relations"].items()})
        self.assertEqual(issue_contract(removed), pinned)
        changes = {"blocks": [{"id": "NEXT-1"}], "blockedBy": [], "duplicateOf": {"id": "DUP-1"}}
        for relation, value in changes.items():
            with self.subTest(relation=relation):
                self.assertNotEqual(issue_contract(dict(issue, relations=dict(issue["relations"], **{relation: value}))),
                                    pinned)
        for field, value in {"description": "- [ ] Produce other output", "projectMilestone": {"id": "later"},
                             "projectId": "other", "assigneeId": "someone", "id": "DEV-2"}.items():
            with self.subTest(field=field):
                self.assertNotEqual(issue_contract(dict(issue, **{field: value})), pinned)
        # Fields outside the contract (title, labels, status) never change it.
        self.assertEqual(issue_contract(dict(issue, title="Renamed", labels=["Maintenance"], status="Done")), pinned)
        live = published_issue(dict(issue, relations=dict(issue["relations"], relatedTo=self.RELATED)))
        self.assertTrue(published_contract_matches(live, issue))

    def test_state_pinned_with_related_links_verifies_against_the_snapshot(self):
        # State written by an older runner: the stored hash covered the whole relations object.
        from linear_runner.engine.runner import legacy_issue_contract, pinned_contract
        self.linear.data["relations"] = {"blocks": [], "blockedBy": [], "relatedTo": list(self.RELATED),
                                         "duplicateOf": None}
        original = self.linear.call
        def interrupted(name, **args):
            if args.get("state") == "Done":
                raise RuntimeError("interrupted write")
            return original(name, **args)
        self.linear.call = interrupted
        with self.assertRaisesRegex(RuntimeError, "interrupted write"):
            self.runner.execute(limit=1)
        self.linear.call = original
        active = self.runner.state["active"]
        self.assertEqual(active["step"], "publish")
        active["contract"] = legacy_issue_contract(active["issue"])
        self.runner.save()
        # Linear adds a related link (a new issue or a comment mentions this one).
        self.linear.data["relations"]["relatedTo"].append({"id": "OTHER-8", "title": "Filed later"})
        self.assertNotEqual(legacy_issue_contract(self.linear.data), active["contract"])  # the old check failed here
        self.assertEqual(pinned_contract(active), issue_contract(self.linear.data))
        # A snapshot that matches neither formula is not trusted.
        tampered = dict(active, issue=dict(active["issue"], description="- [ ] Something else"))
        with self.assertRaisesRegex(RuntimeError, "does not match its pinned contract hash"):
            pinned_contract(tampered)
        # A substantive change still stops, then the paused publication completes without a model.
        self.linear.others["NEW-1"] = {"id": "NEW-1", "statusType": "completed"}
        self.linear.data["relations"]["blockedBy"] = [{"id": "NEW-1"}]
        with self.assertRaisesRegex(RuntimeError, "scope/dependencies/ownership changed"):
            self.runner.execute(limit=1, resume=True)
        self.linear.data["relations"]["blockedBy"] = []
        self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.calls, ["implement", "review"])
        self.assertEqual(self.linear.data["statusType"], "completed")
        self.assertEqual(len(self.runner.state["history"]), 1)

    def test_readback_mismatch_blocks_advancement(self):
        original = self.linear.call
        def dropped(name, **args):
            result = original(name, **args)
            if name == "save_issue" and args.get("state") == "Done":
                self.linear.data["statusType"] = "started"  # write acknowledged but not visible on read-back
            return result
        self.linear.call = dropped
        with self.assertRaisesRegex(RuntimeError, "read-back is not Done"):
            self.runner.execute(limit=1)
        self.assertEqual(self.runner.state["history"], [])
        self.assertEqual(self.runner.state["active"]["step"], "publish")

    def test_interrupted_publish_reconciles_uppercase_without_rewrite_or_model(self):
        original = self.linear.call
        def interrupted(name, **args):
            result = original(name, **args)
            if name == "save_issue" and "description" in args:
                self.linear.data["description"] = args["description"].replace("[x]", "[X]")
                raise RuntimeError("response lost after successful write")
            return result
        self.linear.call = interrupted
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            self.runner.execute(limit=1)
        checkpoint = copy.deepcopy(self.runner.state)
        active = checkpoint["active"]
        self.assertEqual(active["step"], "publish")
        self.assertNotEqual(issue_contract(self.linear.data), active["published_contract"])
        def no_rewrite(name, **args):
            self.assertNotEqual(name, "save_issue")
            return original(name, **args)
        self.linear.call = no_rewrite
        # Substantive edits still block an interrupted publication before any write.
        self.linear.data["description"] += " changed scope"
        with self.assertRaisesRegex(RuntimeError, "scope/dependencies/ownership changed"):
            self.runner.execute(limit=1, resume=True)
        self.assertFalse(self.runner.state["history"])
        self.linear.data["description"] = "- [X] Produce validated output"
        self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.calls, ["implement", "review"])
        self.assertIsNone(self.runner.state["active"])
        self.assertEqual(len(self.runner.state["history"]), 1)

    def test_failed_publish_resumes_without_repeating_model_work(self):
        original = self.linear.call
        def interrupted(name, **args):
            if args.get("state") == "Done":
                raise RuntimeError("interrupted write")
            return original(name, **args)
        self.linear.call = interrupted
        with self.assertRaisesRegex(RuntimeError, "interrupted write"):
            self.runner.execute(limit=1)
        self.assertEqual(self.calls, ["implement", "review"])
        self.linear.call = original
        self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.calls, ["implement", "review"])
        self.assertEqual(self.linear.data["statusType"], "completed")

    def test_failed_event_post_is_pending_in_state_and_blocks_advancing(self):
        self.linear.fail_posts = True
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.runner.execute(limit=1)
        self.assertFalse(self.calls)
        pending = [e for e in self.runner.state["events"].values() if e["status"] == "pending"]
        self.assertEqual([(e["issue"], e["kind"], e["seq"]) for e in pending], [("DEV-1", "claim", 1)])
        self.linear.fail_posts = False
        self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.linear.data["statusType"], "completed")
        self.assertEqual(self.linear.kinds("DEV-1"), ["claim", "ready", "validation", "review", "done", "batch-finished"])
        self.assertFalse([e for e in self.runner.state["events"].values() if e["status"] == "pending"])

    def test_linear_client_appends_new_comments_and_never_edits(self):
        client = LinearClient({"token_env": "TEST"}); calls = []
        comments = [{"id": "c1", "body": "older comment\n\n<!-- linear-runner b/DEV-1/done/1 -->"}]
        client.comments = lambda issue: copy.deepcopy(comments)
        def call(name, **args):
            calls.append((name, args))
            comment = {"id": f"c{len(comments) + 1}", "body": args["body"]}
            comments.append(comment)
            return comment
        client.call = call
        marker = "<!-- linear-runner b/DEV-1/blocked/1 -->"
        body = "DEV-1 is paused.\n\n" + marker
        self.assertEqual(client.post_comment("DEV-1", body, marker), "c2")
        self.assertEqual(calls, [("save_comment", {"issueId": "DEV-1", "body": body})])  # new comment, no id
        # A reconciling retry adopts the written comment instead of posting or editing.
        self.assertEqual(client.post_comment("DEV-1", body, marker, reconcile=True), "c2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(comments[0]["body"], "older comment\n\n<!-- linear-runner b/DEV-1/done/1 -->")
        comments.append({"id": "c9", "body": "copy\n\n" + marker})
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            client.post_comment("DEV-1", body, marker, reconcile=True)
        client.call = lambda name, **args: {"id": "lost"}
        with self.assertRaisesRegex(RuntimeError, "read-back failed"):
            client.post_comment("DEV-1", "Other.\n\n<!-- linear-runner b/DEV-1/claim/1 -->",
                                "<!-- linear-runner b/DEV-1/claim/1 -->")

    def test_expired_oauth_is_not_silently_refreshed(self):
        p = self.root / "credentials.json"
        write_json(p, {"linear": {"server_name": "linear", "server_url": "https://mcp.linear.app/mcp", "expires_at": 1000, "access_token": "test"}})
        with self.assertRaisesRegex(RuntimeError, "expired"):
            LinearClient({"credentials_file": str(p)}).token()

    # --- Evidence, usage and reports ----------------------------------------------

    def test_usage_deduplicates_resumed_counters(self):
        records = [{"session_id": s, "execution_evidence": {"usage_events": [{"usage": {"input_tokens": i, "cached_input_tokens": c}}]}}
                   for s, i, c in [("a", 100, 80), ("a", 160, 130), ("b", 50, 30)]]
        self.assertEqual(usage_totals(records)["totals"]["input_tokens"], 210)
        self.assertEqual(usage_totals(records)["totals"]["cached_input_tokens"], 160)

    def test_missing_telemetry_is_unknown_not_zero(self):
        totals = usage_totals([{"session_id": "missing", "execution_evidence": {}}])
        self.assertIsNone(totals["totals"]["input_tokens"])
        self.assertEqual(totals["covered"], 0)

    # --- Resumed-phase budget: the W-193 replay ---------------------------------------
    # A Codex implement turn failed before any completed turn (no usage counter); the resumed
    # turn completed with a cumulative counter of 1,602,748 input and 39,173 output tokens.

    W193 = {"input_tokens": 1_602_748, "cached_input_tokens": 1_499_392, "output_tokens": 39_173,
            "reasoning_output_tokens": 22_663}

    def replay_implement(self, steps):
        """Implement attempts in one session: each step is (usage counter or None, finished)."""
        original = self.runner.run_session
        def session(prompt, directory, **kwargs):
            if not Path(directory).name.startswith("implement"):
                return original(prompt, directory, **kwargs)
            counter, finished = steps.pop(0)
            self.calls.append("implement")
            Path(directory).mkdir(parents=True)
            (self.repo / "result.txt").write_text("ready")
            identity = kwargs.get("resume") or "sess-resumed"
            write_json(Path(directory) / "session.json", {
                "session_id": identity, "started_at": runner_module.now(), "exit_code": 0 if finished else 1,
                "execution_evidence": {"usage_events": [{"usage": counter}] if counter else None}})
            if not finished:
                self.runner.state["active"]["session_id"] = identity
                self.runner.save()
                raise RuntimeError(f"Codex failed or did not finish a turn; see {directory}")
            result = {"issue_id": "DEV-1", "status": "ready", "commit": "", "summary": "Ready", "limitations": [],
                      "acceptance": [{"criterion": "Produce validated output", "satisfied": True, "evidence": "ok"}]}
            return result, [], identity
        self.runner.run_session = session

    def implement_usage(self):
        run = Path(self.runner.state["history"][0]["run_dir"] if self.runner.state["history"]
                   else self.runner.state["active"]["run_dir"])
        return [json.loads(p.read_text()) for p in sorted(run.glob("implement-*/phase-usage.json"))]

    def canary_budget(self, input_tokens=15_000_000):
        self.policy["phases"]["phases"]["implement"]["budget"] = {"input_tokens": input_tokens,
                                                                  "output_tokens": 150_000, "tool_calls": 250}

    def test_resumed_phase_after_a_counterless_failure_uses_the_upper_bound(self):
        self.canary_budget()
        self.replay_implement([(None, False), (self.W193, True)])
        with self.assertRaisesRegex(RuntimeError, "did not finish a turn"):
            self.runner.execute(limit=1)
        self.runner.execute(limit=1, resume=True)
        self.assertEqual(self.runner.state["phase"], "queue_complete")
        self.assertEqual(self.calls, ["implement", "implement", "review"])
        [usage] = self.implement_usage()  # the failed attempt never reached the budget check
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["basis"]),
                         (1_602_748, 39_173, "cumulative-upper-bound"))
        # The session's cumulative counter counts once in the totals.
        run = Path(self.runner.state["history"][0]["run_dir"])
        totals = usage_totals([json.loads(p.read_text()) for p in run.glob("*/session.json")])
        self.assertEqual(totals["totals"]["input_tokens"], 1_602_748 + 100)  # + the review session
        self.assertEqual(totals["sessions"], 2)

    def test_upper_bound_over_budget_still_checkpoints(self):
        self.canary_budget(input_tokens=1_000_000)
        self.replay_implement([(None, False), (self.W193, True)])
        with self.assertRaisesRegex(RuntimeError, "did not finish a turn"):
            self.runner.execute(limit=1)
        with self.assertRaisesRegex(RuntimeError, r"soft budget exceeded: input_tokens 1602748 > 1000000 \(an upper bound\)"):
            self.runner.execute(limit=1, resume=True)
        exceeded = self.runner.state["active"]["budget_exceeded"]
        self.assertEqual((exceeded["basis"], exceeded["observed"]["input_tokens"]), ("cumulative-upper-bound", 1_602_748))
        with self.assertRaisesRegex(RuntimeError, "reconciliation"):
            self.runner.execute(limit=1, resume=True)

    def test_current_attempt_without_a_counter_is_unavailable_never_zero(self):
        self.canary_budget()
        self.replay_implement([(self.W193, False), (None, True)])
        with self.assertRaisesRegex(RuntimeError, "did not finish a turn"):
            self.runner.execute(limit=1)
        with self.assertRaisesRegex(RuntimeError, "telemetry unavailable.*input_tokens, output_tokens"):
            self.runner.execute(limit=1, resume=True)
        exceeded = self.runner.state["active"]["budget_exceeded"]
        self.assertEqual(exceeded["basis"], "unavailable")
        self.assertIsNone(exceeded["observed"]["input_tokens"])

    def test_attempt_usage_bases(self):
        from linear_runner.engine.runner import attempt_usage
        run = self.root / "attempts"
        def attempt(name, session, started, counter, invocation=None):
            evidence = {"usage_events": [{"usage": counter}] if counter else None}
            if invocation:
                evidence["invocation_usage_events"] = [{"usage": invocation}]
            write_json(run / name / "session.json", {"session_id": session, "started_at": started,
                                                     "execution_evidence": evidence})
            return run / name
        first = attempt("implement-1", "a", "2026-01-01T00:00:00", {"input_tokens": 100, "output_tokens": 10})
        self.assertEqual(attempt_usage(first)["basis"], "delta")  # a new session: its counter is exact
        resumed = attempt("repair-2", "a", "2026-01-01T00:10:00", {"input_tokens": 250, "output_tokens": 30})
        self.assertEqual((attempt_usage(resumed)["input_tokens"], attempt_usage(resumed)["basis"]), (150, "delta"))
        attempt("repair-3", "a", "2026-01-01T00:20:00", None)  # failed: no counter
        after_gap = attempt("repair-4", "a", "2026-01-01T00:30:00", {"input_tokens": 400, "output_tokens": 50})
        usage = attempt_usage(after_gap)
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["basis"]),
                         (150, 20, "cumulative-upper-bound"))  # covers the failed attempt too
        attempt("review-5", "b", "2026-01-01T00:05:00", None)  # another session's gap does not matter
        self.assertEqual(attempt_usage(resumed)["basis"], "delta")
        claude = attempt("implement-6", "c", "2026-01-01T00:40:00", {"input_tokens": 90},
                         invocation={"input_tokens": 40, "output_tokens": 4})
        self.assertEqual({k: attempt_usage(claude)[k] for k in ("input_tokens", "output_tokens", "basis")},
                         {"input_tokens": 40, "output_tokens": 4, "basis": "invocation"})
        empty = attempt_usage(attempt("implement-7", "d", "2026-01-01T00:50:00", None))
        self.assertEqual((empty["input_tokens"], empty["basis"]), (None, "unavailable"))

    def test_report_snapshots_escape_content_and_preserve_versions(self):
        from linear_runner.reporting.report import render_report
        summary = {"outcome": "blocked", "history": [], "scope": "fixture", "error": "<script>alert(1)</script>", "usage": {}}
        path = self.root / "report.html"
        first = render_report(path, summary, [])
        summary["outcome"] = "complete"
        second = render_report(path, summary, [])
        self.assertNotEqual(first["report"], second["report"])
        self.assertTrue(Path(first["report"]).exists())
        html = path.read_text()
        self.assertNotIn("<script>", html)
        self.assertNotIn(second["evidence_sha256"], html.split("Reproducibility appendix")[0])

    def test_execution_evidence_uses_only_cli_metadata(self):
        events = [
            {"type": "thread.started", "model": "reported-model"},
            {"type": "item.completed", "item": {"type": "agent_message", "model": "fake", "text": "I used model fake"}},
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "status": "completed", "result": {"isError": True}}},
            {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 1}},
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "status": "completed"}},
            {"type": "turn.failed"},
        ]
        evidence = execution_evidence(events)
        self.assertEqual(evidence["observed_models"], [{"event_index": 0, "event_type": "thread.started", "model": "reported-model"}])
        self.assertEqual(evidence["completed_tool_calls"], 3)
        self.assertEqual(evidence["failed_tool_calls"], 2)
        self.assertEqual(evidence["error_events"], 1)
        self.assertIsNone(evidence["usage_events"])
        self.assertIsNone(evidence["billed_cost"])

    def test_paused_work_has_patch_and_untracked_snapshot(self):
        (self.repo / "README.md").write_text("after\n")
        (self.repo / "new").write_text("new content\n")
        directory = self.root / "run"; directory.mkdir()
        self.runner.manifest({"issue_id": "DEV-1", "run_dir": str(directory), "starting_commit": "baseline", "session_id": "s"})
        data = json.loads((directory / "manifest.json").read_text())
        snapshot = data["uncommitted_snapshot"]
        self.assertIn("+after", (directory / (snapshot + ".patch")).read_text())
        with tarfile.open(directory / (snapshot + ".tar.gz")) as archive:
            self.assertEqual(archive.extractfile("new").read(), b"new content\n")

    def test_stop_marker_prevents_dispatch(self):
        self.runner.snapshot = lambda: self.fail("No scheduling allowed after STOP")
        (self.runner.root / "STOP").touch()
        self.runner.execute()
        self.assertFalse(self.calls)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "symbolic-ref", "HEAD", "refs/heads/codex/test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "--allow-empty", "-qm", "initial"], check=True)
        self.home, self.batch = make_home(self.root, self.repo)
        self.args = ["--batch", str(self.batch), "--home", str(self.home)]

    def test_duplicate_controller_lock(self):
        with project_lock(self.root / "lock"):
            with self.assertRaises(RuntimeError):
                with project_lock(self.root / "lock"):
                    pass

    def test_validation_fingerprint_survives_commit_but_detects_edits(self):
        path = self.repo / "file.txt"
        path.write_text("validated")
        before = fingerprint(self.repo)
        subprocess.run(["git", "-C", str(self.repo), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "test"], check=True)
        self.assertEqual(before, fingerprint(self.repo))
        path.write_text("not validated")
        self.assertNotEqual(before, fingerprint(self.repo))

    def test_validate_config_is_offline_and_reports_runner_identity(self):
        with patch.object(LinearClient, "call", side_effect=AssertionError("no Linear access")), \
                patch("sys.stdout") as stdout:
            main(["validate-config", *self.args])
        report = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertEqual(report["resolution_pending"], {"project": "Fixture project", "assignee": "me"})
        self.assertEqual(set(report["runner"]), {"commit", "dirty"})
        self.assertFalse((self.root / "state").exists())

    def test_run_resolves_names_pins_config_and_guards_changes(self):
        def resolved(name, **args):
            return {"list_projects": {"projects": [{"id": "p", "name": "Fixture project"}]},
                    "get_user": {"id": "owner"}}[name]
        with patch.object(LinearClient, "call", side_effect=resolved) as call, patch.object(Runner, "execute") as execute:
            main(["run", *self.args])
        execute.assert_called_once()
        self.assertEqual([c.args[0] for c in call.call_args_list], ["list_projects", "get_user"])
        pinned_path = self.root / "state" / "fixture" / "resolved-config.json"
        pinned = json.loads(pinned_path.read_text())
        self.assertEqual(pinned["resolution"]["ids"], {"project_id": "p", "assignee_id": "owner"})
        before = pinned_path.read_bytes()
        (self.home / "guidance.md").write_text("Changed scope")
        with patch.object(LinearClient, "call", side_effect=AssertionError("no Linear access")), \
                patch.object(Runner, "execute") as execute, patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                main(["run", *self.args])
            execute.assert_not_called()
        self.assertEqual(pinned_path.read_bytes(), before)

    def test_state_changed_config_is_refused_without_rewrite(self):
        linear = FakeLinear()
        config, _ = pin_resolution(load_config(self.batch, self.home), linear)
        runner = Runner(config, linear); runner.verify_config(); runner.save()
        Runner(config, linear).verify_config()
        changed = copy.deepcopy(config); changed["policy"]["phases"]["max_repairs"] = 1
        with self.assertRaisesRegex(RuntimeError, "Configuration/guidance changed"):
            Runner(changed, linear).verify_config()
        with self.assertRaisesRegex(RuntimeError, "different project"):
            Runner(dict(config, issues=["DEV-9"]), linear)

    def test_unfingerprinted_state_is_not_rewritten_or_reported(self):
        state = self.root / "state" / "fixture" / "state.json"
        write_json(state, {"phase": "queue_complete", "history": [{"issue_id": "DEV-1"}]})
        before = state.read_bytes()
        with patch.object(cli_module, "pin_resolution", side_effect=lambda config, linear: pin_resolution(config, FakeLinear())), \
                patch.object(Runner, "run_session") as codex, patch.object(Runner, "report_pause") as report, patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                main(["run", *self.args])
            codex.assert_not_called(); report.assert_not_called()
        self.assertEqual(state.read_bytes(), before)
        self.assertFalse((state.parent / "resolved-config.json").exists())

    def test_dry_run_failure_cannot_post_linear_updates(self):
        with patch.object(cli_module, "pin_resolution", side_effect=lambda config, linear: pin_resolution(config, FakeLinear())), \
                patch.object(Runner, "execute", side_effect=RuntimeError("offline")), patch.object(Runner, "report_pause") as report:
            with self.assertRaises(SystemExit):
                main(["dry-run", *self.args])
            report.assert_not_called()

    def test_stop_and_status_need_no_linear(self):
        with patch.object(LinearClient, "call", side_effect=AssertionError("no Linear access")), patch("sys.stdout"):
            main(["stop", *self.args])
            self.assertTrue((self.root / "state" / "fixture" / "STOP").exists())
            main(["status", *self.args])
            main(["clear-stop", *self.args])
        self.assertFalse((self.root / "state" / "fixture" / "STOP").exists())


if __name__ == "__main__":
    unittest.main()
