"""Shared Sphinx configuration for the per-language Operon documentation.

The documentation is one Sphinx project per language and one Read the Docs
project per language:

===============  ====================  ===============================
Source tree      Sphinx configuration  Read the Docs project
===============  ====================  ===============================
``docs/en/``     ``docs/en/conf.py``   ``docs/en/operonproject`` (parent)
``docs/zh/``     ``docs/zh/conf.py``   ``docs/zh/operonproject-zh``
===============  ====================  ===============================

Read the Docs links the two projects as parent and translation; that link is
dashboard state (Admin -> Settings -> Translations), not repository state.
Each project also names the build configuration file it uses in its
``Build configuration file`` setting, so the repository ships
``docs/en/.readthedocs.yaml`` for the parent project and
``docs/.readthedocs-zh.yaml`` for the translation.

Read the Docs runs Sphinx from the directory that holds the configuration
file. That directory is the source directory — every page of a language lives
next to its own ``conf.py`` — and Read the Docs always passes
``-D language=<dashboard language>``, which overrides ``language`` here. The
dashboard is therefore authoritative for the published site, and a project
whose dashboard language contradicts its configuration fails the build (see
``_check_declared_language``) instead of silently publishing the wrong
language.

Everything the language projects share — version markers, MyST settings,
theme options, the gettext entry points — lives in this module; each
``conf.py`` only declares its own language and title.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from pathlib import Path

from sphinx.errors import ConfigError

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent

#: gettext catalogs, one ``.po`` per document, under
#: ``docs/locales/<language>/LC_MESSAGES/<docname>.po``. The directory is
#: optional: only a project that localizes through gettext registers it, so a
#: Markdown-only checkout never gets a missing-locale warning. See
#: ``docs/locales/README.md``.
LOCALES_DIR = DOCS_DIR / "locales"

#: Declare this as a project's ``language`` when the build decides instead of
#: the configuration — the case for a project that reuses another tree's
#: sources with gettext catalogs and takes its language from Read the Docs'
#: ``-D language=``.
LANGUAGE_FROM_BUILD = "auto"

#: ``(language, Read the Docs project)`` pairs declared by the ``conf.py``
#: files loaded in this Sphinx process.
_DECLARED_PROJECTS: list[tuple[str, str]] = []


def normalize_language(code: str) -> str:
    """Return *code* in Sphinx's spelling, from any usual spelling.

    Read the Docs stores and publishes lowercase-dashed codes (``zh-cn``),
    while Sphinx wants ``ll_CC`` (``zh_CN``); ``en`` is the same in both.
    """

    primary, _, region = code.strip().lower().replace("-", "_").partition("_")
    return f"{primary}_{region.upper()}" if region else primary


def package_version() -> str:
    """Return the released ``operon`` version."""

    try:
        return _distribution_version("OperonDBS")
    except PackageNotFoundError:
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)["project"]["version"]


def source_constant(module: str, name: str) -> str:
    """Read a module-level string constant, falling back to the source file."""

    try:
        imported = __import__(f"operon.{module}", fromlist=[name])
        return str(getattr(imported, name))
    except Exception:
        source = (REPO_ROOT / "operon" / f"{module}.py").read_text(encoding="utf-8")
        match = re.search(rf'^{name} = "([^"]+)"', source, re.MULTILINE)
        if match is None:
            raise RuntimeError(f"cannot resolve {name} from operon/{module}.py")
        return match.group(1)


def apply_shared_settings(
    namespace: dict,
    *,
    language: str,
    title_suffix: str,
    rtd_project: str,
) -> None:
    """Fill *namespace* (a ``conf.py``'s globals) with the shared settings.

    ``language`` is the language this tree is written in, ``title_suffix``
    completes the HTML title (``documentation`` / ``文档``), and
    ``rtd_project`` names the Read the Docs project so that the language guard
    can point at the right dashboard page.
    """

    release = package_version()
    author = "Project Operon Development Group"

    namespace.update(
        # `conf.py` is an extension: Sphinx picks `setup` out of its namespace
        # and runs it before `config-inited`, which is where the language guard
        # hangs off. Registering it here keeps the language `conf.py` files
        # free of boilerplate.
        setup=setup,
        release=release,
        version=release,
        project="Operon",
        author=author,
        copyright=f"2026, {author}. All Rights Reserved",
        language=language,
        # Markdown sources reference these as {{ operon_version }} /
        # {{ db_schema }} / {{ metadata_schema }} in paragraph text.
        # Substitutions do not expand inside code spans or fenced code blocks,
        # so examples there use `<version>` placeholders instead. Historical
        # version mentions stay literal and are guarded by
        # tests/unit/test_docs_versions.py.
        myst_substitutions={
            "operon_version": release,
            "db_schema": source_constant("database", "SCHEMA_VERSION"),
            "metadata_schema": source_constant("schema", "METADATA_SCHEMA_VERSION"),
        },
        extensions=["myst_parser"],
        source_suffix={".md": "markdown"},
        root_doc="index",
        exclude_patterns=["_build", "Thumbs.db", ".DS_Store"],
        nitpicky=True,
        suppress_warnings=["myst.header"],
        myst_heading_anchors=4,
        myst_enable_extensions=[
            "colon_fence",
            "deflist",
            "fieldlist",
            "substitution",
        ],
        # One catalog per document, so that the catalogs of a future gettext
        # project mirror the page names of this tree.
        gettext_compact=False,
        html_theme="sphinx_rtd_theme",
        html_title=f"Operon {release} {title_suffix}",
        # The version and language selectors are rendered by the theme on Read
        # the Docs and filled in by the Read the Docs Addons flyout: the
        # language selector appears once the translation project is linked to
        # this one in the dashboard.
        html_theme_options={
            "collapse_navigation": False,
            "navigation_depth": 4,
            "sticky_navigation": True,
            "titles_only": True,
            "version_selector": True,
            "language_selector": True,
        },
    )

    if LOCALES_DIR.is_dir():
        namespace["locale_dirs"] = [str(LOCALES_DIR)]

    _DECLARED_PROJECTS.append((language, rtd_project))


def _check_declared_language(app, config) -> None:
    """Refuse to build a language the configuration does not declare.

    Read the Docs overrides ``language`` with the language of the project, so a
    mismatch means the two settings disagree about what is being published.
    Failing here — with the dashboard page named — is the difference between a
    loud build error and a Chinese project that quietly serves English pages.
    """

    built = normalize_language(config.language)
    mismatched = [
        (declared, project)
        for declared, project in _DECLARED_PROJECTS
        if declared != LANGUAGE_FROM_BUILD and normalize_language(declared) != built
    ]
    if not mismatched:
        return

    declared, project = mismatched[0]
    raise ConfigError(
        f"the {project!r} project is configured to build language {built!r} "
        "(Read the Docs passes the language of a project as `-D language=...`, "
        "which wins over everything set here) while this configuration declares "
        f"{declared!r}. Set the language of {project!r} in Admin -> Settings to "
        "the language of this tree, or point the project at the configuration "
        "file of the tree it should build. A project that is meant to build a "
        "language it does not declare (for example one driven by gettext "
        f"catalogs) declares language = {LANGUAGE_FROM_BUILD!r} instead."
    )


def setup(app):
    app.connect("config-inited", _check_declared_language)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
