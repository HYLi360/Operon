"""Secret backends: detection order, precedence, permissions, failure modes."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from operon import secrets as secret_module
from operon.errors import SecretError
from operon.secrets import (
    MacKeychainBackend,
    SecretToolBackend,
    SystemdCredsBackend,
    active_backend,
    backends,
    clear_secret,
    normalize_secret_name,
    read_secret,
    require_backend,
    resolve_secret,
    secret_status,
    store_secret,
)

SECRET_TOOL_FAKE = """#!/bin/bash
set -eu
state="${FAKE_SECRET_STATE:?}"
command="$1"; shift
name=""
while [ $# -gt 0 ]; do
  case "$1" in
    --label) shift 2 ;;
    service) shift 2 ;;
    key) name="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$name" ] || exit 2
case "$command" in
  store) /usr/bin/mkdir -p "$state"; /usr/bin/cat > "$state/$name" ;;
  lookup) [ -f "$state/$name" ] && /usr/bin/cat "$state/$name" || exit 1 ;;
  clear) /usr/bin/rm -f "$state/$name" ;;
  *) exit 2 ;;
esac
"""

SYSTEMD_CREDS_FAKE = """#!/bin/bash
set -eu
command="$1"; shift
[ "$1" = "--user" ] || exit 2
shift
[ "$1" = "--name" ] || exit 2
name="$2"; shift 2
input="$1"; output="${2:-}"
if [ "${FAKE_CREDS_FAIL:-0}" = "1" ]; then echo "cannot decrypt: different machine" >&2; exit 1; fi
case "$command" in
  encrypt) /usr/bin/base64 -w0 > "$output" ;;
  decrypt) /usr/bin/base64 -d < "$input" ;;
  *) exit 2 ;;
