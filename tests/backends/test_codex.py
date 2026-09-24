"""The Codex backend end to end: the real subprocess argv, events and result with a fake
Codex executable, run through the engine's model session. No Codex, model or network."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from linear_runner.config import load_config, pin_resolution
from linear_runner.engine.runner import RESULT_SCHEMA, Runner, write_json
from tests.fixtures import FakeLinear, TEST_REGISTRY, make_home


class CodexCommandTests(unittest.TestCase):
    """Exercise the real subprocess argv/logging with a fake executable; no Codex or model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fake = self.root / "fake-codex"
        fake.write_text(f"#!{sys.executable}\n" +
                        "import json, pathlib, sys\n"
                        "sys.stdin.read()\n"
                        "schema=json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())\n"
                        "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'received_schema': schema}))\n"
                        "print(json.dumps({'type':'thread.started','thread_id':'fixture-session'}),flush=True)\n"
                        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}),flush=True)\n")
        fake.chmod(0o755)
        self.repo = self.root / "repo"; self.repo.mkdir()
        home, batch = make_home(self.root, self.repo, site={"executables": {"codex": str(fake), "python": sys.executable}})
        self.config, _ = pin_resolution(load_config(batch, home), FakeLinear())
        self.runner = Runner(self.config, FakeLinear())

    def test_selection_and_sandbox_are_forwarded_on_fresh_and_resumed_calls(self):
        cases = [("implement", {"writable": True}), ("repair", {"writable": True, "resume": "original-session"}),
                 ("review", {"writable": False})]
        for phase, kwargs in cases:
            with self.subTest(phase=phase):
                directory = self.root / phase
                result, _, session = self.runner.run_session("test only", directory, phase=phase, model="astra", effort="high", **kwargs)
                self.assertEqual(result["received_schema"], RESULT_SCHEMA)
                self.assertEqual(session, "fixture-session")
                meta = json.loads((directory / "session.json").read_text())
                command = meta["command"]
                self.assertEqual((meta["phase"], meta["requested_model"], meta["requested_reasoning_effort"]), (phase, "astra", "high"))
                self.assertEqual(command[command.index("--model") + 1], "astra")
                self.assertIn('model_reasoning_effort="high"', command)
                self.assertIn("mcp_servers.linear.enabled=false", command)
                if kwargs.get("resume"):
                    self.assertGreater(command.index("--model"), command.index("resume"))
                if kwargs["writable"]:
                    self.assertIn("--approve-for-me", command)
                else:
                    self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertEqual(meta["execution_evidence"]["usage_events"][0]["usage"]["input_tokens"], 10)
                self.assertIsNone(meta["execution_evidence"]["observed_models"])

    def test_custom_schema_reaches_cli_unchanged(self):
        custom = {"type": "object", "properties": {"issue_id": {"type": "string", "enum": ["DEV-1"]}}, "required": ["issue_id"], "additionalProperties": False}
        result, _, _ = self.runner.run_session("schema fixture", self.root / "custom", phase="review", model="astra", effort="high", schema=custom)
        self.assertEqual(result["received_schema"], custom)


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
        result, _, _ = runner.run_session("test only", directory, phase=phase, model="astra", effort="high",
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
        from linear_runner.config import ConfigError
        for bad in (10, "150000"):
            with self.assertRaises(ConfigError):
                self.runner({"compact_token_limit": bad})
