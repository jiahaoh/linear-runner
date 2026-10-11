"""The Linear client: opt-in automatic refresh of an expired credential, and requests that get no answer."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from linear_runner.linear import attention, client
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


class FakeAnswer(io.BytesIO):
    """A JSON answer of the MCP endpoint, as the opener hands it out."""
    headers = {"Content-Type": "application/json"}


class TimedOutRequestTests(unittest.TestCase):
    """W-439: a request to Linear that gets no answer. On 2026-10-09 the batches s210-impl-h-20261009
    and s210-impl-i-20261009 each stopped with the class environment on "The read operation timed
    out"; nothing was wrong, and `recover resume` with a launch cleared both. The supervisor logs
    of the two stops name no call, so these tests show the path with an opener that times out."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"TEST_LINEAR_TOKEN": "t"}); patcher.start(); self.addCleanup(patcher.stop)
        self.linear = LinearClient({"token_env": "TEST_LINEAR_TOKEN", "timeout_seconds": 30})
        self.linear.opener = self
        self.sent, self.failures = [], []

    def open(self, request, timeout):
        """The opener: fails with the next entry of ``failures`` if one is left, else answers."""
        message = json.loads(request.data)
        name = message["params"].get("name") or message["method"]
        self.sent.append(name)
        self.assertEqual(timeout, 30)
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure
        if message["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18"}
        else:
            result = {"content": [{"type": "text", "text": json.dumps({"id": "DEV-1"})}]}
        return FakeAnswer(json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "result": result}).encode())

    TIMEOUT = TimeoutError("The read operation timed out")

    def test_a_read_that_got_no_answer_once_is_tried_again(self):
        self.linear.initialized = True
        for failure in (self.TIMEOUT, urllib.error.URLError(TimeoutError("timed out"))):  # reading, connecting
            with self.subTest(failure=failure):
                self.sent[:], self.failures[:] = [], [failure]
                self.assertEqual(self.linear.issue("DEV-1"), {"id": "DEV-1"})
                self.assertEqual(self.sent, ["get_issue", "get_issue"])

    def test_a_read_that_never_gets_an_answer_stops_after_three_tries_and_says_so(self):
        self.linear.initialized = True
        self.failures[:] = [self.TIMEOUT] * 5
        with self.assertRaisesRegex(client.LinearTimeout, r"^Linear did not answer list_comments within 30 s in each of "
                                                          r"3 tries; check the network and Linear's status, then "
                                                          r"continue with `recover resume`$") as raised:
            self.linear.comments("DEV-1")
        self.assertEqual(self.sent, ["list_comments"] * 3)
        self.assertEqual(attention.classify_stop(raised.exception), "environment")

    def test_a_write_that_got_no_answer_is_sent_once(self):
        self.linear.initialized = True
        for name, arguments in (("save_issue", {"id": "DEV-1", "state": "In Review"}),
                                ("save_comment", {"issueId": "DEV-1", "body": "x"})):
            with self.subTest(name=name):
                self.sent[:], self.failures[:] = [], [self.TIMEOUT]
                with self.assertRaisesRegex(client.LinearTimeout, rf"^Linear did not answer {name} within 30 s\. It is a "
                                                                  r"write, so it was sent once and may have been applied: "
                                                                  r"inspect the issue before retrying$") as raised:
                    self.linear.call(name, **arguments)
                self.assertEqual(self.sent, [name])
                self.assertEqual(attention.classify_stop(raised.exception), "environment")

    def test_the_session_is_opened_again_before_a_write_is_ever_sent(self):
        self.failures[:] = [self.TIMEOUT]
        self.assertEqual(self.linear.call("save_issue", id="DEV-1", state="Done"), {"id": "DEV-1"})
        self.assertEqual(self.sent, ["initialize", "initialize", "notifications/initialized", "save_issue"])
        self.linear.initialized, self.sent[:], self.failures[:] = False, [], [self.TIMEOUT] * 3
        with self.assertRaisesRegex(client.LinearTimeout, r"^Linear did not answer the session's initialization within "
                                                          r"30 s in each of 3 tries, before save_issue was sent; "):
            self.linear.call("save_issue", id="DEV-1", state="Done")
        self.assertEqual(self.sent, ["initialize"] * 3)

    def test_other_failures_of_a_request_are_raised_as_before(self):
        self.linear.initialized = True
        self.failures[:] = [urllib.error.URLError(ConnectionRefusedError("refused"))]
        with self.assertRaises(urllib.error.URLError):
            self.linear.issue("DEV-1")
        self.assertEqual(self.sent, ["get_issue"])
