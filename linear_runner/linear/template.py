"""The Linear issue template "Runner issue contract", generated from ``templates/issue-contract.md``.

``runner.py sync-linear-template`` renders it (``--dry-run`` prints it and touches nothing),
then finds the workspace's issue template of that name through the Linear MCP endpoint,
compares its body with the rendering and records its ID in the private workspace file
(``issue_template``). The official Linear MCP endpoint can read templates but offers no tool
to create or update one, so a missing or outdated template is reported with the steps to
paste the rendering in Linear; the command never creates a second template.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from linear_runner.config import RUNNER_ROOT, read_json, write_json

NAME = "Runner issue contract"
SOURCE = RUNNER_ROOT / "templates" / "issue-contract.md"


def render(labels, release):
    """``{"name", "labels", "body"}``: the template body is the repository file without its
    front matter, after a note naming the source, the evidence rule and the required labels."""
    text = SOURCE.read_text()
    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[1]
    kinds, profiles = labels["task_kinds"], labels["profiles"]
    note = (f"> Runner issue contract, generated from `templates/issue-contract.md` (linear-runner {release}). "
            "Every acceptance criterion names where its evidence comes from. Give the issue exactly one task-kind "
            f"label ({', '.join(kinds)}) and one profile label ({', '.join(profiles)}), then put it in Todo.")
    return {"name": NAME, "labels": {"task_kind": kinds, "profile": profiles},
            "body": note + "\n\n" + text.strip() + "\n"}


def text_of(rendered):
    """What ``--dry-run`` prints."""
    labels = rendered["labels"]
    return (f"Name: {rendered['name']}\nLabels: one task kind ({' | '.join(labels['task_kind'])}) and one profile "
            f"({' | '.join(labels['profile'])})\n\n{rendered['body']}")


def _body(template):
    """The template's description text, whichever field the endpoint returns it in."""
    for key in ("content", "description", "body"):
        if isinstance(template.get(key), str):
            return template[key]
    data = template.get("templateData")
    if isinstance(data, dict) and isinstance(data.get("description"), str):
        return data["description"]
    return None


def _normalized(text):
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


def sync(linear, workspace_path, rendered, team=None):
    """Find the named issue template, compare it, record its ID; return a report.

    ``status`` is ``current`` (the body matches the rendering), ``differs`` (paste the
    rendering into it), ``missing`` (create it in Linear) or ``ambiguous`` (more than one
    template has the name). The workspace file gains or keeps ``issue_template`` only for a
    single match; running again changes nothing unless Linear changed.
    """
    args = {"type": "issue"} | ({"team": team} if team else {})
    listed = linear.call("list_templates", **args)
    templates = listed.get("templates", []) if isinstance(listed, dict) else []
    matches = [t for t in templates if isinstance(t, dict) and t.get("name") == rendered["name"]]
    digest = hashlib.sha256(rendered["body"].encode()).hexdigest()
    report = {"name": rendered["name"], "rendered_sha256": digest, "workspace_file": str(workspace_path)}
    how = ("The Linear MCP endpoint cannot create or update templates: in Linear open Settings, Templates, "
           f"then {{action}} the issue template \"{rendered['name']}\" with the output of "
           "`runner.py sync-linear-template --dry-run` as its description, and run this command again.")
    if not matches:
        return dict(report, status="missing", action=how.format(action="create"))
    if len(matches) > 1:
        return dict(report, status="ambiguous", ids=[t.get("id") for t in matches],
                    action=f"Keep one template named \"{rendered['name']}\" and delete the others in Linear.")
    template = linear.call("get_template", id=matches[0]["id"])
    body = _body(template if isinstance(template, dict) else {})
    status = "current" if body is not None and _normalized(body) == _normalized(rendered["body"]) else "differs"
    workspace = read_json(workspace_path)
    record = {"name": rendered["name"], "id": matches[0]["id"]}
    if workspace.get("issue_template") != record:
        workspace["issue_template"] = record
        write_json(workspace_path, workspace)
        report["recorded"] = True
    else:
        report["recorded"] = False
    report.update(status=status, id=matches[0]["id"])
    if status == "differs":
        report["action"] = how.format(action="update")
    return report
