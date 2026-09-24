"""The Claude Code backend: parsing REAL recorded stream-json (scrubbed samples in
claude_samples/), the argv and isolation it builds, preflight checks and a full model session
through the engine with a fake `claude` executable. No model, network or Linear."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from linear_runner import backends
from linear_runner.backends.claude import ClaudeBackend, normalized_usage
from linear_runner.config import load_config, pin_resolution
from linear_runner.engine.runner import RESULT_SCHEMA, Runner, usage_totals, write_json
from linear_runner.linear.attention import classify_stop
from tests.fixtures import FakeLinear, fake_claude, fake_claude_log, make_home, fake_codex

SAMPLES = Path(__file__).resolve().parent / "claude_samples"


def sample(name):
    return [json.loads(line) for line in (SAMPLES / name).read_text().splitlines() if line.strip()]


class RecordedStreamTests(unittest.TestCase):
    """Real Claude Code 2.1.281 output (W-190 probes): init, assistant, result success and error."""

    def setUp(self):
        self.backend = ClaudeBackend({})

    def test_init_announces_the_session(self):
        events = sample("worker-resume.jsonl")
        started = [e for e in events if self.backend.session_started(e)]
        self.assertEqual(len(started), 1)
        self.assertEqual((started[0]["subtype"], self.backend.session_id(started[0])), ("init", "id-1"))
        # A resumed call keeps the session ID (every event of the call carries it).
        self.assertEqual({e.get("session_id") for e in events if "session_id" in e}, {"id-1"})

    def test_successful_result_usage_models_and_cost(self):
        events = sample("worker-resume.jsonl")
        self.assertTrue(self.backend.finished(events))
        self.assertIsNone(self.backend.failure(events))
        self.assertEqual(self.backend.result(None, events), {"answer": "ok"})
        evidence = self.backend.evidence(events)
        # input = input + cache read + cache creation (2 + 7207 + 249); cached = cache read; reasoning = thinking.
        self.assertEqual(evidence["usage_events"][0]["usage"], {"input_tokens": 7458, "cached_input_tokens": 7207,
                                                                "output_tokens": 53, "reasoning_output_tokens": 0})
        self.assertEqual(evidence["usage_scope"], "invocation")
        self.assertEqual({m["model"] for m in evidence["observed_models"]}, {"claude-opus-5-5"})
        self.assertEqual({m["event_type"] for m in evidence["observed_models"]}, {"assistant", "result/modelUsage"})
        self.assertIsNone(evidence["observed_reasoning_efforts"])  # the events never name the effort
        self.assertIsNone(evidence["billed_cost"])
        self.assertAlmostEqual(evidence["results"][0]["client_cost_estimate_usd"], 0.0654854)
        # The structured-output call is not a tool call.
        self.assertEqual((evidence["completed_tool_calls"], self.backend.tool_calls(events)), (0, 0))
        self.assertEqual(evidence["init"]["mcp_servers"], [])
        self.assertIsNone(evidence["init"]["memory_paths"])

    def test_denied_reviewer_write_is_recorded(self):
        events = sample("reviewer-denied-write.jsonl")
        evidence = self.backend.evidence(events)
        self.assertEqual(self.backend.result(None, events), {"answer": "denied"})
        self.assertEqual(evidence["permission_denials"][0]["tool_name"], "Bash")
        self.assertIn("touch probe-denied.txt", evidence["permission_denials"][0]["tool_input"])
        self.assertEqual((evidence["completed_tool_calls"], evidence["failed_tool_calls"]), (1, 1))
        self.assertEqual(evidence["init"]["permissionMode"], "dontAsk")
        self.assertEqual(evidence["init"]["tools"], ["Bash", "Glob", "Grep", "Read", "StructuredOutput"])
        self.assertGreater(self.backend.tool_output_bytes(events), 0)

    def test_error_result_is_a_named_failure_not_a_result(self):
        events = sample("error-unknown-model.jsonl")
        self.assertFalse(self.backend.finished(events))
        failure = self.backend.failure(events)
        self.assertIn("error result (api_error, API status 404)", failure)
        self.assertIn("claude-probe-no-such-model", failure)
        with self.assertRaisesRegex(RuntimeError, "^Claude failed: error result"):
            self.backend.result(None, events)
        evidence = self.backend.evidence(events)
        self.assertEqual(evidence["error_events"], 1)
        # The init event names the requested model, but no model ran.
        self.assertIsNone(evidence["observed_models"])
        self.assertEqual(evidence["init"]["model"], "claude-probe-no-such-model")

    def test_missing_structured_output_is_missing_result(self):
        events = [e for e in sample("worker-resume.jsonl")]
        events[-1] = {k: v for k, v in events[-1].items() if k != "structured_output"}
        with self.assertRaisesRegex(RuntimeError, "Missing structured result"):
            self.backend.result(None, events)

    def test_usage_normalization_keeps_unknown_counters_unknown(self):
        self.assertEqual(normalized_usage({"input_tokens": 1, "output_tokens": 2}), {"output_tokens": 2})


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backend = ClaudeBackend({"claude": "/opt/claude"})
        self.schema = {"type": "object", "properties": {"issue_id": {"type": "string", "enum": ["DEV-1"]}}}

    def request(self, name, **fields):
        directory = self.root / "run" / name; directory.mkdir(parents=True)
        write_json(directory / "schema.json", self.schema)
        values = dict(prompt="p", directory=directory, cwd=self.root, model="claude-opus-5-5", effort="high",
                      writable=True, resume=None, schema_path=directory / "schema.json", compact_limit=None)
        values.update(fields)
        return backends.SessionRequest(**values)

    def option(self, argv, name):
        return argv[argv.index(name) + 1]

    def test_worker_and_reviewer_isolation(self):
        worker = self.backend.command(self.request("implement"))
        reviewer = self.backend.command(self.request("review", writable=False))
        for argv in (worker, reviewer):
            self.assertEqual(argv[:5], ["/opt/claude", "-p", "--output-format", "stream-json", "--verbose"])
            self.assertEqual((self.option(argv, "--model"), self.option(argv, "--effort")), ("claude-opus-5-5", "high"))
            self.assertEqual(self.option(argv, "--setting-sources"), "")
            self.assertIn("--strict-mcp-config", argv)
            self.assertNotIn("--mcp-config", argv)
            self.assertEqual(self.option(argv, "--permission-prompts"), "none")
            self.assertEqual(json.loads(self.option(argv, "--json-schema")), self.schema)
            self.assertIn("--append-system-prompt", argv)
            for forbidden in ("--bare", "--fallback-model", "--dangerously-skip-permissions", "--autocompact"):
                self.assertNotIn(forbidden, argv)
            settings = json.loads(Path(self.option(argv, "--settings")).read_text())
            self.assertEqual((settings["disableAllHooks"], settings["autoMemoryEnabled"]), (True, False))
            self.assertEqual(set(settings["permissions"]), {"defaultMode", "disableBypassPermissionsMode"})
        self.assertEqual(self.option(worker, "--permission-mode"), "acceptEdits")
        self.assertEqual(self.option(worker, "--tools").split(","),
                         ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit"])
        self.assertEqual(self.option(worker, "--add-dir"), str(self.root / "run"))
        denied = worker[worker.index("--disallowedTools") + 1:worker.index("--add-dir")]
        self.assertIn("Bash(git commit:*)", denied)
        self.assertIn("Bash(git push:*)", denied)
        self.assertEqual(self.option(reviewer, "--permission-mode"), "dontAsk")
        self.assertEqual(self.option(reviewer, "--tools").split(","), ["Read", "Grep", "Glob", "Bash"])
        allowed = reviewer[reviewer.index("--allowedTools") + 1:reviewer.index("--disallowedTools")]
        self.assertEqual([a for a in allowed if a.startswith("Bash")][:3],
                         ["Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"])
        self.assertFalse([a for a in allowed if a == "Bash" or a.startswith(("Edit", "Write"))])
        self.assertNotIn("--add-dir", reviewer)

    def test_fresh_sessions_get_an_id_and_resumes_continue_it(self):
        fresh = self.backend.command(self.request("implement"))
        self.assertEqual(str(uuid.UUID(self.option(fresh, "--session-id"))), self.option(fresh, "--session-id"))
        self.assertNotIn("--resume", fresh)
        resumed = self.backend.command(self.request("repair", resume="earlier-session"))
        self.assertEqual(self.option(resumed, "--resume"), "earlier-session")
        self.assertNotIn("--session-id", resumed)

    def test_compaction_limit_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "no per-call compact_token_limit"):
            self.backend.command(self.request("implement", compact_limit=150000))
        self.assertFalse(ClaudeBackend.capabilities["compact_token_limit"])

    def test_parent_session_and_api_key_variables_do_not_reach_the_child(self):
        env = self.backend.environment({"PATH": "/usr/bin", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk",
                                        "ANTHROPIC_API_KEY": "secret", "ANTHROPIC_BASE_URL": "http://proxy", "HOME": "/h"})
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/h", "DISABLE_AUTOUPDATER": "1",
                               "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})

    def test_capabilities_are_declared_for_every_backend(self):
        for cls in backends.BACKENDS.values():
            self.assertEqual(set(cls.capabilities), set(backends.CAPABILITY_KEYS), cls.name)
        self.assertEqual(backends.BACKENDS["claude"].capabilities["read_only_isolation"], "permission-rules")
        self.assertEqual(backends.BACKENDS["codex"].capabilities["read_only_isolation"], "os-sandbox")


class SelectionCheckTests(unittest.TestCase):
    """Known-model list, CLI version and subscription login, with a fake executable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def backend(self, **plan):
        executable, _ = fake_claude(self.root, **plan)
        return ClaudeBackend({"claude": str(executable)})

    def test_known_model_and_effort_pass(self):
        backend = self.backend()
        backend.check_selection({"model": "claude-opus-5-5", "effort": "medium"})
        report = backend.catalog_report([{"model": "claude-opus-5-5", "effort": "high"},
                                         {"model": "claude-fable-5-1", "effort": "high"}])
        self.assertEqual(report["registry_pool_entries_available"],
                         {"claude-opus-5-5/high": True, "claude-fable-5-1/high": False})
        self.assertEqual(report["auth"], {"loggedIn": True, "authMethod": "claude.ai"})
        self.assertNotIn("example.invalid", json.dumps(report))

    def test_unprobed_model_old_cli_and_other_logins_stop(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable in host CLI catalog"):
            self.backend().check_selection({"model": "claude-fable-5-1", "effort": "high"})
        with self.assertRaisesRegex(RuntimeError, "CLI version 2.1.272 is unsupported") as caught:
            self.backend(version="2.1.272").check_selection({"model": "claude-opus-5-5", "effort": "medium"})
        self.assertEqual(classify_stop(caught.exception), "environment")
        for auth in ({"loggedIn": False}, {"authMethod": "api-key"}):
            with self.subTest(auth=auth), self.assertRaisesRegex(RuntimeError, "credential") as caught:
                self.backend(auth=auth).check_selection({"model": "claude-opus-5-5", "effort": "medium"})
            self.assertEqual(classify_stop(caught.exception), "environment")


class SessionTests(unittest.TestCase):
    """A model session through ``Runner.run_session`` with the fake executable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()

    def runner(self, steps):
        executable, self.plan = fake_claude(self.root, steps)
        home, batch = make_home(self.root, self.repo, site={"executables": {
            "codex": str(fake_codex(self.root)[0]), "claude": str(executable), "python": "python3"}})
        config, _ = pin_resolution(load_config(batch, home), FakeLinear())
        return Runner(config, FakeLinear())

    def test_session_record_names_backend_auth_and_settings_without_the_email(self):
        runner = self.runner([{}])
        directory = self.root / "runs" / "DEV-1" / "run" / "implement-1"
        result, events, session = runner.run_session("test only", directory, phase="implement", model="claude-opus-5-5",
                                                     effort="medium", writable=True, backend="claude")
        self.assertEqual((result["issue_id"], result["status"]), ("DEV-1", "ready"))
        meta = json.loads((directory / "session.json").read_text())
        self.assertEqual((meta["backend"], meta["session_id"]), ("claude", session))
        self.assertEqual(meta["command"][meta["command"].index("--session-id") + 1], session)
        details = meta["backend_details"]
        self.assertEqual(details["auth"], {"loggedIn": True, "authMethod": "claude.ai"})
        self.assertEqual(details["settings"]["disableAllHooks"], True)
        self.assertEqual(details["permission_mode"], "acceptEdits")
        self.assertNotIn("example.invalid", (directory / "session.json").read_text())
        self.assertEqual(json.loads((directory / "schema.json").read_text()), RESULT_SCHEMA)

    def test_resumed_counters_become_cumulative_per_session(self):
        runner = self.runner([{}, {"usage": {"input_tokens": 1, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 9,
                                             "output_tokens": 7, "output_tokens_details": {"thinking_tokens": 3}}}])
        run = self.root / "runs" / "DEV-1" / "run"
        _, _, session = runner.run_session("first", run / "implement-1", phase="implement", model="claude-opus-5-5",
                                           effort="medium", writable=True, backend="claude")
        runner.run_session("second", run / "repair-1", phase="repair", model="claude-opus-5-5", effort="medium",
                           writable=True, resume=session, backend="claude")
        second = json.loads((run / "repair-1" / "session.json").read_text())["execution_evidence"]
        self.assertEqual(second["invocation_usage_events"][0]["usage"],
                         {"input_tokens": 100, "cached_input_tokens": 90, "output_tokens": 7, "reasoning_output_tokens": 3})
        self.assertEqual(second["usage_events"][0]["usage"],
                         {"input_tokens": 200, "cached_input_tokens": 150, "output_tokens": 12, "reasoning_output_tokens": 5})
        records = [json.loads(p.read_text()) for p in run.glob("*/session.json")]
        self.assertEqual(usage_totals(records)["totals"], {"input_tokens": 200, "cached_input_tokens": 150,
                                                            "output_tokens": 12, "reasoning_output_tokens": 5})
        self.assertEqual(fake_claude_log(self.plan)[1]["resume"], session)

    def test_error_result_is_an_environment_stop_without_retry(self):
        runner = self.runner([{"error": True}, {}])
        with self.assertRaisesRegex(RuntimeError, r"Claude failed or did not finish a turn: error result "
                                                  r"\(api_error, API status 404\)") as caught:
            runner.run_session("test only", self.root / "runs" / "x" / "implement-1", phase="implement",
                               model="claude-opus-5-5", effort="medium", writable=True, backend="claude")
        self.assertEqual(classify_stop(caught.exception), "environment")
        self.assertEqual(len(fake_claude_log(self.plan)), 1)


if __name__ == "__main__":
    unittest.main()
