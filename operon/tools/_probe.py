"""Tool launch commands, version probing and command provenance.

Also holds the shared TTL cache (:func:`_identity_cache_get`) behind the
version and database-identity probes: a process-lifetime cache is a
registered hazard for long-lived processes such as the TUI, so entries
expire after ``_IDENTITY_CACHE_TTL_SECONDS``."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from operon.errors import ExternalToolError, ValidationError

from ._config import Recipe, ToolSpec, launcher_prefix, tool_command

_IDENTITY_CACHE_TTL_SECONDS = 300


_VERSION_CACHE: dict[str, tuple[float, tuple[str, str]]] = {}


def _identity_cache_get(cache: dict[str, tuple[float, Any]], key: str) -> Any | None:
    """Return a live cache entry, evicting entries older than the TTL."""
    entry = cache.get(key)
    if entry is None:
        return None
    timestamp, value = entry
    if time.monotonic() - timestamp > _IDENTITY_CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    return value


def _detect_version_record(
    command: list[str],
    pattern: str,
    label: str,
    timeout: float = 120.0,
    executor: Any = None,
) -> tuple[str, str]:
    """Run a version command and return parsed and raw provenance values."""
    executor_identity = (
        executor.cache_identity()
        if executor is not None and hasattr(executor, "cache_identity")
        else (executor.describe() if executor is not None else "local")
    )
    cache_key = json.dumps(
        {"command": command, "pattern": pattern, "executor": executor_identity},
        sort_keys=True,
    )
    cached = _identity_cache_get(_VERSION_CACHE, cache_key)
    if cached is not None:
        return cached

    def _store(value: tuple[str, str]) -> tuple[str, str]:
        _VERSION_CACHE[cache_key] = (time.monotonic(), value)
        return value

    if executor is not None and executor.name != "local":
        combined = _version_output_via_executor(executor, command, timeout)
    else:
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError as exc:
            raise ExternalToolError(
                f"cannot launch {label}: {exc}; check config/tools.yaml launch and version settings"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ExternalToolError(
                f"{label} version detection timed out after {timeout}s"
            ) from exc
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if pattern:
        match = re.search(pattern, combined, flags=re.IGNORECASE)
        if match:
            return _store((match.group(1).strip(), combined.strip()[:4000]))
    for line in combined.splitlines():
        line = line.strip()
        if line:
            # Fallback: first plausible version-like token on the first line.
            m = re.search(r"([0-9]+(?:\.[0-9]+){1,}[^\s]*)", line)
            if m:
                return _store((m.group(1), combined.strip()[:4000]))
            return _store((line[:200], combined.strip()[:4000]))
    raise ExternalToolError(
        f"could not determine version of {label} (command: {' '.join(command)}); "
        f"set 'version_pattern' in config/tools.yaml"
    )


def detect_tool_version_record(
    tool: ToolSpec, config: dict[str, Any], timeout: float = 120.0, executor: Any = None
) -> tuple[str, str]:
    """Run a tool's version_args; return parsed and raw provenance values."""
    if not tool.version_args:
        return "unknown", "version_args not configured"
    command = [*tool_command(tool, config), *tool.version_args]
    return _detect_version_record(
        command,
        tool.version_pattern,
        tool.name,
        timeout=timeout,
        executor=executor,
    )


def recipe_version_probe_command(
    recipe: Recipe,
    tool: ToolSpec,
    config: dict[str, Any],
    rendered_commands: list[list[str]],
) -> tuple[list[str], str, str] | None:
    """Version probe for the recipe's logical owner (the first chain command).

    Returns ``(command, pattern, label)`` when the first command block declares
    its own ``version_args``; otherwise ``None`` and the caller falls back to
    the tool-level probe, which recipe validation guarantees targets the first
    command's program.
    """
    if not rendered_commands or recipe.commands[0].version_args is None:
        return None
    first = recipe.commands[0]
    executable = rendered_commands[0][0]
    return (
        [*launcher_prefix(tool, config), executable, *first.version_args],
        first.version_pattern,
        executable,
    )


def command_step_provenance(
    recipe: Recipe,
    tool: ToolSpec,
    config: dict[str, Any],
    rendered_commands: list[list[str]],
    tool_version: str,
    tool_version_raw: str,
    *,
    executor: Any = None,
    dry_run: bool = False,
    timeout: float = 120.0,
) -> list[dict[str, Any]]:
    """Collect program identity and version provenance for a command chain.

    A step invoking the parent tool executable inherits its already-probed
    version. Other programs are probed only when their command block declares
    ``version_args``; no version flag is guessed. Every probed step version
    also enters the analysis cache fingerprint via ``parameter_fingerprint``.
    """
    if len(recipe.commands) != len(rendered_commands):
        raise ValidationError(
            f"{recipe.name}: rendered command count does not match recipe"
        )
    provenance: list[dict[str, Any]] = []
    launcher = launcher_prefix(tool, config)
    skip_remote_probe = (
        dry_run
        and executor is not None
        and getattr(executor, "name", "local") != "local"
    )
    for command_spec, rendered in zip(recipe.commands, rendered_commands):
        executable = rendered[0]
        if command_spec.version_args is not None:
            source = "command"
            version_command = [*launcher, executable, *command_spec.version_args]
            if skip_remote_probe:
                version = f"not probed (backend={executor.describe()})"
                version_raw = ""
            else:
                try:
                    version, version_raw = _detect_version_record(
                        version_command,
                        command_spec.version_pattern,
                        executable,
                        timeout=timeout,
                        executor=executor,
                    )
                except ExternalToolError as exc:
                    if not dry_run:
                        raise
                    version = f"unavailable ({exc})"
                    version_raw = str(exc)
        elif executable == tool.executable:
            source = "tool"
            version = tool_version
            version_raw = tool_version_raw
            version_command = (
                [*tool_command(tool, config), *tool.version_args]
                if tool.version_args
                else None
            )
        else:
            source = "unconfigured"
            version = "unknown"
            version_raw = "version_args not configured for command"
            version_command = None
        provenance.append(
            {
                "executable": executable,
                "tool_version": version,
                "tool_version_raw": version_raw,
                "version_source": source,
                "version_command": version_command,
            }
        )
    return provenance


def _version_output_via_executor(
    executor: Any, command: list[str], timeout: float
) -> str:
    """Capture version output through a non-local execution backend."""
    temp_parent = getattr(getattr(executor, "project", None), "logs_root", None)
    if temp_parent is not None:
        Path(temp_parent).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="operon-version-", dir=temp_parent
    ) as tmpdir:
        stdout_path = Path(tmpdir) / "stdout.log"
        stderr_path = Path(tmpdir) / "stderr.log"
        result = executor.run(
            command,
            cwd=None,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout=timeout,
        )
        if result.exit_code != 0:
            raise ExternalToolError(
                f"version detection via {executor.describe()} failed: "
                f"{result.error or f'exit code {result.exit_code}'}"
            )
        return (
            stdout_path.read_text(encoding="utf-8", errors="replace")
            + "\n"
            + stderr_path.read_text(encoding="utf-8", errors="replace")
        )


def detect_tool_version(
    tool: ToolSpec, config: dict[str, Any], timeout: float = 120.0
) -> str:
    """Run version_args and extract the version with the configured regex."""
    return detect_tool_version_record(tool, config, timeout=timeout)[0]
