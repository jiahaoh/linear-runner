"""Generic, config-driven delivery integrity (no model, no project specifics).

Project ``delivery_checks`` render an issue's delivery packet from saved evidence and
receive ``RUNNER_DELIVERY_CONTEXT``. Afterwards this step verifies, from files only:

* every validation check the controller recorded passed and its log hash is intact;
* the project's ``required_checks`` are among them;
* every delivery check passed and its log hash is intact;
* the renderer's manifest exists, names the committed revision in ``revision_field``,
  and each ``file_hashes`` field equals the SHA-256 of the named file;
* each ``true_fields`` entry is literally ``true``.

It establishes structural integrity only. The independent reviewer still assesses the
scientific content of the packet.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class DeliveryError(RuntimeError):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _inside(base, relative, where):
    base = Path(base).resolve()
    path = (base / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(base):
        raise DeliveryError(f"{where}: path escapes {base}")
    return path


def _intact(records, where):
    for record in records:
        name = record.get("name") or " ".join(record.get("command", [])[:3])
        if record.get("exit_code") != 0:
            raise DeliveryError(f"{where} {name!r} did not pass")
        log = Path(record.get("log", ""))
        if not log.is_file() or sha256(log) != record.get("sha256"):
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
    names = {r.get("name") for r in validation_records}
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
        digest = sha256(path)
        if manifest.get(field) != digest:
            raise DeliveryError(f"manifest {field!r} does not match the SHA-256 of {relative}")
        files[relative] = digest
    for item in spec.get("true_fields", []):
        path = _inside(manifest_path.parent, item["file"], "true_fields")
        value = _json(path, "flag file")
        if not isinstance(value, dict) or value.get(item["field"]) is not True:
            raise DeliveryError(f"{item['file']} field {item['field']!r} is not true")
        files[item["file"]] = sha256(path)
    return {"commit": commit, "manifest": str(manifest_path), "manifest_sha256": sha256(manifest_path),
            "files_sha256": files, "validation_checks": sorted(n for n in names if n),
            "delivery_checks": len(delivery_records),
            "scope": "Structural integrity only; the independent reviewer assesses the delivered content."}
