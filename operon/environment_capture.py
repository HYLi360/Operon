"""Compute-side, Python-independent probes and portable Conda reconstruction.

Only selected metadata is persisted. Package URLs are stripped of credentials;
installed files are not asserted to match their original package archives.
"""
from __future__ import annotations

import base64
import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from operon.environment import PROBE_SHELL_LINES, environment_fingerprint
from operon.errors import ValidationError


def _encoded(key: str, command: str) -> str:
    return f"printf '{key}='; {{ {command}; }} 2>/dev/null | base64 | tr -d '\\n'; printf '\\n'"


# No Python (or package-manager executable) is required inside the tool env.
CAPTURE_LINES = [
    *PROBE_SHELL_LINES,
    "printf 'capture_schema=1\\n'",
    _encoded("distribution", "cat /etc/os-release"),
    _encoded("cpu", "LC_ALL=C awk -F: '/^(vendor_id|model name|flags|Features|CPU implementer|CPU architecture|CPU part)[ \\t]*:/ {print}' /proc/cpuinfo | LC_ALL=C sort -u"),
    _encoded("libc", "getconf GNU_LIBC_VERSION"),
    _encoded("affinity", "sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/self/status"),
    _encoded("memory", "sed -n 's/^MemTotal:[[:space:]]*//p' /proc/meminfo"),
    _encoded("gpu", "if command -v nvidia-smi >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then timeout 5 nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv,noheader; fi"),
    *[_encoded("setting_" + name, f'printf %s "${{{name}:-}}"') for name in (
        "LANG", "LC_ALL", "LC_COLLATE", "LC_NUMERIC", "TZ", "OMP_NUM_THREADS",
        "OMP_DYNAMIC", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "CUDA_VISIBLE_DEVICES",
    )],
    "if [ -n \"${CONDA_PREFIX:-}\" ] && [ -d \"$CONDA_PREFIX/conda-meta\" ]; then "
    "printf 'conda_present=1\\n'; "
    "for operon_meta in \"$CONDA_PREFIX\"/conda-meta/*.json; do "
    "[ -f \"$operon_meta\" ] || continue; "
    + _encoded("package", 'cat "$operon_meta"') + "; done; "
    # Mark pip/local modifications as outside the explicit Conda contract.
    "for operon_installer in \"$CONDA_PREFIX\"/lib/python*/site-packages/*.dist-info/INSTALLER; do "
    "[ -f \"$operon_installer\" ] || continue; "
    "if [ \"$(cat \"$operon_installer\")\" = pip ]; then "
    + _encoded("pip_distribution", 'basename "$(dirname "$operon_installer")"') + "; fi; done; "
    "else printf 'conda_present=0\\n'; fi",
    "printf 'capture_complete=1\\n'",
]


def probe_command(argv: list[str]) -> list[str] | None:
    """Select the same supported launcher without executing the payload.

    Unknown manager flags/wrappers are deliberately not guessed. Direct commands
    inherit the executor environment; opaque shell/container commands are marked
    unsupported rather than attributed to that outer environment.
    """
    prefix: list[str] = []
    if argv and Path(argv[0]).name in {"conda", "mamba", "micromamba"}:
        index = 1
        while index < len(argv):
            token = argv[index]
            if token == "run":
                index += 1
                break
            if token in {"-r", "--root-prefix"} and index + 1 < len(argv):
                index += 2
            else:
                return None
        else:
            return None
        while index < len(argv):
            token = argv[index]
            if token in {"-n", "--name", "-p", "--prefix", "--cwd", "-r", "--root-prefix"}:
                if index + 1 >= len(argv):
                    return None
                index += 2
            elif token in {"--no-capture-output", "--live-stream", "--no-rc", "--no-env"}:
                index += 1
            elif any(token.startswith(option + "=") for option in (
                "--name", "--prefix", "--cwd", "--root-prefix",
            )):
                index += 1
            elif token == "--":
                index += 1
                break
            elif token.startswith("-"):
                return None
            else:
                break
        if index >= len(argv):
            return None
        prefix = argv[:index]
    elif argv and Path(argv[0]).name in {
        "sh", "bash", "zsh", "env", "docker", "podman", "singularity", "apptainer",
    }:
        return None
    return [*prefix, "sh", "-c", "\n".join(CAPTURE_LINES)]


def probe_shell(argv: list[str]) -> str:
    command = probe_command(argv)
    if command is None:
        command = ["sh", "-c", "\n".join(CAPTURE_LINES) + "\nprintf 'capture_unsupported=1\\n'"]
    # Bound the complete probe, including activation hooks. Without timeout,
    # report unavailable rather than risking a stuck scheduler job.
    return ("if command -v timeout >/dev/null 2>&1; then timeout 30 "
            + shlex.join(command)
            + "; else printf 'capture_schema=1\\ncapture_unavailable=1\\n'; fi")


def capture_local(argv: list[str], cwd: str | Path | None = None) -> dict[str, Any]:
    from operon.environment import parse_probe_output
    try:
        proc = subprocess.run(["sh", "-c", probe_shell(argv)], cwd=cwd,
                              capture_output=True, text=True, timeout=35)
        document = parse_probe_output(proc.stdout)
        if proc.returncode or not document:
            return {"capture_schema": 1, "capture_status": "failed",
                    "reason": "probe failed or timed out", "probe_exit_code": proc.returncode}
        return document
    except (OSError, subprocess.TimeoutExpired):
        return {"capture_schema": 1, "capture_status": "failed",
                "reason": "probe unavailable or timed out"}


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https", "file"}:
        return ""
    host = parts.netloc.rsplit("@", 1)[-1]
    # Conda token authentication convention: /t/<token>/...
    path = re.sub(r"/t/[^/]+/", "/", parts.path)
    return urlunsplit((parts.scheme, host, path, "", ""))


