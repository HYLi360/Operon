"""Project configuration and directory layout.

Code/config/metadata/data separation is a core design principle: code says
what to do, project.yaml says how to do it for this project.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from operon.errors import ConfigError
from operon.schema import default_schemas

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
            self.analysis_root,
            self.reports_root,
            self.logs_root,
            self.releases_root,
            self.taxonomy_reference_sets_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def init(cls, root: str | Path, project_id: str = "PRJ_000001", name: str = "") -> "Project":
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
        from operon.metadata_files import create_empty_metadata_files
        create_empty_metadata_files(project.metadata_dir)
        from operon.tools import ensure_tools_config
        ensure_tools_config(project)
        return project

    @classmethod
    def find(cls, start: str | Path = ".") -> "Project":
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
