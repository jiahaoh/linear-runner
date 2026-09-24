"""The Codex CLI backend: ``codex exec`` argv, its JSONL events and its model catalog.

Everything Codex-specific lives here: sandbox/approval flags for writable and read-only
sessions, ``resume``, the model and ``model_reasoning_effort`` selection, the optional
``model_auto_compact_token_limit``, disabling the Linear MCP server (the controller owns
every Linear read and write), ``--output-schema`` and ``-o`` for the structured result,
the ``--json`` event stream (``thread.started``, ``turn.completed``, ``item.completed``),
the host CLI model catalog (``site.model_catalog``, normally ``models_cache.json``) and what
identifies the installed CLI for the launch start check (``--version``, ``login status``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from linear_runner.config import read_json

TOOL_ITEMS = {"mcp_tool_call", "command_execution"}


def execution_evidence(events):
    """Summarize CLI evidence, never model-authored claims or inferred billing."""
    usage, models, efforts = [], [], []
    tool_calls = tool_failures = 0
    for index, event in enumerate(events):
        kind = event.get("type")
        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage.append({"event_index": index, "usage": event["usage"]})
        # Some CLI versions expose model metadata; 0.154.0 may not.
        if kind in {"thread.started", "turn.started", "turn.completed"} and isinstance(event.get("model"), str):
            models.append({"event_index": index, "event_type": kind, "model": event["model"]})
        if kind in {"thread.started", "turn.started", "turn.completed"} and isinstance(event.get("reasoning_effort"), str):
            efforts.append({"event_index": index, "event_type": kind, "reasoning_effort": event["reasoning_effort"]})
        item = event.get("item", {})
        if kind == "item.completed" and item.get("type") in TOOL_ITEMS:
            tool_calls += 1
            result = item.get("result") or {}
            if (item.get("error") or item.get("status") in {"failed", "declined"}
                    or item.get("exit_code") not in (None, 0)
                    or isinstance(result, dict) and result.get("isError")):
                tool_failures += 1
    return {"observed_models": models or None, "observed_reasoning_efforts": efforts or None, "usage_events": usage or None,
            "completed_tool_calls": tool_calls, "failed_tool_calls": tool_failures,
            "error_events": sum(e.get("type") in {"error", "turn.failed"} for e in events),
            "provider_retries": None, "billed_cost": None,
            "unverified": "Missing model/effort/usage evidence, provider retries and billed cost are not inferred; raw events/stderr retained."}


class CodexBackend:
    name = "codex"
    label = "Codex"
    capabilities = {"resume": True, "preassigned_session_id": False, "structured_output": "output-schema file (-o)",
                    "read_only_isolation": "os-sandbox", "compact_token_limit": True, "usage_scope": "session",
                    "observed_model": "when emitted", "observed_effort": "when emitted", "client_cost_estimate": False}

    def __init__(self, config):
        # The resolved configuration: ``codex`` (the executable), ``artifact_root`` and ``model_catalog``.
        self.config = config

    @property
    def executable(self):
        return self.config["codex"]

    # --- The installed CLI (launch start check identity) ---------------------------------

    def _local(self, *argv):
        """A short local CLI command (no model call)."""
        try:
            return subprocess.run([self.executable, *argv], capture_output=True, text=True, timeout=60,
                                  stdin=subprocess.DEVNULL, env=self.environment(dict(os.environ)))
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"Codex CLI unavailable: {error}") from None

    def version_text(self):
        """``codex --version`` (for example ``codex-cli 0.156.1``), or None."""
        done = self._local("--version")
        lines = (done.stdout or done.stderr or "").strip().splitlines()
        return lines[0].strip() if done.returncode == 0 and lines else None

    def auth_identity(self):
        """How the CLI is logged in, from ``codex login status`` ("ChatGPT", "an API key"); never
        the account or a key."""
        done = self._local("login", "status")
        match = re.search(r"Logged in using ([A-Za-z][A-Za-z ]*)", f"{done.stdout}\n{done.stderr}")
        return {"mode": match.group(1).strip() if match else "not logged in"}

    # --- Model catalog ---------------------------------------------------------------

    def catalog(self):
        return read_json(Path(self.config["model_catalog"]).expanduser())

    def check_selection(self, selection):
        models = [m for m in self.catalog().get("models", []) if m.get("slug") == selection["model"]]
        if len(models) != 1 or selection["effort"] not in {
                level["effort"] for level in models[0].get("supported_reasoning_levels", [])}:
            raise RuntimeError("Requested model/effort unavailable in host CLI catalog; no substitution")
        # Catalog support is not a guarantee of remote quota/entitlement at call time.

    def catalog_report(self, entries):
        available = {m.get("slug"): sorted(level.get("effort") for level in m.get("supported_reasoning_levels", []))
                     for m in self.catalog().get("models", []) if isinstance(m, dict)}
        return {"models": len(available),
                "registry_pool_entries_available": {f"{e['model']}/{e['effort']}": e["model"] in available
                                                    and e["effort"] in available[e["model"]] for e in entries}}

    # --- One ``codex exec`` turn --------------------------------------------------------

    @staticmethod
    def result_path(request):
        return Path(request.directory) / "result.json"

    def command(self, request):
        """``codex exec`` argv; ``compact_limit`` becomes ``-c model_auto_compact_token_limit=<N>``
        on fresh and resumed calls, and ``None`` leaves the Codex default."""
        command = [self.config["codex"], "exec"]
        # Parent options precede the subcommand so resumed turns keep the same approvals.
        if request.writable or request.resume:
            command += ["--approve-for-me", "--add-dir", self.config["artifact_root"]]
        else:
            command += ["--sandbox", "read-only", "-c", 'approval_policy="on-request"',
                        "-c", 'approvals_reviewer="auto_review"']
        if request.resume:
            command += ["resume", request.resume]
        else:
            command += ["-C", str(request.cwd)]
        # Resume has its own --model option; pass selections after the subcommand.
        command += ["--model", request.model, "-c", "model_reasoning_effort=" + json.dumps(request.effort)]
        if request.compact_limit is not None:
            command += ["-c", f"model_auto_compact_token_limit={int(request.compact_limit)}"]
        # The controller owns every Linear read and write.
        command += ["-c", "mcp_servers.linear.enabled=false", "--json", "-o", str(self.result_path(request))]
        command += ["--output-schema", str(request.schema_path), "-"]
        return command

    def environment(self, base):
        return dict(base)

    def describe(self, request):
        return {"isolation": "workspace write with --approve-for-me" if request.writable or request.resume
                else "OS sandbox: --sandbox read-only"}

    # --- Reading the JSONL events and the result ---------------------------------------

    def session_started(self, event):
        return event.get("type") == "thread.started"

    def session_id(self, event):
        return event["thread_id"]

    def finished(self, events):
        return any(e.get("type") == "turn.completed" for e in events)

    def failure(self, events):
        errors = [e for e in events if e.get("type") in {"error", "turn.failed"}]
        if not errors:
            return None
        error = errors[-1].get("error") or errors[-1].get("message")
        text = error.get("message") if isinstance(error, dict) else error
        return " ".join(str(text or errors[-1].get("type")).split())[:300]

    def result(self, request, events):
        path = self.result_path(request)
        if not path.exists():
            raise RuntimeError(f"Missing structured result: {path}")
        return read_json(path)

    def evidence(self, events):
        return execution_evidence(events)

    def tool_calls(self, events):
        return sum(e.get("type") == "item.completed" and e.get("item", {}).get("type") in TOOL_ITEMS for e in events)

    def tool_output_bytes(self, events):
        return sum(len(json.dumps(e.get("item", {}).get("result", e.get("item", {}).get("aggregated_output", ""))).encode())
                   for e in events if e.get("type") == "item.completed")
