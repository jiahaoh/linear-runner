"""Model pools: the default entry, explicitly named entries (issue label or batch), the
out-of-pool preflight failure, review floors, the low-risk review and escalation within the
Deep pool, and the registry/config validation of pools. No model, network or Linear."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from linear_runner import config as config_module
from linear_runner.config import ConfigError, load_config, pin_resolution
from linear_runner.engine.runner import Runner, git, resolve_profile
from linear_runner.linear.attention import classify_stop
from tests.fixtures import TEST_REGISTRY, FakeLinear, make_home, write

ASTRA_MEDIUM = {"backend": "codex", "model": "astra", "effort": "medium"}
ASTRA_HIGH = {"backend": "codex", "model": "astra", "effort": "high"}
LUNA_MAX = {"backend": "codex", "model": "luna", "effort": "max"}
OPUS_MEDIUM = {"backend": "claude", "model": "claude-opus-5-5", "effort": "medium"}
OPUS_HIGH = {"backend": "claude", "model": "claude-opus-5-5", "effort": "high"}


def registry():
    value = copy.deepcopy(TEST_REGISTRY)
    value["models"]["models"]["claude-opus-5-5"] = {"backend": "claude", "efforts": ["medium", "high"]}
    value["profiles"]["phase_overrides"] = {}
    phases = ("implement", "repair", "review")
    value["pools"]["pools"]["*"] = {"Economy": {p: [LUNA_MAX, OPUS_MEDIUM] for p in phases},
                                    "Standard": {p: [ASTRA_MEDIUM, OPUS_MEDIUM] for p in phases},
                                    "Deep": {p: [ASTRA_HIGH, OPUS_HIGH] for p in phases}}
    # A task-kind pool replaces the * pool of that profile and phase only.
    value["pools"]["pools"]["Maintenance"] = {"Standard": {"implement": [OPUS_MEDIUM]}}
    return value


class PoolResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        self.linear = FakeLinear()

    def load(self, batch=None, reg=None):
        home, path = make_home(self.root, self.repo, registry=reg or registry(), batch=batch,
                               site={"executables": {"codex": "codex", "claude": "claude", "python": sys.executable}})
        return load_config(path, home)

    def issue(self, *labels):
        return {"id": "DEV-1", "labels": list(labels)}

    def pick(self, config, labels, phase, **kwargs):
        selection = resolve_profile(config, self.issue(*labels), phase, **kwargs)
        return (selection["backend"], selection["model"], selection["effort"]), selection

    def test_first_entry_is_the_default_and_task_kind_pools_replace_star(self):
        config = self.load()
        entry, selection = self.pick(config, ["Implementation", "Standard"], "implement")
        self.assertEqual(entry, ("codex", "astra", "medium"))
        self.assertEqual((selection["pool"], selection["pool_index"], selection["model_source"]),
                         ("*/Standard/implement", 0, "pool default"))
        self.assertEqual(selection["pool_entries"], ["codex:astra@medium", "claude:claude-opus-5-5@medium"])
        entry, selection = self.pick(config, ["Maintenance", "Standard"], "implement")
        self.assertEqual((entry, selection["pool"]), (("claude", "claude-opus-5-5", "medium"), "Maintenance/Standard/implement"))
        self.assertEqual(self.pick(config, ["Maintenance", "Standard"], "repair")[1]["pool"], "*/Standard/repair")

    def test_issue_label_selects_an_entry_of_the_pool_for_worker_phases(self):
        config = self.load()
        for label in ("model:claude-opus-5-5", "model:claude-opus-5-5@medium"):
            with self.subTest(label=label):
                for phase in ("implement", "repair"):
                    entry, selection = self.pick(config, ["Implementation", "Standard", label], phase)
                    self.assertEqual(entry, ("claude", "claude-opus-5-5", "medium"))
                    self.assertEqual((selection["pool_index"], selection["model_source"]), (1, f"issue label {label}"))
                # The label does not choose the reviewer (it stays independent: pool default).
                entry, selection = self.pick(config, ["Implementation", "Standard", label], "review")
                self.assertEqual((entry, selection["model_source"]), (("codex", "astra", "medium"), "pool default"))

    def test_batch_names_entries_per_phase_including_review(self):
        config = self.load(batch={"model_overrides": {"DEV-1": {"review": "claude-opus-5-5"}}})
        entry, selection = self.pick(config, ["Implementation", "Standard"], "review")
        self.assertEqual((entry, selection["model_source"]), (("claude", "claude-opus-5-5", "medium"), "batch model_overrides"))
        self.assertEqual(self.pick(config, ["Implementation", "Standard"], "implement")[0], ("codex", "astra", "medium"))
        with self.assertRaisesRegex(ConfigError, "not in the issue allowlist"):
            self.load(batch={"model_overrides": {"DEV-9": {"review": "astra"}}})
        with self.assertRaisesRegex(ConfigError, "unknown key 'deploy'"):
            self.load(batch={"model_overrides": {"DEV-1": {"deploy": "astra"}}})

    def test_named_entry_outside_the_pool_or_conflicting_names_fail_clearly(self):
        config = self.load()
        with self.assertRaisesRegex(RuntimeError, r"issue label model:astra@high names 'astra@high', which is not in "
                                                  r"the \*/Standard/implement model pool \[astra@medium, "
                                                  r"claude-opus-5-5@medium\].*no substitution") as caught:
            resolve_profile(config, self.issue("Implementation", "Standard", "model:astra@high"), "implement")
        self.assertEqual(classify_stop(caught.exception), "needs-decision")
        with self.assertRaisesRegex(RuntimeError, "at most one model:"):
            resolve_profile(config, self.issue("Implementation", "Standard", "model:astra", "model:claude-opus-5-5"), "implement")
        config = self.load(batch={"model_overrides": {"DEV-1": {"implement": "astra"}}})
        with self.assertRaisesRegex(RuntimeError, "conflicts with the batch model_overrides"):
            resolve_profile(config, self.issue("Implementation", "Standard", "model:claude-opus-5-5"), "implement")

    def test_out_of_pool_label_fails_preflight_before_claim_or_model(self):
        git(self.repo, "init", "-q"); git(self.repo, "checkout", "-q", "-b", "codex/test")
        git(self.repo, "config", "user.name", "Test"); git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("fixture"); git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "base")
        config, _ = pin_resolution(self.load(), self.linear)
        runner = Runner(config, self.linear)
        calls = []
        runner.run_session = lambda *args, **kwargs: calls.append(kwargs)
        self.linear.data["labels"] = ["Implementation", "Standard", "model:gpt-6-sol"]
        with self.assertRaisesRegex(RuntimeError, "not in the \\*/Standard/implement model pool"):
            runner.execute(limit=1)
        self.assertEqual((self.linear.data["statusType"], self.linear.posts, calls), ("unstarted", [], []))

    def test_review_floors_pick_the_floor_pool(self):
        config = self.load()
        # Research and Validation reviews floor at Deep; a Deep-labelled issue too.
        for labels in (["Research", "Economy"], ["Validation", "Standard"], ["Implementation", "Deep"]):
            entry, selection = self.pick(config, labels, "review")
            self.assertEqual((entry, selection["profile"], selection["pool"]),
                             (("codex", "astra", "high"), "Deep", "*/Deep/review"))
        # The default floor is Standard: an Economy issue's review uses the Standard pool.
        entry, selection = self.pick(config, ["Implementation", "Economy"], "review")
        self.assertEqual((entry, selection["pool"]), (("codex", "astra", "medium"), "*/Standard/review"))
        # A batch review entry must be in the floored pool.
        config = self.load(batch={"model_overrides": {"DEV-1": {"review": "luna"}}})
        with self.assertRaisesRegex(RuntimeError, "not in the \\*/Standard/review model pool"):
            resolve_profile(config, self.issue("Implementation", "Economy"), "review")

    def test_low_risk_review_uses_the_lighter_pool_unless_the_named_review_entry_is_missing(self):
        config = self.load()
        entry, selection = self.pick(config, ["Implementation", "Standard"], "review", light="Economy")
        self.assertEqual((entry, selection["selection_source"]), (("codex", "luna", "max"), "low-risk review rule"))
        config = self.load(batch={"model_overrides": {"DEV-1": {"review": "astra"}}})
        entry, selection = self.pick(config, ["Implementation", "Standard"], "review", light="Economy")
        self.assertEqual((entry, selection["profile"], selection["model_source"]),
                         (("codex", "astra", "medium"), "Standard", "batch model_overrides"))

    def test_escalation_picks_from_the_deep_pool_of_the_task_kind_and_phase(self):
        config = self.load()
        entry, selection = self.pick(config, ["Implementation", "Economy"], "repair", escalation="Deep")
        self.assertEqual((entry, selection["pool"], selection["selection_source"]),
                         (("codex", "astra", "high"), "*/Deep/repair", "escalation"))
        # A named model that the Deep pool has is kept (at the Deep pool's effort) ...
        entry, selection = self.pick(config, ["Implementation", "Economy", "model:claude-opus-5-5"], "repair", escalation="Deep")
        self.assertEqual((entry, selection["model_source"]), (("claude", "claude-opus-5-5", "high"),
                                                              "issue label model:claude-opus-5-5"))
        # ... and one it lacks falls to the Deep pool's first entry, recorded as such.
        entry, selection = self.pick(config, ["Implementation", "Economy", "model:luna"], "repair", escalation="Deep")
        self.assertEqual(entry, ("codex", "astra", "high"))
        self.assertIn("not in the escalation pool", selection["model_source"])


class PoolValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()

    def test_star_must_cover_every_profile_and_phase(self):
        policy, _, _ = config_module.load_registry(self.root / "no-home")
        del policy["pools"]["pools"]["*"]["Deep"]["review"]
        with self.assertRaisesRegex(ConfigError, r"missing \['Deep/review'\]"):
            config_module.check_policy(policy)

    def test_draft_pools_overlay_is_valid_and_marked_draft(self):
        home = Path(config_module.RUNNER_ROOT) / "examples" / "draft-pools"
        policy, sources, _ = config_module.load_registry(home)
        self.assertEqual(sources["policy.pools.pools.Implementation.Standard.implement"], "private registry/pools.json")
        for name in ("pools", "models"):
            self.assertTrue(json.loads((home / "registry" / f"{name}.json").read_text())["notes"][0].startswith("DRAFT"))
        # The owner's rule: Standard and Economy implementation use only gpt-6-luna or claude-opus-5-5 at medium.
        for profile in ("Standard", "Economy"):
            _, entries = config_module.pool_for(policy, "Implementation", profile, "implement")
            self.assertEqual([(e["model"], e["effort"]) for e in entries],
                             [("gpt-6-luna", "medium"), ("claude-opus-5-5", "medium")])

    def test_claude_pools_need_the_executable(self):
        home, batch = make_home(self.root, self.repo, registry=registry())
        with self.assertRaisesRegex(ConfigError, "site.executables.claude"):
            load_config(batch, home)

    def test_compaction_limit_is_refused_for_claude_backed_phases(self):
        site = {"executables": {"codex": "codex", "claude": "claude", "python": sys.executable}}
        home, batch = make_home(self.root, self.repo, registry=registry(), site=site,
                                batch={"context_controls": {"compact_token_limit": 150000}})
        with self.assertRaisesRegex(ConfigError, "compact_token_limit 150000 applies to the implement phase, but pool "
                                                 r"\*/\w+/implement includes claude-opus-5-5 on the Claude backend"):
            load_config(batch, home)
        # Codex-only pools keep accepting it.
        home, batch = make_home(self.root, self.repo, site=site, batch={"context_controls": {"compact_token_limit": 150000}})
        self.assertEqual(load_config(batch, home)["context_controls"]["compact_token_limit"], 150000)


if __name__ == "__main__":
    unittest.main()
