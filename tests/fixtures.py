"""Shared offline test fixtures: a temporary private home and a fake Linear boundary."""
import copy
import json
from pathlib import Path
import re
import sys

from linear_runner.linear.client import append_comment

# The runner checkout (runner.py, registry/, schema/, templates/, examples/, testdata/).
CHECKOUT = Path(__file__).resolve().parent.parent


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) if not isinstance(value, str) else value)
    return path


# Task-kind pools of the public registry (Standard implement/repair). A private override is
# deep-merged, so test registries restate them to keep the ``*`` pools in effect.
KIND_POOLS = ("Implementation", "Maintenance")


def set_pools(registry, profile, phases):
    """Set ``*`` pools of ``profile`` ({phase: entries}) and restate them for KIND_POOLS."""
    pools = registry["pools"]["pools"]
    pools["*"][profile] = {phase: list(entries) for phase, entries in phases.items()}
    if profile == "Standard":
        for kind in KIND_POOLS:
            pools.setdefault(kind, {})["Standard"] = {p: list(phases[p]) for p in ("implement", "repair")}
    return registry


def pools_from(mapping, backend="codex"):
    """A pool registry with one entry per profile and phase: {profile: (model, effort)}."""
    registry = {"pools": {"pools": {"*": {}}}}
    for profile, (model, effort) in mapping.items():
        set_pools(registry, profile, {phase: [{"backend": backend, "model": model, "effort": effort}]
                                      for phase in ("implement", "repair", "review")})
    return registry["pools"]


# Small private-registry override used by engine tests: fake model IDs and tiny budgets.
TEST_REGISTRY = {
    "models": {"models": {"astra": {"backend": "codex", "efforts": ["medium", "high"]},
                          "luna": {"backend": "codex", "efforts": ["max"]}}},
    "profiles": {"routing_version": "test-1", "phase_overrides": {"repair": "Economy"}},
    "pools": pools_from({"Economy": ("luna", "max"), "Standard": ("astra", "medium"), "Deep": ("astra", "high")}),
    "phases": {"phases": {p: {"budget": {"input_tokens": 1000, "output_tokens": 1000, "tool_calls": 10}}
                          for p in ("implement", "repair", "review")}},
}


# A fake `codex` executable (no model or network): `--version` and `login status` from the plan,
# and `exec` answers the launch start check (`{"ok": true}`) or, for other schemas, returns its
# argv. Each `exec` call appends {argv, cwd} to the plan's "calls". Plan keys: version, login,
# start_error (an error event + turn.failed, exit 1), stderr_error (stderr only, exit 1).
FAKE_CODEX = r'''
import json, os, pathlib, sys
PLAN = pathlib.Path(__PLAN__)
plan = json.loads(PLAN.read_text())
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli " + plan.get("version", "0.156.1")); sys.exit(0)
if args[:2] == ["login", "status"]:
    print(plan.get("login", "Logged in using ChatGPT"), file=sys.stderr); sys.exit(0)
sys.stdin.read()
plan.setdefault("calls", []).append({"argv": args, "cwd": os.getcwd()})
PLAN.write_text(json.dumps(plan))
if plan.get("stderr_error"):
    print(plan["stderr_error"], file=sys.stderr); sys.exit(1)
print(json.dumps({"type": "thread.started", "thread_id": "fixture-thread-%d" % len(plan["calls"])}), flush=True)
if plan.get("start_error"):
    print(json.dumps({"type": "error", "message": plan["start_error"]}), flush=True)
    print(json.dumps({"type": "turn.failed", "error": {"message": plan["start_error"]}}), flush=True)
    sys.exit(1)
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
result = {"ok": True} if set(schema.get("properties", {})) == {"ok"} else {"argv": args}
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}}), flush=True)
'''


def fake_codex(root, **plan):
    """Write the fake ``codex`` executable and its plan under ``root``; return (executable, plan path).
    An existing plan is kept (its recorded calls too) unless ``plan`` values are given."""
    root = Path(root)
    path = root / "fake-codex-plan.json"
    if plan or not path.exists():
        write(path, dict(json.loads(path.read_text()) if path.exists() else {}, **plan))
    executable = root / "fake-codex-cli"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_CODEX.replace("__PLAN__", repr(str(path))))
    executable.chmod(0o755)
    return executable, path


