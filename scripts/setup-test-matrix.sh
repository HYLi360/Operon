#!/usr/bin/env bash
# Create the uv-managed CPython 3.10-3.13 interpreters and one venv per version
# for run-test-matrix.sh. 3.14 comes from the project's .venv. Everything stays
# inside .matrix/ so the repository itself is untouched.
#
# Works on Linux and macOS: uv installs a platform-native interpreter, so the
# same commands can reproduce the macOS half of the CI matrix on a Mac.
set -eu
cd "$(dirname "$0")/.."   # repository root
export UV_PYTHON_INSTALL_DIR="$PWD/.matrix/python"
export UV_CACHE_DIR="$PWD/.matrix/cache"

uv python install 3.10 3.11 3.12 3.13
for version in 3.10 3.11 3.12 3.13; do
  interpreter=$(uv python find "$version")
  echo "== $version ($interpreter)"
  uv venv --python "$interpreter" --allow-existing ".matrix/venv${version/./}"
  uv pip install --python ".matrix/venv${version/./}/bin/python" -e '.[test]' pytest-xdist
done
