"""Offline unit tests; the layout mirrors the linear_runner package. Run from the checkout root: python3 -m unittest"""
from linear_runner.linear import client

# A Linear read-back that does not show a write yet is read again after short pauses
# (client.read_back). Offline fakes never lag unless a test says so, so the pauses are skipped;
# the tests of the retry itself pass their own ``sleep``.
client.SLEEP = lambda seconds: None