def fake_codex_calls(path):
    return json.loads(Path(path).read_text()).get("calls", [])


def make_home(root, repo, *, registry=None, site=None, workspace=None, project=None, batch=None):
    """Write site/workspace/project/batch layers under ``root/home``; return (home, batch path).
    The site's ``codex`` is the fake executable (``fake_codex``): a launch preflight starts it."""
    root, home = Path(root), Path(root) / "home"
    codex, _ = fake_codex(root)
    write(home / "guidance.md", "Implement a tiny fixture only.")
    write(root / "models.json", {"models": [{"slug": model, "supported_reasoning_levels": [{"effort": e} for e in ("medium", "high", "max")]}
                                            for model in ("astra", "luna")]})
    for name, value in (registry if registry is not None else TEST_REGISTRY).items():
        write(home / "registry" / f"{name}.json", value)
    write(home / "site.json", dict({"executables": {"codex": str(codex), "python": sys.executable},
                                    "state_root": str(root / "state"), "artifact_root": str(root / "runs"),
                                    "model_catalog": str(root / "models.json")}, **(site or {})))
    write(home / "workspaces" / "test.json", dict({"slug": "test", "auth": {"token_env": "TEST_LINEAR_TOKEN"},
                                                   "assignee": "me"}, **(workspace or {})))
    write(home / "projects" / "fixture.json", dict({
        "workspace": "test", "linear_project": "Fixture project", "repo": str(repo),
        "artifact_owner": "Test", "retention": "test", "guidance_files": ["../guidance.md"],
        "checks": [{"name": "output", "kind": "code", "tier": "default", "inputs": ["result.txt"], "cwd": ".",
                    "command": ["${python}", "-c", "from pathlib import Path; assert Path('result.txt').read_text() == 'ready'"]}],
    }, **(project or {})))
    batch_path = write(home / "batches" / "fixture.json", dict({
        "id": "fixture", "project": "fixture", "issues": ["DEV-1"], "terminal_issue": "DEV-1", "branch": "codex/test",
    }, **(batch or {})))
    return home, batch_path


class FakeLinear:
    """In-memory Linear boundary: issue reads/writes, append-only comments and name resolution.

    Comments go through the real ``append_comment`` (reconcile by marker, read-back).
    ``fail_posts`` refuses writes; ``lose_responses`` applies the next N writes and then
    raises, as when a response is lost after Linear accepted the comment.
    """

    def __init__(self):
        self.data = {"id": "DEV-1", "projectId": "p", "assigneeId": "owner", "projectMilestone": {"id": "m"},
                     "status": "Todo", "statusType": "unstarted", "description": "- [ ] Produce validated output", "labels": ["Implementation", "Standard"],
                     "relations": {"blockedBy": []}}
        self.others = {}
        self.reads = 0
        self.resolutions = []
        self.writes = []
        self.label_writes = []
        self.comment_store = {}   # issue -> [{"id", "body"}], append-only
        self.posts = []           # (issue, body) in creation order
        self.fail_posts = False
        self.lose_responses = 0
        self.on_post = None       # optional hook(issue, body) called after each accepted write

    def issue(self, identifier):
        self.reads += 1
        return copy.deepcopy(self.data if identifier == self.data["id"] else self.others[identifier])

    def comments(self, identifier):
        return []

    def call(self, name, **args):
        target = self.others.get(args.get("id"), self.data) if name == "save_issue" else self.data
        if name == "save_issue":
            if "state" in args:
                self.writes.append(args["state"])
                target["status"] = args["state"]
                target["statusType"] = {"In Progress": "started", "In Review": "started", "Done": "completed"}.get(args["state"], "started")
            if "labels" in args:
                self.label_writes.append((args["id"], list(args["labels"])))
                target["labels"] = list(args["labels"])
            if "description" in args:
                target["description"] = args["description"]
        return copy.deepcopy(target)

    def post_comment(self, issue, body, marker, *, reconcile=False):
        if self.fail_posts:
            raise RuntimeError("Linear offline")

        def create(text):
            comment = {"id": f"comment-{len(self.posts) + 1}", "body": text}
            self.comment_store.setdefault(issue, []).append(comment)
            self.posts.append((issue, text))
            if self.on_post:
                self.on_post(issue, text)
            if self.lose_responses:
                self.lose_responses -= 1
                raise RuntimeError("response lost after the comment was written")
            return dict(comment)
        return append_comment(lambda i: copy.deepcopy(self.comment_store.get(i, [])), create, issue, body, marker,
                              reconcile=reconcile)

    def bodies(self, issue):
        return [c["body"] for c in self.comment_store.get(issue, [])]

    def kinds(self, issue):
        """Event kinds posted on ``issue``, oldest first (from the hidden marker)."""
        return [re.search(r"<!-- linear-runner [^/]+/[^/]+/([^/]+)/\d+ -->", b).group(1) for b in self.bodies(issue)]

    def last(self, issue, kind):
        return [b for b, k in zip(self.bodies(issue), self.kinds(issue)) if k == kind][-1]

    def add_issue(self, identifier, **fields):
        """A second dispatchable issue with the same ownership as DEV-1."""
        issue = dict(copy.deepcopy(self.data), id=identifier, status="Todo", statusType="unstarted",
                     description=f"- [ ] Produce {identifier} output", relations={"blockedBy": []})
        issue.update(fields)
        self.others[identifier] = issue
        return issue

    def resolve_project(self, name):
        self.resolutions.append(("project", name))
        return "p"

    def resolve_user(self, name):
        self.resolutions.append(("user", name))
        return "owner"


