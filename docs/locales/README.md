# gettext catalogs

Reserved directory for a localized build. Nothing here is required while both
language trees are maintained by hand as mirrored Markdown: `docs/en/` and
`docs/zh/` are complete sources, and the two Read the Docs projects linked as
parent and translation publish them directly.

A project that would rather localize with gettext than maintain a second tree
drops its catalogs here, laid out as

```text
docs/locales/<sphinx-language>/LC_MESSAGES/<docname>.po
```

`conf_common.py` registers this directory as a Sphinx `locale_dirs` entry as
soon as it exists, with `gettext_compact = False` so that catalog names mirror
page names. Sphinx compiles `.po` files during the build, so publishing them
needs no extra tooling and no extra dependency. Extract the templates from the
tree that holds the source text with

```bash
sphinx-build -b gettext docs/en docs/_build/gettext
```

then start catalogs with `msginit`/`msgmerge` from the gettext CLI. The
Read the Docs project that publishes a catalog-driven language either points at
a `conf.py` declaring that language, or declares `language = "auto"` in
`conf_common.LANGUAGE_FROM_BUILD` and takes the language from Read the Docs'
`-D language=<dashboard language>` override.

The page tree under `docs/en/` and `docs/zh/` must keep the same relative file
paths (`tests/unit/test_docs_projects.py` enforces it), because the language
selector switches between the two projects' pages and the catalogs follow the
same names.
