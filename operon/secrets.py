"""User-scoped secret storage: keyring, systemd credentials, macOS Keychain.

Operon keeps secrets out of ``config.yml``: API tokens live in the desktop
keyring (``secret-tool``), in user-scoped ``systemd-creds`` ciphertext, or in
the macOS Keychain (``security``).  Environment variables stay supported and
keep precedence over every backend; they are the transient injection path
(CI, HPC job scripts, ``--api-key``), never a storage mechanism.

Backend selection is automatic, in this order:

1. ``secret-tool`` -- libsecret/Secret Service (GNOME Keyring, KWallet)
2. ``systemd-creds --user`` -- systemd 256+; the ciphertext is bound to the
   machine-id and the calling user, and is stored next to the user
   configuration under ``secrets/``
3. ``/usr/bin/security`` -- the macOS Keychain

The keyring and ``systemd-creds --user`` bind the value to the local account
and machine: ciphertext copied to another host, or decrypted by another user,
fails by design.  Runs on remote hosts (SSH/Slurm) must receive the secret
through the environment variable or ``--api-key`` instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from operon.config import user_config_dir
from operon.errors import SecretError

SECRET_NAMES: tuple[str, ...] = ("ncbi.api_key",)
"""Canonical secret names; ``operon config secret`` accepts exactly these."""

SECRET_ENV_VARS: dict[str, str] = {"ncbi.api_key": "NCBI_API_KEY"}
"""Environment variables that override a stored secret (environment wins)."""

SERVICE_NAME = "operon"
_TIMEOUT_SECONDS = 30


# --- process helpers ---------------------------------------------------------


def _message(result: subprocess.CompletedProcess | subprocess.CalledProcessError) -> str:
    """First non-empty stderr (or stdout) line of a failed command."""
    for stream in (result.stderr, result.stdout):
        if not stream:
            continue
        text = stream.decode("utf-8", "replace").strip()
        if text:
            return text.splitlines()[0]
    return f"exit code {result.returncode}"


def _run(command: list[str], *, stdin: bytes | None = None, check: bool = False
         ) -> subprocess.CompletedProcess:
    """Run a backend helper, mapping every process failure to ``SecretError``."""
    try:
        return subprocess.run(
            command, input=stdin, capture_output=True, timeout=_TIMEOUT_SECONDS, check=check,
        )
    except FileNotFoundError as exc:
        raise SecretError(f"{command[0]}: not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise SecretError(f"{command[0]}: timed out after {_TIMEOUT_SECONDS}s") from exc
    except subprocess.CalledProcessError as exc:
        raise SecretError(f"{command[0]} failed: {_message(exc)}") from exc


# --- backends ----------------------------------------------------------------


class SecretBackend:
    """One user-scoped secret store.

    Subclasses implement the four operations; :func:`active_backend` picks the
    first available backend in detection order.
    """

    name = ""

    def available(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def get(self, secret_name: str) -> str | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def store(self, secret_name: str, value: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def clear(self, secret_name: str) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class SecretToolBackend(SecretBackend):
    """libsecret/Secret Service backend (``secret-tool``)."""

    name = "secret-tool"
    binary = "secret-tool"

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def get(self, secret_name: str) -> str | None:
        result = _run([self.binary, "lookup", "service", SERVICE_NAME, "key", secret_name])
        if result.returncode != 0:
            return None
        value = result.stdout.decode("utf-8", "replace")
        return value or None

    def store(self, secret_name: str, value: str) -> None:
        _run(
            [self.binary, "store", "--label", f"Operon {secret_name}",
             "service", SERVICE_NAME, "key", secret_name],
            stdin=value.encode("utf-8"), check=True,
        )

    def clear(self, secret_name: str) -> None:
        _run([self.binary, "clear", "service", SERVICE_NAME, "key", secret_name])


class SystemdCredsBackend(SecretBackend):
    """User-scoped ``systemd-creds`` ciphertext backend (systemd 256+)."""

    name = "systemd-creds"
    binary = "systemd-creds"

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def path(self, secret_name: str) -> Path:
        return self.directory / f"{secret_name}.cred"

    def credential_name(self, secret_name: str) -> str:
        """The ``--name`` recorded inside the ciphertext (required on decrypt)."""
        return f"operon-{secret_name}"

    def get(self, secret_name: str) -> str | None:
        path = self.path(secret_name)
        if not path.is_file():
            return None
        result = _run([
            self.binary, "decrypt", "--user", "--name", self.credential_name(secret_name),
            str(path), "-",
        ])
        if result.returncode != 0:
            raise SecretError(
                f"cannot decrypt {path}: {_message(result)}; user-scoped systemd "
                "credentials are bound to this machine and user -- re-create the "
                "secret here, or inject NCBI_API_KEY / pass --api-key"
            )
        value = result.stdout.decode("utf-8", "replace")
        return value or None

    def store(self, secret_name: str, value: str) -> None:
        path = self.path(secret_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        result = _run([
            self.binary, "encrypt", "--user", "--name", self.credential_name(secret_name),
            "-", str(path),
        ], stdin=value.encode("utf-8"))
        if result.returncode != 0:
            raise SecretError(f"systemd-creds encrypt failed: {_message(result)}")
        os.chmod(path, 0o600)

    def clear(self, secret_name: str) -> None:
        self.path(secret_name).unlink(missing_ok=True)


class MacKeychainBackend(SecretBackend):
    """macOS Keychain backend (``/usr/bin/security``)."""

    name = "keychain"
    binary = "/usr/bin/security"

    def available(self) -> bool:
        return sys.platform == "darwin" and Path(self.binary).exists()

    def get(self, secret_name: str) -> str | None:
        result = _run([
            self.binary, "find-generic-password", "-a", secret_name, "-s", SERVICE_NAME, "-w",
        ])
        if result.returncode != 0:
            return None
        value = result.stdout.decode("utf-8", "replace").strip("\n")
        return value or None

    def store(self, secret_name: str, value: str) -> None:
        # ``security`` accepts the value only as an argument, so it is briefly
        # visible to the same user through the process table.
        result = _run([
            self.binary, "add-generic-password", "-a", secret_name, "-s", SERVICE_NAME,
            "-w", value, "-U",
        ])
        if result.returncode != 0:
            raise SecretError(f"keychain store failed: {_message(result)}")

    def clear(self, secret_name: str) -> None:
        _run([self.binary, "delete-generic-password", "-a", secret_name, "-s", SERVICE_NAME])


def secret_directory(config_dir: Path | None = None) -> Path:
    """Directory holding ``systemd-creds`` ciphertext (one file per secret)."""
    return (Path(config_dir) if config_dir is not None else user_config_dir()) / "secrets"


def backends(*, config_dir: Path | None = None) -> list[SecretBackend]:
    """All known backends in detection order (available or not)."""
    return [
        SecretToolBackend(),
        SystemdCredsBackend(secret_directory(config_dir)),
        MacKeychainBackend(),
    ]


def active_backend(*, config_dir: Path | None = None) -> SecretBackend | None:
    """The first available backend, or ``None`` when the host has none."""
    for backend in backends(config_dir=config_dir):
        if backend.available():
            return backend
    return None


def require_backend(*, config_dir: Path | None = None) -> SecretBackend:
    """Return the active backend or raise the actionable "none available" error."""
    backend = active_backend(config_dir=config_dir)
    if backend is None:
        raise SecretError(
            "no user-scoped secret backend is available; install 'secret-tool' "
            "(libsecret-tools), use systemd >= 256 (systemd-creds), or use the macOS "
            "Keychain -- alternatively inject NCBI_API_KEY or pass --api-key"
        )
    return backend


def normalize_secret_name(name: str) -> str:
    """Validate one canonical secret name."""
    candidate = (name or "").strip()
    if candidate not in SECRET_NAMES:
        raise SecretError(
            f"unknown secret {name!r}; known secrets: {', '.join(SECRET_NAMES)}"
        )
    return candidate


# --- value access ------------------------------------------------------------


def read_secret(secret_name: str, *, config_dir: Path | None = None) -> str | None:
    """Read a stored secret; ``None`` when the backend holds no value."""
    backend = active_backend(config_dir=config_dir)
    if backend is None:
        return None
    return backend.get(normalize_secret_name(secret_name))


def store_secret(secret_name: str, value: str, *, config_dir: Path | None = None) -> str:
    """Store a secret in the active backend; returns the backend name."""
    if not value:
        raise SecretError("refusing to store an empty secret value")
    backend = require_backend(config_dir=config_dir)
    backend.store(normalize_secret_name(secret_name), value)
    return backend.name


def clear_secret(secret_name: str, *, config_dir: Path | None = None) -> bool:
    """Delete a stored secret; ``True`` when one was removed."""
    backend = active_backend(config_dir=config_dir)
    if backend is None:
        return False
    name = normalize_secret_name(secret_name)
    existed = backend.get(name) is not None
    backend.clear(name)
    return existed


def resolve_secret(secret_name: str, explicit: str | None = None, *,
                   environ: Mapping[str, str] | None = None,
                   config_dir: Path | None = None) -> str | None:
    """Resolve one secret: ``--flag`` > environment variable > stored value."""
    name = normalize_secret_name(secret_name)
    candidate = (explicit or "").strip()
    if candidate:
        return candidate
    env = os.environ if environ is None else environ  # env-audit: user-level secrets
    variable = SECRET_ENV_VARS.get(name, "")
    candidate = (env.get(variable) or "").strip() if variable else ""
    if candidate:
        return candidate
    return read_secret(name, config_dir=config_dir)


def secret_status(*, environ: Mapping[str, str] | None = None,
                  config_dir: Path | None = None) -> dict[str, Any]:
    """Backend availability and which secrets are set, never their values."""
    env = os.environ if environ is None else environ  # env-audit: secret status report
    active = active_backend(config_dir=config_dir)
    secrets_report = []
    for name in SECRET_NAMES:
        variable = SECRET_ENV_VARS.get(name, "")
        stored: bool | None = None
        if active is not None:
            stored = active.get(name) is not None
        secrets_report.append({
            "name": name,
            "env_var": variable,
            "env_set": bool((env.get(variable) or "").strip()) if variable else False,
            "stored": stored,
        })
    return {
        "active_backend": active.name if active is not None else None,
        "backends": [
            {"name": backend.name, "available": backend.available(),
             "active": active is not None and backend.name == active.name}
            for backend in backends(config_dir=config_dir)
        ],
        "secrets": secrets_report,
    }
