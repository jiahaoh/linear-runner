"""Worker intake packets.

Schema 2 (``compact``, the default) keeps the packet to what is specific to the issue:

* the issue's own fields a worker needs (id, title, url, labels, milestone, relations and the
  description holding its deliverables and criteria) and the exact unchecked acceptance
  criteria the reviewer will assess;
* the shared contract as a reference, ``{path, sha256, bytes}``: one versioned file named by
  the project's ``contract_file``, identified by content hash and pinned with the
  configuration; it is not inlined;
* the project/batch guidance other than the contract, inline (it carries batch scope and
  authorizations);
* context files as ``{path, bytes, sha256}``, read on demand instead of inlined;
* checks, the model selection, the starting commit and operator notes, as before.

The full Linear issue pinned at intake is written next to the packet as ``issue.json``; it
remains the identity and contract record (``active["issue"]`` in state holds the same data).

Schema 1 (``full``) is the previous packet: the full issue, all guidance as ``constraints``
and every context file's text in ``references``.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

SCHEMA = "linear-runner.intake/2"
ISSUE_FIELDS = ("id", "title", "url", "labels", "projectMilestone", "relations", "status", "description")


def unchecked_criteria(description):
    # Same rule as runner.review_criteria (kept here to avoid an import cycle).
    return list(dict.fromkeys(re.findall(r"^\s*[-*] \[ \] (.+)$", description or "", re.M)))


def file_reference(path, text=None):
    data = text.encode() if text is not None else Path(path).read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def issue_view(issue):
    """The issue fields a worker reads; the full pinned issue stays in ``issue.json``."""
    view = {key: issue[key] for key in ISSUE_FIELDS if key in issue}
    relations = view.get("relations")
    if isinstance(relations, dict):  # identifiers and titles are enough to follow a relation
        view["relations"] = {kind: [{k: r[k] for k in ("id", "title") if isinstance(r, dict) and k in r}
                                    for r in items] if isinstance(items, list) else items
                             for kind, items in relations.items()}
    return view


def compact_packet(*, issue, starting_commit, guidance, contract, context_files, checks, selection,
                   operator_notes, issue_snapshot, extra=None):
    packet = {"schema": SCHEMA, "issue": issue_view(issue),
              "acceptance_criteria": unchecked_criteria(issue.get("description")),
              "contract": contract, "guidance": guidance, "context_files": context_files,
              "checks": checks, "selection": selection, "starting_commit": starting_commit,
              "operator_notes": operator_notes, "issue_snapshot": issue_snapshot}
    packet.update(extra or {})
    return packet


def build(config, active, selection, notes, snapshot=None):
    """The intake packet for the configured ``intake_mode`` (``snapshot``: the issue.json reference)."""
    issue = active["issue"]
    if config.get("intake_mode", "compact") == "full":
        return {"issue": issue, "starting_commit": active["starting_commit"],
                "constraints": config["_worker_instructions"], "references": config["_context"],
                "checks": config["checks"], "selection": selection, "operator_notes": notes}
    contract = config.get("contract")
    guidance = "\n\n".join(text for path, text in zip(config["guidance_files"], config["_guidance_parts"])
                           if not contract or path != contract["path"])
    context = [file_reference(path, entry["text"]) for path, entry in config["_context"].items()
               if not contract or path != contract["path"]]
    return compact_packet(issue=issue, starting_commit=active["starting_commit"], guidance=guidance,
                          contract=dict(contract) if contract else None, context_files=context,
                          checks=config["checks"], selection=selection, operator_notes=notes,
                          issue_snapshot=snapshot)


def compact_from_saved(packet, contract_names=("contract",)):
    """Replay the compact builder on a saved schema-1 intake (offline measurement).

    A saved context file whose name contains one of ``contract_names`` stands in for the
    shared contract (referenced by hash); other context files become references; guidance
    stays inline; run-specific extra keys written by recoveries are kept.
    """
    if packet.get("schema") == SCHEMA:
        return packet
    references = packet.get("references") or {}
    contract, context = None, []
    for path, entry in references.items():
        reference = {"path": path, "bytes": len(entry.get("text", "").encode()),
                     "sha256": entry.get("sha256") or hashlib.sha256(entry.get("text", "").encode()).hexdigest()}
        if contract is None and any(name in Path(path).name for name in contract_names):
            contract = reference
        else:
            context.append(reference)
    known = {"issue", "starting_commit", "constraints", "references", "checks", "selection", "operator_notes"}
    return compact_packet(issue=packet.get("issue") or {}, starting_commit=packet.get("starting_commit"),
                          guidance=packet.get("constraints", ""), contract=contract, context_files=context,
                          checks=packet.get("checks", []), selection=packet.get("selection"),
                          operator_notes=packet.get("operator_notes", []),
                          issue_snapshot={"path": "issue.json"},
                          extra={k: v for k, v in packet.items() if k not in known})
