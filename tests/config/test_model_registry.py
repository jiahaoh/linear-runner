"""W-757 model/effort selections through the offline CLI and unchanged pool policy."""
import contextlib
import io
import json
import sys
from pathlib import Path
import tempfile
import unittest

from linear_runner import cli
from linear_runner.backends.claude import KNOWN_MODELS
from linear_runner.config import load_config, load_registry
from linear_runner.engine.runner import Runner, resolve_profile, write_json
from tests.fixtures import FakeLinear, make_home, set_pools

NEW_MODELS = ("claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "gpt-6.1-terra")


class ModelRegistryTests(unittest.TestCase):
    def test_batch_can_name_every_new_model_and_effort_in_a_private_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            policy, _, _ = load_registry(root / "absent-home")
            entries = [{"backend": policy["models"]["models"][model]["backend"], "model": model, "effort": effort}
                       for model in NEW_MODELS for effort in policy["models"]["models"][model]["efforts"]]
            # Public defaults are untouched: a private pool must permit a batch's named entry.
            reg = {"pools": {"pools": {"*": {}}}}
            phases = {p: list(policy["pools"]["pools"]["*"]["Standard"][p]) + entries
                      for p in ("implement", "repair", "review")}
            set_pools(reg, "Standard", phases)
            for entry in entries:
                with self.subTest(entry=entry):
                    name = entry["model"] + "@" + entry["effort"]
                    home, batch = make_home(root, repo, registry=reg,
                                           site={"executables": {"codex": "codex", "claude": "claude", "python": sys.executable}},
                                           batch={"model_overrides": {"implement": name}})
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        cli.main(["validate-config", "--home", str(home), "--batch", str(batch)])
                    self.assertIs(json.loads(out.getvalue())["valid"], True)
                    config = load_config(batch, home)
                    selected = resolve_profile(config, {"id": "DEV-1", "labels": ["Implementation", "Standard"]}, "implement")
                    self.assertEqual({k: selected[k] for k in entry}, entry)
                    if entry["backend"] == "claude":
                        self.assertIn(entry["effort"], KNOWN_MODELS[entry["model"]])

    def test_finished_state_keeps_its_saved_model_and_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            home, batch = make_home(root, repo, site={"executables": {"codex": "codex", "claude": "claude", "python": sys.executable}})
            config = load_config(batch, home)
            path = Path(config["state_dir"]) / "state.json"
            path.parent.mkdir(parents=True)
            finished = {"issue_id": "DEV-1", "selection": {"backend": "claude", "model": "claude-sonnet-5", "effort": "medium"},
                        "stages": [{"backend": "codex", "model": "gpt-5.6-luna", "effort": "max"}], "status": "done"}
            write_json(path, {"phase": "queue_complete", "history": [finished]})
            before = path.read_bytes()
            runner = Runner(config, FakeLinear())
            self.assertEqual(runner.state["history"], [finished])
            self.assertEqual(path.read_bytes(), before)
