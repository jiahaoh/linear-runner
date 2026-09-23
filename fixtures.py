"""Shared offline test fixtures: a temporary private home and a fake Linear boundary."""
import copy
import json
from pathlib import Path
import re
import sys

from linear_client import append_comment


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
