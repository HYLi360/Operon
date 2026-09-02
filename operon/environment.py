"""Execution-environment capture and content-addressed fingerprinting.

Every external run records the environment it executed in: locally this is
collected in-process, on Slurm/SSH a small POSIX shell probe runs on the
compute side and its ``key=value`` output is parsed back into the same
document shape.  Documents are deduplicated by their fingerprint in the
``execution_environments`` table, so repeated runs on one machine share a
single ``environment_id``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
from pathlib import Path
from typing import Any

from operon import __version__

# Environment variables worth capturing when set; absent or empty variables
# are omitted from the document rather than stored as empty strings.
PROBE_ENV_VARS = (
    "PATH",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "VIRTUAL_ENV",
    "SINGULARITY_NAME",
    "APPTAINER_NAME",
    "container",
)

# Portable POSIX shell lines emitting ``key=value`` rows for the fields of a
# remote environment document.  They must stay embeddable verbatim in sbatch
# scripts and joinable with ``;`` for a single SSH command.
PROBE_SHELL_LINES: list[str] = [
    "printf '%s=%s\\n' hostname \"$(hostname)\"",
    "printf '%s=%s\\n' os \"$(uname -s)\"",
    "printf '%s=%s\\n' os_release \"$(uname -r)\"",
    "printf '%s=%s\\n' machine \"$(uname -m)\"",
    "printf '%s=%s\\n' dockerenv \"$(test -f /.dockerenv && echo 1)\"",
    *[
        f"printf '%s=%s\\n' {name.lower()} \"${{{name}:-}}\""
        for name in PROBE_ENV_VARS
    ],
]


def local_environment() -> dict[str, Any]:
    """Collect the local execution environment document."""
    env: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "operon_version": __version__,
        "dockerenv": Path("/.dockerenv").exists(),
    }
    for name in PROBE_ENV_VARS:
        value = os.environ.get(name)
        if value:
            env[name.lower()] = value
    return env


def parse_probe_output(text: str) -> dict[str, Any]:
    """Parse probe ``key=value`` output into an environment document.

    Keys mirror :func:`local_environment`; empty values are treated as
    missing.  Remote probes cannot report the Python/operon versions of the
    controller, so those fields are absent here by design.
    """
    env: dict[str, Any] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or not value:
            continue
        if key == "dockerenv":
            env["dockerenv"] = value.strip() in ("1", "true", "yes")
        else:
            env[key] = value
    return env


def environment_fingerprint(env: dict[str, Any]) -> str:
    """Content address of an environment document (canonical-JSON sha256)."""
    canonical = json.dumps(env, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
