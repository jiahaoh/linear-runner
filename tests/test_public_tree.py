"""The public tree must not contain private identifiers.

Patterns are assembled from fragments so this file does not match itself. When a private
configuration home exists, its literal values (workspace slugs, project names, owners,
host paths, pinned IDs) are loaded and checked too; without one, only the generic
patterns apply.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

from linear_runner import config

ROOT = Path(__file__).resolve().parent.parent  # the runner checkout
# The public repository URL is the one allowed mention of the owner's account.
ALLOWED_URLS = ("https://github.com/" + "jia" + "haoh" + "/linear-runner",)
PATTERNS = {
    "UUID": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    # Absolute paths only: a directory merely named "home" (examples/home/) is fine.
    "home directory path": re.compile(r"(?<![\w.-])" + re.escape("/ho" + "me/")),
    "user directory path": re.compile(r"(?<![\w.-])" + re.escape("/Us" + "ers/")),
    "lab share name": re.compile("wang" + "lab", re.I),
    "host name": re.compile("gp" + "099", re.I),
    "Linear document URL": re.compile(r"linear\.app/[^/\s]+/document/", re.I),
    # Anthropic keys and Claude OAuth tokens (for example from `claude setup-token`).
    "Anthropic credential": re.compile(r"\bsk-" + "ant" + r"-[a-z]{2,4}\d{2}-[A-Za-z0-9_-]{16,}"),
}
# Generic system locations that may legitimately appear as private site values.
SYSTEM_PREFIXES = ("/usr/", "/bin/", "/etc/", "/tmp/", "/opt/", "/var/")


def tracked_files():
    try:
        names = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
        return sorted(os.fsdecode(n) for n in names.split(b"\0") if n)
    except (OSError, subprocess.CalledProcessError):
        return sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                      if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts)


def public_text(relative):
    path = ROOT / relative
    text = relative + "\n" + (path.read_bytes().decode("utf-8", "replace") if path.is_file() else "")
    for url in ALLOWED_URLS:
        text = text.replace(url, "")
    return text


def private_literals():
    """Distinctive values from the private home, if one exists on this host."""
    home = config.find_home()
    literals = set()
    if not home.is_dir():
        return home, literals

    def strings(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from strings(key)
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    def load(path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    for path in [home / "site.json", *home.glob("workspaces/*.json"), *home.glob("projects/*.json")]:
        data = load(path)
        for key in ("slug", "linear_project", "artifact_owner", "retention"):
            if isinstance(data.get(key), str):
                literals.add(data[key])
        if isinstance(data.get("assignee"), str) and data["assignee"] != "me":
            literals.add(data["assignee"])
        for value in strings(data):
            if value.startswith("/") and not value.startswith(SYSTEM_PREFIXES):
                literals.add(value)
        state_root = data.get("state_root") if path.name == "site.json" else None
        if isinstance(state_root, str) and Path(state_root).is_dir():
            for pinned in Path(state_root).glob("*/" + config.RESOLVED_NAME):
                literals.update(v for v in load(pinned).get("resolution", {}).get("ids", {}).values() if isinstance(v, str))
    return home, {value for value in literals if len(value) >= 4 and "${" not in value}


class PublicTreeTests(unittest.TestCase):
    def test_tracked_files_contain_no_private_identifiers(self):
        files = tracked_files()
        self.assertIn("runner.py", files)
        for relative in files:
            text = public_text(relative)
            for name, pattern in PATTERNS.items():
                match = pattern.search(text)
                with self.subTest(file=relative, pattern=name):
                    self.assertIsNone(match, f"{relative}: {name} {match.group(0) if match else ''!r}")

    def test_tracked_files_contain_no_private_home_values(self):
        home, literals = private_literals()
        if not literals:
            self.skipTest(f"no private home values found at {home}")
        for relative in tracked_files():
            if relative == "LICENSE":  # the copyright holder is intentionally public
                continue
            text = public_text(relative)
            for value in literals:
                with self.subTest(file=relative):
                    self.assertNotIn(value, text, f"{relative} contains a value from the private home")

    def test_example_paths_are_obvious_placeholders(self):
        for path in (ROOT / "examples").rglob("*.json"):
            text = path.read_text()
            for value in re.findall(r'"(/[^"]*)"', text):
                with self.subTest(file=str(path.relative_to(ROOT)), value=value):
                    self.assertTrue(value.startswith("/absolute/path/to/"), value)

    def test_pattern_self_check(self):
        samples = {"UUID": "id " + "-".join(["123e4567", "e89b", "12d3", "a456", "426614174000"]),
                   "home directory path": '"/ho' + 'me/someone/x"', "user directory path": " /Us" + "ers/someone",
                   "lab share name": "WANG" + "LAB share", "host name": "gp" + "099.example",
                   "Linear document URL": "https://linear.app/" + "team/document/plan-1",
                   "Anthropic credential": "token sk-" + "ant-" + "oat01-" + "x" * 24}
        for name, sample in samples.items():
            self.assertIsNotNone(PATTERNS[name].search(sample), name)
        self.assertIsNone(PATTERNS["home directory path"].search("examples/ho" + "me/site.json"))


if __name__ == "__main__":
    unittest.main()
