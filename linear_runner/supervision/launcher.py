"""Model-free launch: preflight, start the host supervisor, confirm startup, exit.

``runner.py launch`` replaces the outer-LLM launch procedure. Preflight steps:

========================  =====================================  ==========================
step                      identity it depends on                 reused when unchanged?
========================  =====================================  ==========================
config                    resolved configuration fingerprint     yes
worktree                  source (branch, HEAD, clean, content)  yes
model_catalog             catalog bytes + configuration          yes
baseline_checks (opt.)    source, configuration, environment,    yes
                          fixtures (identity files)
linear                    live Linear state                      never (always re-read)
========================  =====================================  ==========================

Every step records whether it was reused or rerun and why. The ``linear`` step reads
every allowlisted issue (proving authentication), checks gates, ownership, dependencies,
decision-rule blocks and model/effort availability for each pending issue, and performs
the dry-run selection (or, for a saved active issue, the resume-specific checks).

Backends are pluggable: ``systemd-user`` (a transient ``systemd-run --user`` unit with
Restart=no, KillMode=control-group and a STOP marker written by ExecStopPost) and
``foreground`` (runs the supervisor in this process, for tests and debugging).

The ``systemd-user`` backend also starts a transient user timer,
``<prefix>-<batch>-<launch id>-watchdog.timer``, that runs ``runner.py watchdog`` every
``attention.watchdog.interval_minutes`` (10). The watchdog stops its own timer once the
supervisor has reached an outcome and any alert is posted; a later launch stops earlier
timers. The foreground backend starts no timer.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

from linear_runner import backends
from linear_runner.config import RUNNER_ROOT, batch_argument, config_fingerprint, read_json, write_json
from linear_runner.supervision.recovery import expected_state
from linear_runner.engine.runner import (PHASES, Runner, fingerprint, git, issue_contract, now, project_lock,
                                         published_contract_matches, resolve_profile, run_id)
from linear_runner.supervision.supervisor import STATUS_NAME, Supervisor, pid_alive

PREFLIGHT_NAME = "preflight.json"


class LaunchError(RuntimeError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _file_sha(path):
    path = Path(path).expanduser()
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


# --- Identities --------------------------------------------------------------------

def identities(config):
    repo = Path(config["worktree"])
    executables = {}
    for name, value in sorted(config["variables"].items()):
        if name in ("home", "runner_root", "batch", "worktree"):
            continue
        resolved = shutil.which(value) if not os.path.isabs(value) else value
        if resolved and Path(resolved).is_file():
            executables[name] = {"path": resolved, "sha256": _file_sha(resolved)}
    return {
        "config": {"fingerprint": config_fingerprint(config)},
        "source": {"branch": git(repo, "branch", "--show-current"), "head": git(repo, "rev-parse", "HEAD"),
                   "clean": not git(repo, "status", "--porcelain"), "fingerprint": fingerprint(repo)},
        "environment": {"python": sys.version, "executables": executables,
                        "check_environment": config["check_environment"], "launcher": config["launcher"]},
        "fixtures": {path: _file_sha(path) for path in config["identity_files"]},
        "model_catalog": {"path": config["model_catalog"], "sha256": _file_sha(config["model_catalog"])},
    }


# --- Preflight steps -----------------------------------------------------------------

def _check_worktree(runner):
    runner.verify_repo()
    repo = runner.repo
    active = runner.state.get("active")
    dirty = bool(git(repo, "status", "--porcelain"))
    if dirty and not active:
        raise LaunchError("Worktree has uncommitted changes and no active issue owns them")
    return {"branch": runner.config["branch"], "head": git(repo, "rev-parse", "HEAD"), "clean": not dirty,
            "active": active["issue_id"] if active else None}


def _check_catalog(config):
    """Each backend's catalog report for the distinct pool entries it serves."""
    from linear_runner.config import pool_entries
    entries = {}
    for _, _, entry in pool_entries(config["policy"]):
        entries.setdefault(entry["backend"], {})[(entry["model"], entry["effort"])] = entry
    return {name: backends.create(config, name).catalog_report(list(items.values()))
            for name, items in sorted(entries.items())}


