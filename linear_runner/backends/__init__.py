"""Model backends: the agent CLI that runs a model session for the engine.

(Not to be confused with the launcher's host backends, ``systemd-user`` and ``foreground``.)

The engine (``Runner.run_session``) owns everything that does not depend on the CLI: the
attempt directory, ``prompt.txt`` and the result schema file, the child process (its
process group, timeout, environment and PID in state), the outbox poll while it runs,
``events.jsonl`` (one JSON event per stdout line), ``session.json`` and the soft budgets.
A backend only knows its own CLI. It

* checks that a selected model/effort is available (``check_selection``) and summarizes the
  catalog for launch preflight (``catalog_report``);
* builds the argv that starts or resumes a session from a ``SessionRequest``: a prompt
  (sent on stdin), a model/effort selection, writable or read-only, a result schema and an
  optional auto-compaction limit (``command``);
* reads what the session produced: which event starts a session and its ID
  (``session_started``/``session_id``), whether a turn finished (``finished``), the
  structured result (``result``), and the evidence the engine records: usage counters,
  observed model/effort and tool calls (``evidence``, ``tool_calls``, ``tool_output_bytes``).

``create(config)`` returns the backend a resolved configuration runs with; today that is
always Codex (``codex.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from linear_runner.backends.codex import CodexBackend


@dataclass(frozen=True)
class SessionRequest:
    """One model session as the engine asks for it."""
    prompt: str               # the full prompt, sent on stdin
    directory: Path           # the attempt directory (exists; the backend may write its result here)
    cwd: Path                 # the project worktree the session works in
    model: str                # the selection resolved from the registry profiles
    effort: str
    writable: bool            # False: a read-only session (the independent review)
    resume: str | None        # continue this session ID instead of starting a fresh one
    schema_path: Path         # JSON schema file (written by the engine) the final result must match
    compact_limit: int | None  # auto-compaction threshold in tokens; None keeps the CLI default


class Backend(Protocol):
    name: str   # registry key
    label: str  # how errors and logs name the CLI, e.g. "Codex"

    def check_selection(self, selection: dict) -> None:
        """Raise RuntimeError unless ``selection['model']``/``['effort']`` is available; never substitute."""

    def catalog_report(self, profiles: dict) -> dict:
        """Preflight summary: the catalog size and which registry profiles it can serve."""

    def command(self, request: SessionRequest) -> list[str]:
        """The argv that runs ``request`` (no shell)."""

    def session_started(self, event: dict) -> bool:
        """Whether ``event`` announces the session (its ID is then ``session_id(event)``)."""

    def session_id(self, event: dict) -> str:
        """The session ID a ``session_started`` event announces."""

    def finished(self, events: list[dict]) -> bool:
        """Whether the session completed a turn (with a zero exit, the session succeeded)."""

    def result(self, request: SessionRequest, events: list[dict]) -> dict:
        """The structured result; RuntimeError when it is missing."""

    def evidence(self, events: list[dict]) -> dict:
        """CLI evidence for session.json: usage events, observed model/effort, tool calls and errors."""

    def tool_calls(self, events: list[dict]) -> int:
        """Completed tool calls (the soft budget's ``tool_calls``)."""

    def tool_output_bytes(self, events: list[dict]) -> int:
        """Bytes of tool output returned to the model (context-cost measurement)."""


BACKENDS = {CodexBackend.name: CodexBackend}
DEFAULT = CodexBackend.name


def create(config, name=DEFAULT) -> Backend:
    """The backend ``name`` for a resolved configuration."""
    try:
        return BACKENDS[name](config)
    except KeyError:
        raise ValueError(f"Unknown model backend {name!r}; known: {', '.join(sorted(BACKENDS))}") from None
