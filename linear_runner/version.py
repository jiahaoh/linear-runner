"""The runner release: the newest released version in ``CHANGELOG.md``, tagged ``v<release>``.

``RELEASE`` changes only in a release commit (README "Releases"); a test checks that it is the
newest release in the CHANGELOG. Between releases a checkout reports the last release, its
commit and whether it is exactly the tagged release. A batch may pin a release
(``runner_version``); launch preflight then refuses any other checkout.
"""
from __future__ import annotations

import subprocess

RELEASE = "2.0.0"


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def tagged(root, release=RELEASE):
    """True when HEAD carries the tag ``v<release>``; None outside Git."""
    try:
        return f"v{release}" in _git(root, "tag", "--points-at", "HEAD").split()
    except (OSError, subprocess.CalledProcessError):
        return None


def describe(identity, *, at_tag):
    """One line for ``runner.py --version``."""
    commit = identity.get("commit") or "unknown commit"
    state = {True: "dirty", False: "clean"}.get(identity.get("dirty"), "state unknown")
    release = identity.get("release", RELEASE)
    if at_tag and identity.get("dirty") is False:
        return f"linear-runner {release} (commit {commit}, clean)"
    detail = f"uncommitted changes on v{release}" if at_tag else f"not the tagged release v{release}"
    return f"linear-runner {release} with unreleased changes (commit {commit}, {state}; {detail})"


def pin_problem(config, root):
    """Why this checkout does not match the batch's ``runner_version`` (None when it does or
    when the batch pins none)."""
    wanted = config.get("runner_version")
    if not wanted:
        return None
    identity = config.get("runner") or {}
    release, commit = identity.get("release"), identity.get("commit")
    where = f"this checkout is release {release} at commit {commit}"
    if release != wanted:
        return (f"The batch pins runner_version {wanted}, but {where}; check out the tag v{wanted} "
                "or change the batch's runner_version")
    if not tagged(root, wanted):
        return (f"The batch pins runner_version {wanted}, but {where}, which is not the tagged release v{wanted} "
                f"(unreleased changes, or the tag was not fetched); check out v{wanted} or cut a new release")
    if identity.get("dirty") is not False:
        return f"The batch pins runner_version {wanted}, but the runner checkout has uncommitted changes"
    return None
