"""Inert changes (W-605): changed files whose change cannot alter a test result, decided by the
controller from the repository alone.

``classify`` compares each path's blob in a base commit with the file on disk. A file is inert
by one of two rules, and by nothing else:

* ``python-ast``: the path ends in ``.py`` and both sides parse with this interpreter
  (``ast.parse`` on the bytes, so a coding declaration applies) into the same AST once every
  docstring is removed. A docstring is the first statement of a module, class, function or
  async function body when that statement is an expression whose value is a ``str`` constant.
  Comments never reach the AST, and line and column numbers are not compared, so a docstring
  that grows by a line is still inert.
* ``documentation``: the path ends in ``.md`` or ``.rst``, the project sets
  ``documents_read_by_tests`` (fnmatch patterns, like a check's ``inputs``) and the path
  matches none of its patterns. Without the key no file is inert by this rule.

A file added or deleted since the base (a rename is both), whose Git mode changed (the
executable bit, a symlink), that is not a regular file on either side, or that fails to parse
or decode on either side is never inert, nor is a file with any other suffix. The executable
bit on disk is compared with the base mode also where ``core.fileMode`` is false; such a
worktree then runs its checks as before.

Nothing a worker or reviewer wrote is an input: a summary that calls a change "comments only"
changes nothing. The base side is read with ``git ls-tree`` and ``git cat-file``, which leave
the index and the worktree alone; nothing is cached between calls.
"""
from __future__ import annotations

import ast
import fnmatch
import os
from pathlib import Path
import stat
import subprocess
import warnings

PYTHON_AST = "python-ast"
DOCUMENTATION = "documentation"
DOCUMENT_SUFFIXES = (".md", ".rst")
# The Git modes of a regular file, and whether each is executable. A symlink (120000) or a
# submodule (160000) is never inert.
REGULAR_MODES = {"100644": False, "100755": True}
DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def classify(repo, base, paths, documents_read_by_tests=None):
    """``{path: rule}`` for each of ``paths`` (relative to ``repo``): ``"python-ast"``,
    ``"documentation"``, or None when the file is not inert against the commit ``base``.
    ``documents_read_by_tests`` is the project's list of patterns, None when it sets none."""
    repo = Path(repo)
    paths = list(dict.fromkeys(paths))
    rules = {path: candidate_rule(path, documents_read_by_tests) for path in paths}
    candidates = [path for path, rule in rules.items() if rule]
    entries = base_entries(repo, base, candidates) if candidates else {}
    return {path: rule if rule and _inert(repo, path, entries.get(path), rule) else None
            for path, rule in rules.items()}


def candidate_rule(path, documents_read_by_tests):
    """The rule that could make ``path`` inert, by its name alone, or None."""
    if path.endswith(".py"):
        return PYTHON_AST
    if (path.endswith(DOCUMENT_SUFFIXES) and documents_read_by_tests is not None
            and not any(fnmatch.fnmatch(path, pattern) for pattern in documents_read_by_tests)):
        return DOCUMENTATION
    return None


def base_entries(repo, base, paths):
    """``{path: (mode, object)}`` of the ``paths`` that are files in ``base`` (``git ls-tree``,
    paths taken literally). When Git cannot list them, none is: what the controller cannot
    compare is not inert, and its checks run as before."""
    try:
        output = subprocess.run(["git", "-C", str(repo), "--literal-pathspecs", "ls-tree", "-r", "-z", base, "--",
                                 *paths], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    entries = {}
    for entry in output.split(b"\0"):
        if entry:
            meta, _, name = entry.partition(b"\t")
            mode, kind, obj = meta.decode().split()
            if kind == "blob":
                entries[os.fsdecode(name)] = (mode, obj)
    return entries


def _inert(repo, path, entry, rule):
    if entry is None or entry[0] not in REGULAR_MODES:  # added since the base, or not a regular file there
        return False
    try:
        status = (repo / path).lstat()
    except OSError:  # deleted since the base
        return False
    if not stat.S_ISREG(status.st_mode) or bool(status.st_mode & stat.S_IXUSR) != REGULAR_MODES[entry[0]]:
        return False
    # Through a directory that a symlink replaced, the path reaches another file: Git sees the
    # base's file as deleted.
    if os.path.realpath(repo / path) != os.path.join(os.path.realpath(repo), path):
        return False
    try:
        old = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", entry[1]], capture_output=True,
                             check=True).stdout
        new = (repo / path).read_bytes()
    except (OSError, subprocess.CalledProcessError):
        return False
    if rule == PYTHON_AST:
        old, new = without_docstrings(old), without_docstrings(new)
        return old is not None and old == new
    return _decodes(old) and _decodes(new)


def without_docstrings(source):
    """``ast.dump`` of ``source`` (bytes) without docstrings or positions; None when it does not parse."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # invalid escape sequences warn while parsing
            tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, DOCSTRING_OWNERS) and node.body and _is_docstring(node.body[0]):
                node.body = node.body[1:]
        return ast.dump(tree, include_attributes=False)
    except (SyntaxError, ValueError, RecursionError):
        return None


def _is_docstring(statement):
    return (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str))


def _decodes(data):
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
