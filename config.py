"""Layered configuration: registry -> private overrides -> site -> workspace -> project -> batch.

Loading is offline: it validates every layer against ``schema/`` plus cross-references,
reads guidance/context files and substitutes ``${variable}`` values, but never contacts
Linear or Codex. Linear project and assignee names are resolved to IDs separately
(``pin_resolution``) at dry-run/preflight and pinned into the batch state directory.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

RUNNER_ROOT = Path(__file__).resolve().parent
REGISTRY_NAMES = ("models", "labels", "profiles", "phases", "linear")
PHASES = ("implement", "repair", "review")
HOME_ENV = "LINEAR_RUNNER_HOME"
DEFAULT_HOME = "~/.config/linear-runner"
RESOLVED_NAME = "resolved-config.json"
# ${home} and ${runner_root} everywhere; ${batch} and ${worktree} in project/batch values.
BUILTIN_VARIABLES = ("home", "runner_root", "batch", "worktree")
# Bookkeeping keys that are not configuration and never enter the fingerprint.
META_KEYS = ("_sources", "_layers")
# Host launch settings: they choose how the supervisor process starts, not what the batch
# does, so changing them never blocks resuming. Each launch record stores the values used.
UNFINGERPRINTED = ("launcher",)
_VARIABLE = re.compile(r"\$\{([^}]*)\}")
# Defaults for the supervisor (batch layer) and launcher (site layer).
SUPERVISION_DEFAULTS = {"stop_after": [], "on_block": "continue_independent", "report_issues": [], "decision_rules": "honor",
                        "baseline_checks": False}
LAUNCHER_DEFAULTS = {"backend": "systemd-user", "python": None, "cpu_list": None, "environment": {},
                     "unit_prefix": "linear-runner", "startup_timeout_seconds": 30, "stop_on_exit": True}


class ConfigError(ValueError):
    pass


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


# --- Minimal JSON Schema subset (no third-party dependency) -----------------

def _is_type(value, kind):
    return {"object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "boolean": isinstance(value, bool), "null": value is None,
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool)}[kind]


def check_schema(value, schema, where, *, root=None, partial=False):
    """Validate the subset of JSON Schema used in ``schema/``.

    ``partial`` ignores ``required``/``min*`` so a private registry override may name
    only the values it changes; unknown keys and wrong types are still rejected.
    """
    root = root if root is not None else schema
    if "$ref" in schema:
        name = schema["$ref"].removeprefix("#/$defs/")
        return check_schema(value, root["$defs"][name], where, root=root, partial=partial)
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(_is_type(value, kind) for kind in kinds):
            raise ConfigError(f"{where}: expected {' or '.join(kinds)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ConfigError(f"{where}: must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ConfigError(f"{where}: must not be empty")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ConfigError(f"{where}: invalid value {value!r}")
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise ConfigError(f"{where}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ConfigError(f"{where}: must be <= {schema['maximum']}")
    if isinstance(value, list):
        if not partial and len(value) < schema.get("minItems", 0):
            raise ConfigError(f"{where}: needs at least {schema['minItems']} item(s)")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise ConfigError(f"{where}: items must be unique")
        for index, item in enumerate(value):
            if "items" in schema:
                check_schema(item, schema["items"], f"{where}[{index}]", root=root, partial=partial)
    if isinstance(value, dict):
        if not partial:
            missing = [key for key in schema.get("required", []) if key not in value]
            if missing:
                raise ConfigError(f"{where}: missing required key(s) {missing}")
            if len(value) < schema.get("minProperties", 0):
                raise ConfigError(f"{where}: needs at least {schema['minProperties']} entr(y/ies)")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if "propertyNames" in schema:
                check_schema(key, schema["propertyNames"], f"{where}: key {key!r}", root=root)
            if key in properties:
                check_schema(item, properties[key], f"{where}.{key}", root=root, partial=partial)
            elif extra is False:
                raise ConfigError(f"{where}: unknown key {key!r}")
            elif isinstance(extra, dict):
                check_schema(item, extra, f"{where}.{key}", root=root, partial=partial)


def load_schema(name):
    return read_json(RUNNER_ROOT / "schema" / f"{name}.schema.json")


def read_layer(path, schema, label, *, partial=False):
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"{label}: file not found: {path}")
    try:
        data = read_json(path)
    except ValueError as error:
        raise ConfigError(f"{label}: invalid JSON in {path}: {error}") from None
    check_schema(data, load_schema(schema), label, partial=partial)
    return data


# --- Source tracking ----------------------------------------------------------

def _record(sources, prefix, value, label):
    """Record ``label`` for every leaf under ``prefix``; lists are atomic values."""
    for key in [k for k in sources if k == prefix or k.startswith(prefix + ".")]:
        del sources[key]
    if isinstance(value, dict) and value:
        for key, item in value.items():
            _record(sources, f"{prefix}.{key}", item, label)
    else:
        sources[prefix] = label


def _merge(base, override, prefix, label, sources):
    for key, value in override.items():
        path = f"{prefix}.{key}"
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _merge(base[key], value, path, label, sources)
        else:
            base[key] = copy.deepcopy(value)
            _record(sources, path, value, label)


# --- Registry -------------------------------------------------------------------

def load_registry(home):
    """Public ``registry/`` defaults deep-merged with optional ``<home>/registry/`` overrides."""
    private = Path(home) / "registry"
    if private.is_dir():
        unknown = sorted(p.name for p in private.iterdir() if p.stem not in REGISTRY_NAMES or p.suffix != ".json")
        if unknown:
            raise ConfigError(f"Unknown private registry file(s) {unknown}; expected {[n + '.json' for n in REGISTRY_NAMES]}")
    policy, sources, layers = {}, {}, {}
    for name in REGISTRY_NAMES:
        public_path = RUNNER_ROOT / "registry" / f"{name}.json"
        label = f"registry/{name}.json"
        value = read_layer(public_path, f"registry-{name}", label)
        layers[label] = str(public_path)
        _record(sources, f"policy.{name}", value, label)
        override_path = private / f"{name}.json"
        if override_path.is_file():
            override_label = f"private registry/{name}.json"
            override = read_layer(override_path, f"registry-{name}", override_label, partial=True)
            layers[override_label] = str(override_path)
            _merge(value, override, f"policy.{name}", override_label, sources)
            check_schema(value, load_schema(f"registry-{name}"), f"merged registry/{name}.json")
        value.pop("notes", None)
        sources.pop(f"policy.{name}.notes", None)
        policy[name] = value
    check_policy(policy)
    return policy, sources, layers


def check_policy(policy):
    """Cross-reference checks the schemas cannot express."""
    models, labels, profiles = policy["models"], policy["labels"], policy["profiles"]
    for model, entry in models["models"].items():
        unknown = sorted(set(entry["efforts"]) - set(models["efforts"]))
        if unknown:
            raise ConfigError(f"registry models.{model}: unknown effort(s) {unknown}")
    if set(labels["task_kinds"]) & set(labels["profiles"]):
        raise ConfigError("registry labels: task-kind and profile labels must be distinct")
    order = profiles["order"]
    if set(order) != set(profiles["profiles"]) or set(order) != set(labels["profiles"]):
        raise ConfigError("registry: profiles.order, profiles.profiles and labels.profiles must name the same profiles")
    for name, entry in profiles["profiles"].items():
        if entry["model"] not in models["models"]:
            raise ConfigError(f"registry profiles.{name}: unknown model {entry['model']!r}")
        if entry["effort"] not in models["models"][entry["model"]]["efforts"]:
            raise ConfigError(f"registry profiles.{name}: effort {entry['effort']!r} is not allowed for {entry['model']!r}")
    floors = profiles["review_floors"]
    references = [("escalation_profile", profiles["escalation_profile"]), ("review_floors.default", floors["default"])]
    references += [(f"review_floors.by_profile.{k}", v) for k, v in floors["by_profile"].items()]
    references += [(f"review_floors.by_task_kind.{k}", v) for k, v in floors["by_task_kind"].items()]
    references += [(f"phase_overrides.{k}", v) for k, v in profiles["phase_overrides"].items()]
    for where, name in references:
        if name not in order:
            raise ConfigError(f"registry profiles.{where}: unknown profile {name!r}")
    for kind in floors["by_task_kind"]:
        if kind not in labels["task_kinds"]:
            raise ConfigError(f"registry profiles.review_floors.by_task_kind: unknown task kind {kind!r}")
    for name in floors["by_profile"]:
        if name not in order:
            raise ConfigError(f"registry profiles.review_floors.by_profile: unknown profile {name!r}")


# --- Layers -----------------------------------------------------------------------

def find_home(explicit=None):
    """``--home``, then ``$LINEAR_RUNNER_HOME``, then ``~/.config/linear-runner``."""
    value = explicit or os.environ.get(HOME_ENV) or DEFAULT_HOME
    return Path(value).expanduser().resolve()


def substitute(text, variables, where):
    def replace(match):
        name = match.group(1)
        if name not in variables:
            raise ConfigError(f"{where}: undefined variable ${{{name}}}")
        return variables[name]
    return _VARIABLE.sub(replace, text)


def substitute_known(text, variables):
    """Guidance prose: replace defined ``${name}`` only; others (e.g. shell examples) stay literal."""
    return _VARIABLE.sub(lambda match: variables.get(match.group(1), match.group(0)), text)


def _path(value, base, variables, where):
    path = Path(substitute(value, variables, where)).expanduser()
    return (path if path.is_absolute() else Path(base) / path).resolve()


def _read_text(path, where):
    if not path.is_file():
        raise ConfigError(f"{where}: file not found: {path}")
    return path.read_text()


def load_config(batch_path, home=None):
    """Validate and merge every layer offline; Linear IDs remain unresolved (None)."""
    home = find_home(home)
    batch_path = Path(batch_path).expanduser().resolve()
    policy, sources, layers = load_registry(home)

    site_path = home / "site.json"
    site = read_layer(site_path, "site", "site")
    batch = read_layer(batch_path, "batch", "batch")
    project_path = home / "projects" / f"{batch['project']}.json"
    project = read_layer(project_path, "project", f"project {batch['project']}")
    workspace_path = home / "workspaces" / f"{project['workspace']}.json"
    workspace = read_layer(workspace_path, "workspace", f"workspace {project['workspace']}")
    if workspace["slug"] != project["workspace"]:
        raise ConfigError(f"workspace {project['workspace']}: slug {workspace['slug']!r} differs from its file name")
    site_label, batch_label = "site", f"batch {batch['id']}"
    project_label, workspace_label = f"project {batch['project']}", f"workspace {workspace['slug']}"
    layers.update({site_label: str(site_path), workspace_label: str(workspace_path),
                   project_label: str(project_path), batch_label: str(batch_path)})

    variables = {"home": str(home), "runner_root": str(RUNNER_ROOT), "batch": batch["id"]}
    for group in ("executables", "variables"):
        for name, value in site.get(group, {}).items():
            if name in variables or name in BUILTIN_VARIABLES:
                raise ConfigError(f"site.{group}.{name}: variable is already defined")
            variables[name] = os.path.expanduser(value)

    config = {}

    def put(key, value, label):
        config[key] = value
        _record(sources, key, value, label)

    # Batch: the authorized queue and its dedicated branch/worktree.
    put("batch_id", batch["id"], batch_label)
    put("issues", batch["issues"], batch_label)
    put("terminal_issue", batch["terminal_issue"], batch_label)
    put("required_done", batch.get("required_done", []), batch_label)
    put("human_gates", batch.get("human_gates", []), batch_label)
    put("branch", batch["branch"], batch_label)
    for gate in config["human_gates"]:
        if gate["issue_id"] in config["issues"]:
            raise ConfigError(f"{batch_label}: human gate {gate['issue_id']} cannot also be an implementation issue")
    for identity in config["required_done"]:
        if identity in config["issues"]:
            raise ConfigError(f"{batch_label}: required_done issue {identity} cannot also be an implementation issue")
    # Supervisor behavior: planned checkpoints, blocking policy, reporting and rules.
    # report_issues may name issues outside the allowlist (for example a tracking issue).
    supervision = dict(copy.deepcopy(SUPERVISION_DEFAULTS), **copy.deepcopy(batch.get("supervision", {})))
    outside = [i for i in supervision["stop_after"] if i not in config["issues"]]
    if outside:
        raise ConfigError(f"{batch_label}.supervision.stop_after: {outside} not in the issue allowlist")
    put("supervision", supervision, batch_label)
    if "worktree" in batch:
        put("worktree", str(_path(batch["worktree"], batch_path.parent, variables, f"{batch_label}.worktree")), batch_label)
    else:
        put("worktree", str(_path(project["repo"], project_path.parent, variables, f"{project_label}.repo")), project_label)
    repo = Path(config["worktree"])
    variables["worktree"] = str(repo)

    # Workspace: Linear identity, credential reference and state names.
    auth = workspace["auth"]
    if bool(auth.get("token_env")) == bool(auth.get("credentials_file")):
        raise ConfigError(f"{workspace_label}.auth: choose exactly one of token_env or credentials_file; never store credentials")
    linear = dict(auth)
    if "credentials_file" in linear:
        linear["credentials_file"] = str(_path(linear["credentials_file"], workspace_path.parent, variables,
                                               f"{workspace_label}.auth.credentials_file"))
    put("linear", linear, workspace_label)
    put("linear_workspace", workspace["slug"], workspace_label)
    put("assignee", workspace.get("assignee", "me"), workspace_label)
    states = copy.deepcopy(policy["linear"]["states"])
    for key in states:
        sources[f"states.{key}"] = sources[f"policy.linear.states.{key}"]
    for key, value in workspace.get("states", {}).items():
        states[key] = value
        sources[f"states.{key}"] = workspace_label
    config["states"] = states
    config["require_milestone"] = policy["linear"]["require_milestone"]
    sources["require_milestone"] = sources["policy.linear.require_milestone"]

    # Project: Linear project name, provenance, guidance, checks and identities.
    put("project_name", project["linear_project"], project_label)
    for key in ("artifact_owner", "retention"):
        put(key, project[key], project_label)
    put("backup_status", project.get("backup_status", "unverified"), project_label)
    environment = {name: substitute(value, variables, f"{project_label}.check_environment.{name}")
                   for name, value in project.get("check_environment", {}).items()}
    put("check_environment", environment, project_label)

    def inside(cwd, where):
        if Path(cwd).is_absolute() or not (repo / cwd).resolve().is_relative_to(repo):
            raise ConfigError(f"{where}: cwd must be a relative path inside the worktree")
        return cwd

    checks = []
    for index, spec in enumerate(project["checks"]):
        where = f"{project_label}.checks[{index}]"
        for pattern in spec["inputs"]:
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ConfigError(f"{where}.inputs: patterns must stay inside the worktree")
        checks.append(dict(spec, cwd=inside(substitute(spec["cwd"], variables, where), where),
                           command=[substitute(arg, variables, where) for arg in spec["command"]]))
    if len({c["name"] for c in checks}) != len(checks):
        raise ConfigError(f"{project_label}.checks: names must be unique")
    put("checks", checks, project_label)
    delivery = []
    for index, spec in enumerate(project.get("delivery_checks", [])):
        where = f"{project_label}.delivery_checks[{index}]"
        delivery.append({"cwd": inside(substitute(spec["cwd"], variables, where), where),
                         "command": [substitute(arg, variables, where) for arg in spec["command"]]})
    put("delivery_checks", delivery, project_label)
    integrity = copy.deepcopy(project.get("delivery_integrity"))
    if integrity:
        unknown = sorted(set(integrity.get("required_checks", [])) - {c["name"] for c in checks})
        if unknown:
            raise ConfigError(f"{project_label}.delivery_integrity.required_checks: unknown check(s) {unknown}")
    put("delivery_integrity", integrity, project_label)
    identities = []
    for index, value in enumerate(project.get("identity_files", [])):
        path = _path(value, project_path.parent, variables, f"{project_label}.identity_files[{index}]")
        if not path.is_file():
            raise ConfigError(f"{project_label}.identity_files[{index}]: every data/environment identity file must exist: {path}")
        identities.append(str(path))
    put("identity_files", identities, project_label)
    context = {}
    for index, value in enumerate(project.get("context_files", [])):
        path = _path(value, project_path.parent, variables, f"{project_label}.context_files[{index}]")
        body = _read_text(path, f"{project_label}.context_files[{index}]")
        context[str(path)] = {"sha256": hashlib.sha256(body.encode()).hexdigest(), "text": body}
    put("context_files", list(context), project_label)
    config["_context"] = context
    sources["_context"] = project_label

    # Guidance: project files first, then batch-specific files.
    guidance, texts = [], []
    for label, base, values in ((project_label, project_path.parent, project["guidance_files"]),
                                (batch_label, batch_path.parent, batch.get("guidance_files", []))):
        for index, value in enumerate(values):
            path = _path(value, base, variables, f"{label}.guidance_files[{index}]")
            text = substitute_known(_read_text(path, f"{label}.guidance_files[{index}]"), variables)
            if not text.strip():
                raise ConfigError(f"{label}.guidance_files[{index}]: must not be empty")
            guidance.append(str(path))
            texts.append(text)
    config["guidance_files"] = guidance
    sources["guidance_files"] = project_label if not batch.get("guidance_files") else f"{project_label} + {batch_label}"
    config["_worker_instructions"] = "\n\n".join(texts)
    sources["_worker_instructions"] = sources["guidance_files"]

    # Site: host executables, storage roots and the model catalog.
    builtins = {name: variables[name] for name in ("home", "runner_root")}
    put("codex", variables["codex"], site_label)
    put("model_catalog", str(_path(site["model_catalog"], site_path.parent, builtins, "site.model_catalog")), site_label)
    put("artifact_root", str(_path(site["artifact_root"], site_path.parent, builtins, "site.artifact_root")), site_label)
    state_root = _path(site["state_root"], site_path.parent, builtins, "site.state_root")
    put("state_dir", str(state_root / batch["id"]), f"{site_label} + {batch_label}")
    put("variables", variables, site_label)
    launcher = dict(copy.deepcopy(LAUNCHER_DEFAULTS), **copy.deepcopy(site.get("launcher", {})))
    for key in ("python", "cpu_list"):
        if launcher.get(key) is not None:
            launcher[key] = substitute(launcher[key], variables, f"site.launcher.{key}")
    if launcher.get("cpu_list") is not None and not re.fullmatch(r"[0-9]+([,-][0-9]+)*", launcher["cpu_list"]):
        raise ConfigError(f"site.launcher.cpu_list: invalid CPU list {launcher['cpu_list']!r}")
    launcher["environment"] = {name: substitute(value, variables, f"site.launcher.environment.{name}")
                               for name, value in launcher["environment"].items()}
    put("launcher", launcher, site_label)
    for name in BUILTIN_VARIABLES:
        sources[f"variables.{name}"] = "built-in"
    for key in ("state_dir", "artifact_root"):
        if Path(config[key]).is_relative_to(repo):
            raise ConfigError(f"{key} must be outside the worktree")

    config["policy"] = policy  # leaf sources were recorded while merging the registry
    # Resolved at dry-run/preflight; never guessed offline.
    config["project_id"] = config["assignee_id"] = None
    sources["project_id"] = sources["assignee_id"] = "unresolved"
    config["runner"] = runner_identity()
    sources["runner.commit"] = sources["runner.dirty"] = "runner checkout"
    config["_sources"] = dict(sorted(sources.items()))
    config["_layers"] = layers
    return config


# --- Fingerprint and name resolution ---------------------------------------

def runner_identity(root=RUNNER_ROOT):
    """Identify the runner by Git commit and clean/dirty state, never by checkout path."""
    try:
        def run(*args):
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()
        return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


def _portable(value, root):
    """Replace the runner checkout path with ``${runner_root}`` so a moved checkout keeps its hash."""
    if isinstance(value, str):
        return "${runner_root}" if value == root else value.replace(root + os.sep, "${runner_root}" + os.sep)
    if isinstance(value, list):
        return [_portable(item, root) for item in value]
    if isinstance(value, dict):
        return {_portable(key, root): _portable(item, root) for key, item in value.items()}
    return value


def config_fingerprint(config):
    """Hash the resolved configuration: commands, guidance text, policy, IDs and runner commit.

    The runner checkout path is normalized away; its commit and dirty flag identify it.
    """
    effective = {k: v for k, v in config.items() if k not in META_KEYS + UNFINGERPRINTED}
    root = effective.get("variables", {}).get("runner_root") or str(RUNNER_ROOT)
    return hashlib.sha256(json.dumps(_portable(effective, root), sort_keys=True).encode()).hexdigest()


def resolution_names(config):
    return {"workspace": config["linear_workspace"], "project": config["project_name"], "assignee": config["assignee"]}


def _with_ids(config, ids, label):
    resolved = copy.deepcopy(config)
    resolved.update(ids)
    for key in ids:
        resolved["_sources"][key] = label
    return resolved


def pin_resolution(config, linear):
    """Return (resolved config, fresh). Reuse pinned IDs; resolve live only for a new batch."""
    path = Path(config["state_dir"]) / RESOLVED_NAME
    if path.exists():
        pinned = read_json(path)
        if pinned.get("resolution", {}).get("names") != resolution_names(config):
            raise ConfigError("Linear names changed since this batch pinned its IDs; restore the configuration or prepare a new batch")
        resolved = _with_ids(config, pinned["resolution"]["ids"], f"pinned {RESOLVED_NAME}")
        if config_fingerprint(resolved) != pinned.get("config_sha256"):
            raise ConfigError(f"Configuration/guidance changed since {path} was pinned; restore the saved batch configuration before resuming")
        return resolved, False
    ids = {"project_id": linear.resolve_project(config["project_name"]),
           "assignee_id": linear.resolve_user(config["assignee"])}
    return _with_ids(config, ids, "Linear name resolution"), True


def write_resolved(config):
    """Pin resolved IDs, the effective configuration and the source layer of each value."""
    write_json(Path(config["state_dir"]) / RESOLVED_NAME, {
        "resolved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config_sha256": config_fingerprint(config),
        "resolution": {"names": resolution_names(config),
                       "ids": {"project_id": config["project_id"], "assignee_id": config["assignee_id"]}},
        "layers": config["_layers"],
        "sources": config["_sources"],
        "config": {k: v for k, v in config.items() if k not in META_KEYS},
    })