def _baseline_checks(runner, directory):
    if git(runner.repo, "status", "--porcelain"):
        raise LaunchError("Baseline checks need a clean worktree")
    records = {}
    for spec in runner.config["checks"]:
        if spec["tier"] != "default":
            continue
        target = directory / spec["name"]
        target.mkdir(parents=True)
        passed = runner.validate(target, [{k: spec[k] for k in ("cwd", "command", "allow_empty") if k in spec}],
                                 runner.config["check_environment"])
        record = read_json(target / "checks.json")[0]
        records[spec["name"]] = {k: record[k] for k in ("exit_code", "status", "log", "sha256")}
        if not passed:
            raise LaunchError(f"Baseline check {spec['name']!r} failed; see {record['log']}")
    return records


def _check_linear(runner, supervisor):
    """Live Linear read-back, gates, ownership, dependencies, rules, models and dry-run."""
    config, linear, state = runner.config, runner.linear, runner.state
    runner.check_gates()
    issues = {}
    done = {h["issue_id"] for h in state["history"]}
    for identifier in config["issues"]:
        live = linear.issue(identifier)
        issues[identifier] = {"status": live.get("status"), "statusType": live.get("statusType"),
                              "blockedBy": [p["id"] for p in live.get("relations", {}).get("blockedBy", [])]}
        if identifier not in done and live.get("statusType") != "completed":
            supervisor.rules(live)
    pending = state.get("pending_recovery")
    if pending and expected_state(state) != pending["expected"]:
        raise LaunchError(f"State changed since recovery {pending['id']} was recorded; record a new recovery")
    if not pending and state.get("phase") == "paused":
        raise LaunchError("The controller is paused; inspect `status`, then record a recovery (runner.py recover ...)")
    active = state.get("active")
    result = {"issues": issues, "pending_recovery": pending["id"] if pending else None,
              "history": sorted(done), "deferred": sorted(state.get("deferred", {}))}
    if active:
        if not pending:
            raise LaunchError(f"{active['issue_id']} is unfinished at step {active['step']!r}; record a recovery first")
        live = linear.issue(active["issue_id"])
        runner.verify_issue(live)
        runner.verify_live_state(live, active["step"])
        if issue_contract(live) != active["contract"] and not (
                active["step"] in ("publish", "done") and published_contract_matches(live, active["issue"])):
            raise LaunchError(f"{active['issue_id']} scope/dependencies/ownership changed since intake; "
                              "use --repin-contract in the recovery if the edit is authorized")
        if active["step"] in ("review", "publish", "done"):
            runner.verify_frozen(active)
        for phase in PHASES:
            runner.verify_model(resolve_profile(config, active["issue"], phase, active.get("escalation")))
        result["resume"] = {"issue": active["issue_id"], "step": active["step"]}
    else:
        selected, waiting = supervisor.select(dry=True)
        result.update(selected=selected, waiting=waiting)
    return result


def preflight(config, runner, *, launch_id, force=False):
    """Run (or reuse) each step; write preflight/<launch_id>.json and preflight.json."""
    root = Path(config["state_dir"])
    latest = root / PREFLIGHT_NAME
    previous = read_json(latest) if latest.exists() else None
    ids = identities(config)
    digests = {name: _digest(value) for name, value in ids.items()}
    record = {"launch_id": launch_id, "at": now(), "identities": ids, "digests": digests, "steps": {}, "passed": False}
    directory = root / "preflight" / launch_id
    supervisor = Supervisor(runner, launch_id=launch_id)

    def step(name, depends, action, *, live=False):
        identity = {d: digests[d] for d in depends}
        prior = (previous or {}).get("steps", {}).get(name)
        if live:
            reason = "live state: always re-read"
        elif force:
            reason = "rerun requested (--rerun-preflight)"
        elif not prior:
            reason = "no previous preflight result"
        elif prior.get("status") != "passed":
            reason = "previous result did not pass"
        elif prior.get("identity") != identity:
            reason = "changed: " + ", ".join(d for d in depends if prior["identity"].get(d) != identity[d])
        else:
            reused_from = prior.get("reused_from") or previous["launch_id"]
            record["steps"][name] = dict(prior, reused=True, reused_from=reused_from,
                                         reason=f"reused: {', '.join(depends)} unchanged since {reused_from}")
            return
        entry = {"status": "failed", "reused": False, "reason": reason, "identity": identity, "at": now()}
        record["steps"][name] = entry
        try:
            entry["result"] = action()
            entry["status"] = "passed"
        except Exception as error:
            entry["error"] = str(error)
            write_json(directory.with_suffix(".json"), record)
            write_json(latest, record)
            raise LaunchError(f"Preflight step {name!r} failed: {error}") from error

    step("config", ["config"], lambda: {"fingerprint": ids["config"]["fingerprint"], "layers": config["_layers"]})
    step("worktree", ["source", "config"], lambda: _check_worktree(runner))
    step("model_catalog", ["model_catalog", "config"], lambda: _check_catalog(config))
    if config["supervision"]["baseline_checks"]:
        step("baseline_checks", ["source", "config", "environment", "fixtures"],
             lambda: _baseline_checks(runner, directory / "baseline"))
    step("linear", [], lambda: _check_linear(runner, supervisor), live=True)
    record["passed"] = True
    write_json(directory.with_suffix(".json"), record)
    write_json(latest, record)
    return record


