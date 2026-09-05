# Installation

## Requirements

- Python 3.10 or later
- Python `venv` and `pip`
- A working C toolchain; `operon` builds and uses the Cython QC extension by default
- Optional external tools such as BUSCO, QUAST, FastQC, and fastp

## Platform support

Linux is the primary platform for standalone application releases, local
Slurm, and the broad external bioinformatics tool ecosystem. macOS is
supported for source-installed local execution and as an SSH/SFTP client,
including remote Slurm submission to a Linux host. Local resource sampling
uses procfs when available and the system `ps` command on macOS.

External commands configured in `tools.yaml` must themselves be installed for
the controller platform. SSH direct execution still requires util-linux
`setsid` on the remote host, so that compute-side mode targets Linux; this does
not prevent a Mac from acting as the controller. Signed or notarized macOS
standalone bundles are not currently published.

## Install from PyPI

The distribution is named `OperonDBS`; the imported package and command are
both named `operon`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install OperonDBS
```

Published wheels include the compiled QC parser. SSH/SFTP support and the TUI
are included in the standard installation. PyPI installation does not use
cx_Freeze; that tool is only needed for the separate standalone application
directory.

## Install from the repository

Run the following commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Verify the installation:

```bash
operon --version

operon --help
```

The first command prints `operon` followed by the installed version —
{{ operon_version }} for the release this documentation matches.

To build a standalone cx_Freeze application, install the build extra and use the unified release entry point:

```bash
python -m pip install -e '.[build]'
python tools/build.py
```

See [Application Release](../contributor/application-release.md) for the release directory layout and validation rules.
