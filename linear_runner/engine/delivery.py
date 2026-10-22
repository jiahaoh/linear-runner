"""Generic, config-driven delivery integrity (no model, no project specifics).

Project ``delivery_checks`` render an issue's delivery packet from saved evidence and
receive ``RUNNER_DELIVERY_CONTEXT``. Afterwards this step verifies, from files only:

* every validation check the controller recorded passed (or selected no tests where its
  definition allows that, ``allow_empty``) and its log hash is intact;
* the project's ``required_checks`` are among them;
* every delivery check passed and its log hash is intact;
* the renderer's manifest exists, names the committed revision in ``revision_field``,
  and each ``file_hashes`` field equals the SHA-256 of the named file;
* each ``true_fields`` entry is literally ``true``.

It establishes structural integrity only. The independent reviewer still assesses the
scientific content of the packet.
"""
from __future__ import annotations

import json
from pathlib import Path

from linear_runner.config import file_sha256


class DeliveryError(RuntimeError):
    pass


# pytest's exit code when no test was collected or every test was deselected. A check with
# ``"allow_empty": true`` records this exit code as the distinct outcome ``empty``, which
# counts as passing; every other nonzero exit (and exit 5 without the flag) is a failure.
EMPTY_EXIT_CODE = 5
EMPTY_NOTE = "no tests selected; not applicable"


def check_outcome(exit_code, allow_empty=False):
    """``passed``, ``empty`` (allowed empty selection) or ``failed`` for one check run."""
    if exit_code == 0:
        return "passed"
    if allow_empty and exit_code == EMPTY_EXIT_CODE:
        return "empty"
    return "failed"


# An extended check with ``"last_issue": "when_changed"`` that no change of the batch matched:
# it did not run at the last issue. The record carries the reason and counts as not applicable.
SKIPPED_NOTE = "no file changed in this batch matches its inputs; not applicable"


def check_skipped(record):
    """Whether a saved record is a check that was skipped by its own ``last_issue`` rule."""
    return (record.get("status") == "skipped" and record.get("last_issue") == "when_changed"
            and record.get("exit_code") is None)


# An extended check that a validation did not run because one of its default checks failed
# (W-552): the validation goes to repair, and the check runs in the validation after it. The
# record names the failed check in ``skipped_after``; it is neither passing nor evidence.
FAIL_FAST_NOTE = "not run: the default check {check} failed, so this validation goes to repair; it runs after the repair"


def check_deferred(record):
    """Whether a saved record is an extended check left unrun after a default check failed."""
    return (record.get("status") == "skipped" and bool(record.get("skipped_after"))
            and record.get("exit_code") is None)


def check_passed(record):
    """Whether a saved check record counts as passing: exit 0, an allowed empty selection, or
    a check skipped by its own ``last_issue`` rule.

    An ``empty`` outcome is accepted only with its own evidence: exit code 5 and the
    ``allow_empty`` flag recorded from the check definition.
    """
    if record.get("exit_code") == 0 or check_skipped(record):
        return True
    return (record.get("status") == "empty" and record.get("exit_code") == EMPTY_EXIT_CODE
            and record.get("allow_empty") is True)


def evidence_intact(record):
    """A saved check record is passing evidence (passed, or an allowed empty selection) and its
    log is still the file that was hashed."""
    log = Path(record["log"]) if record.get("log") else None
    return bool(check_passed(record) and log and log.is_file() and file_sha256(log) == record.get("sha256"))


def _inside(base, relative, where):
    base = Path(base).resolve()
    path = (base / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(base):
        raise DeliveryError(f"{where}: path escapes {base}")
    return path


def _intact(records, where):
    for record in records:
        if check_skipped(record):  # it did not run: there is no log to verify
            continue
        name = record.get("name") or " ".join(record.get("command", [])[:3])
        if not check_passed(record):
            raise DeliveryError(f"{where} {name!r} did not pass")
        if not evidence_intact(record):
            raise DeliveryError(f"{where} {name!r} log is missing or altered")


def _json(path, where):
    if not path.is_file():
        raise DeliveryError(f"{where} is missing: {path.name}")
    try:
        return json.loads(path.read_text())
    except ValueError:
        raise DeliveryError(f"{where} is not valid JSON: {path.name}") from None


def verify_delivery(spec, delivery_dir, commit, validation_records):
    """Return an evidence record, or raise DeliveryError naming the first failure."""
    delivery_dir = Path(delivery_dir)
    _intact(validation_records, "validation check")
    names = {r.get("name") for r in validation_records if not check_skipped(r)}  # a skipped check is no evidence
    missing = sorted(set(spec.get("required_checks", [])) - names)
    if missing:
        raise DeliveryError(f"required check(s) {missing} are not in the validated evidence")
    delivery_checks = delivery_dir / "checks.json"
    delivery_records = json.loads(delivery_checks.read_text()) if delivery_checks.is_file() else []
    _intact(delivery_records, "delivery check")
    manifest_path = _inside(delivery_dir, spec["manifest"], "manifest")
    manifest = _json(manifest_path, "renderer output manifest")
    if not isinstance(manifest, dict):
        raise DeliveryError("renderer output manifest must be a JSON object")
    if manifest.get(spec["revision_field"]) != commit:
        raise DeliveryError(f"manifest {spec['revision_field']!r} does not match the committed revision")
    files = {}
    for field, relative in spec.get("file_hashes", {}).items():
        path = _inside(manifest_path.parent, relative, f"file_hashes.{field}")
        if not path.is_file():
            raise DeliveryError(f"delivered file for {field!r} is missing: {relative}")
        digest = file_sha256(path)
        if manifest.get(field) != digest:
            raise DeliveryError(f"manifest {field!r} does not match the SHA-256 of {relative}")
        files[relative] = digest
    for item in spec.get("true_fields", []):
        path = _inside(manifest_path.parent, item["file"], "true_fields")
        value = _json(path, "flag file")
        if not isinstance(value, dict) or value.get(item["field"]) is not True:
            raise DeliveryError(f"{item['file']} field {item['field']!r} is not true")
        files[item["file"]] = file_sha256(path)
    return {"commit": commit, "manifest": str(manifest_path), "manifest_sha256": file_sha256(manifest_path),
            "files_sha256": files, "validation_checks": sorted(n for n in names if n),
            "delivery_checks": len(delivery_records),
            "scope": "Structural integrity only; the independent reviewer assesses the delivered content."}