# --- Backends ----------------------------------------------------------------------

class ForegroundBackend:
    """Runs the supervisor in this process (tests, debugging); returns after it exits."""
    name = "foreground"

    def __init__(self, supervise):
        self.supervise = supervise

    def start(self, spec):
        try:
            outcome = self.supervise(spec)
            return {"pid": os.getpid(), "exit_code": 0, "outcome": outcome}
        except Exception as error:
            return {"pid": os.getpid(), "exit_code": 1, "error": str(error)}

    def confirm(self, spec, started):
        status = _status(spec)
        if not status:
            raise LaunchError(f"Supervisor did not start: {started.get('error')}")
        return {"supervisor": status}

    def start_watchdog(self, spec):
        return None  # tests and debugging: run `runner.py watchdog` by hand if needed

    def stop_watchdog(self, timer):
        return None


class SystemdUserBackend:
    """A transient ``systemd-run --user`` service; no automatic restart."""
    name = "systemd-user"

    def __init__(self, run=subprocess.run, sleep=time.sleep, clock=time.monotonic):
        self.run, self.sleep, self.clock = run, sleep, clock

    def argv(self, spec):
        argv = ["systemd-run", "--user", f"--unit={spec['unit']}", "--property=Restart=no",
                "--property=KillMode=control-group", f"--property=WorkingDirectory={spec['workdir']}",
                f"--property=StandardOutput=append:{spec['log']}", f"--property=StandardError=append:{spec['log']}"]
        if spec.get("stop_marker"):
            touch = shutil.which("touch") or "/usr/bin/touch"
            argv.append(f"--property=ExecStopPost={touch} {shlex.quote(spec['stop_marker'])}")
        argv += [f"--setenv={k}={v}" for k, v in sorted(spec["environment"].items())]
        # Credential variables are inherited by name; their values never enter argv or records.
        argv += [f"--setenv={k}" for k in spec["inherit"]]
        return argv + spec["command"]

    def watchdog_argv(self, spec):
        watch = spec["watchdog"]
        minutes = watch["interval_minutes"]
        argv = ["systemd-run", "--user", f"--unit={watch['unit']}", f"--on-active={minutes}min",
                f"--on-unit-active={minutes}min", "--timer-property=AccuracySec=30s",
                f"--property=WorkingDirectory={spec['workdir']}", f"--property=StandardOutput=append:{watch['log']}",
                f"--property=StandardError=append:{watch['log']}"]
        argv += [f"--setenv={k}={v}" for k, v in sorted(spec["environment"].items())]
        argv += [f"--setenv={k}" for k in spec["inherit"]]
        return argv + watch["command"]

    def start_watchdog(self, spec):
        argv = self.watchdog_argv(spec)
        process = self.run(argv, capture_output=True, text=True)
        if process.returncode:
            raise LaunchError(f"Watchdog timer did not start ({process.returncode}): {process.stderr.strip()}")
        watch = spec["watchdog"]
        return {"unit": watch["unit"], "timer": watch["timer"], "interval_minutes": watch["interval_minutes"],
                "argv": argv, "log": watch["log"], "started_at": now()}

    def stop_watchdog(self, timer):
        process = self.run(["systemctl", "--user", "stop", timer], capture_output=True, text=True)
        return {"exit_code": process.returncode, "stderr": (process.stderr or "").strip()}

    def show(self, unit):
        output = self.run(["systemctl", "--user", "show", unit, "-p", "MainPID", "-p", "ActiveState", "-p", "SubState",
                           "-p", "ExecMainStatus", "-p", "Result"], capture_output=True, text=True, check=True).stdout
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def start(self, spec):
        argv = self.argv(spec)
        process = self.run(argv, capture_output=True, text=True)
        if process.returncode:
            raise LaunchError(f"systemd-run failed ({process.returncode}): {process.stderr.strip()}")
        return {"unit": spec["unit"], "argv": argv, "stdout": process.stdout, "stderr": process.stderr}

    def confirm(self, spec, started):
        deadline = self.clock() + spec["startup_timeout_seconds"]
        unit = {}
        while True:
            status = _status(spec)
            unit = self.show(spec["unit"])
            if status and status.get("status") in ("running", "exited", "refused"):
                return {"supervisor": status, "unit": unit}
            if unit.get("ActiveState") in ("failed", "inactive") and not status:
                raise LaunchError(f"Unit {spec['unit']} stopped before the supervisor started: {unit}; see {spec['log']}")
            if self.clock() >= deadline:
                raise LaunchError(f"Supervisor startup not confirmed within {spec['startup_timeout_seconds']} s: {unit}")
            self.sleep(1)


