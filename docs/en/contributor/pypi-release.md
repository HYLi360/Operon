# PyPI release

The PyPI distribution is named `OperonDBS` because the `operon` distribution
name belongs to an unrelated project. This does not change the import name or
console command: users install `OperonDBS`, import `operon`, and run `operon`.

The PEP 517 build dependencies contain only setuptools and Cython.

## GitHub Actions release

`.github/workflows/publish.yml` runs only when a GitHub Release is published.
It validates that the release tag is exactly `v<project.version>`, then builds:

- one source distribution;
- CPython 3.10-3.14 manylinux x86-64 wheels;
- CPython 3.10-3.14 macOS Intel wheels; and
- CPython 3.10-3.14 macOS Apple Silicon wheels.

A `verify-release` job runs before every other job, and they all need it: it
checks out the release tag and refuses to continue unless the `test` workflow's run
for that exact commit concluded `success` and `scripts/release-preflight.sh --ci
--tag <tag>` passes. It imports the maintainer's published GPG key first, so the
tag's signature is verified rather than merely present; without that key the gate
reports the check as skipped instead of failed. A tag on a commit whose CI is red — or cancelled before it ran
— therefore builds and uploads nothing.

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

1. Update `[project].version`, plus `SCHEMA_VERSION` / `METADATA_SCHEMA_VERSION`
   in the code when they change. Documentation version markers render from
   these single sources through `myst_substitutions` in `docs/conf_common.py`, so no
   manual sweep is needed; `tests/unit/test_docs_versions.py` rejects
   hardcoded current versions in the Markdown sources.
2. Run `scripts/release-preflight.sh` — the release gate in one command, one exit
   code: the full pytest suite, the strict documentation build for both language
   trees, the version in `pyproject.toml` against the installed metadata, the defect
   registry (every record shipping in this version is `verified`, names a
   `fix_commit`, and that commit is in the tree), and the cross-version matrix
   evidence for the commit you are about to tag (`--run-matrix` runs the matrix as
   part of the gate; `--tag vX.Y.Z` additionally asserts the created tag is
   annotated, GPG-signed, named `v<version>`, and pointing at `HEAD`). Nothing about
   a release depends on remembering the individual commands.
3. Commit the release state and create tag `v<project.version>` on that exact
   commit. Never reuse a tag that points to older package metadata.
4. Wait for the `test` workflow on the tagged commit to pass. The publish
   workflow refuses to build or upload while that run is missing, cancelled, or red;
   give such a commit its own run with `gh run rerun <run-id>` and publish again.
5. Create a GitHub Release from that tag, initially as a draft if release notes
   still need review, then publish it.
6. Verify the files and metadata on the `OperonDBS` PyPI project page.

PyPI files are immutable for a given version. If an upload has already been
published with incorrect contents, increment the project version rather than
trying to replace it.
