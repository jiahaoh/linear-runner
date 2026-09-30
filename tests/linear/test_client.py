"""The Linear client's credential handling: opt-in automatic refresh of an expired credential."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

from linear_runner.linear import client
from linear_runner.linear.client import LinearClient

ENDPOINT = "https://mcp.linear.app/mcp"

# A fake refresh command: rewrites the credential file with a new token and an expiry an hour
# ahead (in milliseconds, as Codex's file backend records it), or exits with the given status.
FAKE_REFRESH = r'''
import json, os, sys, time
path, status = sys.argv[1], int(sys.argv[2])
with open(path + ".calls", "a") as calls:
    calls.write(json.dumps({"cwd": os.getcwd(), "stdin_closed": sys.stdin.read() == ""}) + "\n")
if status:
    sys.exit(status)
entries = json.load(open(path))
entries["linear"].update(access_token="fresh-token", expires_at=int((time.time() + 3600) * 1000))
json.dump(entries, open(path, "w"))
'''


class CredentialRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "credentials.json"
        self.write_credential(int(time.time()) - 600)
        self.script = self.root / "fake-refresh.py"
        self.script.write_text(FAKE_REFRESH)

    def write_credential(self, expires_at):
        self.path.write_text(json.dumps({"linear": {"server_name": "linear", "server_url": ENDPOINT,
                                                    "expires_at": expires_at, "access_token": "old-token"}}))

    def client(self, status=0, **auth):
        command = shlex.join([sys.executable, str(self.script), str(self.path), str(status)])
        linear = LinearClient(dict({"credentials_file": str(self.path), "refresh_command": command}, **auth))
        self.lines = []
        linear.log = self.lines.append
        return linear

    def calls(self):
        calls = Path(str(self.path) + ".calls")
        return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

    def test_an_expired_credential_is_refreshed_once_and_used(self):
        linear = self.client(auto_refresh=True)
        self.assertEqual(linear.token(), "fresh-token")
        self.assertEqual(len(self.calls()), 1)
        call = self.calls()[0]
        self.assertTrue(call["stdin_closed"])
        self.assertNotEqual(Path(call["cwd"]).resolve(), Path.cwd().resolve())  # a temporary directory
        self.assertFalse(Path(call["cwd"]).exists())
        self.assertEqual(len(self.lines), 1)
        self.assertRegex(self.lines[0], r"^Linear OAuth credential refreshed automatically with `.*`: it expired at "
                                        r".* and now expires at .*")
        self.assertNotRegex(self.lines[0], "fresh-token|old-token")  # never the token
        self.assertEqual(linear.token(), "fresh-token")  # the new expiry is in the future: no second run
        self.assertEqual(len(self.calls()), 1)

    def test_a_failed_refresh_raises_the_expired_error_and_is_not_repeated(self):
        linear = self.client(status=3, auto_refresh=True)
        with self.assertRaisesRegex(RuntimeError, r"^Linear OAuth expired at .*; refresh it with `.*fake-refresh\.py.*` "
                                                  r"\(the owning CLI refreshes a credential once it has expired\), "
                                                  r"then resume$"):
            linear.token()
        with self.assertRaisesRegex(RuntimeError, "Linear OAuth expired at"):
            linear.token()
        self.assertEqual(len(self.calls()), 1)  # one attempt per expiry value per process
        self.assertEqual(self.lines, [])
        # Another client in the same process does not retry the same expiry either.
        with self.assertRaisesRegex(RuntimeError, "Linear OAuth expired at"):
            self.client(status=0, auto_refresh=True).token()
        self.assertEqual(len(self.calls()), 1)

    def test_a_timeout_or_an_unchanged_expiry_raises_the_expired_error(self):
        linear = self.client(auto_refresh=True)
        def slow(argv, **kwargs):
            self.assertEqual(kwargs["timeout"], client.REFRESH_TIMEOUT_SECONDS)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        linear.run = slow
        with self.assertRaisesRegex(RuntimeError, "Linear OAuth expired at"):
            linear.token()
        self.write_credential(int(time.time()) - 300)  # a new expiry value may be tried once
        linear = self.client(auto_refresh=True)
        linear.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0)  # the expiry does not move
        with self.assertRaisesRegex(RuntimeError, "Linear OAuth expired at"):
            linear.token()
        self.assertEqual(self.lines, [])

    def test_refresh_is_off_by_default(self):
        with self.assertRaisesRegex(RuntimeError, "Linear OAuth expired at"):
            self.client().token()
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
