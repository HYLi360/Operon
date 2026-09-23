"""User-level configuration (XDG) — laziness, defaults, validation, permissions."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

from operon import config as config_module
from operon.config import (
    DEFAULT_CONFIG,
    USER_CONFIG_DEFAULT,
    USER_CONFIG_FILENAME,
    USER_CONFIG_SCHEMA_VERSION,
    UserConfig,
    effective_config,
    flatten_user_config,
    has_secret_like_keys,
    normalize_user_value,
    resolve_actor,
    resolve_ncbi_email,
    resolve_splash,
    user_config,
    user_config_dir,
    user_config_path,
)
from operon.errors import ConfigError


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path: Path):
    """Every test runs against a throwaway HOME and XDG config directory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for variable in ("OPERON_ACTOR", "USER", "LOGNAME", "USERNAME", "NCBI_EMAIL",
                     "OPERON_SPLASH"):
        monkeypatch.delenv(variable, raising=False)
    config_module.reset_user_config()
    yield tmp_path
    config_module.reset_user_config()


# --- location ----------------------------------------------------------------


def test_import_does_not_touch_the_filesystem(tmp_path: Path) -> None:
    """Reading the configuration is lazy: importing it creates nothing."""
    assert not (tmp_path / "xdg").exists()
    instance = user_config()
    assert instance.data == USER_CONFIG_DEFAULT
    assert not (tmp_path / "xdg").exists()


def test_config_path_follows_xdg(tmp_path: Path, monkeypatch) -> None:
    assert user_config_dir() == tmp_path / "xdg" / "operon"
    assert user_config_path() == tmp_path / "xdg" / "operon" / USER_CONFIG_FILENAME

    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert user_config_path() == tmp_path / "home" / ".config" / "operon" / USER_CONFIG_FILENAME
    # The home-directory shortcut is never used.
    assert user_config_path().name != ".operon"
    assert user_config_path().parent.name == "operon"


def test_config_path_ignores_blank_xdg_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "   ")
    assert user_config_path() == tmp_path / "home" / ".config" / "operon" / USER_CONFIG_FILENAME


# --- loading -----------------------------------------------------------------


def test_missing_file_yields_defaults_without_writing() -> None:
    instance = UserConfig()
    assert instance.load() == USER_CONFIG_DEFAULT
    assert not instance.path.exists()


def test_load_merges_defaults_and_keeps_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(
        "identity:\n  actor: alice\nfuture:\n  flag: true\n", encoding="utf-8",
    )
    data = UserConfig(path).load()
    assert data["identity"]["actor"] == "alice"
    assert data["schema_version"] == USER_CONFIG_SCHEMA_VERSION
    assert data["ncbi"]["email"] == ""
    assert data["ui"]["splash"] == "auto"
    assert data["future"] == {"flag": True}  # unknown keys survive round-trips


