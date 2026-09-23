"""Layered configuration, registry validation and Linear name resolution (offline, mocked)."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import config
from config import ConfigError, config_fingerprint, find_home, load_config, pin_resolution, write_resolved
from fixtures import FakeLinear, TEST_REGISTRY, make_home, write
from linear_client import LinearClient

ROOT = Path(__file__).resolve().parent


class LayeredConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"

    def load(self, **layers):
        home, batch = make_home(self.root, self.repo, **layers)
        return load_config(batch, home)

    def test_examples_validate_offline_without_state(self):
        loaded = load_config(ROOT / "examples/home/batches/example.json", ROOT / "examples/home")
        self.assertIsNone(loaded["project_id"])
        self.assertEqual(loaded["project_name"], "Example project")
        self.assertEqual(loaded["checks"][0]["command"][0], "python3")
        self.assertEqual(loaded["check_environment"]["TMPDIR"], "/absolute/path/to/local/scratch/example-batch")
        self.assertTrue(loaded["guidance_files"][0].endswith("prompts/generic.md"))
        self.assertEqual(set(loaded["runner"]), {"commit", "dirty"})
        self.assertFalse(Path(loaded["state_dir"]).exists())

    def test_settled_registry_values(self):
        policy, sources, _ = config.load_registry(self.root / "no-home")
        self.assertEqual(policy["labels"]["task_kinds"], ["Research", "Implementation", "Validation", "Maintenance"])
        self.assertEqual(policy["profiles"]["profiles"]["Economy"], {"model": "gpt-5.6-luna", "effort": "max"})
        self.assertEqual(policy["linear"]["states"], {"in_progress": "In Progress", "review": "In Review", "done": "Done"})
        phases = policy["phases"]
        budgets = {name: (p["budget"]["input_tokens"], p["budget"]["output_tokens"], p["budget"]["tool_calls"], p["timeout_seconds"])
                   for name, p in phases["phases"].items()}
        self.assertEqual(budgets, {"implement": (15_000_000, 150_000, 250, 5400), "repair": (5_000_000, 50_000, 100, 5400),
                                   "review": (5_000_000, 40_000, 120, 1800)})
        self.assertEqual((phases["max_repairs"], phases["check_timeout_seconds"]), (2, 1800))
        self.assertEqual(sources["policy.phases.phases.review.budget.input_tokens"], "registry/phases.json")
        for name in config.REGISTRY_NAMES:
            text = (ROOT / "registry" / f"{name}.json").read_text()
            self.assertNotIn("draft", text.lower())

    def test_private_registry_override_merges_and_records_source(self):
        loaded = self.load()
        self.assertEqual(loaded["policy"]["profiles"]["profiles"]["Standard"]["model"], "astra")
        self.assertEqual(loaded["policy"]["phases"]["phases"]["review"]["timeout_seconds"], 1800)
        sources = loaded["_sources"]
        self.assertEqual(sources["policy.phases.phases.review.budget.input_tokens"], "private registry/phases.json")
        self.assertEqual(sources["policy.phases.phases.review.timeout_seconds"], "registry/phases.json")
        self.assertEqual(sources["policy.labels.task_kinds"], "registry/labels.json")
        with self.assertRaisesRegex(ConfigError, "unknown key 'draft'"):
            self.load(registry=dict(TEST_REGISTRY, labels={"draft": True}))

    def test_registry_rejects_unknown_keys_and_files(self):
        cases = [{"models": {"modles": {}}}, {"phases": {"phases": {"implement": {"budget": {"tokens": 1}}}}},
                 {"labels": {"extra": []}}, {"linear": {"states": {"blocked": "Blocked"}}},
                 {"profiles": {"phase_overrides": {"deploy": "Deep"}}}]
        for override in cases:
            with self.subTest(override=override), self.assertRaisesRegex(ConfigError, "unknown key|must be one of"):
                self.load(registry=dict(TEST_REGISTRY, **override))
        home, batch = make_home(self.root, self.repo)
        write(home / "registry" / "budgets.json", {})
        with self.assertRaisesRegex(ConfigError, "Unknown private registry file"):
            load_config(batch, home)

    def test_registry_rejects_bad_references(self):
        def profiles(**changes):
            value = copy.deepcopy(TEST_REGISTRY)
            value["profiles"] = dict(value["profiles"], **changes)
            return value
        cases = [
            (profiles(profiles={"Economy": {"model": "missing", "effort": "max"}}), "unknown model"),
            (profiles(profiles={"Economy": {"model": "astra", "effort": "max"}}), "not allowed"),
            (profiles(escalation_profile="Heroic"), "unknown profile"),
            (profiles(review_floors={"default": "Ultra"}), "unknown profile"),
            (profiles(review_floors={"by_task_kind": {"Docs": "Deep"}}), "unknown task kind"),
            (profiles(review_floors={"by_profile": {"Tiny": "Deep"}}), "unknown profile"),
            (profiles(phase_overrides={"repair": "Tiny"}), "unknown profile"),
            (profiles(order=["Economy", "Standard"]), "same profiles"),
            (dict(TEST_REGISTRY, labels={"profiles": ["Deep", "Standard", "Economy", "Turbo"]}), "same profiles"),
            (dict(TEST_REGISTRY, labels={"task_kinds": ["Deep"]}), "distinct"),
            (dict(TEST_REGISTRY, models={"models": {"astra": {"efforts": ["warp"]}}}), "unknown effort"),
            (dict(TEST_REGISTRY, phases={"max_repairs": 3}), "<= 2"),
            (dict(TEST_REGISTRY, phases={"phases": {"review": {"timeout_seconds": 0}}}), ">= 1"),
        ]
        for registry, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ConfigError, error):
                self.load(registry=registry)

    def test_layers_reject_unknown_keys_and_missing_references(self):
        cases = [({"site": {"typo": 1}}, "unknown key"), ({"site": {"supervisor": {}}}, "unknown key"),
                 ({"workspace": {"team": "Core"}}, "unknown key"), ({"workspace": {"token": "secret"}}, "unknown key"),
                 ({"project": {"state_dir": "x"}}, "unknown key"), ({"batch": {"efficiency": {}}}, "unknown key"),
                 ({"batch": {"project": "missing"}}, "file not found"),
                 ({"project": {"workspace": "other"}}, "file not found"),
                 ({"batch": {"issues": ["DEV-1", "DEV-1"]}}, "unique"),
                 ({"batch": {"issues": ["../escape"]}}, "invalid value"),
                 ({"batch": {"model": "gpt"}}, "unknown key")]
        for layers, error in cases:
            with self.subTest(layers=layers), self.assertRaisesRegex(ConfigError, error):
                self.load(**layers)

    def test_layer_precedence_substitution_and_sources(self):
        write(self.root / "home" / "batch-guidance.md", "Batch authorization text.")
        loaded = self.load(site={"variables": {"scratch": "/tmp/scratch"}},
                           workspace={"states": {"done": "Shipped"}},
                           project={"check_environment": {"TMPDIR": "${scratch}/${batch}", "HOME_COPY": "${home}",
                                                          "PYTHONPATH": "${worktree}/src"}},
                           batch={"guidance_files": ["../batch-guidance.md"], "worktree": "../../worktree"})
        sources = loaded["_sources"]
        self.assertEqual(loaded["states"], {"in_progress": "In Progress", "review": "In Review", "done": "Shipped"})
        self.assertEqual(sources["states.done"], "workspace test")
        self.assertEqual(sources["states.in_progress"], "registry/linear.json")
        self.assertEqual(loaded["check_environment"]["TMPDIR"], "/tmp/scratch/fixture")
        self.assertEqual(loaded["check_environment"]["PYTHONPATH"], str(self.root / "worktree" / "src"))
        self.assertEqual(loaded["check_environment"]["HOME_COPY"], str(self.root / "home"))
        self.assertEqual(loaded["worktree"], str(self.root / "worktree"))
        self.assertEqual(sources["worktree"], "batch fixture")
        self.assertEqual(loaded["state_dir"], str(self.root / "state" / "fixture"))
        self.assertEqual(sources["checks"], "project fixture")
        self.assertEqual(sources["codex"], "site")
        self.assertEqual(sources["project_id"], "unresolved")
        self.assertEqual(sources["guidance_files"], "project fixture + batch fixture")
        # Batch guidance is appended after project guidance; a missing file fails.
        with self.assertRaisesRegex(ConfigError, "file not found"):
            self.load(batch={"guidance_files": ["../missing.md"]})
        loaded = self.load(batch={"guidance_files": ["../batch-guidance.md"]})
        self.assertTrue(loaded["_worker_instructions"].endswith("Batch authorization text."))
        self.assertEqual(loaded["worktree"], str(self.repo))

    def test_guidance_substitutes_known_variables_only(self):
        write(self.root / "home" / "vars.md", "Run ${python} in ${worktree} for ${batch}; keep shell ${HOME} literal.")
        loaded = self.load(project={"guidance_files": ["../vars.md"]})
        self.assertEqual(loaded["_worker_instructions"],
                         f"Run {sys.executable} in {self.repo} for fixture; keep shell ${{HOME}} literal.")

    def test_substitution_rejects_undefined_or_redefined_variables(self):
        with self.assertRaisesRegex(ConfigError, "undefined variable"):
            self.load(project={"checks": [{"name": "x", "kind": "code", "tier": "default", "inputs": ["*"], "cwd": ".",
                                           "command": ["${uv}", "run"]}]})
        for name in ("home", "runner_root", "batch", "worktree"):
            with self.subTest(name=name), self.assertRaisesRegex(ConfigError, "already defined"):
                self.load(site={"variables": {name: "/elsewhere"}})
        with self.assertRaisesRegex(ConfigError, "undefined variable"):
            self.load(batch={"worktree": "${worktree}/nested"})

    def test_credentials_are_references_only(self):
        for auth in ({}, {"token_env": "A", "credentials_file": "/c.json"}):
            with self.subTest(auth=auth), self.assertRaisesRegex(ConfigError, "exactly one"):
                self.load(workspace={"auth": auth})
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            self.load(workspace={"auth": {"token": "lin_api_secret"}})
        loaded = self.load(workspace={"auth": {"credentials_file": "~/.codex/.credentials.json"}})
        self.assertEqual(loaded["linear"], {"credentials_file": str(Path("~/.codex/.credentials.json").expanduser())})

    def test_checks_gates_and_paths_are_validated(self):
        check = {"name": "x", "kind": "code", "tier": "default", "inputs": ["*.py"], "cwd": ".", "command": ["true"]}
        cases = [({"project": {"checks": [dict(check, cwd="..")]}}, "inside the worktree"),
                 ({"project": {"checks": [dict(check, command="python test.py")]}}, "expected array"),
                 ({"project": {"checks": [check, check]}}, "unique"),
                 ({"project": {"checks": [dict(check, kind="lint")]}}, "must be one of"),
                 ({"project": {"checks": [dict(check, inputs=[])]}}, "at least 1"),
                 ({"project": {"checks": [dict(check, inputs=["../x"])]}}, "inside the worktree"),
                 ({"project": {"checks": []}}, "at least 1"),
                 ({"project": {"delivery_checks": [{"cwd": "/abs", "command": ["true"]}]}}, "inside the worktree"),
                 ({"project": {"identity_files": ["missing.lock"]}}, "must exist"),
                 ({"project": {"guidance_files": ["empty.md"]}}, "file not found"),
                 ({"batch": {"human_gates": [{"issue_id": "DEV-1", "comment_id": "c", "author_id": "a", "approval_text": "ok"}]}},
                  "cannot also be"),
                 ({"batch": {"human_gates": [{"issue_id": "GATE-1", "comment_id": "c", "author_id": "a"}]}}, "missing required"),
                 ({"batch": {"required_done": ["DEV-1"]}}, "cannot also be"),
                 ({"site": {"state_root": str(self.repo / "state")}}, "outside the worktree")]
        for layers, error in cases:
            with self.subTest(layers=layers), self.assertRaisesRegex(ConfigError, error):
                self.load(**layers)
        write(self.root / "home" / "projects" / "empty.md", "  \n")
        with self.assertRaisesRegex(ConfigError, "must not be empty"):
            self.load(project={"guidance_files": ["empty.md"]})

    def test_draft_supervision_launcher_and_integrity_fields(self):
        loaded = self.load()
        self.assertEqual(loaded["supervision"], {"stop_after": [], "on_block": "stop", "report_issues": [],
                                                 "decision_rules": "honor", "baseline_checks": False})
        self.assertEqual(loaded["launcher"]["backend"], "systemd-user")
        self.assertTrue(loaded["launcher"]["stop_on_exit"])
        self.assertIsNone(loaded["delivery_integrity"])
        loaded = self.load(site={"variables": {"cpus": "0-3"},
                                 "launcher": {"python": "${python}", "cpu_list": "${cpus}", "environment": {"PATH": "${home}/bin"}}},
                           batch={"supervision": {"stop_after": ["DEV-1"], "report_issues": ["TRACK-9"]}},
                           project={"delivery_integrity": {"manifest": "packet/manifest.json", "revision_field": "code_commit",
                                                           "required_checks": ["output"]}})
        self.assertEqual(loaded["launcher"]["cpu_list"], "0-3")
        self.assertEqual(loaded["launcher"]["python"], sys.executable)
        self.assertEqual(loaded["launcher"]["environment"]["PATH"], str(self.root / "home") + "/bin")
        self.assertEqual(loaded["_sources"]["supervision.stop_after"], "batch fixture")
        self.assertEqual(loaded["_sources"]["launcher.cpu_list"], "site")
        integrity = {"manifest": "m.json", "revision_field": "commit"}
        cases = [({"batch": {"supervision": {"stop_after": ["DEV-9"]}}}, "not in the issue allowlist"),
                 ({"batch": {"supervision": {"on_block": "retry"}}}, "must be one of"),
                 ({"batch": {"supervision": {"typo": 1}}}, "unknown key"),
                 ({"site": {"launcher": {"cpu_list": "all"}}}, "invalid CPU list"),
                 ({"site": {"launcher": {"backend": "cron"}}}, "must be one of"),
                 ({"project": {"delivery_integrity": dict(integrity, required_checks=["docs"])}}, "unknown check"),
                 ({"project": {"delivery_integrity": dict(integrity, manifest="../m.json")}}, "invalid value"),
                 ({"project": {"delivery_integrity": dict(integrity, file_hashes={"sha": "/abs"})}}, "invalid value"),
                 ({"project": {"delivery_integrity": {"manifest": "m.json"}}}, "missing required")]
        for layers, error in cases:
            with self.subTest(layers=layers), self.assertRaisesRegex(ConfigError, error):
                self.load(**layers)

    def test_home_discovery_order(self):
        with patch.dict(os.environ, {config.HOME_ENV: str(self.root / "env-home")}):
            self.assertEqual(find_home(str(self.root / "flag-home")), self.root / "flag-home")
            self.assertEqual(find_home(), self.root / "env-home")
        with patch.dict(os.environ, {}, clear=True), patch.dict(os.environ, {"HOME": str(self.root / "user")}):
            self.assertEqual(find_home(), self.root / "user" / ".config" / "linear-runner")

    def test_fingerprint_covers_resolved_values_not_provenance(self):
        loaded = self.load()
        resolved, fresh = pin_resolution(loaded, FakeLinear())
        self.assertTrue(fresh)
        baseline = config_fingerprint(resolved)
        self.assertNotEqual(baseline, config_fingerprint(dict(resolved, project_id="other")))
        self.assertNotEqual(baseline, config_fingerprint(dict(resolved, _worker_instructions="changed")))
        changed = copy.deepcopy(resolved); changed["policy"]["phases"]["max_repairs"] = 1
        self.assertNotEqual(baseline, config_fingerprint(changed))
        self.assertEqual(baseline, config_fingerprint(dict(resolved, _sources={}, _layers={})))


class RunnerIdentityTests(unittest.TestCase):
    """The fingerprint names the runner by commit and dirty flag, not by checkout path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home, self.batch = make_home(self.root, self.root / "repo",
                                          project={"guidance_files": ["${runner_root}/prompts/generic.md", "../guidance.md"]})

    def checkout(self, name):
        """Copy the runner sources into a fresh Git repository with a deterministic commit."""
        target = self.root / name
        for relative in ("config.py", "linear_client.py", "prompts/generic.md", *[
                str(p.relative_to(ROOT)) for folder in ("registry", "schema") for p in (ROOT / folder).glob("*.json")]):
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target / relative)
        env = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
                   GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
        for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "runner"]):
            subprocess.run(["git", "-C", str(target), *args], check=True, env=env)
        return target, env

    def fingerprint(self, checkout):
        script = ("import json, sys, config\n"
                  "c = config.load_config(sys.argv[1], sys.argv[2]); c.update(project_id='p', assignee_id='u')\n"
                  "print(json.dumps([config.config_fingerprint(c), c['runner'], c['variables']['runner_root']]))")
        output = subprocess.run([sys.executable, "-c", script, str(self.batch), str(self.home)], cwd=checkout,
                                check=True, capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1")).stdout
        return json.loads(output)

    def test_moving_checkout_keeps_fingerprint_but_commit_or_dirty_changes_it(self):
        first, env = self.checkout("checkout-a")
        second, _ = self.checkout("elsewhere/checkout-b")
        a, b = self.fingerprint(first), self.fingerprint(second)
        self.assertNotEqual(a[2], b[2])
        self.assertEqual(a[1], b[1])
        self.assertFalse(a[1]["dirty"])
        self.assertEqual(a[0], b[0])
        (second / "prompts" / "generic.md").write_text("Edited guidance in the runner checkout.")
        dirty = self.fingerprint(second)
        self.assertTrue(dirty[1]["dirty"])
        self.assertNotEqual(dirty[0], a[0])
        subprocess.run(["git", "-C", str(second), "commit", "-qam", "new runner revision"], check=True, env=env)
        committed = self.fingerprint(second)
        self.assertNotEqual(committed[1]["commit"], a[1]["commit"])
        self.assertFalse(committed[1]["dirty"])
        self.assertNotEqual(committed[0], a[0])

    def test_runner_identity_outside_git_is_unknown(self):
        self.assertEqual(config.runner_identity(self.root / "not-a-repo"), {"commit": None, "dirty": None})

    def test_path_normalization_is_exact(self):
        root = "/opt/runner"
        value = {"a": root, "b": root + "/prompts/x.md", "c": root + "-other/x", root + "/k": [root + "/y"]}
        self.assertEqual(config._portable(value, root),
                         {"a": "${runner_root}", "b": "${runner_root}/prompts/x.md", "c": root + "-other/x",
                          "${runner_root}/k": ["${runner_root}/y"]})


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def client(self, responses):
        client = LinearClient({"token_env": "UNUSED_TEST_TOKEN"}); calls = []
        def call(name, **args):
            calls.append((name, args))
            value = responses[name]
            return copy.deepcopy(value(args) if callable(value) else value)
        client.call = call
        return client, calls

    def test_project_names_resolve_exactly_once_or_fail(self):
        page = {"projects": [{"id": "p-1", "name": "Thesis"}, {"id": "p-2", "name": "Thesis archive"}], "hasNextPage": False}
        client, calls = self.client({"list_projects": page})
        self.assertEqual(client.resolve_project("Thesis"), "p-1")
        self.assertEqual(calls[0], ("list_projects", {"query": "Thesis", "limit": 250}))
        with self.assertRaisesRegex(RuntimeError, "no exact match"):
            client.resolve_project("thesis")
        client, _ = self.client({"list_projects": [{"id": "a", "name": "Dup"}, {"id": "b", "name": "Dup"}]})
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            client.resolve_project("Dup")
        pages = {None: {"projects": [{"id": "x", "name": "Other"}], "hasNextPage": True, "cursor": "c2"},
                 "c2": {"projects": [{"id": "y", "name": "Later"}], "hasNextPage": False}}
        client, _ = self.client({"list_projects": lambda args: pages[args.get("cursor")]})
        self.assertEqual(client.resolve_project("Later"), "y")

    def test_assignee_me_and_exact_user_names(self):
        client, calls = self.client({"get_user": {"id": "u-me", "name": "Owner"},
                                     "list_users": {"users": [{"id": "u-1", "name": "Ada", "email": "ada@example.invalid"},
                                                              {"id": "u-2", "name": "Ada Lovelace"}]}})
        self.assertEqual(client.resolve_user("me"), "u-me")
        self.assertEqual(calls[0], ("get_user", {"query": "me"}))
        self.assertEqual(client.resolve_user("ada@example.invalid"), "u-1")
        with self.assertRaisesRegex(RuntimeError, "no exact match"):
            client.resolve_user("Grace")
        client, _ = self.client({"get_user": {"name": "No id"}})
        with self.assertRaisesRegex(RuntimeError, "no ID"):
            client.resolve_user("me")

    def test_resolution_is_pinned_with_sources_and_reused(self):
        home, batch = make_home(self.root, self.root / "repo")
        loaded = load_config(batch, home)
        linear = FakeLinear()
        resolved, fresh = pin_resolution(loaded, linear)
        self.assertTrue(fresh)
        self.assertEqual((resolved["project_id"], resolved["assignee_id"]), ("p", "owner"))
        self.assertEqual(linear.resolutions, [("project", "Fixture project"), ("user", "me")])
        write_resolved(resolved)
        pinned = json.loads((Path(resolved["state_dir"]) / "resolved-config.json").read_text())
        self.assertEqual(pinned["resolution"]["ids"], {"project_id": "p", "assignee_id": "owner"})
        self.assertEqual(pinned["sources"]["project_id"], "Linear name resolution")
        self.assertEqual(pinned["sources"]["checks"], "project fixture")
        self.assertEqual(pinned["config_sha256"], config_fingerprint(resolved))
        self.assertNotIn("_sources", pinned["config"])
        self.assertIn("TEST_LINEAR_TOKEN", json.dumps(pinned))  # the reference, never a value
        # A later run reuses pinned IDs without any Linear call.
        class NoNetwork:
            def __getattr__(self, name):
                raise AssertionError("pinned IDs must be reused without Linear access")
        again, fresh = pin_resolution(load_config(batch, home), NoNetwork())
        self.assertFalse(fresh)
        self.assertEqual(config_fingerprint(again), config_fingerprint(resolved))
        self.assertEqual(again["_sources"]["project_id"], "pinned resolved-config.json")
        # Renamed project or changed guidance cannot silently reuse the pinned batch.
        project = json.loads((home / "projects/fixture.json").read_text())
        write(home / "projects/fixture.json", dict(project, linear_project="Renamed"))
        with self.assertRaisesRegex(ConfigError, "names changed"):
            pin_resolution(load_config(batch, home), NoNetwork())
        write(home / "projects/fixture.json", project)
        write(home / "guidance.md", "Different scope")
        with self.assertRaisesRegex(ConfigError, "Configuration/guidance changed"):
            pin_resolution(load_config(batch, home), NoNetwork())


if __name__ == "__main__":
    unittest.main()