esac
"""


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FAKE_SECRET_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    (tmp_path / "bin").mkdir()
    return tmp_path


@pytest.fixture
def with_secret_tool(scratch: Path, monkeypatch) -> Path:
    _write_executable(scratch / "bin" / "secret-tool", SECRET_TOOL_FAKE)
    for variable in ("NCBI_API_KEY",):
        monkeypatch.delenv(variable, raising=False)
    return scratch


@pytest.fixture
def with_systemd_creds(scratch: Path, monkeypatch) -> Path:
    _write_executable(scratch / "bin" / "systemd-creds", SYSTEMD_CREDS_FAKE)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    return scratch


# --- detection ---------------------------------------------------------------


def test_backend_order_and_detection(scratch: Path) -> None:
    assert [backend.name for backend in backends(config_dir=scratch)] == [
        "secret-tool", "systemd-creds", "keychain",
    ]
    assert active_backend(config_dir=scratch) is None  # nothing on PATH


def test_secret_tool_wins_when_both_are_present(with_secret_tool: Path) -> None:
    _write_executable(with_secret_tool / "bin" / "systemd-creds", SYSTEMD_CREDS_FAKE)
    assert active_backend(config_dir=with_secret_tool).name == "secret-tool"


def test_systemd_creds_used_when_secret_tool_is_absent(with_systemd_creds: Path) -> None:
    assert active_backend(config_dir=with_systemd_creds).name == "systemd-creds"


# --- secret-tool backend -----------------------------------------------------


def test_secret_tool_round_trip(with_secret_tool: Path) -> None:
    backend = SecretToolBackend()
    assert backend.get("ncbi.api_key") is None
    backend.store("ncbi.api_key", "s3cr3t")
    assert backend.get("ncbi.api_key") == "s3cr3t"
    backend.clear("ncbi.api_key")
    assert backend.get("ncbi.api_key") is None


def test_store_read_resolve_through_helpers(with_secret_tool: Path) -> None:
    assert read_secret("ncbi.api_key", config_dir=with_secret_tool) is None
    assert store_secret("ncbi.api_key", "stored-value", config_dir=with_secret_tool) == "secret-tool"
    assert read_secret("ncbi.api_key", config_dir=with_secret_tool) == "stored-value"
    assert resolve_secret("ncbi.api_key", config_dir=with_secret_tool) == "stored-value"
    assert clear_secret("ncbi.api_key", config_dir=with_secret_tool) is True
    assert clear_secret("ncbi.api_key", config_dir=with_secret_tool) is False
    assert read_secret("ncbi.api_key", config_dir=with_secret_tool) is None


def test_resolve_precedence_flag_then_environment_then_backend(
        with_secret_tool: Path, monkeypatch) -> None:
    store_secret("ncbi.api_key", "stored-value", config_dir=with_secret_tool)
    monkeypatch.setenv("NCBI_API_KEY", "env-value")
    assert resolve_secret("ncbi.api_key", "flag-value", config_dir=with_secret_tool) == "flag-value"
    assert resolve_secret("ncbi.api_key", None, config_dir=with_secret_tool) == "env-value"
    monkeypatch.delenv("NCBI_API_KEY")
    assert resolve_secret("ncbi.api_key", None, config_dir=with_secret_tool) == "stored-value"
    assert resolve_secret("ncbi.api_key", "  ", environ={}, config_dir=with_secret_tool) == "stored-value"


# --- systemd-creds backend ---------------------------------------------------


def test_systemd_creds_round_trip_and_permissions(with_systemd_creds: Path) -> None:
    config_dir = with_systemd_creds / "cfg"
    backend = SystemdCredsBackend(config_dir / "secrets")
    assert backend.get("ncbi.api_key") is None
    assert store_secret("ncbi.api_key", "systemd-value", config_dir=config_dir) == "systemd-creds"
    ciphertext = config_dir / "secrets" / "ncbi.api_key.cred"
    assert ciphertext.is_file()
    assert stat.S_IMODE(os.stat(ciphertext).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(ciphertext.parent).st_mode) == 0o700
    assert "systemd-value" not in ciphertext.read_text(encoding="utf-8")
    assert read_secret("ncbi.api_key", config_dir=config_dir) == "systemd-value"
    assert clear_secret("ncbi.api_key", config_dir=config_dir) is True


def test_systemd_creds_decrypt_failure_is_actionable(
        with_systemd_creds: Path, monkeypatch) -> None:
    config_dir = with_systemd_creds / "cfg"
    store_secret("ncbi.api_key", "systemd-value", config_dir=config_dir)
    monkeypatch.setenv("FAKE_CREDS_FAIL", "1")
    with pytest.raises(SecretError, match="bound to this machine and user"):
        read_secret("ncbi.api_key", config_dir=config_dir)


# --- macOS keychain backend --------------------------------------------------


def test_mac_keychain_round_trip(scratch: Path, monkeypatch) -> None:
    log = scratch / "keychain.log"

    def _run(command, *, stdin=None, check=False):
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(" ".join(command) + "\n")
        if command[1] == "find-generic-password":
            return secret_module.subprocess.CompletedProcess(command, 1, b"", b"not found")
        if command[1] == "delete-generic-password":
            return secret_module.subprocess.CompletedProcess(command, 0, b"", b"")
        return secret_module.subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(secret_module, "_run", _run)
    backend = MacKeychainBackend()
    assert backend.get("ncbi.api_key") is None
    backend.store("ncbi.api_key", "keychain-value")
    backend.clear("ncbi.api_key")
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "/usr/bin/security find-generic-password -a ncbi.api_key -s operon -w"
    assert lines[1] == (
        "/usr/bin/security add-generic-password -a ncbi.api_key -s operon -w keychain-value -U"
    )
    assert lines[2] == "/usr/bin/security delete-generic-password -a ncbi.api_key -s operon"


def test_mac_backend_is_darwin_only() -> None:
    backend = MacKeychainBackend()
    assert backend.available() is (sys.platform == "darwin")


# --- failure modes and reporting --------------------------------------------


def test_no_backend_is_an_actionable_error(scratch: Path) -> None:
    with pytest.raises(SecretError, match="no user-scoped secret backend is available"):
        require_backend(config_dir=scratch)
    with pytest.raises(SecretError, match="NCBI_API_KEY"):
        store_secret("ncbi.api_key", "value", config_dir=scratch)
    # Reading degrades to "nothing stored" instead of failing every command.
    assert read_secret("ncbi.api_key", config_dir=scratch) is None
    assert resolve_secret("ncbi.api_key", environ={}, config_dir=scratch) is None
    assert resolve_secret("ncbi.api_key", "flag-value", environ={}, config_dir=scratch) == "flag-value"


def test_empty_value_is_refused(with_secret_tool: Path) -> None:
    with pytest.raises(SecretError, match="empty secret value"):
        store_secret("ncbi.api_key", "", config_dir=with_secret_tool)


def test_unknown_secret_name_is_refused(with_secret_tool: Path) -> None:
    with pytest.raises(SecretError, match="unknown secret"):
        normalize_secret_name("github.token")
    with pytest.raises(SecretError, match="unknown secret"):
        store_secret("github.token", "x", config_dir=with_secret_tool)


def test_missing_binary_is_reported(scratch: Path) -> None:
    backend = SecretToolBackend()
    backend.binary = "operon-missing-helper"  # shadows the class attribute
    with pytest.raises(SecretError, match="not found"):
        backend.get("ncbi.api_key")
    with pytest.raises(SecretError, match="not found"):
        backend.store("ncbi.api_key", "value")


def test_run_maps_timeout_and_failure(monkeypatch) -> None:
    def _timeout(*args, **kwargs):
        raise secret_module.subprocess.TimeoutExpired(cmd="secret-tool", timeout=1)

    monkeypatch.setattr(secret_module.subprocess, "run", _timeout)
    with pytest.raises(SecretError, match="timed out"):
        SecretToolBackend().get("ncbi.api_key")

    def _missing(*args, **kwargs):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(secret_module.subprocess, "run", _missing)
    with pytest.raises(SecretError, match="not found"):
        SecretToolBackend().get("ncbi.api_key")


def test_secret_status_reports_without_values(with_secret_tool: Path, monkeypatch) -> None:
    store_secret("ncbi.api_key", "stored-value", config_dir=with_secret_tool)
    monkeypatch.setenv("NCBI_API_KEY", "env-value")
    status = secret_status(environ={"NCBI_API_KEY": "env-value"}, config_dir=with_secret_tool)
    assert status["active_backend"] == "secret-tool"
    assert [row["name"] for row in status["backends"]] == ["secret-tool", "systemd-creds", "keychain"]
    assert status["backends"][0]["active"] is True
    row = status["secrets"][0]
    assert row == {"name": "ncbi.api_key", "env_var": "NCBI_API_KEY",
                   "env_set": True, "stored": True}
    assert "stored-value" not in repr(status)


def test_secret_status_without_backend(scratch: Path) -> None:
    status = secret_status(environ={}, config_dir=scratch)
    assert status["active_backend"] is None
    assert status["secrets"][0]["stored"] is None
