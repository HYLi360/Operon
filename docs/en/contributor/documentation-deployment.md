# Documentation builds and Read the Docs deployment

## Documentation layout

The documentation is built with Sphinx, MyST Parser, and the Read the Docs theme while retaining Markdown source files:

- `docs/zh/`: Chinese documentation;
- `docs/en/`: English documentation;
- `docs/index.md`: language-neutral landing page;
- `docs/conf.py`: shared Sphinx configuration;
- `.readthedocs.yaml`: Read the Docs environment and installation steps.

The Chinese and English trees must use the same relative file paths. The sidebar language link uses this convention to open the same page in the other language. When adding, moving, or deleting a page, update both trees and the relevant `toctree` entries together.

## Strict local build

Run the following from the repository root with the project virtual environment:

```bash
.venv/bin/python -m pip install -e '.[docs]'
.venv/bin/sphinx-build -W --keep-going -b html docs docs/_build/html
```

`-W` treats warnings as errors, while `--keep-going` reports as many problems as possible in one run. The generated `docs/_build/` directory is ignored by Git. This command must complete without warnings before a documentation change is submitted.

## Connect Read the Docs

1. Import the GitHub repository `HYLi360/Operon` in Read the Docs.
2. Keep the configuration path set to `.readthedocs.yaml` at the repository root.
3. Select the default branch to publish and trigger the first build.
4. After the build, check the landing page, `/zh/`, `/en/`, and the language switch on a nested page.
5. Enable only the branches or tags that should be public in the Read the Docs version settings.

Dependencies are installed from the `docs` optional extra in `pyproject.toml`. RTD uses Python 3.12 and fails the build on Sphinx warnings, matching the strict local build and the CI gate.

The current setup publishes the two existing complete Markdown trees in one RTD project and does not require converting the source into gettext PO files. If the project later needs the platform-native language menu, separate search indexes, or independent translation-version lifecycles, create linked translation projects in RTD. The current bilingual source and mirrored relative paths can still be reused during that migration.

Official standalone application releases use the same `docs/conf.py`. Before freezing the application, `python tools/build.py` runs the strict Sphinx build and places its output in `share/doc/operon/html/` inside the release. Any documentation warning or missing language entry point prevents publication.