def _status(spec):
    path = Path(spec["state_dir"]) / STATUS_NAME
    status = read_json(path) if path.exists() else None
    return status if status and status.get("launch_id") == spec["launch_id"] else None


def backend_for(name, supervise=None):
    if name == "systemd-user":
        return SystemdUserBackend()
    if name == "foreground":
        return ForegroundBackend(supervise)
    raise LaunchError(f"Unknown launcher backend {name!r}")


# --- Launch --------------------------------------------------------------------------

def unit_name(config, launch_id):
    base = f"{config['launcher']['unit_prefix']}-{config['batch_id']}-{launch_id}"
    return re.sub(r"[^A-Za-z0-9:_.-]", "-", base) + ".service"


def watchdog_unit(config, launch_id):
    """Base name of the watchdog timer/service pair, next to the supervisor unit's name."""
    return unit_name(config, launch_id).removesuffix(".service") + "-watchdog"


def stop_earlier_timers(root, backend, current):
    """Stop watchdog timers of earlier launches that were not stopped yet (recorded)."""
    from linear_runner.supervision import watchdog
    stopped = []
    for path in sorted((Path(root) / "launches").glob("*.json")):
        record = read_json(path)
        timer = (record.get("watchdog_timer") or {}).get("timer")
        if timer and record["launch_id"] != current and not watchdog.timer_stopped(root, timer):
            watchdog.record_timer_stop(root, timer, f"superseded by launch {current}", backend.stop_watchdog(timer))
            stopped.append(timer)
    return stopped


