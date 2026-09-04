# PyPI release

The PyPI distribution is named `OperonDBS` because the `operon` distribution
name belongs to an unrelated project. This does not change the import name or
console command: users install `OperonDBS`, import `operon`, and run `operon`.

PyPI publishing and standalone application publishing are independent. The
PEP 517 build dependencies contain only setuptools and Cython; cx_Freeze stays
in the optional `build` extra and is used only by `python tools/build.py`.

## GitHub Actions release

`.github/workflows/publish.yml` runs only when a GitHub Release is published.
It validates that the release tag is exactly `v<project.version>`, then builds:

- one source distribution;
- CPython 3.10-3.14 manylinux x86-64 wheels;
- CPython 3.10-3.14 macOS Intel wheels; and
- CPython 3.10-3.14 macOS Apple Silicon wheels.

Each wheel is installed in an isolated test environment before upload. The
test imports the compiled parser and invokes the CLI. The source distribution
is checked with Twine. The final publish job cannot start until all artifacts
have been built successfully, and it authenticates with PyPI Trusted
Publishing rather than a long-lived API token.

Configure the PyPI trusted publisher with owner `HYLi360`, repository
`Operon`, workflow `publish.yml`, and environment `pypi`. The GitHub
environment name must match exactly; its optional protection rules can require
manual approval before the final upload.

## Release procedure

1. Update `[project].version` and every documented version marker together.
2. Run the full pytest suite and strict documentation build.
3. Commit the release state and create tag `v<project.version>` on that exact
   commit. Never reuse a tag that points to older package metadata.
4. Wait for the `deploy` workflow on the tagged commit to pass.
5. Create a GitHub Release from that tag, initially as a draft if release notes
   still need review, then publish it.
6. Verify the files and metadata on the `OperonDBS` PyPI project page.

PyPI files are immutable for a given version. If an upload has already been
published with incorrect contents, increment the project version rather than
trying to replace it.
