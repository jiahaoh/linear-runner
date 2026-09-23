"""Shared offline test fixtures: a temporary private home and a fake Linear boundary."""
import copy
import json
from pathlib import Path
import sys


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) if not isinstance(value, str) else value)
    return path


# Small private-registry override used by engine tests: fake model IDs and tiny budgets.
TEST_REGISTRY = {
    "models": {"models": {"astra": {"efforts": ["medium", "high"]}, "luna": {"efforts": ["max"]}}},
    "profiles": {"routing_version": "test-1", "phase_overrides": {"repair": "Economy"},
                 "profiles": {"Economy": {"model": "luna", "effort": "max"},
                              "Standard": {"model": "astra", "effort": "medium"},
                              "Deep": {"model": "astra", "effort": "high"}}},
    "phases": {"phases": {p: {"budget": {"input_tokens": 1000, "output_tokens": 1000, "tool_calls": 10}}
                          for p in ("implement", "repair", "review")}},
}


def make_home(root, repo, *, registry=None, site=None, workspace=None, project=None, batch=None):
    """Write site/workspace/project/batch layers under ``root/home``; return (home, batch path)."""
    root, home = Path(root), Path(root) / "home"
    write(home / "guidance.md", "Implement a tiny fixture only.")
    write(root / "models.json", {"models": [{"slug": model, "supported_reasoning_levels": [{"effort": e} for e in ("medium", "high", "max")]}
                                            for model in ("astra", "luna")]})
    for name, value in (registry if registry is not None else TEST_REGISTRY).items():
        write(home / "registry" / f"{name}.json", value)
    write(home / "site.json", dict({"executables": {"codex": "codex", "python": sys.executable},
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
    """In-memory Linear boundary: issue reads/writes, marker summaries and name resolution."""

    def __init__(self):
        self.data = {"id": "DEV-1", "projectId": "p", "assigneeId": "owner", "projectMilestone": {"id": "m"},
                     "status": "Todo", "statusType": "unstarted", "description": "- [ ] Produce validated output", "labels": ["Implementation", "Standard"],
                     "relations": {"blockedBy": []}}
        self.others = {}
        self.summaries = {}; self.reads = 0; self.fail_summary = False
        self.resolutions = []
        self.writes = []

    def issue(self, identifier):
        self.reads += 1
        return copy.deepcopy(self.data if identifier == self.data["id"] else self.others[identifier])

    def comments(self, identifier):
        return []

    def call(self, name, **args):
        if name == "save_issue":
            self.writes.append(args.get("state"))
            self.data["status"] = args.get("state", self.data["status"])
            self.data["statusType"] = {"In Progress": "started", "In Review": "started", "Done": "completed"}.get(args.get("state"), "started")
            if "description" in args:
                self.data["description"] = args["description"]
        return copy.deepcopy(self.data)

    def summary(self, issue, marker, body):
        if self.fail_summary:
            raise RuntimeError("offline")
        self.summaries[marker] = body

    def resolve_project(self, name):
        self.resolutions.append(("project", name))
        return "p"

    def resolve_user(self, name):
        self.resolutions.append(("user", name))
        return "owner"