def enrich_document(env: dict[str, Any], text: str) -> dict[str, Any]:
    """Decode selected probe records; malformed/partial captures stay explicit."""
    decoded: dict[str, list[str]] = {}
    errors: list[str] = []
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if key not in {"distribution", "cpu", "libc", "affinity", "memory", "gpu",
                       "package", "pip_distribution"} and not key.startswith("setting_"):
            continue
        env.pop(key, None)
        if separator:
            try:
                decoded.setdefault(key, []).append(base64.b64decode(value, validate=True).decode("utf-8"))
            except (ValueError, UnicodeError):
                errors.append(f"invalid {key} record")
    def first(key: str) -> str:
        return next(iter(decoded.get(key, [])), "").strip()
    unsupported = bool(env.pop("capture_unsupported", None))
    if env.pop("capture_unavailable", None):
        return {"capture_schema": 1, "capture_status": "unavailable", "reason": "timeout utility unavailable"}
    complete = env.pop("capture_complete", None) == "1"
    present = env.pop("conda_present", None) == "1"
    env["capture_schema"] = 1
    distribution = {}
    for line in first("distribution").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"ID", "VERSION_ID", "PRETTY_NAME"}:
            distribution[key.lower()] = value.strip('"')
    system = {key: env[key] for key in ("os", "os_release", "machine") if key in env}
    system.update(distribution=distribution, libc=first("libc"))
    hardware = {"cpu": sorted(set(first("cpu").splitlines())),
                "memory_total": first("memory"), "nvidia_gpus": sorted(first("gpu").splitlines())}
    env["system"] = system
    env["hardware"] = hardware
    env["system_fingerprint"] = environment_fingerprint(system)
    env["hardware_fingerprint"] = environment_fingerprint(hardware)
    env["runtime_settings"] = {key.removeprefix("setting_"): values[0] for key, values in decoded.items()
                               if key.startswith("setting_") and values and values[0]}
    env["cpu_affinity"] = first("affinity")
    env["hardware_capture"] = {"cpu": "captured" if hardware["cpu"] else "unavailable",
                               "gpu": "captured" if hardware["nvidia_gpus"] else "unavailable_or_absent"}
    if present and not unsupported:
        packages = []
        for raw in decoded.get("package", []):
            try:
                record = json.loads(raw)
                if not isinstance(record, dict) or not all(record.get(k) for k in ("name", "version", "build")):
                    raise ValueError("missing identity")
                package = {key: record[key] for key in (
                    "name", "version", "build", "build_number", "subdir", "sha256", "md5", "depends",
                ) if key in record}
                package["url"] = _safe_url(str(record.get("url", "")))
                packages.append(package)
            except (ValueError, TypeError):
                errors.append("invalid Conda package record")
        packages.sort(key=lambda record: (record["name"], record["version"], record["build"]))
        explicit = []
        missing = []
        for package in packages:
            checksum = next((str(package[key]) for key, length in (("sha256", 64), ("md5", 32))
                             if re.fullmatch(r"[0-9a-fA-F]{%d}" % length, str(package.get(key, "")))), "")
            if package["url"] and checksum:
                explicit.append(package["url"] + "#" + checksum.lower())
            else:
                missing.append(package["name"])
        conda = {"status": "captured" if complete and not errors and not missing and packages else "partial",
                 "packages": packages, "package_fingerprint": environment_fingerprint(packages),
                 "missing_artifacts": missing,
                 "pip_distributions": sorted({value.strip() for value in decoded.get("pip_distribution", [])}),
                 "scope": "Conda package artifacts only; pip/local edits and activation scripts are not restored"}
        if conda["status"] == "captured":
            conda["explicit"] = "@EXPLICIT\n" + "\n".join(explicit) + "\n"
        env["conda"] = conda
    else:
        env["conda"] = {"status": "not_detected" if complete else "unknown"}
    env["capture_status"] = "complete" if complete and not errors else "partial"
    if errors:
        env["capture_errors"] = sorted(set(errors))
    if unsupported:
        env["capture_status"] = "unsupported_launcher"
        env["capture_scope"] = "executor_only"
        env["conda"] = {"status": "unknown"}
    else:
        env["capture_scope"] = "tool_launch_context"
    return env


def export_conda(document: dict[str, Any], fmt: str = "explicit") -> str:
    """Export stored metadata only; never inspect or mutate a live environment."""
    conda = document.get("conda", {})
    if fmt == "explicit":
        if not conda.get("explicit"):
            raise ValidationError("snapshot has no complete Conda explicit specification")
        return conda["explicit"]
    if fmt != "yaml":
        raise ValidationError(f"unknown environment format: {fmt}")
    if conda.get("status") != "captured":
        raise ValidationError("snapshot has no complete Conda package inventory")
    import yaml
    channels = sorted({item["url"].rsplit("/", 2)[0] for item in conda["packages"]})
    return yaml.safe_dump({"name": "operon-restored", "channels": channels,
                           "dependencies": [f"{item['name']}={item['version']}={item['build']}"
                                            for item in conda["packages"]]}, sort_keys=False)
