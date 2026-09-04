# Application release

## Application release file structure

This workflow is separate from the `OperonDBS` package published to PyPI.
PyPI wheels and sdists neither invoke nor require cx_Freeze. The project keeps
cx_Freeze only to build an optional standalone application directory from
`pyproject.toml`. A full standalone application release has exactly one entry
point:

```bash
python -m pip install -e '.[build]'
python tools/build.py
```

The `build` extra contains cx_Freeze, Cython, Sphinx/MyST/the RTD theme, and the conditional dependency `tomli` needed by Python 3.10 to read `pyproject.toml`; Python 3.11 and above use the standard-library `tomllib` directly and do not install `tomli`. Paramiko and Textual are standard runtime dependencies and are therefore also present when this extra is installed. Python 3.10 and 3.11+ share the same build command.

`tools/build.py`, in order: rebuilds the required Cython parsers, strictly builds the bilingual Sphinx HTML site, resolves the single application version from `pyproject.toml`, collects licenses for frozen runtime dependencies and rendered documentation assets, generates the corresponding source sdist, invokes cx_Freeze, and assembles and verifies the final directory. If any step fails, the target version is not published; an existing same-version directory refuses to be overwritten by default, and only an explicit `--force` replaces it. Do not invoke cx_Freeze directly for an official release package, because that skips documentation validation, licenses, corresponding source, and the final smoke test.

Release content lands in a versioned directory:

A Linux build machine additionally needs the system command `patchelf`; it is a build-time tool for cx_Freeze's ELF dependency handling, not a Python runtime dependency of `operon`. When it is missing, cx_Freeze stops right at the `build_exe` stage.

```text
build/release/v0.6.2/
├── operon                  # command-line executable; operon.exe on Windows
├── lib/                    # Python runtime, the operon package, and third-party dependencies
├── LICENSE                 # Operon's own license (AGPL-3.0-or-later)
├── licenses/               # THIRD_PARTY_NOTICES.md and full license texts of third-party dependencies
├── source/
│   └── operondbs-0.6.2.tar.gz # complete project source sdist corresponding to this binary
├── frozen_application_license.txt  # license of the frozen bootstrap code automatically included by cx_Freeze
└── share/doc/operon/
    ├── README.md           # English project overview
    ├── README_ZH.md        # Chinese project overview
    └── html/               # directly browsable bilingual Sphinx HTML site
```

The version in both the directory name and the source package name is read dynamically from `[project].version`; no second copy of the version number is maintained in code. The installed `operon.__version__` reads the same value through distribution metadata. The source package is controlled by `MANIFEST.in` and includes Python/Cython sources, build scripts, tests, the bilingual Sphinx documentation and its RTD configuration, and licenses, while excluding `.so`, `.pyd`, generated C files, caches, and local run data.

The application release does not copy the repository's `docs/` source tree directly. Sphinx runs in a temporary staging directory with strict `-W --keep-going` checks, and only the complete HTML result is copied to `share/doc/operon/html/`. The Markdown sources, `conf.py`, RTD configuration, and templates remain available in the corresponding source sdist under `source/`. Source copies and the “view source” link are disabled for the release HTML, and the separate doctree cache is deleted after the build. Consequently, local `docs/_build/` content, pickle caches, and duplicate Markdown cannot leak into an official release.

Runtime version reading depends on `importlib.metadata`, which parses distribution metadata through the standard-library `email` package. cx_Freeze's static analysis cannot reliably discover this indirect import, so `[tool.cxfreeze.build_exe].packages` explicitly includes the whole `email` package; do not remove it as an unused module, or the frozen program may exit at startup for lack of `email.header`.

The PyPI distribution name (`OperonDBS`) differs from the import package name
(`operon`), so cx_Freeze cannot infer which distribution metadata belongs to
the package. The release builder explicitly copies the installed
`operondbs-<version>.dist-info` directory into the frozen library before the
version smoke test. Without it, `importlib.metadata.version("OperonDBS")`
would fall back to `0+unknown` in the standalone executable.

An application release directory and the dataset snapshot produced by `operon release` are two different concepts: the former delivers the program, the latter delivers filtered and verifiable data.
