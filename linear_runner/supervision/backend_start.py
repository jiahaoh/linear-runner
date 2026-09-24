"""Launch preflight ``backend_start.<backend>``: start each model backend once in the batch worktree.

A backend that cannot start there (a CLI upgrade that rejects the project layout, a credential
the service refuses, a missing executable) otherwise surfaces only after an issue is claimed,
mid-turn. This check runs, once per backend the batch can select, the command a worker session
would run: the backend's own ``command`` and ``environment`` builders, the batch worktree as
the working directory, the writable (implement) permissions, and the launcher environment the
supervisor unit gets. Only the request is small: a fixed prompt, a one-field result schema, the
first selectable model of that backend at the lowest effort the backend accepts for it, and a
strict timeout (``site.launcher.backend_start_timeout_seconds``).

It is a real model call (a few tokens): no model-free CLI command (``codex debug prompt-input``,
``codex doctor``, ``claude auth status``) goes through the start path a session uses (W-195).
The result is reused while the backend's identity is unchanged: the executable (configured
path, where it resolves, its SHA-256), the CLI's version line, its auth mode, the worktree, the
probe selection, the environment overrides and the start-check code. ``--rerun-preflight`` runs
it again. Its files (prompt, schema, events, stderr) are kept next to the preflight record.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from linear_runner import backends
from linear_runner.config import PHASES, write_json
from linear_runner.engine.runner import inherited_environment, resolve_profile

PROMPT = ("linear-runner launch preflight: start check. Do not read files, run commands or use any tool. "
          "Reply only with the JSON object {\"ok\": true}.")
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
          "additionalProperties": False}
DEFAULT_TIMEOUT_SECONDS = 180
DETAIL_CHARS = 500


class StartError(RuntimeError):
    pass


class _Timeout(Exception):
    """The start check outlived its timeout; ``args`` are the output read so far."""


def _collapse(text, limit=DETAIL_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else "…" + text[-limit:]


def selections(config, issues):
    """Every selection the given (pending) issues can run with, in issue and phase order: each
    phase as routed now, repair and review after the single escalation, and the lighter review
    when the batch enables the low-risk review rule. Unresolvable ones are left to the
    ``linear`` step and to dispatch."""
    profiles = config["policy"]["profiles"]
    escalation = profiles["escalation_profile"]
    light = ((profiles.get("review_routing") or {}).get("light_review") or {}).get("profile")
    if not (config.get("context_controls") or {}).get("low_risk_review"):
        light = None
    variants = {"implement": [(None, None)], "repair": [(None, None), (escalation, None)],
                "review": [(None, None), (escalation, None)] + ([(None, light)] if light else [])}
    found = []
    for issue in issues:
        for phase in PHASES:
            for escalated, lighter in variants[phase]:
                try:
                    selection = resolve_profile(config, issue, phase, escalated, lighter)
                except RuntimeError:
                    continue
                found.append(dict(selection, issue=issue["id"]))
    return found


def lowest_effort(config, backend, model):
    """The first effort of the registry's effort order that ``backend`` accepts for ``model``."""
    for effort in config["policy"]["models"]["efforts"]:
        try:
            backend.check_selection({"model": model, "effort": effort})
            return effort
        except RuntimeError:
            continue
    return None


def plan(config, issues):
    """``{backend name: {"model", "effort", "from"}}``: one start check per backend the issues can
    select, with the first such selection's model at the lowest effort the backend accepts."""
    first = {}
    for selection in selections(config, issues):
        first.setdefault(selection["backend"], selection)
    checks = {}
    for name, selection in sorted(first.items()):
        backend = backends.create(config, name)
        effort = lowest_effort(config, backend, selection["model"]) or selection["effort"]
        checks[name] = {"model": selection["model"], "effort": effort,
                        "from": f"{selection['issue']} {selection['phase']} ({selection['pool']})"}
    return checks


def _executable(path):
    found = path if os.path.isabs(path) else shutil.which(path)
    resolved = os.path.realpath(found) if found else None
    sha = hashlib.sha256(Path(resolved).read_bytes()).hexdigest() if resolved and os.path.isfile(resolved) else None
    return {"path": path, "resolved": resolved, "sha256": sha}


