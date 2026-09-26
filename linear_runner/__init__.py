"""linear-runner: a sequential Linear-to-model issue controller (Codex or Claude Code).

Subpackages: ``config`` (layered configuration), ``engine`` (the per-issue state machine,
checks, delivery and intake), ``linear`` (Linear client, comments and attention),
``supervision`` (launch, supervisor, recovery, watchdog, rules) and ``reporting``
(offline records, trajectory, measurement and rendered samples). ``cli`` is the
command line that the checkout's ``runner.py`` runs.
"""
import sys as _sys

# The code needs Python 3.10+ (README); an older interpreter would fail later with an
# unrelated traceback, so say so first. Kept to syntax any Python 3 can parse.
if _sys.version_info < (3, 10):
    raise SystemExit("linear-runner needs Python 3.10 or newer, but {} is Python {}; run it with a newer "
                     "python3".format(_sys.executable, _sys.version.split()[0]))
