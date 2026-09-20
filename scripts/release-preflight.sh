#!/usr/bin/env bash

# Release preflight: the release gate in one command, one exit code.
#
# Usage:
#   scripts/release-preflight.sh                 # every local check a release needs
#   scripts/release-preflight.sh --run-matrix    # …and run the cross-version matrix first
#   scripts/release-preflight.sh --tag v0.8.3    # also assert the tag is signed and on HEAD
#   scripts/release-preflight.sh --ci --tag vX   # the subset a CI runner asserts
#
# Checks
#   1. version  pyproject.toml's version, and the installed metadata for it
#   2. tests    python -m pytest (the coverage gate lives in its configuration)
#   3. docs     sphinx-build -W --keep-going for docs/en and docs/zh
#   4. registry tests/unit/test_defect_registry.py, plus: every record whose
#               fixed_in is this version is verified, names a fix_commit, and —
#               when --tag is given — that commit is an ancestor of HEAD
#   5. matrix   the cross-version matrix ran on this exact commit and passed
#               (.matrix/logs/head names HEAD, every .matrix/logs/*.status is 0)
#   6. tag      with --tag: named v<version>, annotated, GPG-signed, peels to HEAD
#
# Options: --run-matrix, --no-matrix, --no-tests, --no-docs, --ci (all three
# off: the checks a runner can make without the local matrix or a repo venv),
# --tag <name>, --version <X.Y.Z>.  A failing check reports why and sets the
# exit status; the run continues so one invocation shows every problem.

set -u
cd "$(dirname "$0")/.."   # repository root
ROOT=$PWD
PYTHON=${PREFLIGHT_PYTHON:-$ROOT/.venv/bin/python}
[ -x "$PYTHON" ] || PYTHON=python3

run_matrix=0 use_tests=1 use_docs=1 check_matrix=1 tag="" version=""
while [ $# -gt 0 ]; do
  case "$1" in
    --run-matrix) run_matrix=1 ;;
    --no-matrix)  check_matrix=0 ;;
    --no-tests)   use_tests=0 ;;
    --no-docs)    use_docs=0 ;;
    --ci)         check_matrix=0; use_tests=0; use_docs=0 ;;
    --tag)        tag=${2:-}; shift ;;
    --version)    version=${2:-}; shift ;;
    -h|--help)    sed -n '2,24p' "$0"; exit 0 ;;
    *)            echo "unknown option: $1" >&2; exit 64 ;;
  esac
  shift
done

failures=0
check() { printf '\n== %s\n' "$1"; }
pass() { printf '  ok    %s\n' "$1"; }
skip() { printf '  skip  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n        %s\n' "$1" "$2"; failures=$((failures + 1)); }

pyproject_version=$("$PYTHON" - <<'PY' 2>/dev/null || true
import tomllib
with open("pyproject.toml", "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
PY
)
[ -n "$pyproject_version" ] || pyproject_version=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)
[ -n "$version" ] || version=$pyproject_version

check "version"
if [ -z "$pyproject_version" ]; then
  fail "pyproject.toml" "no [project] version found"
elif [ "$version" != "$pyproject_version" ]; then
  fail "version under test" "--version $version but pyproject.toml says $pyproject_version"
else
  pass "pyproject.toml says $pyproject_version"
fi
installed=$("$PYTHON" - "$ROOT" <<'PY' 2>/dev/null || true
import importlib.metadata as md, json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
dist = md.distribution("OperonDBS")
try:
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
except Exception:
    direct = {}
url = (direct.get("url") or "").removeprefix("file://")
# An editable install of another checkout (or a PyPI install) says nothing
# about this tree: report it as "OTHER" instead of a mismatch.
print("OTHER" if url and pathlib.Path(url).resolve() != root else dist.version)
PY
)
if [ -z "$installed" ]; then
  skip "OperonDBS is not installed for $PYTHON (editable metadata not checked)"
elif [ "$installed" = "OTHER" ]; then
  skip "the installed OperonDBS belongs to another checkout (editable metadata not checked)"
elif [ "$installed" = "$pyproject_version" ]; then
  pass "installed metadata matches ($installed)"
else
  fail "installed metadata" "$installed != $pyproject_version — re-run: pip install -e '.[dev]'"
fi

if [ "$use_tests" = 1 ]; then
  check "tests"
  if "$PYTHON" -m pytest -q; then
    pass "full suite"
  else
    fail "full suite" "python -m pytest failed (see the output above)"
  fi
else
  check "tests"
  skip "not run (--no-tests): the test workflow's own run is the evidence"
fi

if [ "$use_docs" = 1 ]; then
  check "docs"
  for language in en zh; do
    if "$PYTHON" -m sphinx -W --keep-going -b html "docs/$language" "docs/_build/$language/html" >/dev/null 2>&1; then
      pass "docs/$language builds with -W"
    else
      fail "docs/$language" "sphinx-build -W failed — re-run it without the redirect to read the warning"
    fi
  done
else
  check "docs"
  skip "not run (--no-docs)"
fi