def _source_sha(*objects):
    digest = hashlib.sha256()
    for item in objects:
        digest.update(Path(inspect.getsourcefile(item)).read_bytes())
    return digest.hexdigest()


def environment_overrides(config):
    """What the start check adds to the launching environment: the launcher environment the
    supervisor unit gets, then the project's check environment (as sessions get it)."""
    return dict(config["launcher"]["environment"], **config["check_environment"])


def _reported(call):
    """``call()``, or the error it raised: the start check itself then fails with the details."""
    try:
        return call()
    except (RuntimeError, OSError) as error:
        return {"error": str(error)}


def identity(config, name, check):
    """What the start check result depends on (never a secret)."""
    backend = backends.create(config, name)
    return {"backend": name, "executable": _reported(lambda: _executable(backend.executable)),
            "version": _reported(backend.version_text), "auth": _reported(backend.auth_identity),
            "worktree": config["worktree"],
            "selection": {"model": check["model"], "effort": check["effort"]},
            "environment_overrides": environment_overrides(config),
            "code_sha256": _source_sha(type(backend), identity)}


def _execute(command, cwd, env, prompt, timeout):
    """``(returncode, stdout, stderr)``; the process group is killed on timeout."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # a descendant outside the group still holds a pipe
            stdout, stderr = "", ""
        raise _Timeout(stdout, stderr) from None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return process.returncode, stdout, stderr


def run(config, name, check, directory, *, timeout=None):
    """Start backend ``name`` once in the worktree with the tiny request; return the record.
    StartError names the backend and quotes its own error text (trimmed)."""
    backend = backends.create(config, name)
    timeout = timeout or config["launcher"].get("backend_start_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "prompt.txt").write_text(PROMPT)
    schema_path = directory / "schema.json"
    write_json(schema_path, SCHEMA)
    cwd = Path(config["worktree"])
    request = backends.SessionRequest(prompt=PROMPT, directory=directory, cwd=cwd, model=check["model"],
                                      effort=check["effort"], writable=True, resume=None, schema_path=schema_path,
                                      compact_limit=None)
    where = f"{backend.label} ({name} backend) could not start in {cwd}"
    try:
        command = backend.command(request)
        # Built (and a configured credential read) now; passed only to this child, never recorded.
        env = backend.environment(dict(inherited_environment(config), **environment_overrides(config)))
    except (RuntimeError, OSError) as error:
        raise StartError(f"{where}: {_collapse(error)}") from None
    record = {"backend": name, "selection": check, "command": command, "cwd": str(cwd),
              "environment_overrides": sorted(environment_overrides(config)), "directory": str(directory)}
    started = time.monotonic()
    try:
        returncode, stdout, stderr = _execute(command, cwd, env, PROMPT, timeout)
    except _Timeout as error:
        stdout, stderr = error.args
        (directory / "events.jsonl").write_text(stdout or "")
        (directory / "stderr.log").write_text(stderr or "")
        raise StartError(f"{backend.label} ({name} backend) did not finish the start check within {timeout} s "
                         f"in {cwd}; see {directory}") from None
    except OSError as error:
        raise StartError(f"{where}: {_collapse(error)}") from None
    record.update(exit_code=returncode, wall_seconds=round(time.monotonic() - started, 3))
    (directory / "events.jsonl").write_text(stdout)
    (directory / "stderr.log").write_text(stderr)
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    session = next((backend.session_id(e) for e in events if backend.session_started(e)), None)
    evidence = backend.evidence(events)
    record.update(session_id=session, usage_events=evidence.get("usage_events"),
                  observed_models=evidence.get("observed_models"))
    if returncode != 0 or not backend.finished(events):
        detail = backend.failure(events) or _collapse(stderr) or f"exit code {returncode} and no output"
        raise StartError(f"{where}: {_collapse(detail)} (exit code {returncode}; see {directory})")
    try:
        record["result"] = backend.result(request, events)
    except RuntimeError as error:
        raise StartError(f"{where}: {_collapse(error)} (see {directory})") from None
    return record
