"""Base-parity checks (W-344): one command on the issue's base revision and on the result.

A check with ``base_parity`` runs its command twice with the same check environment: once in a
copy of the issue's starting commit, exported with ``git archive`` to a temporary directory
outside the worktree, and once in the worktree. Every path in the check environment and the
command that names the worktree names the exported tree in the base run, so for example
``PYTHONPATH=${worktree}/src/python`` imports the base revision's source there. The files named
in ``base_parity.outputs`` are then compared byte for byte; both sets and the comparison stay in
the validation directory. In s29-amend-20261005 every worker wrote this comparison itself, and a
review blocked when one did not (W-336).
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

COMPARISON_NAME = "parity.json"


def export_tree(repo, commit, target):
    """Extract ``commit`` of ``repo`` into ``target`` (``git archive``; the worktree is untouched)."""
    archive = Path(target).parent / "base.tar"
    with archive.open("wb") as output:
        subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], stdout=output, check=True)
    with tarfile.open(archive) as tar:
        try:
            tar.extractall(target, filter="data")
        except TypeError:  # Python 3.10/3.11 before the extraction filters; git archive output is trusted
            tar.extractall(target)
    archive.unlink()


def retarget(value, worktree, tree):
    """``value`` with the worktree's absolute path replaced by the exported tree's."""
    return value.replace(str(worktree), str(tree))


def output_path(entry, run_dir, cwd):
    """Where one ``outputs`` entry lives for one side: ``{run_dir}`` is that side's validation
    directory, and a relative path is relative to that side's ``cwd``."""
    path = Path(entry.replace("{run_dir}", str(run_dir)))
    return path if path.is_absolute() else Path(cwd) / path


def first_difference(base, result):
    """A short description of where two byte strings first differ."""
    if base == result:
        return None
    base_lines, result_lines = base.splitlines(), result.splitlines()
    for number, (left, right) in enumerate(zip(base_lines, result_lines), start=1):
        if left != right:
            return (f"line {number}: base {left[:120].decode(errors='replace')!r}, "
                    f"result {right[:120].decode(errors='replace')!r}")
    shorter = min(len(base_lines), len(result_lines))
    if len(base_lines) != len(result_lines):
        return f"line {shorter + 1}: one side has {abs(len(base_lines) - len(result_lines))} more line(s)"
    offset = next((i for i, (a, b) in enumerate(zip(base, result)) if a != b), min(len(base), len(result)))
    return f"byte {offset}"


def compare(outputs, sides, directory):
    """Copy each output of both sides into ``directory``/{base,result}/ and compare them.

    ``sides`` maps "base" and "result" to ``(run_dir, cwd)``. Returns the comparison record:
    ``equal`` and, per output, both SHA-256 values (None when missing) and the first difference."""
    entries = []
    for index, entry in enumerate(outputs):
        found = {}
        for side, (run_dir, cwd) in sides.items():
            path = output_path(entry, run_dir, cwd)
            if path.is_file():
                kept = Path(directory) / side / f"{index}-{path.name}"
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, kept)
                found[side] = kept.read_bytes()
        record = {"output": entry, **{f"{side}_sha256": hashlib.sha256(found[side]).hexdigest() if side in found
                                      else None for side in sides}}
        missing = [side for side in sides if side not in found]
        if missing:
            record.update(equal=False, difference=f"missing in {' and '.join(missing)}")
        else:
            difference = first_difference(found["base"], found["result"])
            record.update(equal=difference is None, difference=difference)
        entries.append(record)
    return {"equal": all(e["equal"] for e in entries), "outputs": entries}


def describe(comparison, base_commit):
    """One line for the check record and its log."""
    differing = [e for e in comparison["outputs"] if not e["equal"]]
    if not differing:
        return f"every output equals the base revision {base_commit[:12]}"
    first = differing[0]
    return f"{first['output']} differs from the base revision {base_commit[:12]}: {first['difference']}"


def temporary_tree():
    """A temporary directory outside any worktree for the exported base revision."""
    return tempfile.TemporaryDirectory(prefix="linear-runner-base-")