# A fake `claude` executable for backend and engine tests; no model or network. It answers
# `--version` and `auth status` (with an email the runner must never record; with
# CLAUDE_CODE_OAUTH_TOKEN set it reports authMethod "oauth_token", as 2.1.281 does), and for `-p`
# replays stream-json in the shape of the recorded samples (tests/backends/claude_samples/).
# Each `-p` call consumes the next step of the JSON plan file and appends its argv and the
# CLAUDE_CODE_OAUTH_TOKEN it received (its environment, recorded only in the plan file) to "log";
# each `auth status` appends that token to "auth_probes".
# Step keys: write {relative: text} (in the cwd), outbox (a progress draft), acceptance (list
# overriding the one built from the prompt), status, error (an API error result, exit 1),
# auth_error (the W-191 "Failed to refresh OAuth token" error result, exit 1), usage (raw
# Claude usage). The launch start check (a `{"ok"}` schema) consumes no step: it is appended to
# "start_checks" and fails with an error result when the plan has "start_error".
FAKE_CLAUDE = r'''
import json, os, pathlib, re, sys
PLAN = pathlib.Path(__PLAN__)
args = sys.argv[1:]
plan = json.loads(PLAN.read_text())
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
if args == ["--version"]:
    print(plan.get("version", "2.1.281") + " (Claude Code)"); sys.exit(0)
if args[:2] == ["auth", "status"]:
    plan.setdefault("auth_probes", []).append(token)
    PLAN.write_text(json.dumps(plan))
    status = ({"loggedIn": True, "authMethod": "oauth_token", "apiProvider": "firstParty"} if token else
              {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
               "email": "owner@example.invalid", "orgName": "Fixture org"})
    print(json.dumps(dict(status, **plan.get("auth", {}))))
    sys.exit(0)
prompt = sys.stdin.read()
def opt(name):
    return args[args.index(name) + 1] if name in args else None
if set(json.loads(opt("--json-schema"))["properties"]) == {"ok"}:  # the launch start check
    session = opt("--session-id")
    plan.setdefault("start_checks", []).append({"argv": args, "cwd": os.getcwd(), "model": opt("--model"),
                                                "effort": opt("--effort"), "oauth_token": token})
    PLAN.write_text(json.dumps(plan))
    def emit(event):
        print(json.dumps(dict(event, session_id=session)), flush=True)
    emit({"type": "system", "subtype": "init", "model": opt("--model"), "tools": [], "mcp_servers": []})
    if plan.get("start_error"):
        emit({"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
              "result": plan["start_error"], "usage": {}, "modelUsage": {}})
        sys.exit(1)
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "structured_output": {"ok": True},
          "usage": {"input_tokens": 3, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                    "output_tokens": 4}, "modelUsage": {opt("--model"): {}}})
    sys.exit(0)
log = plan.setdefault("log", [])
step = plan.get("steps", [])[len(log)] if len(log) < len(plan.get("steps", [])) else {}
session = opt("--resume") or opt("--session-id")
log.append({"argv": args, "session": session, "resume": opt("--resume"), "model": opt("--model"),
            "effort": opt("--effort"), "mode": opt("--permission-mode"), "oauth_token": token,
            "environment": sorted(os.environ)})
PLAN.write_text(json.dumps(plan))
for relative, text in (step.get("write") or {}).items():
    pathlib.Path(relative).write_text(text)
if step.get("outbox"):
    outbox = pathlib.Path(re.search(r"drafts to (\S+)/NNN-", prompt).group(1))
    (outbox / "001-progress.md").write_text(step["outbox"])
def emit(event):
    print(json.dumps(dict(event, session_id=session)), flush=True)
emit({"type": "system", "subtype": "init", "model": opt("--model"), "permissionMode": opt("--permission-mode"),
      "tools": opt("--tools").split(","), "mcp_servers": [], "apiKeySource": "none", "claude_code_version": "2.1.281"})
zero = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}
if step.get("auth_error"):
    emit({"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
          "result": "Failed to refresh OAuth token: another Claude Code process is refreshing it or exited mid-refresh.",
          "usage": zero, "modelUsage": {}, "total_cost_usd": 0})
    sys.exit(1)
if step.get("error"):
    emit({"type": "assistant", "message": {"model": "<synthetic>", "role": "assistant",
                                           "content": [{"type": "text", "text": "There's an issue with the selected model."}]}})
    emit({"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error", "api_error_status": 404,
          "result": "There's an issue with the selected model.", "usage": zero, "modelUsage": {}, "total_cost_usd": 0})
    sys.exit(1)
schema = json.loads(opt("--json-schema"))["properties"]
marker = "Exact required criteria: "
criteria = (json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0] if marker in prompt
            else ["Produce validated output"])
result = {"issue_id": schema["issue_id"].get("enum", ["DEV-1"])[0], "status": step.get("status", "ready"),
          "summary": step.get("summary", "Ready"), "commit": schema["commit"].get("enum", [""])[0],
          "acceptance": step.get("acceptance", [{"criterion": c, "satisfied": True, "evidence": "fixture evidence"}
                                                for c in criteria]), "limitations": []}
if "deliverables" in schema:
    result["deliverables"] = []
model = opt("--model")
emit({"type": "assistant", "message": {"model": model, "role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "git status"}}]}})
emit({"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "clean"}]}})
emit({"type": "assistant", "message": {"model": model, "role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_2", "name": "StructuredOutput", "input": result}]}})
emit({"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_2", "content": "Structured output provided successfully"}]}})
usage = step.get("usage", {"input_tokens": 2, "cache_read_input_tokens": 60, "cache_creation_input_tokens": 38,
                           "output_tokens": 5, "output_tokens_details": {"thinking_tokens": 2}})
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": json.dumps(result),
      "structured_output": result, "usage": usage, "total_cost_usd": 0.01 * (len(log)),
      "modelUsage": {model: {"inputTokens": usage["input_tokens"], "outputTokens": usage["output_tokens"]}},
      "permission_denials": []})
'''


def fake_claude(root, steps=(), **plan):
    """Write the fake ``claude`` executable and its plan under ``root``; return (executable, plan path)."""
    root = Path(root)
    path = write(root / "fake-claude-plan.json", dict(plan, steps=list(steps)))
    executable = root / "fake-claude"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE.replace("__PLAN__", repr(str(path))))
    executable.chmod(0o755)
    return executable, path


def fake_claude_log(path):
    return json.loads(Path(path).read_text()).get("log", [])
