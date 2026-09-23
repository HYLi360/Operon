# User configuration

`operon` keeps user-level settings in one optional YAML file outside the
project. It never replaces project configuration: `project.yaml` still owns the
storage layout, database path, resources, and execution backends, while the user
file owns the handful of settings that belong to *you* rather than to a project.

```{note}
The file is optional. Without it, built-in defaults apply and the per-user
identity falls back to the environment, exactly as before.
```

## Location

The user configuration follows the XDG Base Directory specification:

```text
$XDG_CONFIG_HOME/operon/config.yml      # typically ~/.config/operon/config.yml
```

- `XDG_CONFIG_HOME` unset or empty → `~/.config/operon/config.yml`.
- There is no `OPERON_CONFIG_HOME`: the path is fully determined by XDG.
- `operon` never creates `~/.operon`, and there is no per-project user file.
- The file is written with mode `0600`, its directory with `0700`.
- `operon config` is project independent: it never opens `operon.sqlite` and
  works before `operon init` and outside any project directory.

## Precedence

Command-line flags win over environment variables, environment variables win
over the file, and the file wins over built-in defaults.

| Setting | Flag | Environment | User file | Fallback |
| --- | --- | --- | --- | --- |
| Audit actor | `--actor` | `OPERON_ACTOR`, `USER`, `LOGNAME`, `USERNAME` | `identity.actor` | the local account (`getpass`), then no actor |
| NCBI contact | `--email` | `NCBI_EMAIL` | `ncbi.email` | unset |
| NCBI API key | `--api-key` | `NCBI_API_KEY` | never stored in the file | a stored secret (see below) |
| Terminal graphics | — | `OPERON_SPLASH` | `ui.splash` | `auto` (detection) |

Two details are worth stating explicitly:

- The local account sits between the environment and the file: on a normal
  login the actor is still `$USER`, and `identity.actor` only fills the gap in
  containers and cron jobs where no login variable and no password entry exist.
- Operations that require an actor (`retire --apply`, `restore --apply`, the
  TUI lifecycle and curate dialogs) fail with a clear error instead of writing
  a nameless audit row.

## Keys

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | `1` | Document version; written by `operon config init` |
| `identity.actor` | string | `""` | Fallback audit actor |
| `ncbi.email` | string | `""` | NCBI contact address for API requests |
| `ui.splash` | choice | `auto` | `auto`, `text`, `blocks`, or `kitty` |

Unknown keys are preserved in the file but ignored by this version, and
`operon config check` reports them. Keys that look like secret material
(`…api_key`, `token`, `secret`, `password`, `credential`) are refused by
`operon config set`: credentials belong in a secret backend.

## Commands

```bash
operon config path
operon config show [--effective] [--json]
operon config get KEY
operon config set KEY VALUE
operon config unset KEY
operon config check
operon config init [--force]
operon config secret list
operon config secret set NAME
operon config secret get NAME
operon config secret clear NAME
```

- `path`: prints the resolved file path.
- `show`: prints the stored document (defaults when the file does not exist).
  It never creates the file. With `--effective` it prints the value that wins
  for every setting, plus where that value comes from; `--json` switches the
  output format.
- `get`: prints one value and exits `0`; a key without a value prints nothing
  and exits `1`, like `git config`.
- `set` / `unset`: validate the key and the value before writing, then write
  atomically with user-only permissions. `unset` restores the built-in default.
- `check`: validates the YAML, warns about a group/world-readable file and
  about unknown keys, fails on secret material, and always prints the effective
  settings — the quickest way to see which layer wins.
- `init`: writes the default document; refuses to overwrite without `--force`.

## Secrets

Credentials never live in the user file and never have to live in the
environment. `operon` stores them through the first available system backend:

1. `secret-tool` — the Secret Service (GNOME Keyring, KWallet; `libsecret-tools`).
2. `systemd-creds --user` — encrypted credentials under
   `$XDG_CONFIG_HOME/operon/secrets/`.
3. `/usr/bin/security` — the macOS Keychain.

All three are used through `subprocess`; no new runtime dependency is required.
With none of them available, `operon` stops with a clear error and tells you to
pass `--api-key` or set `NCBI_API_KEY`. The key is resolved as
`--api-key` > `NCBI_API_KEY` > stored secret.

```bash
# Store without exposing the value in argv or shell history
printf '%s' "$NCBI_TOKEN" | operon config secret set ncbi.api_key
operon config secret list
operon config secret clear ncbi.api_key
```

`secret set` reads the value from stdin, or prompts with hidden input when a
terminal is attached; the value is never accepted as a command-line argument.
`secret list` shows backend availability and whether each secret is set — never
a value. `secret get` prints the value for scripts, with a warning when stdout
is a terminal.

```{note}
`systemd-creds --user` binds a credential to the user, the machine ID and the
kernel's boot ID. On a shared HPC home it may therefore not decrypt on another
node: for remote execution prefer `NCBI_API_KEY` in the environment or
`--api-key` on the command line.
```

## Examples

```bash
# Fallback identity for containers and cron jobs
operon config set identity.actor alice

# NCBI contact address, then the API key in the secret backend
operon config set ncbi.email you@example.org
printf '%s' "$NCBI_TOKEN" | operon config secret set ncbi.api_key

# Skip graphics detection for this user without an environment variable
operon config set ui.splash text

# What wins right now, and why
operon config show --effective
```

## Environment variables are not deprecated

Every variable keeps working and keeps winning over the file. The variables
`operon` reads directly are audited and documented in
`tests/unit/test_env_audit.py`: an unannotated `os.environ` read fails that
test, so any new variable has to be reviewed and documented before it ships.
Environment capture for `environment_id` (`PROBE_ENV_VARS`) and the HTTP/SSH
proxy variables read by `requests`/`paramiko` are unaffected.
