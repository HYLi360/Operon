# Application release

## Application release file structure

The project uses cx_Freeze to build a standalone application directory from `pyproject.toml`. A full application release has exactly one entry point:

```bash
python -m pip install -e '.[build]'
python tools/build.py
```

The `build` extra contains cx_Freeze, Paramiko for remote functionality, and the conditional dependency `tomli` needed by Python 3.10 to read `pyproject.toml`; Python 3.11 and above use the standard-library `tomllib` directly and do not install `tomli`. Python 3.10 and 3.11+ therefore share the same build command.

`tools/build.py`, in order: rebuilds the required Cython parsers, resolves the single application version from `pyproject.toml`, collects the licenses of frozen runtime dependencies, generates the corresponding source sdist, invokes cx_Freeze, and assembles and verifies the final directory. If any step fails, the target version is not published; an existing same-version directory refuses to be overwritten by default, and only an explicit `--force` replaces it. Do not invoke cx_Freeze directly for an official release package, because that skips licenses, corresponding source, and the final smoke test.

Release content lands in a versioned directory:

A Linux build machine additionally needs the system command `patchelf`; it is a build-time tool for cx_Freeze's ELF dependency handling, not a Python runtime dependency of `operon`. When it is missing, cx_Freeze stops right at the `build_exe` stage.

```text
build/release/v0.6.0/
├── operon                  # command-line executable; operon.exe on Windows
├── lib/                    # Python runtime, the operon package, and third-party dependencies
├── LICENSE                 # Operon's own license (AGPL-3.0-or-later)
├── licenses/               # THIRD_PARTY_NOTICES.md and full license texts of third-party dependencies
├── source/
│   └── operon-0.6.0.tar.gz # complete project source sdist corresponding to this binary
├── frozen_application_license.txt  # license of the frozen bootstrap code automatically included by cx_Freeze
└── share/doc/operon/       # README and docs/
```

The version in both the directory name and the source package name is read dynamically from `[project].version`; no second copy of the version number is maintained in code. The installed `operon.__version__` reads the same value through distribution metadata. The source package is controlled by `MANIFEST.in` and includes Python/Cython sources, build scripts, tests, the bilingual Sphinx documentation and its RTD configuration, and licenses, while excluding `.so`, `.pyd`, generated C files, caches, and local run data.

Runtime version reading depends on `importlib.metadata`, which parses distribution metadata through the standard-library `email` package. cx_Freeze's static analysis cannot reliably discover this indirect import, so `[tool.cxfreeze.build_exe].packages` explicitly includes the whole `email` package; do not remove it as an unused module, or the frozen program may exit at startup for lack of `email.header`.

An application release directory and the dataset snapshot produced by `operon release` are two different concepts: the former delivers the program, the latter delivers filtered and verifiable data.
