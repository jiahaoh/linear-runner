"""Model backends: the agent CLI that runs a model session for the engine.

(Not to be confused with the launcher's host backends, ``systemd-user`` and ``foreground``.)

The engine (``Runner.run_session``) owns everything that does not depend on the CLI: the
attempt directory, ``prompt.txt`` and the result schema file, the child process (its
process group, timeout, environment and PID in state), the outbox poll while it runs,
``events.jsonl`` (one JSON event per stdout line), ``session.json`` and the soft budgets.
A backend only knows its own CLI. It

* declares what it can do (``capabilities``; see ``CAPABILITY_KEYS``);
* identifies the installed CLI for the launch start check (``executable``, ``version_text``,
  ``auth_identity``; see ``linear_runner.supervision.backend_start``);
* checks that a selected model/effort is available (``check_selection``) and summarizes its
  catalog for launch preflight (``catalog_report``);
* builds the argv that starts or resumes a session from a ``SessionRequest``: a prompt
  (sent on stdin), a model/effort selection, writable or read-only, a result schema and an
  optional auto-compaction limit (``command``), the child environment (``environment``) and
  the session details recorded next to the command (``describe``);
* reads what the session produced: which event starts a session and its ID
  (``session_started``/``session_id``), whether a turn finished (``finished``), a CLI-reported
  error (``failure``), the structured result (``result``), and the evidence the engine
  records: usage counters, observed model/effort and tool calls (``evidence``,
  ``tool_calls``, ``tool_output_bytes``).

Each registry pool entry names its backend (``codex.py`` or ``claude.py``);
``create(config, name)`` returns it for a resolved configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from linear_runner.backends.claude import ClaudeBackend
from linear_runner.backends.codex import CodexBackend

# What ``capabilities`` declares, per backend:
#   resume                  continue a session by ID
#   preassigned_session_id  the runner chooses a fresh session's ID
#   structured_output       how the schema-bound result comes back
#   read_only_isolation     "os-sandbox" (enforced by the OS) or "permission-rules" (by the CLI)
#   compact_token_limit     accepts a per-call auto-compaction threshold
#   usage_scope             "session" (counters cumulative over resumes) or "invocation"
#   observed_model          the CLI's events name the model that ran
#   observed_effort         the CLI's events name the effort that ran
#   client_cost_estimate    the CLI reports its own cost estimate (never billed cost)
CAPABILITY_KEYS = ("resume", "preassigned_session_id", "structured_output", "read_only_isolation",
                   "compact_token_limit", "usage_scope", "observed_model", "observed_effort",
                   "client_cost_estimate")


@dataclass(frozen=True)
class SessionRequest:
    """One model session as the engine asks for it."""
    prompt: str               # the full prompt, sent on stdin
    directory: Path           # the attempt directory (exists; the backend may write its files here)
    cwd: Path                 # the project worktree the session works in
    model: str                # the selection resolved from the registry pools
    effort: str
    writable: bool            # False: a read-only session (the independent review)
    resume: str | None        # continue this session ID instead of starting a fresh one
    schema_path: Path         # JSON schema file (written by the engine) the final result must match
    compact_limit: int | None  # auto-compaction threshold in tokens; None keeps the CLI default


class Backend(Protocol):
    name: str   # registry key
    label: str  # how errors and logs name the CLI, e.g. "Codex"
    capabilities: dict  # see CAPABILITY_KEYS

    executable: str  # the configured CLI executable (a path or a name looked up on PATH)

    def version_text(self) -> str | None:
        """The CLI's own version line (a local command, no model call)."""

    def auth_identity(self) -> dict:
        """How the CLI authenticates (a mode, a file path or variable name); never a secret."""

    def check_selection(self, selection: dict) -> None:
        """Raise RuntimeError unless ``selection['model']``/``['effort']`` is available; never substitute."""

    def catalog_report(self, entries: list[dict]) -> dict:
        """Preflight summary: the catalog size and which registry pool entries it can serve."""

    def command(self, request: SessionRequest) -> list[str]:
        """The argv that runs ``request`` (no shell)."""

    def environment(self, base: dict) -> dict:
        """The child environment derived from ``base`` (the runner's environment plus check overrides)."""

    def describe(self, request: SessionRequest) -> dict:
        """Session details recorded in session.json next to the command (never secrets)."""

    def session_started(self, event: dict) -> bool:
        """Whether ``event`` announces the session (its ID is then ``session_id(event)``)."""

    def session_id(self, event: dict) -> str:
        """The session ID a ``session_started`` event announces."""

    def finished(self, events: list[dict]) -> bool:
        """Whether the session completed a turn (with a zero exit, the session succeeded)."""

    def failure(self, events: list[dict]) -> str | None:
        """A CLI-reported error to name in the stop message, or None."""

    def result(self, request: SessionRequest, events: list[dict]) -> dict:
        """The structured result; RuntimeError when it is missing."""

    def evidence(self, events: list[dict]) -> dict:
        """CLI evidence for session.json: usage events, observed model/effort, tool calls and errors."""

    def tool_calls(self, events: list[dict]) -> int:
        """Completed tool calls (the soft budget's ``tool_calls``)."""

    def tool_output_bytes(self, events: list[dict]) -> int:
        """Bytes of tool output returned to the model (context-cost measurement)."""


BACKENDS = {CodexBackend.name: CodexBackend, ClaudeBackend.name: ClaudeBackend}
DEFAULT = CodexBackend.name


def backend_class(name):
    try:
        return BACKENDS[name]
    except KeyError:
        raise ValueError(f"Unknown model backend {name!r}; known: {', '.join(sorted(BACKENDS))}") from None


def create(config, name=DEFAULT) -> Backend:
    """The backend ``name`` for a resolved configuration."""
    return backend_class(name)(config)


def failure_patterns():
    """Stop-message fragments naming a backend (``<label> failed``, ``<label> exceeded``)."""
    return tuple(f"{cls.label} {verb}" for cls in BACKENDS.values() for verb in ("failed", "exceeded"))
