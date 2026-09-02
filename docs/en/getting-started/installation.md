# Installation

## Requirements

- Python 3.10 or later
- Python `venv` and `pip`
- A working C toolchain; `operon` builds and uses the Cython QC extension by default
- Optional external tools such as BUSCO, QUAST, FastQC, and fastp

## Install from the repository

Run the following commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Install the SSH/SFTP extra when remote storage or execution is required:

```bash
python -m pip install -e '.[remote]'
```

Verify the installation:

```bash
operon --version
# Expected: operon 0.6.0

operon --help
```

To build a standalone cx_Freeze application, install the build extra and use the unified release entry point:

```bash
python -m pip install -e '.[build]'
python tools/build.py
```

See [Application Release](../contributor/application-release.md) for the release directory layout and validation rules.