check "registry"
registry_log=$(mktemp)
if [ "$use_tests" = 1 ]; then
  # --no-cov: the repository's coverage gate measures the whole suite, so a
  # single file has to be run without it.
  if "$PYTHON" -m pytest -q --no-cov tests/unit/test_defect_registry.py >"$registry_log" 2>&1; then
    pass "tests/unit/test_defect_registry.py"
  else
    fail "registry schema" "tests/unit/test_defect_registry.py failed"
    tail -12 "$registry_log" | sed 's/^/        /'
  fi
else
  skip "schema test not run (--no-tests/--ci): the suite's own run is the evidence"
fi
rm -f "$registry_log"
registry_report=$(PREFLIGHT_VERSION="$version" PREFLIGHT_TAG="$tag" "$PYTHON" - <<'PY' 2>&1
import os, subprocess, sys
import yaml

version = os.environ["PREFLIGHT_VERSION"]
tag = os.environ["PREFLIGHT_TAG"]
records = yaml.safe_load(open("defects.yml", encoding="utf-8"))["defects"]
released = [r for r in records if str(r.get("fixed_in") or "") == version]
problems = []
for record in released:
    if record["status"] != "verified" or not record.get("fix_commit"):
        problems.append(f"{record['id']} ships in {version} but is {record['status']} with fix_commit {record.get('fix_commit')}")
        continue
    if not tag:
        continue
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", record["fix_commit"], "HEAD"],
        capture_output=True,
    ).returncode == 0
    if not ancestor:
        problems.append(f"{record['id']}'s fix_commit {record['fix_commit'][:12]} is not in HEAD, which {tag} points at")
print(f"records shipping in {version}: {len(released)}")
for problem in problems:
    print(f"PROBLEM {problem}")
PY
)
printf '%s\n' "$registry_report" | sed 's/^/  /'
if printf '%s\n' "$registry_report" | grep -q '^PROBLEM'; then
  fail "released records" "a record that ships in $version is not verified/contained (see above)"
else
  pass "$(printf '%s\n' "$registry_report" | head -1)"
fi

check "matrix"
if [ "$run_matrix" = 1 ]; then
  if scripts/run-test-matrix.sh; then
    pass "cross-version matrix"
  else
    fail "cross-version matrix" "scripts/run-test-matrix.sh reported a failure"
  fi
elif [ "$check_matrix" = 0 ]; then
  skip "not checked (--no-matrix): CI runs the same versions itself"
else
  head_sha=$(head -1 .matrix/logs/head 2>/dev/null || true)
  head_at=$(sed -n '2p' .matrix/logs/head 2>/dev/null || true)
  now=$(git rev-parse HEAD)
  if [ -z "$head_sha" ]; then
    fail "matrix evidence" "no .matrix/logs/head — run: scripts/run-test-matrix.sh (or pass --run-matrix)"
  elif [ "$head_sha" != "$now" ]; then
    fail "matrix evidence" "the matrix ran on ${head_sha:0:12} ($head_at), HEAD is ${now:0:12} — re-run: scripts/run-test-matrix.sh"
  else
    bad=""
    for leg in 3.10 3.11 3.12 3.13 3.14 3.15; do
      status=$(cat ".matrix/logs/$leg.status" 2>/dev/null || echo "?")
      [ "$status" = "0" ] || bad="$bad $leg(exit=$status)"
    done
    if [ -n "$bad" ]; then
      fail "matrix evidence" "legs not green on ${now:0:12}:$bad"
    else
      pass "six legs green on ${now:0:12} (run at $head_at)"
    fi
  fi
fi

if [ -n "$tag" ]; then
  check "tag $tag"
  if [ "$tag" = "v$version" ]; then
    pass "name matches v<version>"
  else
    fail "tag name" "$tag != v$version"
  fi
  if [ "$(git cat-file -t "$tag" 2>/dev/null)" = "tag" ]; then
    pass "annotated"
  else
    fail "annotated" "$tag is not an annotated tag"
  fi
  # Three outcomes, not two: a keyring without the signing key cannot check the
  # signature at all, and reporting that as a failure is a false negative (it
  # failed the publish job for a correctly signed tag).  Check the signature
  # block whenever the keyring cannot, and report the limitation as a skip.
  signature=$(git verify-tag "$tag" 2>&1 || true)
  if printf '%s\n' "$signature" | grep -q "Good signature"; then
    pass "GPG signature verifies"
  elif printf '%s\n' "$signature" | grep -q "No public key"; then
    if git cat-file tag "$tag" | grep -q "BEGIN PGP SIGNATURE"; then
      skip "the tag carries a signature block, but this keyring has no public key for it (import one, e.g. curl -sSL https://github.com/<owner>.gpg | gpg --import)"
    else
      fail "GPG signature" "$tag has neither a verifiable nor a present signature"
    fi
  else
    fail "GPG signature" "git verify-tag $tag did not report a good signature"
  fi
  peeled=$(git rev-parse "$tag^{commit}" 2>/dev/null || true)
  if [ "$peeled" = "$(git rev-parse HEAD)" ]; then
    pass "peels to HEAD ($(git rev-parse --short HEAD))"
  else
    fail "tag target" "$tag peels to ${peeled:0:12}, HEAD is $(git rev-parse --short HEAD)"
  fi
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  echo "preflight: PASS — $version is safe to tag and release"
  exit 0
fi
echo "preflight: FAIL — $failures check(s) failed"
exit 1
