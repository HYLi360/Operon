"""``operon config``: the per-user configuration CLI (project independent)."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest
import yaml

from operon import config as config_module
from operon import secrets as secrets_module
from operon.cli import main
from operon.config import (
    USER_CONFIG_FILENAME,
    USER_CONFIG_SCHEMA_VERSION,
    user_config_path,
)


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path: Path):
    """Every test runs against a throwaway HOME and XDG config directory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for variable in ("OPERON_ACTOR", "USER", "LOGNAME", "USERNAME", "NCBI_EMAIL",
                     "NCBI_API_KEY", "OPERON_SPLASH"):
        monkeypatch.delenv(variable, raising=False)
    config_module.reset_user_config()
    yield tmp_path
    config_module.reset_user_config()


def _document(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# --- path / show -------------------------------------------------------------


def test_config_path_prints_the_xdg_location(capsys) -> None:
    assert main(["config", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(user_config_path())
    assert user_config_path().name == USER_CONFIG_FILENAME


def test_config_show_prints_defaults_without_writing_anything(capsys) -> None:
    assert main(["config", "show"]) == 0
    document = yaml.safe_load(capsys.readouterr().out)
    assert document["schema_version"] == USER_CONFIG_SCHEMA_VERSION
    assert document["identity"]["actor"] == ""
    assert document["ui"]["splash"] == "auto"
    assert not user_config_path().exists()


def test_config_show_json_matches_the_yaml_document(capsys) -> None:
    main(["config", "set", "ncbi.email", "someone@example.org"])
    capsys.readouterr()
    assert main(["config", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ncbi"]["email"] == "someone@example.org"


def test_config_show_effective_reports_every_source(capsys, monkeypatch) -> None:
    main(["config", "set", "identity.actor", "config-actor"])
    main(["config", "set", "ui.splash", "text"])
    capsys.readouterr()

    def _no_account() -> str:
        raise KeyError("USER")

    # The local account outranks the stored actor: drop it to see the stored one.
    monkeypatch.setattr(config_module.getpass, "getuser", _no_account)
    monkeypatch.setenv("OPERON_SPLASH", "kitty")
    assert main(["config", "show", "--effective"]) == 0
    rows = {}
    for line in capsys.readouterr().out.strip().splitlines():
        key, source = line.split("  # ", 1)
        rows[key] = source
    assert rows["identity.actor = config-actor"].startswith("user configuration")
    assert rows["ui.splash = kitty"] == "environment (OPERON_SPLASH)"


def test_config_show_effective_json(capsys) -> None:
    assert main(["config", "show", "--effective", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"identity.actor", "ncbi.email", "ui.splash"}
    assert payload["ui.splash"]["source"] == "terminal detection"


# --- set / get / unset -------------------------------------------------------


def test_config_set_get_unset_round_trip(capsys) -> None:
    assert main(["config", "set", "identity.actor", "alice"]) == 0
    assert "identity.actor = alice" in capsys.readouterr().out
    assert main(["config", "get", "identity.actor"]) == 0
    assert capsys.readouterr().out.strip() == "alice"
    assert main(["config", "unset", "identity.actor"]) == 0
    capsys.readouterr()
    # An unset key reports "no value" through the exit code, like `git config`.
    assert main(["config", "get", "identity.actor"]) == 1
    assert capsys.readouterr().out.strip() == ""
    assert _document(user_config_path())["identity"]["actor"] == ""


def test_config_set_writes_user_only_permissions() -> None:
    assert main(["config", "set", "identity.actor", "alice"]) == 0
    assert stat.S_IMODE(user_config_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(user_config_path().parent.stat().st_mode) == 0o700


def test_config_set_coerces_and_validates_choices(capsys) -> None:
    assert main(["config", "set", "ui.splash", "KITTY"]) == 0
    assert _document(user_config_path())["ui"]["splash"] == "kitty"
    assert main(["config", "set", "ui.splash", "hologram"]) == 2
    assert "ui.splash" in capsys.readouterr().err


def test_config_set_rejects_unknown_keys_without_writing(capsys) -> None:
    assert main(["config", "set", "identity.nickname", "alice"]) == 2
    assert "unknown configuration key" in capsys.readouterr().err
    assert not user_config_path().exists()


def test_config_set_refuses_secret_material(capsys) -> None:
    assert main(["config", "set", "ncbi.api_key", "not-a-real-key"]) == 2
    message = capsys.readouterr().err
    assert "looks like secret material" in message
    assert "operon config secret set ncbi.api_key" in message
    assert not user_config_path().exists()


def test_config_unset_rejects_unknown_keys(capsys) -> None:
    assert main(["config", "unset", "index.size"]) == 2
    assert "unknown configuration key" in capsys.readouterr().err


# --- init / check ------------------------------------------------------------


def test_config_init_writes_defaults_once(capsys) -> None:
    assert main(["config", "init"]) == 0
    assert _document(user_config_path()) == config_module.USER_CONFIG_DEFAULT
    assert main(["config", "init"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert main(["config", "init", "--force"]) == 0


def test_config_check_reports_a_missing_file_as_valid(capsys) -> None:
    assert main(["config", "check"]) == 0
    captured = capsys.readouterr()
    assert "built-in defaults apply" in captured.out
    assert not user_config_path().exists()


def test_config_check_flags_permissions_and_unknown_keys(capsys) -> None:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version: 1\nidentity:\n  actor: alice\nui:\n  splash: text\n  palette: mono\n",
        encoding="utf-8",
    )
    path.chmod(0o644)
    assert main(["config", "check"]) == 0
    captured = capsys.readouterr()
    assert "valid YAML" in captured.out
    # The local account outranks the stored actor, so the effective value is
    # reported as such -- the stored one is only a fallback.
    assert "ui.splash = text" in captured.out
    assert "accessible to other users" in captured.err
    assert "palette" in captured.err


def test_config_check_fails_on_secret_material(capsys) -> None:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("schema_version: 1\nncbi:\n  email: x@example.org\n  api_key: leaked\n",
                    encoding="utf-8")
    path.chmod(0o600)
    assert main(["config", "check"]) == 1
    assert "must not live in the configuration file" in capsys.readouterr().err


def test_config_check_reports_malformed_yaml(capsys) -> None:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("identity: [unclosed\n", encoding="utf-8")
    assert main(["config", "check"]) == 2
    assert "invalid YAML" in capsys.readouterr().err


# --- secrets -----------------------------------------------------------------


def test_config_secret_list_without_a_backend(monkeypatch, tmp_path, capsys) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    assert main(["config", "secret", "list"]) == 0
    captured = capsys.readouterr()
    assert "active backend: none available" in captured.out
    assert "ncbi.api_key: unset" in captured.out


def test_config_secret_list_reports_the_environment(monkeypatch, tmp_path, capsys) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("NCBI_API_KEY", "from-the-environment")
    assert main(["config", "secret", "list"]) == 0
    assert "ncbi.api_key: environment (NCBI_API_KEY)" in capsys.readouterr().out


def test_config_secret_set_reads_stdin(monkeypatch, capsys) -> None:
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(
        secrets_module, "store_secret",
        lambda name, value, **kwargs: stored.append((name, value)) or "fake-backend",
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("shh\n"))
    assert main(["config", "secret", "set", "ncbi.api_key"]) == 0
    assert stored == [("ncbi.api_key", "shh")]
    assert "stored ncbi.api_key in fake-backend" in capsys.readouterr().out


def test_config_secret_set_refuses_an_empty_value(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert main(["config", "secret", "set", "ncbi.api_key"]) == 2
    assert "no secret value on stdin" in capsys.readouterr().err


def test_config_secret_set_rejects_unknown_names(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("shh\n"))
    assert main(["config", "secret", "set", "slurm.token"]) == 2
    assert "unknown secret" in capsys.readouterr().err


def test_config_secret_get_and_clear(monkeypatch, capsys) -> None:
    monkeypatch.setattr(secrets_module, "read_secret", lambda name, **kwargs: "stored-value")
    assert main(["config", "secret", "get", "ncbi.api_key"]) == 0
    assert capsys.readouterr().out.strip() == "stored-value"

    monkeypatch.setattr(secrets_module, "read_secret", lambda name, **kwargs: None)
    assert main(["config", "secret", "get", "ncbi.api_key"]) == 1
    assert "is not stored" in capsys.readouterr().err

    monkeypatch.setattr(secrets_module, "clear_secret", lambda name, **kwargs: True)
    assert main(["config", "secret", "clear", "ncbi.api_key"]) == 0
    assert "cleared ncbi.api_key" in capsys.readouterr().out

    monkeypatch.setattr(secrets_module, "clear_secret", lambda name, **kwargs: False)
    assert main(["config", "secret", "clear", "ncbi.api_key"]) == 0
    assert "was not stored" in capsys.readouterr().out


def test_config_secret_list_marks_available_backends(monkeypatch, capsys) -> None:
    report = {
        "active_backend": None,
        "backends": [
            {"name": "secret-tool", "available": False, "active": False},
            {"name": "systemd-creds", "available": True, "active": False},
        ],
        "secrets": [
            {"name": "ncbi.api_key", "env_var": "NCBI_API_KEY", "env_set": False, "stored": None},
        ],
    }
    monkeypatch.setattr(secrets_module, "secret_status", lambda **kwargs: report)
    assert main(["config", "secret", "list"]) == 0
    captured = capsys.readouterr()
    assert "none available" in captured.out
    assert "systemd-creds: available" in captured.out


# --- project independence ----------------------------------------------------


def test_config_works_outside_a_project(monkeypatch, tmp_path, capsys) -> None:
    empty = tmp_path / "not-a-project"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert main(["config", "set", "identity.actor", "roaming"]) == 0
    assert main(["config", "show"]) == 0
    assert "roaming" in capsys.readouterr().out
