"""Per-user configuration commands (``operon config``)."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from operon.errors import ValidationError


def add_config_parser(sub):
    p = sub.add_parser(
        "config", help="inspect and edit the per-user configuration (XDG)"
    )
    config_sub = p.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("path", help="print the user configuration file path")
    cp = config_sub.add_parser("show", help="print the stored user configuration")
    cp.add_argument(
        "--effective",
        action="store_true",
        help="print the value that wins and where each setting comes from",
    )
    cp.add_argument("--json", action="store_true", help="print JSON instead of YAML")
    cp = config_sub.add_parser("get", help="print one setting")
    cp.add_argument("key")
    cp = config_sub.add_parser(
        "set", help="store one setting (validated before writing)"
    )
    cp.add_argument("key")
    cp.add_argument("value")
    cp = config_sub.add_parser(
        "unset", help="restore one setting to its built-in default"
    )
    cp.add_argument("key")
    config_sub.add_parser(
        "check", help="validate the file and the settings it resolves"
    )
    cp = config_sub.add_parser("init", help="write the default configuration file")
    cp.add_argument("--force", action="store_true", help="overwrite an existing file")
    secret_parser = config_sub.add_parser(
        "secret", help="manage stored secrets (never in the file)"
    )
    secret_sub = secret_parser.add_subparsers(dest="secret_command", required=True)
    secret_sub.add_parser("list", help="backend availability and which secrets are set")
    sp = secret_sub.add_parser("get", help="print a stored secret value")
    sp.add_argument("name")
    sp = secret_sub.add_parser(
        "set", help="store a secret; the value is read from stdin or prompted"
    )
    sp.add_argument("name")
    sp = secret_sub.add_parser("clear", help="delete a stored secret")
    sp.add_argument("name")


def _config_value_text(value: Any) -> str:
    """Render one configuration value for human-readable output."""
    return "(unset)" if value is None or value == "" else str(value)


def _config_check(instance: Any) -> int:
    """Validate the user configuration file and report what it resolves to."""
    from operon import config as user_config_module

    path = instance.path
    warnings: list[str] = []
    secrets: list[str] = []
    if not path.exists():
        print(f"{path}: not present; built-in defaults apply")
    else:
        data = instance.load()  # ConfigError on malformed YAML
        if path.stat().st_mode & 0o077:
            warnings.append(
                f"{path} is accessible to other users; run `chmod 600 {path}`"
            )
        known = user_config_module.USER_CONFIG_TYPES
        unknown = [
            key
            for key in user_config_module.flatten_user_config(data)
            if key not in known
        ]
        if unknown:
            warnings.append(
                f"{path}: ignoring unknown keys: {', '.join(sorted(unknown))}"
            )
        secrets = user_config_module.has_secret_like_keys(data)
        print(f"{path}: valid YAML")
    for key, row in user_config_module.effective_config().items():
        print(f"  {key} = {_config_value_text(row['value'])}  # {row['source']}")
    for message in warnings:
        print(f"warning: {message}", file=sys.stderr)
    for key in secrets:
        print(
            f"error: {key} must not live in the configuration file; move it with "
            f"`operon config secret set {key}`",
            file=sys.stderr,
        )
    return 1 if secrets else 0


def _cmd_config_secret(args: argparse.Namespace) -> int:
    """``operon config secret``: keep credentials out of the configuration file."""
    import getpass

    from operon.secrets import clear_secret, read_secret, secret_status, store_secret

    command = args.secret_command

    if command == "list":
        report = secret_status()
        active = report["active_backend"]
        print(f"active backend: {active or 'none available'}")
        for backend in report["backends"]:
            state = "available" if backend["available"] else "unavailable"
            marker = "*" if backend["active"] else " "
            print(f"  {marker} {backend['name']}: {state}")
        for row in report["secrets"]:
            if row["env_set"]:
                where = f"environment ({row['env_var']})"
            elif row["stored"]:
                where = f"stored in {active}"
            else:
                where = "unset"
            print(f"  {row['name']}: {where}")
        return 0

    if command == "get":
        value = read_secret(args.name)
        if value is None:
            print(f"{args.name} is not stored", file=sys.stderr)
            return 1
        if sys.stdout.isatty():
            print("warning: printing a secret to the terminal", file=sys.stderr)
        print(value)
        return 0

    if command == "set":
        if sys.stdin.isatty():
            value = getpass.getpass(f"{args.name} (input hidden): ")
        else:
            value = sys.stdin.readline().rstrip("\r\n")
        if not value:
            raise ValidationError(
                "no secret value on stdin; pipe it in or run interactively"
            )
        print(f"stored {args.name} in {store_secret(args.name, value)}")
        return 0

    if command == "clear":
        removed = clear_secret(args.name)
        print(f"cleared {args.name}" if removed else f"{args.name} was not stored")
        return 0

    raise ValidationError(f"unsupported config secret command {command!r}")


def _cmd_config(args: argparse.Namespace) -> int:
    """``operon config``: the per-user configuration, project independent."""
    import yaml

    from operon import config as user_config_module

    if args.config_command == "secret":
        return _cmd_config_secret(args)

    instance = user_config_module.user_config()
    command = args.config_command

    if command == "path":
        print(instance.path)
        return 0

    if command == "show":
        if not args.effective:
            data = instance.data
            if args.json:
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                sys.stdout.write(
                    yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
                )
            return 0
        rows = user_config_module.effective_config()
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        for key, row in rows.items():
            print(f"{key} = {_config_value_text(row['value'])}  # {row['source']}")
        return 0

    if command == "get":
        value = _config_value_text(instance.get(args.key))
        print("" if value == "(unset)" else value)
        return 1 if value == "(unset)" else 0

    if command == "set":
        secret_like = user_config_module.has_secret_like_keys({args.key: args.value})
        if secret_like:
            raise ValidationError(
                f"{secret_like[0]} looks like secret material; store it with "
                f"`operon config secret set {secret_like[0]}` instead of the configuration file"
            )
        print(f"{args.key} = {instance.set(args.key, args.value)}")
        return 0

    if command == "unset":
        print(f"{args.key} = {_config_value_text(instance.unset(args.key))}")
        return 0

    if command == "init":
        print(f"wrote {instance.init(force=args.force)}")
        return 0

    if command == "check":
        return _config_check(instance)

    raise ValidationError(f"unsupported config command {command!r}")
