"""The Claude Code CLI backend: ``claude -p`` argv, its stream-json events and its known models.

Authentication (``site.claude.auth``, see ``auth_mode``) is one of:

* ``subscription-login`` (the default): the host's ``claude.ai`` OAuth login in ``~/.claude``.
  Every Claude Code process on the host shares it, and when its access token expires,
  concurrent refreshes race: the loser fails with "Failed to refresh OAuth token";
* ``oauth-token-file``: a long-lived token from ``claude setup-token`` in a file (mode 600 or
  stricter), read when each session starts;
* ``oauth-token-env``: the same token in a named variable of the supervisor's environment.

In both token modes the token reaches only the ``claude`` child process, as
``CLAUDE_CODE_OAUTH_TOKEN``; it needs no refresh, so it cannot race. It is never written to
argv, records, logs or the configuration fingerprint (which covers the mode and the file path
or variable name only).

``--bare`` is never used because bare mode does not read the subscription login. Isolation
instead comes from these flags on every call:

* ``--setting-sources ''`` (no user, project or local settings files, so no hooks, plugins or
  permission rules from the host) plus a per-session ``--settings`` file the runner writes
  (hooks off, auto-memory off, bypass mode disabled, no permission rules);
* ``--strict-mcp-config`` with no ``--mcp-config``: no MCP servers, so no Linear MCP (the
  controller owns every Linear read and write);
* ``--tools`` narrows the built-in tools, ``--permission-mode`` and the allow/deny rules decide
  what runs, ``--permission-prompts none`` denies anything that would ask a person;
* ``--append-system-prompt`` adds a fixed runner instruction to Claude Code's system prompt;
* inherited ``CLAUDE*``/``ANTHROPIC*`` variables (and a token-env variable) are removed from
  the child environment, so a parent Claude Code session or an API key cannot redirect the
  call; only a configured token is added back, as ``CLAUDE_CODE_OAUTH_TOKEN``.

Workers run in ``acceptEdits`` (edits inside the worktree and the issue's run directory,
added with ``--add-dir``; Bash allowed except Git history/branch commands and nested agents).
The reviewer runs in ``dontAsk`` with Read/Grep/Glob and a narrow read-only Git allowlist.
That read-only guarantee is Claude Code's permission rules, NOT an OS sandbox like Codex's
``--sandbox read-only``; the runner's frozen-source check after review still catches any
change to the worktree.

Fresh sessions get a pre-assigned ``--session-id``; continuing uses ``--resume``. The result
is schema-bound with ``--json-schema`` and read from ``structured_output`` of the ``result``
event. Model fallback (``--fallback-model``) is never passed: a model is never substituted.
Claude Code has no per-call auto-compaction threshold matching Codex's, so a configured
``compact_token_limit`` is refused, never ignored.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

# Models verified with real probes (W-190) on Claude Code 2.1.281; each accepts every effort
# of the CLI's --effort. A model not listed here fails preflight until it is probed and added.
KNOWN_MODELS = {
    "claude-opus-5-5": ("low", "medium", "high", "xhigh", "max"),
    "claude-sonnet-5": ("low", "medium", "high", "xhigh", "max"),
}
# claude-opus-5-5 needs 2.1.280 or newer (an older CLI returns an API 400 error result).
MIN_VERSION = (2, 1, 280)
# Only these two fields of `claude auth status` are ever recorded (never the account email).
AUTH_FIELDS = ("loggedIn", "authMethod")
SUBSCRIPTION_AUTH = "claude.ai"
SUBSCRIPTION_MODE = "subscription-login"
TOKEN_MODES = ("oauth-token-file", "oauth-token-env")
# `claude auth status` reports this authMethod when CLAUDE_CODE_OAUTH_TOKEN is set (2.1.281). The
# command is local: it shows the CLI sees the token, not that the service accepts it.
TOKEN_AUTH = "oauth_token"
TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
# Stops that name this (with the auth mode in parentheses) get an authentication next action.
AUTH_FAILURE = "Claude authentication"
# An error result that is an authentication failure (W-191: "Failed to refresh OAuth token").
AUTH_ERROR = re.compile(r"oauth|authenticat|\b401\b|/login|setup-token|invalid (api key|bearer|x-api-key)|"
                        r"token (has )?(expired|been revoked)", re.I)
# Variables a parent Claude Code session or a shell may set; none reaches the child.
SCRUBBED_ENVIRONMENT = re.compile(r"^(CLAUDE|ANTHROPIC)")
CHILD_ENVIRONMENT = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}

WORKER_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit")
WORKER_ALLOWED = ("Bash", "Read", "Glob", "Grep")
# Git history, branch and remote changes belong to the controller; nested agents are not allowed.
WORKER_DENIED = tuple(f"Bash({command}:*)" for command in (
    "git commit", "git push", "git pull", "git merge", "git rebase", "git reset", "git checkout", "git switch",
    "git branch", "git tag", "git stash", "git worktree", "git cherry-pick", "git revert", "git am",
    "claude", "codex"))
REVIEWER_TOOLS = ("Read", "Grep", "Glob", "Bash")
REVIEWER_ALLOWED = ("Read", "Grep", "Glob") + tuple(f"Bash({command}:*)" for command in (
    "git diff", "git log", "git show", "git status", "git rev-parse", "git ls-files", "git grep", "git blame"))
REVIEWER_DENIED = ("Edit", "Write", "NotebookEdit")

WORKER_PROMPT = ("You are a non-interactive worker session started by linear-runner. The runner owns Linear, "
                 "Git commits, branches and publication: never commit, push or change branches, and do not "
                 "start other agents. Work only in the project worktree and the artifact paths the task names. "
                 "Your final message must be the JSON result the task asks for.")
REVIEWER_PROMPT = ("You are a non-interactive, read-only reviewer session started by linear-runner. Do not change "
                   "any file, Git state or Linear; read files and run read-only Git commands only. Your final "
                   "message must be the JSON result the task asks for.")


def settings(writable):
    """The per-session ``--settings`` contents: no hooks, no memory, no bypass, no permission rules."""
    return {"disableAllHooks": True, "autoMemoryEnabled": False, "includeCoAuthoredBy": False,
            "permissions": {"defaultMode": "acceptEdits" if writable else "dontAsk",
                            "disableBypassPermissionsMode": "disable"}}


def auth_mode(config):
    """``subscription-login``, ``oauth-token-file`` or ``oauth-token-env`` (never the token)."""
    return (config.get("claude_auth") or {}).get("mode", SUBSCRIPTION_MODE)


def auth_reference(config):
    """What the configuration names: the mode plus the token file path or variable name."""
    auth = config.get("claude_auth") or {}
    return {"mode": auth_mode(config), **{k: v for k, v in auth.items() if k != "mode"}}


def check_token_file(path):
    """Raise RuntimeError unless ``path`` is a non-empty regular file that only its owner can
    read or write (mode 600 or stricter). Metadata only: the content is not read."""
    where = f"{AUTH_FAILURE} (oauth-token-file): the token file {path}"
    try:
        info = os.stat(path)
    except FileNotFoundError:
        raise RuntimeError(f"{where} does not exist; create it with `claude setup-token` (mode 600)") from None
    except OSError as error:
        raise RuntimeError(f"{where} cannot be read: {error.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"{where} is not a regular file")
    if info.st_mode & 0o177:
        raise RuntimeError(f"{where} has mode {stat.S_IMODE(info.st_mode):03o}; it must not be readable by the "
                           f"group or others (run `chmod 600 {path}`)")
    if info.st_size == 0:
        raise RuntimeError(f"{where} is empty; write the token from `claude setup-token` into it")


def read_token(config, environ=None):
    """The configured token, or None for the subscription login. Read at each call and never
    stored; errors name the file or variable, never the value."""
    auth = config.get("claude_auth") or {}
    mode = auth_mode(config)
    if mode == "oauth-token-file":
        path = auth["oauth_token_file"]
        check_token_file(path)
        try:
            value = Path(path).read_text().strip()
        except (OSError, UnicodeDecodeError) as error:
            raise RuntimeError(f"{AUTH_FAILURE} ({mode}): the token file {path} cannot be read: "
                               f"{getattr(error, 'strerror', None) or type(error).__name__}") from None
        where = f"the token file {path}"
    elif mode == "oauth-token-env":
        name = auth["oauth_token_env"]
        value = (os.environ if environ is None else environ).get(name, "").strip()
        if not value:
            raise RuntimeError(f"{AUTH_FAILURE} ({mode}): the variable {name} is not set in the runner's environment; "
                               f"export it before `launch` (it is passed to the supervisor by name)")
        where = f"the variable {name}"
    else:
        return None
    if not value:
        raise RuntimeError(f"{AUTH_FAILURE} ({mode}): {where} is empty; write the token from `claude setup-token`")
    if any(c.isspace() for c in value):
        raise RuntimeError(f"{AUTH_FAILURE} ({mode}): {where} must hold one token on one line")
    return value


def offline_auth_check(config):
    """``validate-config``: the configured reference and, for a token file, its metadata checks
    (never its content). A token variable is reported as set or not in this shell, not required:
    it must be set where ``launch`` runs, and launch preflight checks it there."""
    record = auth_reference(config)
    if record["mode"] == "oauth-token-file":
        check_token_file(record["oauth_token_file"])
        record["token_file"] = "ok: regular, mode 600 or stricter, non-empty (content not read)"
    elif record["mode"] == "oauth-token-env":
        record["set_in_this_environment"] = bool(os.environ.get(record["oauth_token_env"], "").strip())
    return record


def auth_failure(mode, text):
    """The stop wording for an authentication error result, naming the mode (see ``AUTH_FAILURE``)."""
    return f"{AUTH_FAILURE} ({mode}) failed: {text}"


def parse_version(text):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(v) for v in match.groups()) if match else None


def normalized_usage(usage):
    """The runner's counters from a result event's ``usage``: input includes cache reads and
    cache writes (as Codex input includes cached input); reasoning is the thinking tokens."""
    def count(key):
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    parts = [count(k) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
    values = {"input_tokens": sum(parts) if None not in parts else None,
              "cached_input_tokens": parts[1],
              "output_tokens": count("output_tokens"),
              "reasoning_output_tokens": thinking if isinstance(thinking, int) else None}
    return {k: v for k, v in values.items() if v is not None}


def _blocks(event, role):
    message = event.get("message") if event.get("type") == role else None
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


# --json-schema is answered through this tool; like Codex's output file it is not a tool call.
RESULT_TOOL = "StructuredOutput"


def tool_results(events):
    """``tool_result`` blocks of real tool calls (the structured-output call excluded)."""
    names = {b.get("id"): b.get("name") for e in events for b in _blocks(e, "assistant") if b.get("type") == "tool_use"}
    return [b for e in events for b in _blocks(e, "user")
            if b.get("type") == "tool_result" and names.get(b.get("tool_use_id")) != RESULT_TOOL]


def execution_evidence(events):
    """Summarize CLI evidence, never model-authored claims or inferred billing.

    Observed models come from assistant messages and the result's ``modelUsage`` (the init
    event names the requested model even when it never ran). Usage counters are per
    invocation (a resumed call reports only its own tokens); ``total_cost_usd`` is the CLI's
    own estimate and, on 2.1.281, cumulative over the session.
    """
    usage, models, results, denials = [], [], [], []
    rate_limit = None
    for index, event in enumerate(events):
        kind = event.get("type")
        message = event.get("message") if kind == "assistant" else None
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and not model.startswith("<") and not any(
                m["model"] == model and m["event_type"] == "assistant" for m in models):
            models.append({"event_index": index, "event_type": "assistant", "model": model})
        if kind == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            info = event["rate_limit_info"]
            rate_limit = {k: info.get(k) for k in ("status", "rateLimitType", "isUsingOverage")}
        if kind == "result":
            if isinstance(event.get("usage"), dict):
                usage.append({"event_index": index, "usage": normalized_usage(event["usage"])})
            for name in (event.get("modelUsage") or {}):
                models.append({"event_index": index, "event_type": "result/modelUsage", "model": name})
            results.append({"event_index": index, "subtype": event.get("subtype"), "is_error": event.get("is_error"),
                            "terminal_reason": event.get("terminal_reason"), "num_turns": event.get("num_turns"),
                            "duration_ms": event.get("duration_ms"), "api_error_status": event.get("api_error_status"),
                            "client_cost_estimate_usd": event.get("total_cost_usd"),
                            "raw_usage": event.get("usage"), "model_usage": event.get("modelUsage")})
            for denial in event.get("permission_denials") or []:
                if isinstance(denial, dict):
                    denials.append({"tool_name": denial.get("tool_name"),
                                    "tool_input": json.dumps(denial.get("tool_input"))[:300]})
    calls = tool_results(events)
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    return {"observed_models": models or None, "observed_reasoning_efforts": None, "usage_events": usage or None,
            "usage_scope": "invocation",
            "completed_tool_calls": len(calls), "failed_tool_calls": sum(bool(b.get("is_error")) for b in calls),
            "error_events": sum(bool(r["is_error"]) for r in results), "results": results or None,
            "permission_denials": denials, "rate_limit": rate_limit,
            "init": {k: init.get(k) for k in ("model", "claude_code_version", "permissionMode", "mcp_servers",
                                              "apiKeySource", "tools", "plugins", "memory_paths")} if init else None,
            "provider_retries": None, "billed_cost": None,
            "unverified": "Claude Code events do not name the effort that ran; client_cost_estimate_usd is the CLI's "
                          "own estimate (cumulative over the session), never billed cost. Raw events/stderr retained."}


class ClaudeBackend:
    name = "claude"
    label = "Claude"
    capabilities = {"resume": True, "preassigned_session_id": True, "structured_output": "structured_output of the result event",
                    "read_only_isolation": "permission-rules", "compact_token_limit": False, "usage_scope": "invocation",
                    "observed_model": True, "observed_effort": False, "client_cost_estimate": True}
    cache_seconds = 300

    def __init__(self, config):
        # The resolved configuration: ``claude`` (the executable, site.executables.claude).
        self.config = config
        self._cache = {}

    @property
    def executable(self):
        executable = self.config.get("claude")
        if not executable:
            raise RuntimeError("The Claude backend needs site.executables.claude (credential/environment setup)")
        return executable

    def _probe(self, key, argv):
        """``(returncode, stdout)`` of a short local CLI command, cached for ``cache_seconds``."""
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]
        try:
            done = subprocess.run([self.executable, *argv], capture_output=True, text=True, timeout=60,
                                  stdin=subprocess.DEVNULL, env=self.environment(dict(os.environ)))
            value = (done.returncode, done.stdout)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"Claude CLI unavailable: {error}") from None
        self._cache[key] = (time.monotonic(), value)
        return value

    def version(self):
        return parse_version(self._probe("version", ["--version"])[1])

    def version_text(self):
        """``claude --version`` (for example ``2.1.281 (Claude Code)``), or None."""
        code, text = self._probe("version", ["--version"])
        lines = (text or "").strip().splitlines()
        return lines[0].strip() if code == 0 and lines else None

    def auth_identity(self):
        """The auth mode and the token file path or variable name (see ``auth_reference``); for a
        token file also its modification time, so a replaced token counts as a change. Never the
        token or anything derived from it."""
        record = auth_reference(self.config)
        if record["mode"] == "oauth-token-file":
            try:
                record["token_file_mtime_ns"] = os.stat(record["oauth_token_file"]).st_mtime_ns
            except OSError:
                record["token_file_mtime_ns"] = None
        return record

    @property
    def auth_mode(self):
        return auth_mode(self.config)

    def auth_status(self):
        """Only ``loggedIn`` and ``authMethod`` of ``claude auth status``; never the email.
        In a token mode the probe runs with the token, as a session would."""
        code, text = self._probe("auth", ["auth", "status", "--json"])
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        return {k: data.get(k) for k in AUTH_FIELDS} if isinstance(data, dict) else dict.fromkeys(AUTH_FIELDS)

    # --- Known models, CLI version and login ----------------------------------------

    def check_selection(self, selection):
        if selection["effort"] not in KNOWN_MODELS.get(selection["model"], ()):
            raise RuntimeError(f"Requested model/effort {selection['model']}/{selection['effort']} unavailable in host "
                               "CLI catalog (the Claude known-model list); no substitution")
        version = self.version()
        if version is None or version < MIN_VERSION:
            raise RuntimeError(f"Claude CLI version {version and '.'.join(map(str, version))} is unsupported; "
                               f"{'.'.join(map(str, MIN_VERSION))} or newer is required")
        self.check_auth()

    def check_auth(self):
        """Subscription login: ``claude auth status`` must show the claude.ai login. Token modes:
        the token must be readable (file: mode 600 or stricter, non-empty; variable: set) and
        ``claude auth status`` must show the CLI uses it. That command makes no model or service
        call, so a revoked or expired token is only found by the first session (an
        ``environment`` stop that says to regenerate it)."""
        mode = self.auth_mode
        if mode in TOKEN_MODES:
            read_token(self.config)
            auth = self.auth_status()
            if auth["loggedIn"] is not True or auth["authMethod"] != TOKEN_AUTH:
                raise RuntimeError(f"{AUTH_FAILURE} ({mode}): the Claude CLI does not use the configured token "
                                   f"(loggedIn={auth['loggedIn']}, authMethod={auth['authMethod']})")
            return
        auth = self.auth_status()
        if auth["loggedIn"] is not True or auth["authMethod"] != SUBSCRIPTION_AUTH:
            raise RuntimeError(f"Claude CLI credential is not the subscription login (loggedIn={auth['loggedIn']}, "
                               f"authMethod={auth['authMethod']}); run `claude auth login` on the host")

    def catalog_report(self, entries):
        version = self.version()
        return {"models": len(KNOWN_MODELS), "cli_version": version and ".".join(map(str, version)),
                "auth_mode": self.auth_mode, "auth": self.auth_status(),
                "registry_pool_entries_available": {f"{e['model']}/{e['effort']}": e["effort"] in KNOWN_MODELS.get(e["model"], ())
                                                    for e in entries}}

    # --- One ``claude -p`` call --------------------------------------------------------

    @staticmethod
    def settings_path(request):
        return Path(request.directory) / "claude-settings.json"

    @staticmethod
    def permissions(request):
        """(permission mode, tools, allowed rules, denied rules, added directories) for ``request``."""
        if request.writable:
            # The issue run directory holds the attempt directory, its outbox and handoff.
            return ("acceptEdits", WORKER_TOOLS, WORKER_ALLOWED, WORKER_DENIED, [str(Path(request.directory).parent)])
        return "dontAsk", REVIEWER_TOOLS, REVIEWER_ALLOWED, REVIEWER_DENIED, []

    def command(self, request):
        if request.compact_limit is not None:
            raise RuntimeError("The Claude backend has no per-call compact_token_limit; unset it for Claude-backed phases")
        mode, tools, allowed, denied, directories = self.permissions(request)
        path = self.settings_path(request)
        path.write_text(json.dumps(settings(request.writable), indent=2) + "\n")
        command = [self.executable, "-p", "--output-format", "stream-json", "--verbose",
                   "--model", request.model, "--effort", request.effort,
                   "--setting-sources", "", "--settings", str(path), "--strict-mcp-config",
                   "--permission-mode", mode, "--permission-prompts", "none",
                   "--tools", ",".join(tools), "--allowedTools", *allowed, "--disallowedTools", *denied]
        for directory in directories:
            command += ["--add-dir", directory]
        command += ["--append-system-prompt", WORKER_PROMPT if request.writable else REVIEWER_PROMPT,
                    "--json-schema", json.dumps(json.loads(Path(request.schema_path).read_text()), separators=(",", ":"))]
        command += ["--resume", request.resume] if request.resume else ["--session-id", str(uuid.uuid4())]
        return command

    def environment(self, base):
        """``base`` without ``CLAUDE*``/``ANTHROPIC*`` (and a token-env variable), plus the fixed
        child settings; in a token mode also ``CLAUDE_CODE_OAUTH_TOKEN``, read now. Only the
        ``claude`` child gets this environment; it is never recorded."""
        hidden = (self.config.get("claude_auth") or {}).get("oauth_token_env")
        env = {k: v for k, v in base.items() if not SCRUBBED_ENVIRONMENT.match(k) and k != hidden}
        env.update(CHILD_ENVIRONMENT)
        token = read_token(self.config)
        if token is not None:
            env[TOKEN_VARIABLE] = token
        return env

    def describe(self, request):
        mode, tools, allowed, denied, directories = self.permissions(request)
        return {"auth_mode": self.auth_mode, "auth": self.auth_status(), "settings": settings(request.writable),
                "settings_path": str(self.settings_path(request)), "setting_sources": [],
                "permission_mode": mode, "tools": list(tools), "allowed_tools": list(allowed),
                "disallowed_tools": list(denied), "add_dirs": directories, "mcp_servers": [],
                "system_prompt_append": WORKER_PROMPT if request.writable else REVIEWER_PROMPT,
                "environment": {"removed": "CLAUDE*/ANTHROPIC* variables", "set": CHILD_ENVIRONMENT,
                                **({"token": f"{TOKEN_VARIABLE} ({self.auth_mode}, value not recorded)"}
                                   if self.auth_mode in TOKEN_MODES else {})},
                "isolation": "Claude Code permission rules (not an OS sandbox)"}

    # --- Reading the stream-json events and the result -----------------------------------

    def session_started(self, event):
        return event.get("type") == "system" and event.get("subtype") == "init" and bool(event.get("session_id"))

    def session_id(self, event):
        return event["session_id"]

    @staticmethod
    def last_result(events):
        return next((e for e in reversed(events) if e.get("type") == "result"), None)

    def finished(self, events):
        result = self.last_result(events)
        return bool(result) and result.get("subtype") == "success" and result.get("is_error") is False

    def failure(self, events):
        result = self.last_result(events)
        if result is None or not (result.get("is_error") or result.get("subtype") != "success"):
            return None
        text = " ".join(str(result.get("result") or "; ".join(map(str, result.get("errors") or [])) or "").split())
        reason = result.get("terminal_reason") or result.get("subtype")
        status = result.get("api_error_status")
        message = (f"error result ({reason}" + (f", API status {status}" if status else "") + ")"
                   + (f": {text[:300]}" if text else ""))
        if status == 401 or AUTH_ERROR.search(text):
            return auth_failure(self.auth_mode, message)
        return message

    def result(self, request, events):
        result = self.last_result(events)
        if not self.finished(events):
            raise RuntimeError(f"Claude failed: {self.failure(events) or 'no result event'}")
        value = result.get("structured_output")
        if not isinstance(value, dict):
            raise RuntimeError("Missing structured result: the Claude result event has no structured_output")
        return value

    def evidence(self, events):
        return execution_evidence(events)

    def tool_calls(self, events):
        return len(tool_results(events))

    def tool_output_bytes(self, events):
        return sum(len(json.dumps(b.get("content", "")).encode()) for b in tool_results(events))
