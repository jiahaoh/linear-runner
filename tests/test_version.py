"""Releases: the CHANGELOG contract, --version, the recorded release and the batch pin (W-204)."""
import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from linear_runner import cli
from linear_runner.config import load_config, pin_resolution, write_resolved
from linear_runner.engine.runner import Runner, git
from linear_runner.supervision.launcher import LaunchError, preflight
from linear_runner.version import RELEASE, describe, pin_problem
from tests.fixtures import CHECKOUT, FakeLinear, make_home

HEADING = re.compile(r"^## (Unreleased|v(\d+\.\d+\.\d+) — (\d{4}-\d{2}-\d{2}))$", re.M)
TIERS = ("first-issue checkpoint", "canary batch")


def changelog_entries():
    """``[(version or None for Unreleased, body)]`` in file order."""
    text = (CHECKOUT / "CHANGELOG.md").read_text()
    headings = list(HEADING.finditer(text))
    return [(m.group(2), text[m.end():headings[i + 1].start() if i + 1 < len(headings) else len(text)])
            for i, m in enumerate(headings)]


class ChangelogTests(unittest.TestCase):
    def test_every_release_states_its_project_impact_and_canary_tier(self):
        entries = changelog_entries()
        self.assertIsNone(entries[0][0], "the first section is ## Unreleased")
        releases = [(version, body) for version, body in entries if version]
        self.assertTrue(releases)
        for version, body in releases:
            with self.subTest(version=version):
                self.assertRegex(body, r"(?m)^\*\*Project impact:\*\* \S")
                tier = re.search(r"(?m)^\*\*Canary tier:\*\* (.+?)[.;]", body)
                self.assertIsNotNone(tier)
                self.assertIn(tier.group(1), TIERS)

    def test_the_code_names_the_newest_release(self):
        newest = next(version for version, _ in changelog_entries() if version)
        self.assertEqual(newest, RELEASE)

    def test_the_first_release_covers_milestones_03_and_04_and_w200(self):
        body = dict(changelog_entries())["2.0.0"]
        for text in ("milestone 03", "milestone 04", "`W-200`"):
            self.assertIn(text, body)


class VersionTests(unittest.TestCase):
    def test_version_names_the_release_commit_and_state(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_:
            cli.main(["--version"])
        self.assertEqual(exit_.exception.code, 0)
        line = out.getvalue().strip()
        self.assertTrue(line.startswith(f"linear-runner {RELEASE}"), line)
        self.assertIn(git(CHECKOUT, "rev-parse", "HEAD"), line)

    def test_describe_distinguishes_the_tagged_release(self):
        identity = {"release": "2.0.0", "commit": "c" * 40, "dirty": False}
        self.assertEqual(describe(identity, at_tag=True), f"linear-runner 2.0.0 (commit {'c' * 40}, clean)")
        self.assertEqual(describe(identity, at_tag=False),
                         f"linear-runner 2.0.0 with unreleased changes (commit {'c' * 40}, clean; "
                         "not the tagged release v2.0.0)")
        self.assertIn("dirty; uncommitted changes on v2.0.0", describe(dict(identity, dirty=True), at_tag=True))


class PinTests(unittest.TestCase):
    """A batch's ``runner_version`` against this checkout, recorded and checked at preflight."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        git(self.repo, "init", "-q"); git(self.repo, "checkout", "-q", "-b", "codex/test")
        git(self.repo, "config", "user.name", "Test"); git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "result.txt").write_text("ready"); git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "base")
        self.linear = FakeLinear()
        self.identity = {"release": RELEASE, "commit": "a" * 40, "dirty": False}
        patcher = patch("linear_runner.config.runner_identity", side_effect=lambda *a: dict(self.identity))
        patcher.start(); self.addCleanup(patcher.stop)

    def runner(self, **batch):
        home, path = make_home(self.root, self.repo, batch=batch or None)
        config, fresh = pin_resolution(load_config(path, home), self.linear)
        write_resolved(config)
        return Runner(config, self.linear)

    def test_resolved_config_records_the_release(self):
        runner = self.runner()
        pinned = json.loads((Path(runner.config["state_dir"]) / "resolved-config.json").read_text())
        self.assertEqual(pinned["config"]["runner"], self.identity)
        self.assertNotIn("runner_version", pinned["config"])  # unpinned fingerprints are unchanged

    def test_a_batch_pinned_to_another_release_fails_preflight(self):
        runner = self.runner(runner_version="9.9.9")
        with self.assertRaisesRegex(LaunchError, r"Preflight step 'config' failed: The batch pins runner_version "
                                                 rf"9\.9\.9, but this checkout is release {re.escape(RELEASE)} at "
                                                 r"commit a{40}; check out the tag v9\.9\.9"):
            preflight(runner.config, runner, launch_id="L-pinned")
        record = json.loads((Path(runner.config["state_dir"]) / "preflight.json").read_text())
        self.assertFalse(record["passed"])

    def test_the_pin_needs_the_clean_tagged_release(self):
        runner = self.runner(runner_version=RELEASE)
        with patch("linear_runner.version.tagged", return_value=False):
            self.assertIn("which is not the tagged release", pin_problem(runner.config, CHECKOUT))
        with patch("linear_runner.version.tagged", return_value=True):
            self.assertIsNone(pin_problem(runner.config, CHECKOUT))
            record = preflight(runner.config, runner, launch_id="L-match")
            self.assertEqual(record["steps"]["config"]["result"]["runner_version"], RELEASE)
            dirty = dict(runner.config, runner=dict(self.identity, dirty=True))
            self.assertIn("uncommitted changes", pin_problem(dirty, CHECKOUT))
