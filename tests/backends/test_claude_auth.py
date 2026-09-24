"""Claude authentication (``site.claude.auth``): the default subscription login, a long-lived
OAuth token in a file or a named variable. The token reaches only the ``claude`` child (as
CLAUDE_CODE_OAUTH_TOKEN) and never argv, records, logs, the fingerprint or launch settings.
A fake ``claude`` executable records the token it received; no model, network or Linear."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from linear_runner.backends.claude import ClaudeBackend, check_token_file, read_token
from linear_runner.cli import main
from linear_runner.config import ConfigError, _effective, config_fingerprint, load_config, pin_resolution, write_resolved
from linear_runner.engine.runner import Runner, git, write_json
from linear_runner.linear import messages
from linear_runner.linear.attention import classify_stop
from linear_runner.supervision.launcher import ForegroundBackend, LaunchError, SystemdUserBackend, launch
from linear_runner.supervision.supervisor import supervise
from tests.engine.test_claude_engine import claude_registry
from tests.fixtures import FakeLinear, fake_claude, fake_claude_log, make_home, fake_codex
from tests.supervision.test_supervisor import Harness

TOKEN = "fake-oauth-token-never-recorded"
VARIABLE = "RUNNER_CLAUDE_TOKEN"
W191 = {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
        "result": "Failed to refresh OAuth token: another Claude Code process is refreshing it or exited mid-refresh."}


def write_token(path, text=TOKEN + "\n", mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def files_containing(root, needle):
    return [str(p) for p in Path(root).rglob("*") if p.is_file() and needle.encode() in p.read_bytes()]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.token_file = self.root / "secrets" / "claude-oauth.token"

    def load(self, auth=None, **site):
        if auth is not None:
            site["claude"] = {"auth": auth}
        self.home, self.batch = make_home(self.root, self.repo, site=site or None)
        return load_config(self.batch, self.home)


class AuthConfigTests(Fixture):
    def test_default_is_the_subscription_login_and_keeps_existing_fingerprints(self):
        default = self.load()
        self.assertNotIn("claude_auth", default)
        self.assertEqual(ClaudeBackend(default).auth_mode, "subscription-login")
        self.assertEqual(config_fingerprint(self.load(claude={})), config_fingerprint(default))
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            main(["validate-config", "--batch", str(self.batch), "--home", str(self.home)])
        self.assertEqual(json.loads(out.getvalue())["claude_auth"], {"mode": "subscription-login"})

    def test_token_file_and_variable_are_references(self):
        loaded = self.load({"oauth_token_file": "../secrets/claude-oauth.token"})
        self.assertEqual(loaded["claude_auth"], {"mode": "oauth-token-file", "oauth_token_file": str(self.token_file)})
        self.assertEqual(loaded["_sources"]["claude_auth.oauth_token_file"], "site")
        loaded = self.load({"oauth_token_env": VARIABLE})
        self.assertEqual(loaded["claude_auth"], {"mode": "oauth-token-env", "oauth_token_env": VARIABLE})

    def test_schema_and_references_are_validated(self):
        cases = [({}, "exactly one"), ({"oauth_token_file": "/a.token", "oauth_token_env": VARIABLE}, "exactly one"),
                 ({"oauth_token": TOKEN}, "unknown key"), ({"oauth_token_env": "1-bad"}, "invalid value"),
                 ({"oauth_token_file": ""}, "must not be empty"), ({"oauth_token_file": "${missing}/t"}, "undefined variable"),
                 ({"oauth_token_file": str(self.repo / "claude.token")}, "outside the worktree"),
                 ({"oauth_token_file": "${runner_root}/claude.token"}, "outside the worktree")]
        for auth, error in cases:
            with self.subTest(auth=auth), self.assertRaisesRegex(ConfigError, error):
                self.load(auth)
        with self.assertRaisesRegex(ConfigError, "unknown key 'token'"):
            self.load(claude={"token": TOKEN})

    def test_fingerprint_covers_the_mode_and_reference_never_the_token(self):
        write_token(self.token_file)
        first = self.load({"oauth_token_file": str(self.token_file)})
        write_token(self.token_file, "fake-oauth-token-rotated\n")
        rotated = self.load({"oauth_token_file": str(self.token_file)})
        self.assertEqual(config_fingerprint(first), config_fingerprint(rotated))
        others = [self.load(), self.load({"oauth_token_env": VARIABLE}),
                  self.load({"oauth_token_file": str(self.root / "elsewhere.token")})]
        self.assertEqual(len({config_fingerprint(c) for c in [first, *others]}), 4)
        self.assertNotIn(TOKEN, json.dumps(first))
        self.assertNotIn(TOKEN, json.dumps(_effective(first)))


class TokenFileTests(Fixture):
    def config(self):
        return {"claude_auth": {"mode": "oauth-token-file", "oauth_token_file": str(self.token_file)}}

    def test_missing_directory_permissive_and_empty_files_are_refused_without_the_content(self):
        with self.assertRaisesRegex(RuntimeError, "does not exist; create it with `claude setup-token`"):
            check_token_file(self.token_file)
        self.token_file.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "is not a regular file"):
            check_token_file(self.token_file)
        self.token_file.rmdir()
        for mode in (0o644, 0o640, 0o604, 0o660, 0o700):
            write_token(self.token_file, mode=mode)
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(RuntimeError, f"has mode {mode:03o}.*chmod 600") as caught:
                read_token(self.config())
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertEqual(classify_stop(caught.exception), "environment")
        write_token(self.token_file, "")
        with self.assertRaisesRegex(RuntimeError, "is empty"):
            check_token_file(self.token_file)
        for text, error in (("  \n", "is empty"), (TOKEN + "\n" + TOKEN + "\n", "one token on one line")):
            write_token(self.token_file, text)
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, error) as caught:
                read_token(self.config())
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_owner_only_files_pass_and_the_token_is_stripped(self):
        for mode in (0o600, 0o400):
            write_token(self.token_file, "  " + TOKEN + "\n", mode=mode)
            self.assertEqual(read_token(self.config()), TOKEN)

    def test_validate_config_checks_the_file_metadata_and_never_prints_the_token(self):
        write_token(self.token_file)
        self.load({"oauth_token_file": str(self.token_file)})
        args = ["validate-config", "--batch", str(self.batch), "--home", str(self.home)]
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            main(args)
        report = json.loads(out.getvalue())["claude_auth"]
        self.assertEqual((report["mode"], report["oauth_token_file"]), ("oauth-token-file", str(self.token_file)))
        self.assertTrue(report["token_file"].startswith("ok"))
        self.assertNotIn(TOKEN, out.getvalue())
        for mode, problem in ((0o644, "has mode 644"), (None, "does not exist")):
            if mode is None:
                self.token_file.unlink()
            else:
                self.token_file.chmod(mode)
            with self.subTest(problem=problem), patch("sys.stdout", new_callable=io.StringIO) as out, \
                    patch("sys.stderr", new_callable=io.StringIO) as err, self.assertRaises(SystemExit):
                main(args)
            self.assertIn(problem, err.getvalue())
            self.assertNotIn(TOKEN, err.getvalue() + out.getvalue())

    def test_validate_config_reports_whether_a_token_variable_is_set_here(self):
        self.load({"oauth_token_env": VARIABLE})
        for value, expected in (({}, False), ({VARIABLE: TOKEN}, True)):
            with patch.dict(os.environ, value), patch("sys.stdout", new_callable=io.StringIO) as out:
                os.environ.pop(VARIABLE, None) if not value else None
                main(["validate-config", "--batch", str(self.batch), "--home", str(self.home)])
            self.assertEqual(json.loads(out.getvalue())["claude_auth"],
                             {"mode": "oauth-token-env", "oauth_token_env": VARIABLE, "set_in_this_environment": expected})
            self.assertNotIn(TOKEN, out.getvalue())


class PreflightTests(Fixture):
    """``check_selection``/``check_auth`` with the fake CLI (no model call)."""

    def backend(self, claude_auth=None, **plan):
        executable, self.plan = fake_claude(self.root, **plan)
        return ClaudeBackend(dict({"claude": str(executable)}, **({"claude_auth": claude_auth} if claude_auth else {})))

    def probes(self):
        return json.loads(Path(self.plan).read_text()).get("auth_probes", [])

    def test_subscription_login_is_unchanged(self):
        backend = self.backend()
        backend.check_selection({"model": "claude-opus-5-5", "effort": "medium"})
        self.assertEqual(self.probes(), [None])
        with self.assertRaisesRegex(RuntimeError, "not the subscription login"):
            self.backend(auth={"authMethod": "oauth_token"}).check_auth()

    def test_token_file_mode_needs_a_usable_file_not_the_subscription_login(self):
        auth = {"mode": "oauth-token-file", "oauth_token_file": str(self.token_file)}
        with self.assertRaisesRegex(RuntimeError, r"Claude authentication \(oauth-token-file\).*does not exist") as caught:
            self.backend(auth).check_selection({"model": "claude-opus-5-5", "effort": "medium"})
        self.assertEqual(classify_stop(caught.exception), "environment")
        write_token(self.token_file)
        backend = self.backend(auth)
        backend.check_selection({"model": "claude-opus-5-5", "effort": "medium"})
        self.assertEqual(self.probes(), [TOKEN])  # `claude auth status` ran with the token
        self.assertEqual(backend.auth_status(), {"loggedIn": True, "authMethod": "oauth_token"})
        report = backend.catalog_report([{"model": "claude-opus-5-5", "effort": "high"}])
        self.assertEqual((report["auth_mode"], report["auth"]["authMethod"]), ("oauth-token-file", "oauth_token"))
        self.assertNotIn(TOKEN, json.dumps(report))
        # A CLI that ignores the token (for example one that prefers another credential) fails preflight.
        with self.assertRaisesRegex(RuntimeError, "does not use the configured token"):
            self.backend(auth, auth={"authMethod": "claude.ai"}).check_auth()

    def test_token_variable_mode_needs_the_variable_set(self):
        auth = {"mode": "oauth-token-env", "oauth_token_env": VARIABLE}
        with patch.dict(os.environ, {}):
            os.environ.pop(VARIABLE, None)
            with self.assertRaisesRegex(RuntimeError, f"{VARIABLE} is not set") as caught:
                self.backend(auth).check_auth()
            self.assertEqual(classify_stop(caught.exception), "environment")
        with patch.dict(os.environ, {VARIABLE: TOKEN}):
            backend = self.backend(auth)
            backend.check_auth()
            self.assertEqual(self.probes(), [TOKEN])
            env = backend.environment(dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN="inherited", HOME="/h"))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
        self.assertNotIn(VARIABLE, env)  # only under the name the CLI reads


class ErrorClassificationTests(unittest.TestCase):
    CTX = {"batch": "b", "batch_arg": "b", "home": None, "prefix": "runner", "mention": "", "branch": "",
           "max_repairs": 2}

    def stop(self, mode, result=W191):
        config = {"claude_auth": {"mode": mode}} if mode != "subscription-login" else {}
        failure = ClaudeBackend(config).failure([result])
        error = RuntimeError(f"Claude failed or did not finish a turn: {failure}; see /runs/x")
        body = messages.blocked(self.CTX, issue="DEV-1", classification=classify_stop(error), error=str(error),
                                step="implement")
        return failure, classify_stop(error), body

    def test_refresh_race_in_subscription_mode_suggests_a_token_file(self):
        failure, cls, body = self.stop("subscription-login")
        self.assertTrue(failure.startswith("Claude authentication (subscription-login) failed: error result (api_error): "
                                           "Failed to refresh OAuth token"))
        self.assertEqual(cls, "environment")
        self.assertIn("can race when it refreshes", body)
        self.assertIn("claude.auth.oauth_token_file", body)

    def test_token_modes_say_to_regenerate_the_token(self):
        for mode in ("oauth-token-file", "oauth-token-env"):
            for result in (W191, dict(W191, result="Invalid bearer token", api_error_status=401)):
                with self.subTest(mode=mode, result=result["result"]):
                    failure, cls, body = self.stop(mode, result)
                    self.assertIn(f"Claude authentication ({mode}) failed", failure)
                    self.assertEqual(cls, "environment")
                    self.assertIn("Regenerate it with claude setup-token", body)

    def test_unreadable_token_at_session_start_says_to_fix_the_token(self):
        error = RuntimeError("Claude authentication (oauth-token-file): the token file /t has mode 644; ...")
        body = messages.blocked(self.CTX, issue="DEV-1", classification=classify_stop(error), error=str(error))
        self.assertIn("Regenerate it with claude setup-token", body)

    def test_other_errors_keep_the_generic_environment_action(self):
        result = dict(W191, result="There's an issue with the selected model.", api_error_status=404)
        failure, cls, body = self.stop("oauth-token-file", result)
        self.assertNotIn("Claude authentication", failure)
        self.assertEqual(cls, "environment")
        self.assertIn("Fix the host or service problem", body)
        body = messages.blocked(self.CTX, issue="DEV-1", classification="environment", error="Linear OAuth expired")
        self.assertIn("Fix the host or service problem", body)


class TokenReachesOnlyTheChildTests(Fixture):
    """A full launch (foreground supervisor), implementation and Claude review with the fake CLI."""

    def setUp(self):
        super().setUp()
        git(self.repo, "init", "-q"); git(self.repo, "checkout", "-q", "-b", "codex/test")
        git(self.repo, "config", "user.name", "Test"); git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("fixture")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "baseline")
        self.linear = FakeLinear()

    def launch(self, auth, check_script=None):
        executable, self.plan = fake_claude(self.root, [{"write": {"result.txt": "ready"}}, {}])
        project = None
        if check_script:
            project = {"checks": [{"name": "output", "kind": "code", "tier": "default", "inputs": ["result.txt"],
                                   "cwd": ".", "command": [sys.executable, "-c", check_script]}]}
        self.home, self.batch = make_home(self.root, self.repo, registry=claude_registry(), project=project,
                                          site={"executables": {"codex": str(fake_codex(self.root)[0]), "claude": str(executable),
                                                                "python": sys.executable},
                                                "claude": {"auth": auth}})

        def runner():
            config, fresh = pin_resolution(load_config(self.batch, self.home), self.linear)
            if fresh:
                write_resolved(config)
            return Runner(config, self.linear)

        first = runner()
        self.output = []
        entry = launch(first.config, self.linear, runner=first, out=self.output.append, backend=ForegroundBackend(
            lambda spec: supervise(first.config, self.linear, launch_id=spec["launch_id"], stop_after=spec["stop_after"],
                                   scope=spec["scope"], runner=runner())))
        return first.config, entry

    def assert_never_recorded(self, config, entry):
        calls = fake_claude_log(self.plan)
        self.assertEqual([c["mode"] for c in calls], ["acceptEdits", "dontAsk"])  # implementation, then review
        self.assertEqual({c["oauth_token"] for c in calls}, {TOKEN})  # the child got it ...
        for call in calls:
            self.assertNotIn(TOKEN, json.dumps(call["argv"]))  # ... never in argv
        self.assertEqual(entry["confirmation"]["supervisor"]["status"], "exited")
        self.assertEqual([h["issue_id"] for h in json.loads((self.root / "state/fixture/state.json").read_text())["history"]],
                         ["DEV-1"])
        # State, launch record, preflight, resolved-config.json, runs (session.json, events.jsonl,
        # stderr, prompts, checks), the private home and printed output: never the token.
        for directory in (self.root / "state", self.root / "runs", self.home):
            self.assertEqual(files_containing(directory, TOKEN), [], directory)
        self.assertNotIn(TOKEN, "\n".join(self.output) + json.dumps(entry) + json.dumps(_effective(config)))
        self.assertNotIn(TOKEN, "".join(json.dumps(p) for p in self.linear.posts))
        preflight = json.loads((self.root / "state/fixture/preflight.json").read_text())
        step = preflight["steps"]["claude_auth"]
        self.assertEqual((step["status"], step["reused"], step["result"]["mode"]),
                         ("passed", False, config["claude_auth"]["mode"]))
        sessions = [json.loads(p.read_text()) for p in (self.root / "runs").rglob("session.json")]
        self.assertEqual({s["backend_details"]["auth_mode"] for s in sessions}, {config["claude_auth"]["mode"]})

    def test_token_file_reaches_only_the_claude_child(self):
        write_token(self.token_file)
        config, entry = self.launch({"oauth_token_file": str(self.token_file)})
        self.assertEqual(entry["spec"]["inherit"], ["TEST_LINEAR_TOKEN"])  # the file needs nothing passed
        self.assert_never_recorded(config, entry)

    def test_token_variable_reaches_only_the_claude_child_under_the_cli_name(self):
        # The check fails if the runner's variable leaks into check subprocesses.
        script = (f"import os; from pathlib import Path; assert {VARIABLE!r} not in os.environ; "
                  "assert Path('result.txt').read_text() == 'ready'")
        with patch.dict(os.environ, {VARIABLE: TOKEN}):
            config, entry = self.launch({"oauth_token_env": VARIABLE}, check_script=script)
        self.assertEqual(entry["spec"]["inherit"], ["TEST_LINEAR_TOKEN", VARIABLE])
        self.assert_never_recorded(config, entry)
        for call in fake_claude_log(self.plan):
            self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", call["environment"])
            self.assertNotIn(VARIABLE, call["environment"])


class SystemdTokenVariableTests(Harness):
    SITE = {"claude": {"auth": {"oauth_token_env": VARIABLE}},
            "launcher": {"backend": "systemd-user", "environment": {"PATH": "/usr/bin:/bin"}, "startup_timeout_seconds": 5}}

    def systemd(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "systemd-run" and "supervise" in argv:
                write_json(self.state_dir / "supervisor.json",
                           {"launch_id": argv[argv.index("--launch-id") + 1], "pid": 4242, "status": "running"})
            return subprocess.CompletedProcess(argv, 0, "MainPID=4242\nActiveState=active\n", "")
        return SystemdUserBackend(run=run, sleep=lambda s: None), calls

    def test_the_unit_copies_the_variable_by_name_only(self):
        backend, calls = self.systemd()
        runner = self.make_runner()
        with patch.dict(os.environ, {VARIABLE: TOKEN, "TEST_LINEAR_TOKEN": "fake-linear-token"}):
            entry = launch(runner.config, self.linear, backend=backend, runner=runner, out=lambda text: None)
        supervisor = next(c for c in calls if "supervise" in c)
        watchdog = next(c for c in calls if "watchdog" in c)
        for argv in (supervisor, watchdog):
            self.assertIn(f"--setenv={VARIABLE}", argv)
            self.assertNotIn(TOKEN, json.dumps(argv))
        self.assertEqual(files_containing(self.state_dir, TOKEN), [])
        self.assertNotIn(TOKEN, json.dumps(entry))

    def test_preflight_fails_when_the_variable_is_unset(self):
        backend, calls = self.systemd()
        runner = self.make_runner()
        with patch.dict(os.environ, {}):
            os.environ.pop(VARIABLE, None)
            with self.assertRaisesRegex(LaunchError, f"Preflight step 'claude_auth' failed: .*{VARIABLE} is not set"):
                launch(runner.config, self.linear, backend=backend, runner=runner, out=lambda text: None)
        self.assertFalse([c for c in calls if "supervise" in c])


if __name__ == "__main__":
    unittest.main()