def test_malformed_yaml_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text("identity: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        UserConfig(path).load()


def test_non_mapping_document_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a mapping"):
        UserConfig(path).load()


def test_empty_file_is_all_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text("", encoding="utf-8")
    assert UserConfig(path).load() == USER_CONFIG_DEFAULT


# --- writing -----------------------------------------------------------------


def test_init_writes_permissions_and_refuses_overwrite(tmp_path: Path) -> None:
    instance = UserConfig(tmp_path / "xdg" / "operon" / USER_CONFIG_FILENAME)
    written = instance.init()
    assert written.is_file()
    assert stat.S_IMODE(os.stat(written).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(written.parent).st_mode) == 0o700
    assert yaml.safe_load(written.read_text(encoding="utf-8"))["identity"]["actor"] == ""

    with pytest.raises(ConfigError, match="already exists"):
        instance.init()
    instance.init(force=True)


def test_set_get_unset_round_trip(tmp_path: Path) -> None:
    instance = UserConfig(tmp_path / "config.yml")
    assert instance.set("identity.actor", "alice") == "alice"
    assert instance.set("ui.splash", "kitty") == "kitty"
    assert instance.set("schema_version", "2") == 2  # argv strings coerce for ints
    path = instance.path
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["identity"]["actor"] == "alice"

    assert instance.unset("identity.actor") == ""
    assert instance.get("identity.actor") == ""


@pytest.mark.parametrize(
    "key, value, message",
    [
        ("ncbi.token", "x", "unknown configuration key"),
        ("ui.splash", "rainbow", "must be one of"),
        ("schema_version", "abc", "expects an integer"),
        ("", "x", "must not be empty"),
    ],
)
def test_set_rejects_invalid_input(tmp_path: Path, key: str, value: str, message: str) -> None:
    instance = UserConfig(tmp_path / "config.yml")
    with pytest.raises(ConfigError, match=message):
        instance.set(key, value)
    assert not instance.path.exists()  # nothing is written on a rejected value


def test_normalize_user_value_type_errors() -> None:
    assert normalize_user_value("schema_version", 3) == 3
    with pytest.raises(ConfigError, match="expects an integer"):
        normalize_user_value("schema_version", 3.5)
    with pytest.raises(ConfigError, match="expects str"):
        normalize_user_value("identity.actor", 5)


# --- invariants --------------------------------------------------------------


def test_user_config_carries_no_secret_and_no_project_keys() -> None:
    user_keys = set(flatten_user_config(USER_CONFIG_DEFAULT))
    assert user_keys == {"schema_version", "identity.actor", "ncbi.email", "ui.splash"}
    # No secret-looking key may exist, and the two layers never overlap.
    assert has_secret_like_keys(USER_CONFIG_DEFAULT) == []
    project_roots = {key.split(".")[0] for key in flatten_user_config(DEFAULT_CONFIG)}
    assert {key.split(".")[0] for key in user_keys}.isdisjoint(project_roots)


def test_has_secret_like_keys_flags_hand_written_tokens() -> None:
    data = {"ncbi": {"api_key": "ABCD"}, "ui": {"splash": "auto"}}
    assert has_secret_like_keys(data) == ["ncbi.api_key"]


# --- accessors and resolvers -------------------------------------------------


def test_typed_accessors(tmp_path: Path) -> None:
    instance = UserConfig(tmp_path / "config.yml")
    assert (instance.actor, instance.ncbi_email, instance.splash) == ("", "", "auto")
    instance.set("identity.actor", "alice")
    instance.set("ncbi.email", "alice@example.org")
    instance.set("ui.splash", "text")
    assert (instance.actor, instance.ncbi_email, instance.splash) == (
        "alice", "alice@example.org", "text",
    )


def test_actor_precedence(monkeypatch, tmp_path: Path) -> None:
    instance = UserConfig(tmp_path / "config.yml")
    instance.set("identity.actor", "config-actor")
    calls: list[str] = []

    def _account() -> str:
        calls.append("getpass")
        raise KeyError("USER")

    monkeypatch.setattr(config_module.getpass, "getuser", _account)
    env = {"USER": "env-user", "OPERON_ACTOR": "env-actor"}
    assert resolve_actor("flag-actor", environ=env, config=instance) == "flag-actor"
    assert resolve_actor(None, environ=env, config=instance) == "env-actor"
    assert resolve_actor(None, environ={"USER": "env-user"}, config=instance) == "env-user"
    assert resolve_actor(None, environ={"LOGNAME": "env-logname"}, config=instance) == "env-logname"
    assert resolve_actor(None, environ={"USERNAME": "env-username"}, config=instance) == "env-username"
    # Blank values (containers, cron) are skipped, never recorded as an actor.
    assert resolve_actor(None, environ={"USER": "   "}, config=instance) == "config-actor"
    assert resolve_actor(None, environ={}, config=instance) == "config-actor"
    assert calls  # the local account is consulted before the configuration


def test_local_account_outranks_the_configuration(monkeypatch, tmp_path: Path) -> None:
    instance = UserConfig(tmp_path / "config.yml")
    instance.set("identity.actor", "config-actor")
    monkeypatch.setattr(config_module.getpass, "getuser", lambda: "local-account")
    assert resolve_actor(None, environ={}, config=instance) == "local-account"


def test_actor_falls_back_to_the_local_account(monkeypatch, tmp_path: Path) -> None:
    instance = UserConfig(tmp_path / "config.yml")
    # An empty HOME for the resolver is not enough: pin getpass explicitly.
    monkeypatch.setattr(config_module.getpass, "getuser", lambda: "local-account")
    assert resolve_actor(None, environ={}, config=instance) == "local-account"


def test_actor_none_when_nothing_is_available(monkeypatch, tmp_path: Path) -> None:
    instance = UserConfig(tmp_path / "config.yml")
    def _raise() -> str:
        raise KeyError("USER")
    monkeypatch.setattr(config_module.getpass, "getuser", _raise)
    assert resolve_actor(None, environ={}, config=instance) is None


def test_email_precedence(tmp_path: Path) -> None:
    instance = UserConfig(tmp_path / "config.yml")
    instance.set("ncbi.email", "config@example.org")
    assert resolve_ncbi_email("flag@example.org", environ={"NCBI_EMAIL": "env@example.org"},
                              config=instance) == "flag@example.org"
    assert resolve_ncbi_email(None, environ={"NCBI_EMAIL": "env@example.org"},
                              config=instance) == "env@example.org"
    assert resolve_ncbi_email(None, environ={}, config=instance) == "config@example.org"
    assert resolve_ncbi_email(None, environ={}, config=UserConfig(tmp_path / "other.yml")) is None


def test_splash_precedence_and_degradation(tmp_path: Path) -> None:
    instance = UserConfig(tmp_path / "config.yml")
    instance.set("ui.splash", "kitty")
    assert resolve_splash(None, environ={"OPERON_SPLASH": "text"}, config=instance) == "text"
    assert resolve_splash(None, environ={"OPERON_SPLASH": "bogus"}, config=instance) == "kitty"
    assert resolve_splash(None, environ={}, config=instance) == "kitty"
    assert resolve_splash("blocks", environ={}, config=instance) == "blocks"
    # A malformed configuration must not keep the UI from starting.
    broken = tmp_path / "broken.yml"
    broken.write_text("ui: [oops\n", encoding="utf-8")
    assert resolve_splash(None, environ={}, config=UserConfig(broken)) == "auto"
    assert resolve_splash(None, environ={}, config=UserConfig(tmp_path / "absent.yml")) == "auto"


def test_effective_config_reports_sources(tmp_path: Path, monkeypatch) -> None:
    instance = UserConfig(tmp_path / "config.yml")
    instance.set("identity.actor", "config-actor")
    instance.set("ncbi.email", "config@example.org")
    rows = effective_config(environ={"USER": "env-user"}, config=instance)
    assert rows["identity.actor"] == {"value": "env-user", "source": "environment (USER)"}
    assert rows["ncbi.email"]["value"] == "config@example.org"
    assert rows["ui.splash"]["source"] == "terminal detection"

    rows = effective_config(environ={"OPERON_ACTOR": "env-actor", "NCBI_EMAIL": "env@example.org",
                                     "OPERON_SPLASH": "blocks"}, config=instance)
    assert rows["identity.actor"]["source"] == "environment (OPERON_ACTOR)"
    assert rows["ncbi.email"]["source"] == "environment (NCBI_EMAIL)"
    assert rows["ui.splash"] == {"value": "blocks", "source": "environment (OPERON_SPLASH)"}

    def _raise() -> str:
        raise KeyError("USER")

    monkeypatch.setattr(config_module.getpass, "getuser", _raise)
    rows = effective_config(environ={}, config=instance)
    assert rows["identity.actor"] == {
        "value": "config-actor",
        "source": f"user configuration ({config_module.user_config_path({})})",
    }


def test_reset_user_config_drops_the_cached_document() -> None:
    target = Path(os.environ["XDG_CONFIG_HOME"]) / "operon" / USER_CONFIG_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("identity:\n  actor: cached\n", encoding="utf-8")
    config_module.reset_user_config()
    assert user_config().actor == "cached"
    target.write_text("identity:\n  actor: changed\n", encoding="utf-8")
    assert user_config().actor == "cached"  # cached until reset
    config_module.reset_user_config()
    assert user_config().actor == "changed"


def test_module_singleton_is_used_when_no_instance_is_passed(monkeypatch) -> None:
    target = Path(os.environ["XDG_CONFIG_HOME"]) / "operon" / USER_CONFIG_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("identity:\n  actor: singleton\n", encoding="utf-8")

    def _raise() -> str:
        raise KeyError("USER")

    monkeypatch.setattr(config_module.getpass, "getuser", _raise)
    config_module.reset_user_config()
    assert resolve_actor(None, environ={}) == "singleton"
    assert resolve_splash(environ={}) == "auto"


def test_sys_module_name_is_not_shadowed() -> None:
    """``operon.secrets`` must not shadow the standard library module."""
    import importlib
    stdlib_secrets = importlib.import_module("secrets")
    operon_secrets = importlib.import_module("operon.secrets")
    assert stdlib_secrets is not operon_secrets
    assert operon_secrets.__name__ == "operon.secrets"
    assert "randbelow" in dir(stdlib_secrets)
    assert sys.version_info >= (3, 10)
