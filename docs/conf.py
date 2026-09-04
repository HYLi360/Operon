"""Sphinx configuration for the bilingual Operon documentation."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


DOCS_DIR = Path(__file__).resolve().parent

project = "Operon"
author = "Operon contributors"
copyright = "2026, Operon contributors"

try:
    release = version("OperonDBS")
except PackageNotFoundError:
    release = "0.6.2"
version = release

extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
root_doc = "index"
templates_path = ["_templates"]

# The source is already maintained as parallel Chinese and English trees. MyST
# resolves their relative Markdown links as Sphinx cross-references, while the
# toctrees provide one coherent navigation hierarchy for both languages.
myst_heading_anchors = 4
myst_enable_extensions = ["colon_fence", "deflist", "fieldlist"]

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
nitpicky = True
suppress_warnings = ["myst.header"]

html_theme = "sphinx_rtd_theme"
html_title = f"Operon {release} documentation"
html_static_path = ["_static"]
html_css_files = ["operon.css"]
html_js_files = ["language-switcher.js"]
html_theme_options = {
    "collapse_navigation": False,
    "navigation_depth": 4,
    "sticky_navigation": True,
    "titles_only": True,
}


def _counterpart_page(pagename: str) -> tuple[str, str] | None:
    """Return the translated page name and its link label, when available."""

    if pagename == "index":
        return None
    if pagename.startswith("zh/"):
        counterpart = f"en/{pagename.removeprefix('zh/')}"
        label = "English"
    elif pagename.startswith("en/"):
        counterpart = f"zh/{pagename.removeprefix('en/')}"
        label = "中文"
    else:
        return None

    if (DOCS_DIR / f"{counterpart}.md").is_file():
        return counterpart, label
    return None


def _add_language_context(app, pagename, templatename, context, doctree) -> None:
    """Expose the mirror page URL to the small client-side language switcher."""

    if pagename.startswith("zh/"):
        context["language"] = "zh-CN"
    else:
        context["language"] = "en"

    counterpart = _counterpart_page(pagename)
    if counterpart is None:
        context["operon_language_url"] = ""
        context["operon_language_label"] = ""
        return

    counterpart_page, label = counterpart
    context["operon_language_url"] = app.builder.get_relative_uri(
        pagename, counterpart_page
    )
    context["operon_language_label"] = label


def setup(app):
    app.connect("html-page-context", _add_language_context)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
