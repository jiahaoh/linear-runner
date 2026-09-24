"""Model pools: every phase's default, the per-phase override mechanism (issue labels and batch
model_overrides) and its precedence, the out-of-pool preflight failure for each phase, review
floors, the low-risk review and escalation within the Deep pool, and pool/config validation.
No model, network or Linear."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

from linear_runner import config as config_module
from linear_runner.config import ConfigError, load_config, pin_resolution
from linear_runner.engine.runner import Runner, git, resolve_profile
from linear_runner.linear.attention import classify_stop
from tests.fixtures import TEST_REGISTRY, FakeLinear, make_home, set_pools

PHASES = ("implement", "repair", "review")
ASTRA_MEDIUM = {"backend": "codex", "model": "astra", "effort": "medium"}
ASTRA_HIGH = {"backend": "codex", "model": "astra", "effort": "high"}
LUNA_MAX = {"backend": "codex", "model": "luna", "effort": "max"}
OPUS_MEDIUM = {"backend": "claude", "model": "claude-opus-5-5", "effort": "medium"}
OPUS_HIGH = {"backend": "claude", "model": "claude-opus-5-5", "effort": "high"}
SITE = {"executables": {"codex": "codex", "claude": "claude", "python": sys.executable}}


def registry():
    value = copy.deepcopy(TEST_REGISTRY)
    value["models"]["models"]["claude-opus-5-5"] = {"backend": "claude", "efforts": ["medium", "high"]}
    value["profiles"]["phase_overrides"] = {}
    set_pools(value, "Economy", {p: [LUNA_MAX, OPUS_MEDIUM] for p in PHASES})
    set_pools(value, "Standard", {p: [ASTRA_MEDIUM, OPUS_MEDIUM] for p in PHASES})
    set_pools(value, "Deep", {p: [ASTRA_HIGH, OPUS_HIGH] for p in PHASES})
    # A task-kind pool replaces the * pool of that profile and phase only.
    value["pools"]["pools"]["Maintenance"]["Standard"]["implement"] = [OPUS_MEDIUM]
    return value


class PoolResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        self.linear = FakeLinear()

    def load(self, overrides=None, reg=None):
        home, path = make_home(self.root, self.repo, registry=reg or registry(), site=SITE,
                               batch={"model_overrides": overrides} if overrides is not None else None)
        return load_config(path, home)

    def pick(self, config, labels, phase, **kwargs):
        selection = resolve_profile(config, {"id": "DEV-1", "labels": list(labels)}, phase, **kwargs)
        return (selection["model"], selection["effort"]), selection

    def fails(self, config, labels, phase, pattern, **kwargs):
        with self.assertRaisesRegex(RuntimeError, pattern) as caught:
            resolve_profile(config, {"id": "DEV-1", "labels": list(labels)}, phase, **kwargs)
        return caught.exception

    def test_each_phase_defaults_to_its_first_entry_and_task_kind_pools_replace_star(self):
        config = self.load()
        for phase in PHASES:
            entry, selection = self.pick(config, ["Implementation", "Standard"], phase)
            self.assertEqual((entry, selection["pool_index"], selection["model_source"]),
                             (("astra", "medium"), 0, "pool default"))
        self.assertEqual(self.pick(config, ["Validation", "Standard"], "implement")[1]["pool"], "*/Standard/implement")
        self.assertEqual(self.pick(config, ["Implementation", "Standard"], "implement")[1]["pool_entries"],
                         ["codex:astra@medium", "claude:claude-opus-5-5@medium"])
        entry, selection = self.pick(config, ["Maintenance", "Standard"], "implement")
        self.assertEqual((entry, selection["backend"], selection["pool"]),
                         (("claude-opus-5-5", "medium"), "claude", "Maintenance/Standard/implement"))

    def test_phase_labels_select_their_own_phase(self):
        config = self.load()
        for phase in PHASES:
            label = f"{phase}-model:claude-opus-5-5"
            for target in PHASES:
                entry, selection = self.pick(config, ["Implementation", "Standard", label], target)
                if target == phase:
                    self.assertEqual((entry, selection["pool_index"], selection["model_source"]),
                                     (("claude-opus-5-5", "medium"), 1, f"issue label {label}"))
                else:
                    self.assertEqual((entry, selection["model_source"]), (("astra", "medium"), "pool default"))

    def test_shorthand_covers_implement_and_repair_and_a_phase_label_wins(self):
        config = self.load()
        labels = ["Implementation", "Standard", "model:claude-opus-5-5@medium"]
        for phase in ("implement", "repair"):
            self.assertEqual(self.pick(config, labels, phase)[0], ("claude-opus-5-5", "medium"))
        self.assertEqual(self.pick(config, labels, "review")[0], ("astra", "medium"))
        entry, selection = self.pick(config, labels + ["implement-model:astra"], "implement")
        self.assertEqual((entry, selection["model_source"]), (("astra", "medium"), "issue label implement-model:astra"))
        self.assertEqual(self.pick(config, labels + ["implement-model:astra"], "repair")[0], ("claude-opus-5-5", "medium"))

    def test_batch_overrides_win_over_labels_per_issue_before_batch_wide(self):
        config = self.load({"implement": "astra", "review": "claude-opus-5-5",
                            "issues": {"DEV-1": {"implement": "claude-opus-5-5"}}})
        labels = ["Implementation", "Standard", "implement-model:astra@medium", "review-model:astra"]
        entry, selection = self.pick(config, labels, "implement")
        self.assertEqual((entry, selection["model_source"]),
                         (("claude-opus-5-5", "medium"), "batch model_overrides.issues.DEV-1.implement"))
        self.assertEqual(selection["shadowed_requests"], ["astra (batch model_overrides.implement)",
                                                          "astra@medium (issue label implement-model:astra@medium)"])
        entry, selection = self.pick(config, labels, "review")
        self.assertEqual((entry, selection["model_source"]), (("claude-opus-5-5", "medium"), "batch model_overrides.review"))
        self.assertEqual(self.pick(config, labels, "repair")[1]["model_source"], "pool default")

    def test_a_name_outside_the_phase_pool_fails_for_every_phase(self):
        config = self.load()
        for phase in PHASES:
            error = self.fails(config, ["Implementation", "Standard", f"{phase}-model:luna"], phase,
                               rf"issue label {phase}-model:luna names 'luna', which is not in the \S+/Standard/{phase} "
                               r"model pool \[astra@medium, claude-opus-5-5@medium\].*no substitution")
            self.assertEqual(classify_stop(error), "needs-decision")
        self.fails(config, ["Implementation", "Standard", "model:astra@high"], "repair", "not in the Implementation/Standard/repair")
        for phase in PHASES:
            self.fails(self.load({phase: "luna"}), ["Implementation", "Standard"], phase,
                       f"batch model_overrides.{phase} names 'luna'")
        self.fails(self.load({"issues": {"DEV-1": {"review": "luna"}}}), ["Implementation", "Standard"], "review",
                   "batch model_overrides.issues.DEV-1.review names 'luna'")

    def test_malformed_model_labels_fail(self):
        config = self.load()
        self.fails(config, ["Implementation", "Standard", "model:astra", "model:claude-opus-5-5"], "implement",
                   "at most one model:")
        self.fails(config, ["Implementation", "Standard", "validate-model:astra"], "implement", "unknown model label")

    def test_out_of_pool_name_fails_preflight_before_claim_or_model(self):
        git(self.repo, "init", "-q"); git(self.repo, "checkout", "-q", "-b", "codex/test")
        git(self.repo, "config", "user.name", "Test"); git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("fixture"); git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "base")
        config, _ = pin_resolution(self.load(), self.linear)
        runner = Runner(config, self.linear)
        calls = []
        runner.run_session = lambda *args, **kwargs: calls.append(kwargs)
        self.linear.data["labels"] = ["Implementation", "Standard", "review-model:gpt-6-sol"]
        with self.assertRaisesRegex(RuntimeError, "not in the Implementation/Standard/review|not in the \\*/Standard/review"):
            runner.execute(limit=1)
        self.assertEqual((self.linear.data["statusType"], self.linear.posts, calls), ("unstarted", [], []))

    def test_review_floors_pick_the_floor_pool_for_defaults_and_names(self):
        config = self.load()
        for labels in (["Research", "Economy"], ["Validation", "Standard"], ["Implementation", "Deep"]):
            entry, selection = self.pick(config, labels, "review")
            self.assertEqual((entry, selection["profile"], selection["pool"]), (("astra", "high"), "Deep", "*/Deep/review"))
        entry, selection = self.pick(config, ["Implementation", "Economy"], "review")
        self.assertEqual((entry, selection["pool"]), (("astra", "medium"), "*/Standard/review"))
        # A named reviewer must be in the floored pool.
        entry, _ = self.pick(config, ["Research", "Economy", "review-model:claude-opus-5-5"], "review")
        self.assertEqual(entry, ("claude-opus-5-5", "high"))
        self.fails(config, ["Research", "Economy", "review-model:claude-opus-5-5@medium"], "review",
                   "not in the \\*/Deep/review model pool")
        # The same model may implement and review; nothing forbids it.
        labels = ["Implementation", "Standard", "model:claude-opus-5-5", "review-model:claude-opus-5-5"]
        self.assertEqual(self.pick(config, labels, "implement")[0], self.pick(config, labels, "review")[0])

    def test_low_risk_review_uses_the_lighter_pool_unless_the_named_review_entry_is_missing(self):
        config = self.load()
        entry, selection = self.pick(config, ["Implementation", "Standard"], "review", light="Economy")
        self.assertEqual((entry, selection["selection_source"]), (("luna", "max"), "low-risk review rule"))
        entry, selection = self.pick(config, ["Implementation", "Standard", "review-model:astra"], "review", light="Economy")
        self.assertEqual((entry, selection["profile"], selection["model_source"]),
                         (("astra", "medium"), "Standard", "issue label review-model:astra"))

    def test_escalation_keeps_a_named_model_the_deep_pool_has_else_uses_its_default(self):
        config = self.load()
        entry, selection = self.pick(config, ["Implementation", "Economy"], "repair", escalation="Deep")
        self.assertEqual((entry, selection["pool"], selection["selection_source"], selection["model_source"]),
                         (("astra", "high"), "*/Deep/repair", "escalation", "pool default"))
        entry, selection = self.pick(config, ["Implementation", "Economy", "model:claude-opus-5-5@medium"], "repair",
                                     escalation="Deep")
        self.assertEqual((entry, selection["model_source"]),
                         (("claude-opus-5-5", "high"), "issue label model:claude-opus-5-5@medium, kept in the escalation pool"))
        entry, selection = self.pick(config, ["Implementation", "Economy", "model:luna"], "repair", escalation="Deep")
        self.assertEqual(entry, ("astra", "high"))
        self.assertEqual(selection["model_source"], "escalation pool default (issue label model:luna names 'luna', "
                                                    "which that pool does not have)")


class PoolValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()

    def test_star_must_cover_every_profile_and_phase(self):
        policy, _, _ = config_module.load_registry(self.root / "no-home")
        del policy["pools"]["pools"]["*"]["Deep"]["review"]
        with self.assertRaisesRegex(ConfigError, r"missing \['Deep/review'\]"):
            config_module.check_policy(policy)

    def test_batch_overrides_are_validated(self):
        cases = [({"issues": {"DEV-9": {"review": "astra"}}}, "not in the issue allowlist"),
                 ({"deploy": "astra"}, "unknown key 'deploy'"), ({"issues": {"DEV-1": {"merge": "astra"}}}, "unknown key"),
                 ({"review": "gpt-9"}, "unknown model 'gpt-9'"), ({"review": "astra high"}, "invalid value")]
        for overrides, pattern in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ConfigError, pattern):
                home, batch = make_home(self.root, self.repo, registry=registry(), site=SITE,
                                        batch={"model_overrides": overrides})
                load_config(batch, home)

    def test_claude_executable_is_needed_when_claude_runs_by_default_or_by_batch_name(self):
        reg = registry()
        home, batch = make_home(self.root, self.repo, registry=reg)
        with self.assertRaisesRegex(ConfigError, "site.executables.claude"):  # Maintenance/Standard/implement default
            load_config(batch, home)
        reg["pools"]["pools"]["Maintenance"]["Standard"]["implement"] = [ASTRA_MEDIUM, OPUS_MEDIUM]
        home, batch = make_home(self.root, self.repo, registry=reg)
        load_config(batch, home)  # Claude only as a named alternative: labels are checked at preflight
        home, batch = make_home(self.root, self.repo, registry=reg, batch={"model_overrides": {"review": "claude-opus-5-5"}})
        with self.assertRaisesRegex(ConfigError, "site.executables.claude"):
            load_config(batch, home)

    def test_compaction_limit_is_refused_for_claude_backed_phases(self):
        home, batch = make_home(self.root, self.repo, registry=registry(), site=SITE,
                                batch={"context_controls": {"compact_token_limit": 150000}})
        with self.assertRaisesRegex(ConfigError, "compact_token_limit 150000 applies to the implement phase, but pool "
                                                 r"\S+/\w+/implement includes claude-opus-5-5 on the Claude backend"):
            load_config(batch, home)
        # Codex-only pools keep accepting it.
        codex_only = copy.deepcopy(TEST_REGISTRY)
        home, batch = make_home(self.root, self.repo, registry=codex_only, site=SITE,
                                batch={"context_controls": {"compact_token_limit": 150000}})
        self.assertEqual(load_config(batch, home)["context_controls"]["compact_token_limit"], 150000)


if __name__ == "__main__":
    unittest.main()