def launch(config, linear, *, backend, stop_after=(), scope="queue", clear_stop=False, force_preflight=False,
           runner=None, out=print):
    root = Path(config["state_dir"])
    root.mkdir(parents=True, exist_ok=True)
    launch_id = "L-" + run_id()
    unknown = [i for i in stop_after if i not in config["issues"]]
    if unknown:
        raise LaunchError(f"--stop-after {unknown} not in the issue allowlist")
    status_path = root / STATUS_NAME
    with project_lock(root / "controller.lock"):
        previous = read_json(status_path) if status_path.exists() else {}
        if previous.get("status") == "running" and pid_alive(previous.get("pid")) and previous.get("host") == os.uname().nodename:
            raise LaunchError(f"Supervisor {previous['launch_id']} (PID {previous['pid']}) is still running")
        runner = runner or Runner(config, linear)
        runner.verify_config()
        if pid_alive(runner.state.get("child_pid")):
            raise LaunchError(f"Previous worker PID {runner.state['child_pid']} may still be alive; inspect first")
        stop = root / "STOP"
        cleared = None
        if stop.exists():
            # A pending recovery was recorded against this exact marker: carrying out that
            # recovery clears it. Any other marker (a bare continuation, or a STOP written
            # after the recovery was recorded) needs an explicit --clear-stop.
            pending = runner.state.get("pending_recovery") or {}
            digest = hashlib.sha256(stop.read_bytes()).hexdigest()
            if pending.get("stop_marker_sha256") == digest:
                cleared = {"text": stop.read_text(), "sha256": digest, "by": f"pending recovery {pending['id']}"}
            elif clear_stop:
                cleared = {"text": stop.read_text(), "sha256": digest, "by": "--clear-stop"}
            elif pending:
                raise LaunchError(f"STOP marker changed after recovery {pending['id']} was recorded "
                                  f"({stop.read_text().strip()!r}); inspect `status`, then relaunch with --clear-stop")
            else:
                raise LaunchError(f"STOP marker present ({stop.read_text().strip()!r}); inspect `status`, then "
                                  "relaunch with --clear-stop")
        record = preflight(config, runner, launch_id=launch_id, force=force_preflight)
        if cleared:
            stop.unlink()
    launcher = config["launcher"]
    python = launcher["python"] or sys.executable
    batch = next(v for k, v in config["_layers"].items() if k.startswith("batch "))
    command = [python, str(RUNNER_ROOT / "runner.py"), "supervise", "--batch", batch, "--home",
               config["variables"]["home"], "--launch-id", launch_id, "--scope", scope]
    for issue in stop_after:
        command += ["--stop-after", issue]
    if launcher["cpu_list"]:
        command = [shutil.which("taskset") or "taskset", "-c", launcher["cpu_list"], *command]
    watch_unit = watchdog_unit(config, launch_id)
    watch_command = [python, str(RUNNER_ROOT / "runner.py"), "watchdog", "--batch", batch_argument(config), "--home",
                     config["variables"]["home"], "--launch-id", launch_id, "--timer", watch_unit + ".timer"]
    spec = {"launch_id": launch_id, "unit": unit_name(config, launch_id), "command": command,
            "watchdog": {"unit": watch_unit, "timer": watch_unit + ".timer", "command": watch_command,
                         "interval_minutes": config["attention"]["watchdog"]["interval_minutes"],
                         "log": str(root / "watchdog.log")},
            "workdir": str(RUNNER_ROOT), "log": str(root / "supervisor.log"), "state_dir": str(root),
            "environment": dict(launcher["environment"]), "inherit": [config["linear"]["token_env"]]
            if config["linear"].get("token_env") else [], "stop_marker": str(root / "STOP")
            if launcher["stop_on_exit"] else None, "startup_timeout_seconds": launcher["startup_timeout_seconds"],
            "stop_after": list(stop_after), "scope": scope}
    # The launcher block is outside the configuration fingerprint; record what was used.
    entry = {"launch_id": launch_id, "at": now(), "backend": backend.name, "spec": spec,
             "launcher": dict(launcher, python=python),
             "preflight": str(root / "preflight" / f"{launch_id}.json"), "cleared_stop": cleared,
             "state_before": expected_state(runner.state), "config_sha256": record["identities"]["config"]["fingerprint"],
             "source_head": record["identities"]["source"]["head"]}
    path = root / "launches" / f"{launch_id}.json"
    write_json(path, entry)
    try:
        entry["stopped_earlier_timers"] = stop_earlier_timers(root, backend, launch_id)
        # The timer starts first: a launch that cannot be watched does not start.
        entry["watchdog_timer"] = backend.start_watchdog(spec)
        write_json(path, entry)
        entry["started"] = backend.start(spec)
        entry["confirmation"] = backend.confirm(spec, entry["started"])
        if entry["confirmation"]["supervisor"].get("status") == "refused":
            raise LaunchError(f"Supervisor refused to start: {entry['confirmation']['supervisor'].get('error')}")
    except Exception as error:
        entry["error"] = str(error)
        if entry.get("watchdog_timer"):
            from linear_runner.supervision import watchdog
            watchdog.record_timer_stop(root, entry["watchdog_timer"]["timer"], f"launch failed: {error}",
                                       backend.stop_watchdog(entry["watchdog_timer"]["timer"]))
        write_json(path, entry)
        if not stop.exists():
            stop.write_text(f"Launch {launch_id} failed: {error}\n")
        raise
    write_json(path, entry)
    supervisor = entry["confirmation"]["supervisor"]
    out(json.dumps({"launch_id": launch_id, "backend": backend.name, "unit": spec["unit"]
                    if backend.name == "systemd-user" else None, "pid": supervisor.get("pid"),
                    "supervisor_status": supervisor.get("status"), "outcome": supervisor.get("outcome"),
                    "state_dir": str(root), "log": spec["log"], "launch_record": str(path),
                    "terminal_report": str(root / "terminal-report.html"),
                    "watchdog_timer": (entry.get("watchdog_timer") or {}).get("timer"),
                    "preflight": {n: ("reused" if s.get("reused") else "ran") + f" ({s['reason']})"
                                  for n, s in record["steps"].items()}}, indent=2))
    return entry
