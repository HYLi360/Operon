"""Project configuration and directory layout.

Code/config/metadata/data separation is a core design principle: code says
what to do, project.yaml says how to do it for this project.

Two configuration layers live here:

* :class:`Project` -- the per-project ``project.yaml`` (storage roots,
  database paths, execution backends, resources).
* :class:`UserConfig` -- the per-user ``$XDG_CONFIG_HOME/operon/config.yml``
  (audit identity, an NCBI contact address, UI preferences).  It never
  carries secrets, and its key set stays disjoint from the project layer.

Environment variables are never deprecated and always outrank the user
configuration: ``--flag`` > environment > user configuration > default.
"""

from __future__ import annotations

import getpass
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from operon.errors import ConfigError
from operon.schema import default_schemas
from operon.utils import atomic_write_text

DEFAULT_CONFIG: dict[str, Any] = {
    "project": {
        "id": "PRJ_000001",
        "name": "unnamed genome project",
        "description": "",
    },
    "storage": {
        "raw_root": "raw",
        "standardized_root": "standardized",
        "qc_root": "qc",
        "analysis_root": "analysis",
        "reports_root": "reports",
        "logs_root": "logs",
        "releases_root": "releases",
        "taxonomy_root": "taxonomy",
    },
    "database": {
        "path": "operon.sqlite",
        "metadata_dir": "metadata",
        "schema_path": "config/schemas.yaml",
        "profiles_dir": "config/profiles",
    },
    "qc": {
        "default_profile": "assembly_production_v1",
        "sample_reads_for_duplicates": 1000000,
    },
    "resources": {
        "default_threads": 4,
        "max_memory_gb": 64,
    },
    "execution": {
        "backend": "local",
        "slurm": {
            "partition": "",
            "time": "24:00:00",
            "mem_gb": 0,
            "extra_sbatch": [],
            "setup_commands": [],
            "poll_interval": 15,
        },
        "ssh": {
            "host": "",
            "user": "",
            "port": 22,
            "key_file": "",
            "remote_root": "",
            "storage_remote": "",
            "scheduler": "none",
            "connect_timeout": 30,
            "known_hosts": "",
            "host_key_sha256": "",
            "insecure_accept_unknown_host": False,
        },
    },
    "remotes": {},
}


