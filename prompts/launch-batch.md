# Launching a batch

Outer model sessions are no longer needed to start, continue or recover a batch. Run:

```bash
python3 runner.py launch --batch <batch file>
```

`launch` validates the configuration, checks the worktree, Linear, gates, dependencies and
the model catalog, starts the host supervisor, confirms startup, prints the unit, PID and
state location, and exits. For continuation, planned checkpoints and the named recovery
commands see "Stop, recovery and continuation" in the README.
