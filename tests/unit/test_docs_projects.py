"""Guard: the documentation keeps one Sphinx project per language.

Each language owns a source tree, a Sphinx configuration, and the Read the Docs
build file naming it: ``docs/<language>/conf.py`` and
``docs/<language>/.readthedocs.yaml`` — Read the Docs accepts no other file name
there, so the language lives in the directory and the dashboard points each
project at the matching path. Read the Docs links those projects as parent and
translation, so a tree that drops a page, a configuration that stops going
through the shared module, or a build file pointing at another language's
configuration, would silently publish the wrong pages.

Everything here is checked by parsing files instead of importing them: the test
extra installs no Sphinx, so the documentation must stay verifiable without it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"

#: Language directory -> (Sphinx language, Read the Docs project, build file).
PROJECTS = {
    "en": ("en", "operonproject", "docs/en/.readthedocs.yaml"),
    "zh": ("zh_CN", "operonproject-zh", "docs/zh/.readthedocs.yaml"),
}

BUILD_FILES = sorted(project[2] for project in PROJECTS.values())


def _settings_call(conf_path: Path) -> ast.Call:
    """Return the ``apply_shared_settings(...)`` call of a language ``conf.py``."""

    module = ast.parse(conf_path.read_text(encoding="utf-8"), filename=str(conf_path))
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute):
            name = function.attr
        elif isinstance(function, ast.Name):
            name = function.id
        else:
            continue
        if name == "apply_shared_settings":
            return node
    raise AssertionError(
        f"{conf_path.relative_to(REPO_ROOT)} must configure itself through "
        "docs/conf_common.py: apply_shared_settings(globals(), ...) keeps the "
        "version substitutions, the theme options, and the language guard"
    )


def _keyword(call: ast.Call, name: str, conf_path: Path) -> str:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    raise AssertionError(
        f"{conf_path.relative_to(REPO_ROOT)} does not pass {name}= to "
        "apply_shared_settings"
    )


@pytest.mark.parametrize("directory", sorted(PROJECTS))
def test_language_project_is_complete(directory):
    language, rtd_project, _ = PROJECTS[directory]
    conf_path = DOCS_DIR / directory / "conf.py"

    assert conf_path.is_file(), f"docs/{directory}/conf.py is missing"
    assert (DOCS_DIR / directory / "index.md").is_file(), (
        f"docs/{directory}/index.md is the root document of the {directory} project"
    )

    call = _settings_call(conf_path)
    declared = _keyword(call, "language", conf_path)
    assert declared == language, (
        f"docs/{directory}/conf.py declares language {declared!r}; the "
        f"{directory} project is {language!r}"
    )
    assert _keyword(call, "rtd_project", conf_path) == rtd_project, (
        f"docs/{directory}/conf.py names the wrong Read the Docs project; the "
        f"language guard points the maintainer at the project named here"
    )


def test_language_trees_mirror_each_other():
    trees = {
        directory: {
            page.relative_to(DOCS_DIR / directory).as_posix()
            for page in (DOCS_DIR / directory).rglob("*.md")
        }
        for directory in PROJECTS
    }
    english, chinese = trees["en"], trees["zh"]

    assert english == chinese, (
        "docs/en and docs/zh must keep the same relative page paths — the "
        f"language selector pairs them page by page; only in en: {sorted(english - chinese)}; "
        f"only in zh: {sorted(chinese - english)}"
    )
    assert english, "no documentation pages found"


@pytest.mark.parametrize("directory", sorted(PROJECTS))
def test_readthedocs_build_file_targets_its_language(directory):
    _, _, build_file = PROJECTS[directory]
    config = yaml.safe_load((REPO_ROOT / build_file).read_text(encoding="utf-8"))

    assert config["version"] == 2, f"{build_file} must stay a v2 configuration"
    assert config["sphinx"]["configuration"] == f"docs/{directory}/conf.py", (
        f"{build_file} must build docs/{directory}/conf.py; Read the Docs runs "
        "Sphinx from the directory holding that file, which makes it the source "
        "directory of the project"
    )
    assert config["sphinx"]["fail_on_warning"] is True, (
        f"{build_file} must keep failing the build on warnings, matching the "
        "strict local build and the CI docs job"
    )
    assert config["python"]["install"] == [{"requirements": "docs/requirements.txt"}], (
        f"{build_file} must install the documentation extra both projects share"
    )


def test_readthedocs_build_files_are_all_registered():
    """Read the Docs accepts only ``.readthedocs.yaml`` as the file name.

    The language therefore lives in the directory, never in the file name: each
    tree carries one ``.readthedocs.yaml`` next to its ``conf.py``, and its Read
    the Docs project is pointed at exactly that path in the dashboard. A build
    file at the repository root, or a second one somewhere under a language
    tree, would be a configuration no project publishes.
    """

    root_level = sorted(path.name for path in REPO_ROOT.glob(".readthedocs*"))
    assert root_level == [], (
        "the build configurations belong to the language directories, not to "
        f"the repository root: {root_level}"
    )

    found = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in DOCS_DIR.rglob(".readthedocs*")
        if "_build" not in path.parts
    )
    assert found == BUILD_FILES, (
        "every Read the Docs build file needs a language project: found "
        f"{found}, registered {BUILD_FILES}"
    )


@pytest.mark.bug("ODR-0025")
def test_every_workflow_builds_both_language_trees():
    """A workflow that builds the documentation builds every language project.

    ``docs/`` stopped being a Sphinx source directory when each language got
    its own project, so a workflow left on the old command fails instead of
    building anything — which is how the publish workflow's documentation job
    broke after the split (ODR-0025).  Every ``sphinx-build`` line has to name
    the language directory it builds.
    """

    built: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            command = line.strip()
            for prefix in ("- run:", "run:"):
                if command.startswith(prefix):
                    command = command[len(prefix):].strip()
                    break
            words = command.split()
            if not words or words[0] != "sphinx-build":
                continue
            built.setdefault(path.name, []).extend(
                word for word in words[1:] if word.startswith(("docs/en", "docs/zh"))
            )

    assert built, "no workflow builds the documentation"
    for name, directories in built.items():
        assert sorted(directories) == ["docs/en", "docs/zh"], (
            f".github/workflows/{name} must build both language trees; it names "
            f"{directories or ['docs']} — the pre-split source directory no "
            "longer holds a conf.py"
        )