@dataclass
class Project:
    """An operon project rooted at a directory containing project.yaml."""

    root: Path
    config: dict[str, Any]
    config_path: Path

    @property
    def project_id(self) -> str:
        return str(self.config["project"]["id"])

    @property
    def db_path(self) -> Path:
        return self._resolve(self.config["database"]["path"])

    @property
    def metadata_dir(self) -> Path:
        return self._resolve(self.config["database"]["metadata_dir"])

    @property
    def schema_path(self) -> Path:
        return self._resolve(self.config["database"]["schema_path"])

    @property
    def profiles_dir(self) -> Path:
        return self._resolve(self.config["database"]["profiles_dir"])

    @property
    def tools_config_path(self) -> Path:
        return self.root / "config" / "tools.yaml"

    @property
    def raw_root(self) -> Path:
        return self._resolve(self.config["storage"]["raw_root"])

    @property
    def standardized_root(self) -> Path:
        return self._resolve(self.config["storage"]["standardized_root"])

    @property
    def qc_root(self) -> Path:
        return self._resolve(self.config["storage"]["qc_root"])

    @property
    def analysis_root(self) -> Path:
        return self._resolve(self.config["storage"]["analysis_root"])

    @property
    def reports_root(self) -> Path:
        return self._resolve(self.config["storage"]["reports_root"])

    @property
    def logs_root(self) -> Path:
        return self._resolve(self.config["storage"]["logs_root"])

    @property
    def releases_root(self) -> Path:
        return self._resolve(self.config["storage"]["releases_root"])

    @property
    def taxonomy_root(self) -> Path:
        return self._resolve(self.config["storage"].get("taxonomy_root", "taxonomy"))

    @property
    def taxonomy_reference_sets_dir(self) -> Path:
        return self.taxonomy_root / "reference_sets"

    def _resolve(self, value: str | Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        return path

    def ensure_dirs(self) -> None:
        for directory in (
                self.metadata_dir,
                self.schema_path.parent,
                self.profiles_dir,
                self.raw_root / "reads",
                self.raw_root / "assemblies",
                self.raw_root / "annotations",
                self.standardized_root / "reads",
                self.standardized_root / "assemblies",
                self.standardized_root / "annotations",
                self.qc_root / "reads",
                self.qc_root / "assemblies",
                self.qc_root / "annotations",
                self.qc_root / "aggregate",
                self.qc_root / "cache" / "fasta_lengths",
                self.analysis_root,
                self.reports_root,
                self.logs_root,
                self.releases_root,
                self.taxonomy_reference_sets_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def init(cls, root: str | Path, project_id: str = "PRJ_000001", name: str = "") -> Project:
        root = Path(root).resolve()
        config_path = root / "project.yaml"
        if config_path.exists():
            raise ConfigError(f"project already initialized: {config_path}")
        root.mkdir(parents=True, exist_ok=True)
        config = yaml.safe_load(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False)) or {}
        config["project"]["id"] = project_id
        config["project"]["name"] = name or project_id
        config_path.write_text(
            "# Operon project configuration (YAML)\n"
            "# Code = what to do; this file = how to do it for this project.\n"
            + yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        project = cls(root, config, config_path)
        project.ensure_dirs()
        project.schema_path.write_text(
            "# Operon metadata schema (YAML). Extend fields here before importing metadata.\n"
            + yaml.safe_dump(default_schemas(), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        from operon.profiles import write_default_profiles
        write_default_profiles(project.profiles_dir)
        from operon.metadata_files import initialize_metadata_directory
        initialize_metadata_directory(project.metadata_dir)
        from operon.tools import ensure_tools_config
        ensure_tools_config(project)
        # A freshly initialized project must be immediately usable by
        # read-only preview commands.  Create the current empty database as
        # part of init instead of relying on the first later write command to
        # materialize it as a side effect.
        from operon.database import Database
        database = Database(project.db_path)
        database.close()
        return project

    @classmethod
    def find(cls, start: str | Path = ".") -> Project:
        current = Path(start).resolve()
        if current.is_file():
            current = current.parent
        for candidate in [current, *current.parents]:
            config_path = candidate / "project.yaml"
            if config_path.exists():
                with open(config_path, encoding="utf-8") as handle:
                    config = yaml.safe_load(handle) or {}
                return cls(candidate, config, config_path)
        raise ConfigError("no project.yaml found; run `operon init` first")


def load_project(path: str | Path = ".") -> Project:
    path = Path(path)
    if path.is_file() and path.name == "project.yaml":
        with open(path, encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        return Project(path.parent.resolve(), config, path.resolve())
    return Project.find(path)


def project_rel(project: Project, path: str | Path) -> str:
    """Return a path relative to the project root for storage in the manifest."""
    return os.path.relpath(os.path.abspath(path), os.path.abspath(project.root))


# --- user-level configuration (XDG) ------------------------------------------

USER_CONFIG_SCHEMA_VERSION = 1
"""Version of the user configuration document."""

USER_CONFIG_DIRECTORY = "operon"
USER_CONFIG_FILENAME = "config.yml"

USER_CONFIG_DEFAULT: dict[str, Any] = {
    "schema_version": USER_CONFIG_SCHEMA_VERSION,
    "identity": {"actor": ""},
    "ncbi": {"email": ""},
    "ui": {"splash": "auto"},
}
"""Every user-level key with its built-in default.

The user configuration carries no secrets -- API tokens live in a user-scoped
secret backend (:mod:`operon.secrets`) -- and it must never repeat a key the
project layer owns (``project.yaml`` storage roots, database paths, resources,
execution backends).
"""

USER_CONFIG_TYPES: dict[str, type] = {
    "schema_version": int,
    "identity.actor": str,
    "ncbi.email": str,
    "ui.splash": str,
}

USER_CONFIG_CHOICES: dict[str, tuple[str, ...]] = {
    "ui.splash": ("auto", "text", "blocks", "kitty"),
}

ACTOR_ENV_VARS: tuple[str, ...] = ("OPERON_ACTOR", "USER", "LOGNAME", "USERNAME")
"""Environment variables consulted for the audit actor, in precedence order."""

_SECRET_LIKE_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|credential)")


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    """The environment to read settings from (``os.environ`` by default)."""
    return os.environ if environ is None else environ  # env-audit: user-level settings


def user_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The XDG configuration directory for ``operon``.

    ``$XDG_CONFIG_HOME`` selects the base directory and defaults to
    ``~/.config``; a ``.operon`` entry in the home directory is never created
    or read.
    """
    base = (_environ(environ).get("XDG_CONFIG_HOME") or "").strip()
    root = Path(base) if base else Path.home() / ".config"
    return root / USER_CONFIG_DIRECTORY


def user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """The user configuration file: ``<user_config_dir>/config.yml``."""
    return user_config_dir(environ) / USER_CONFIG_FILENAME


def _split_key(key: str) -> list[str]:
    parts = [part.strip() for part in str(key).split(".") if part.strip()]
    if not parts:
        raise ConfigError("configuration key must not be empty")
    return parts


def _lookup(data: Mapping[str, Any], parts: list[str]) -> Any:
    node: Any = data
    for part in parts:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _assign(data: dict[str, Any], parts: list[str], value: Any) -> None:
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def flatten_user_config(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested configuration document into dotted keys."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(flatten_user_config(value, prefix=f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def normalize_user_value(key: str, value: Any) -> Any:
    """Validate one dotted key/value pair (the ``config set`` entry point)."""
    expected = USER_CONFIG_TYPES.get(key)
    if expected is None:
        raise ConfigError(
            f"unknown configuration key {key!r}; known keys: "
            + ", ".join(sorted(USER_CONFIG_TYPES))
        )
    if expected is int:
        if isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError as exc:
                raise ConfigError(f"{key} expects an integer, got {value!r}") from exc
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key} expects an integer, got {type(value).__name__}")
    elif not isinstance(value, expected):
        raise ConfigError(f"{key} expects {expected.__name__}, got {type(value).__name__}")
    choices = USER_CONFIG_CHOICES.get(key)
    if choices and value not in choices:
        raise ConfigError(f"{key} must be one of {', '.join(choices)}; got {value!r}")
    return value


def _merge_user_defaults(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Fill in defaults without dropping user values or unknown keys."""
    merged = deepcopy(USER_CONFIG_DEFAULT)
    for key, value in raw.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **dict(value)}
        else:
            merged[key] = value
    return merged


class UserConfig:
    """Lazy, file-backed per-user configuration (``~/.config/operon/config.yml``).

    Importing this module never touches the filesystem: the file is read on
    the first :attr:`data` access and written only by ``operon config``.  A
    missing file means "all defaults"; an unreadable or malformed file raises
    :class:`ConfigError` instead of silently ignoring the user's intent.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._data: dict[str, Any] | None = None

    # --- location ---------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path or user_config_path()

    # --- loading / saving -------------------------------------------------
    @property
    def data(self) -> dict[str, Any]:
        """The in-memory document, loaded on first access."""
        if self._data is None:
            self._data = self.load()
        return self._data

    @data.setter
    def data(self, value: dict[str, Any] | None) -> None:
        self._data = value

    def load(self) -> dict[str, Any]:
        """Read the file (or return the defaults when it does not exist)."""
        path = self.path
        if not path.is_file():
            return deepcopy(USER_CONFIG_DEFAULT)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"{path}: cannot be read: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ConfigError(
                f"{path}: top level must be a mapping, got {type(raw).__name__}"
            )
        return _merge_user_defaults(raw)

    def save(self) -> None:
        """Write the document atomically with user-only permissions."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        atomic_write_text(path, yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True))
        os.chmod(path, 0o600)

    def init(self, *, force: bool = False) -> Path:
        """Write the default document; refuse to overwrite without ``force``."""
        path = self.path
        if path.exists() and not force:
            raise ConfigError(f"{path} already exists; pass --force to overwrite it")
        self._data = deepcopy(USER_CONFIG_DEFAULT)
        self.save()
        return path

    def reset(self) -> None:
        """Restore the built-in defaults in memory."""
        self._data = deepcopy(USER_CONFIG_DEFAULT)

    # --- key access -------------------------------------------------------
    def get(self, key: str) -> Any:
        return _lookup(self.data, _split_key(key))

    def set(self, key: str, value: Any) -> Any:
        """Validate and persist one dotted key; returns the stored value."""
        parts = _split_key(key)
        normalized = normalize_user_value(".".join(parts), value)
        _assign(self.data, parts, normalized)
        self.save()
        return normalized

    def unset(self, key: str) -> Any:
        """Restore one dotted key to its default value and persist."""
        dotted = ".".join(_split_key(key))
        if dotted not in USER_CONFIG_TYPES:
            raise ConfigError(
                f"unknown configuration key {dotted!r}; known keys: "
                + ", ".join(sorted(USER_CONFIG_TYPES))
            )
        default = _lookup(USER_CONFIG_DEFAULT, dotted.split("."))
        _assign(self.data, dotted.split("."), default)
        self.save()
        return default

    # --- typed accessors --------------------------------------------------
    @property
    def actor(self) -> str:
        return str(self.get("identity.actor") or "")

    @property
    def ncbi_email(self) -> str:
        return str(self.get("ncbi.email") or "")

    @property
    def splash(self) -> str:
        return str(self.get("ui.splash") or "auto")


_user_config = UserConfig()


def user_config() -> UserConfig:
    """The process-wide user configuration (loaded on first access)."""
    return _user_config


def reset_user_config() -> None:
    """Drop the cached document (tests; after an external file change)."""
    _user_config.data = None


def _resolve_actor_with_source(
        environ: Mapping[str, str] | None, config: UserConfig | None
) -> tuple[str | None, str]:
    env = _environ(environ)
    for variable in ACTOR_ENV_VARS:
        value = (env.get(variable) or "").strip()
        if value:
            return value, f"environment ({variable})"
    try:
        value = (getpass.getuser() or "").strip()
    except (KeyError, OSError):  # no password entry and no environment
        value = ""
    if value:
        return value, "the local account"
    value = ((config or user_config()).actor or "").strip()
    if value:
        return value, f"user configuration ({user_config_path(environ)})"
    return None, "unset"


def resolve_actor(explicit: str | None = None, *,
                  environ: Mapping[str, str] | None = None,
                  config: UserConfig | None = None) -> str | None:
    """The audit actor of an operation.

    ``--actor`` > ``OPERON_ACTOR`` > ``USER`` > ``LOGNAME`` > ``USERNAME`` >
    the local account (``getpass``) > ``identity.actor`` in the user
    configuration.  ``None`` means no identity is available; callers that
    require an actor must reject the operation instead of writing NULL.
    """
    candidate = (explicit or "").strip()
    if candidate:
        return candidate
    return _resolve_actor_with_source(environ, config)[0]


def resolve_ncbi_email(explicit: str | None = None, *,
                       environ: Mapping[str, str] | None = None,
                       config: UserConfig | None = None) -> str | None:
    """The NCBI contact address: ``--email`` > ``NCBI_EMAIL`` > ``ncbi.email``."""
    candidate = (explicit or "").strip()
    if candidate:
        return candidate
    candidate = (_environ(environ).get("NCBI_EMAIL") or "").strip()
    if candidate:
        return candidate
    return ((config or user_config()).ncbi_email or "").strip() or None


def resolve_splash(explicit: str | None = None, *,
                   environ: Mapping[str, str] | None = None,
                   config: UserConfig | None = None) -> str:
    """The terminal-graphics override: ``OPERON_SPLASH`` > ``ui.splash`` > ``auto``.

    ``auto`` leaves capability detection in ``operon.tui.splash_terminal`` in
    charge.  A malformed user configuration degrades to ``auto``: this value
    is cosmetic and must never keep the UI from starting.
    """
    choices = USER_CONFIG_CHOICES["ui.splash"]
    candidate = (explicit or _environ(environ).get("OPERON_SPLASH") or "").strip().lower()
    if candidate in choices:
        return candidate
    try:
        stored = ((config or user_config()).splash or "").strip().lower()
    except ConfigError:
        return "auto"
    return stored if stored in choices else "auto"


def effective_config(*, environ: Mapping[str, str] | None = None,
                     config: UserConfig | None = None) -> dict[str, dict[str, Any]]:
    """Every user-level setting with the value that wins and where it comes from."""
    env = _environ(environ)
    resolved = config or user_config()
    actor, actor_source = _resolve_actor_with_source(env, resolved)
    email = (env.get("NCBI_EMAIL") or "").strip()
    if email:
        email_row = {"value": email, "source": "environment (NCBI_EMAIL)"}
    else:
        stored_email = (resolved.ncbi_email or "").strip() or None
        email_row = {
            "value": stored_email,
            "source": f"user configuration ({user_config_path(env)})"
            if stored_email else "unset",
        }
    requested = (env.get("OPERON_SPLASH") or "").strip().lower()
    splash = resolve_splash(environ=env, config=resolved)
    if requested in USER_CONFIG_CHOICES["ui.splash"]:
        splash_source = "environment (OPERON_SPLASH)"
    elif splash == "auto":
        splash_source = "terminal detection"
    else:
        splash_source = f"user configuration ({user_config_path(env)})"
    return {
        "identity.actor": {"value": actor, "source": actor_source},
        "ncbi.email": email_row,
        "ui.splash": {"value": splash, "source": splash_source},
    }


def has_secret_like_keys(data: Mapping[str, Any]) -> list[str]:
    """Dotted keys that look like hand-written secret material."""
    return [key for key in flatten_user_config(data) if _SECRET_LIKE_KEY_RE.search(key)]
