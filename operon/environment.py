"""Execution-environment capture and content-addressed fingerprinting.

The legacy in-process and shell probes remain available as fallback. Rich
compute-side probes in ``environment_capture`` run through supported tool
launchers and are decoded here. Documents are deduplicated by fingerprint
in ``execution_environments``; identical captures share an ``environment_id``.
Capture-time redaction hashes hostnames and collapses home-directory
prefixes, so identifying values never reach the database.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
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
# scripts and joinable with ``;`` for a single SSH command.  ``home`` is a
# transient redaction aid: it is consumed by :func:`parse_probe_output` and
# never persisted.
PROBE_SHELL_LINES: list[str] = [
    "printf '%s=%s\\n' hostname \"$(hostname)\"",
    "printf '%s=%s\\n' home \"$HOME\"",
    "printf '%s=%s\\n' os \"$(uname -s)\"",
    "printf '%s=%s\\n' os_release \"$(uname -r)\"",
    "printf '%s=%s\\n' machine \"$(uname -m)\"",
    "printf '%s=%s\\n' dockerenv \"$(test -f /.dockerenv && echo 1)\"",
    *[
        f"printf '%s=%s\\n' {name.lower()} \"${{{name}:-}}\""
        for name in PROBE_ENV_VARS
    ],
]


def _hash_hostname(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _redact_home(value: str, home: str) -> str:
    """Replace whole-segment occurrences of the home directory with ``~``."""
    if not home or home == "/":
        return value
    return re.sub(
        r"(?<![^\s:=])" + re.escape(home) + r"(?=[/\s:=]|$)",
        "~",
        value,
    )


def _redact(document: dict[str, Any], home: str) -> dict[str, Any]:
    """Strip identifying values from a document before any fingerprinting.

    Hostnames are hashed (comparable, not readable) and home-directory
    prefixes collapse to ``~`` so identical setups on different hosts or
    accounts still deduplicate.  Fields without paths (environment names,
    container markers) are untouched.
    """
    hostname = document.get("hostname")
    if isinstance(hostname, str) and hostname:
        document["hostname"] = _hash_hostname(hostname)
    for key, value in document.items():
        if key != "hostname" and isinstance(value, str):
            redacted = _redact_home(value, home)
            if redacted != value:
                document[key] = redacted
    return document


def _local_home() -> str:
    try:
        return str(Path.home())
    except RuntimeError:  # home directory cannot be resolved
        return os.environ.get("HOME", "")


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
    return _redact(env, _local_home())


def parse_probe_output(text: str) -> dict[str, Any]:
    """Parse probe ``key=value`` output into an environment document.

    Keys mirror :func:`local_environment`; empty values are treated as
    missing.  Remote probes cannot report the Python/operon versions of the
    controller, so those fields are absent here by design.  The transient
    ``home`` key only drives redaction and is never part of the document.
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
    home = env.pop("home", "")
    if "capture_schema" in env:
        from operon.environment_capture import enrich_document
        return enrich_document(_redact(env, home), text)
    return _redact(env, home)


def environment_fingerprint(env: dict[str, Any]) -> str:
    """Content address of an environment document (canonical-JSON sha256)."""
    canonical = json.dumps(env, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def relevance_fingerprint(document: dict[str, Any]) -> str | None:
    """Fingerprint of the environment aspects relevant to rerunning a tool.

    Combines the sub-fingerprints present in the document
    (``system_fingerprint``, ``hardware_fingerprint`` and the conda
    ``package_fingerprint``); the set of present keys participates so a
    capture without hardware data never collides with one that has it.
    Transient sections (hostname, affinity, runtime settings) are excluded by
    construction.  Legacy documents without any sub-fingerprint are not
    comparable and yield ``None``.
    """
    if not isinstance(document, dict):
        return None
    parts: dict[str, str] = {}
    for key in ("system_fingerprint", "hardware_fingerprint"):
        value = document.get(key)
        if isinstance(value, str) and value:
            parts[key] = value
    conda = document.get("conda")
    if isinstance(conda, dict):
        value = conda.get("package_fingerprint")
        if isinstance(value, str) and value:
            parts["conda.package_fingerprint"] = value
    if not parts:
        return None
    canonical = json.dumps(parts, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def environment_summary(document: dict[str, Any]) -> str:
    """Render a single-line human summary of an environment document.

    Segments whose fields are missing are omitted; partial or legacy
    documents never raise.
    """
    parts: list[str] = []
    system = document.get("system")
    distribution = system.get("distribution") if isinstance(system, dict) else None
    pretty = distribution.get("pretty_name") if isinstance(distribution, dict) else None
    if pretty:
        parts.append(str(pretty))
    else:
        source = system if isinstance(system, dict) else document
        os_bits = " ".join(str(source[key]) for key in ("os", "os_release") if source.get(key))
        if os_bits:
            parts.append(os_bits)
    hardware = document.get("hardware")
    if isinstance(hardware, dict):
        cpu = hardware.get("cpu")
        if isinstance(cpu, list):
            model = next(
                (entry for entry in cpu
                 if isinstance(entry, str) and entry.lstrip().startswith("model name")),
                "",
            )
            if model:
                parts.append(" ".join(model.partition(":")[2].split()))
        if hardware.get("memory_total"):
            parts.append(str(hardware["memory_total"]))
        gpus = [entry for entry in hardware.get("nvidia_gpus") or [] if entry]
        if gpus:
            fields = [field.strip() for field in str(gpus[0]).split(",")]
            label = " ".join(fields[0].split())
            if len(fields) > 1 and fields[1]:
                label += f" (driver {fields[1]})"
            if len(gpus) > 1:
                label = f"{len(gpus)}x {label}"
            parts.append(label)
    conda = document.get("conda")
    if isinstance(conda, dict) and conda.get("status") in ("captured", "partial"):
        packages = conda.get("packages") or []
        env_name = document.get("conda_default_env")
        label = f"conda: {env_name}" if env_name else "conda"
        parts.append(f"{label} ({len(packages)} packages)")
    status = document.get("capture_status")
    if status and status != "complete":
        parts.append(f"capture: {status}")
    return "; ".join(parts)
