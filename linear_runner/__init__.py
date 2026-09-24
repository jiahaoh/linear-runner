"""linear-runner: a sequential Linear-to-model issue controller (Codex or Claude Code).

Subpackages: ``config`` (layered configuration), ``engine`` (the per-issue state machine,
checks, delivery and intake), ``linear`` (Linear client, comments and attention),
``supervision`` (launch, supervisor, recovery, watchdog, rules) and ``reporting``
(offline records, trajectory, measurement and rendered samples). ``cli`` is the
command line that the checkout's ``runner.py`` runs.
"""
