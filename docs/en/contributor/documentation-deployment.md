# Documentation builds and Read the Docs deployment

## Documentation layout

The documentation is built with Sphinx, MyST Parser, and the Read the Docs theme while retaining Markdown source files. Every language is both a Sphinx project and a Read the Docs project of its own:

| Source tree | Sphinx configuration | Read the Docs project |
| --- | --- | --- |
| `docs/en/` | `docs/en/conf.py` | `operonproject` (parent), English |
| `docs/zh/` | `docs/zh/conf.py` | `operonproject-zh` (translation), Chinese (China) |

- `docs/conf_common.py`: everything the two projects share — version substitutions, MyST options, theme options, and the language guard below. A language `conf.py` only declares its own language, title suffix, and Read the Docs project;
- `.readthedocs.yaml`: build configuration of the parent (English) project;
- `.readthedocs-zh.yaml`: build configuration of the translation (Chinese) project;
- `docs/requirements.txt`: the dependency file both projects install (the `docs` extra of `pyproject.toml`);
- `docs/locales/`: catalog directory for a language maintained through gettext instead of a second Markdown tree; see the README inside it.

Read the Docs runs Sphinx from the directory holding the configuration file named in the project's `Build configuration file` setting, so `docs/en/` and `docs/zh/` are also the Sphinx source directories. That is the point of the split: each page of a language lives next to its own `conf.py`, each project builds one language, and each keeps its own search index, language-tagged HTML, and URL namespace.

The Chinese and English trees must use the same relative file paths, and `tests/unit/test_docs_projects.py` fails when they drift. When adding, moving, or deleting a page, update both trees and the relevant `toctree` entries together.

## Strict local build

Run both projects from the repository root with the project virtual environment:

```bash
.venv/bin/python -m pip install -e '.[docs]'
.venv/bin/sphinx-build -W --keep-going -b html docs/en docs/_build/en/html
.venv/bin/sphinx-build -W --keep-going -b html docs/zh docs/_build/zh/html
```

`-W` treats warnings as errors, while `--keep-going` reports as many problems as possible in one run. Both commands must complete without warnings before a documentation change is submitted; the CI `docs` job runs the same two commands. The generated `docs/_build/` directory is ignored by Git.

## Connect Read the Docs

1. Import the GitHub repository `HYLi360/Operon` in Read the Docs.
2. Parent project `operonproject`: leave `Build configuration file` at `.readthedocs.yaml` and its language at English, select the default branch to publish, and trigger the first build.
3. Create the translation project from the same repository, named `operonproject-zh`, with `Chinese (China)` as its language and `.readthedocs-zh.yaml` as its `Build configuration file`.
4. On the Translations page of the parent project, add `operonproject-zh`. Read the Docs then serves it under the parent's domain with the language prefix and lists it in the language selector.
5. After both builds finish, check the domain root (it redirects to the parent's language), the translation URL with its language prefix, the language selector in the sidebar, and one nested page per language.
6. Enable only the branches or tags that should be public in the Read the Docs version settings.

Dependencies are installed from the `docs` optional extra in `pyproject.toml`. Read the Docs uses Python 3.12 and fails the build on Sphinx warnings, matching the strict local build and the CI gate.

The language selector is drawn by the Read the Docs theme and filled in by the Read the Docs Addons flyout, so it appears only where a project has at least one linked translation. The retired language chooser page, the single shared `docs/conf.py`, and the hand-written sidebar switcher are gone: selecting a language now leaves the translated project's own home page, which is the platform-native behaviour. The published paths change as well — one English-language project used to serve the chooser at `/en/latest/` and both trees under `/en/latest/en/` and `/en/latest/zh/`, where two linked projects now serve English at `/en/latest/` and Chinese at `/zh-cn/latest/`. Add redirect rules under Admin -> Redirects if old links have to keep working.

### Language mismatch fails the build

Read the Docs passes the language of the project to Sphinx as `-D language=<language>`, which overrides whatever the configuration declares, so the dashboard is authoritative for what is published. The shared configuration compares the two and stops a build that disagrees, naming the project that needs fixing. Without that guard a Chinese project left at the default language would silently publish the English pages. Set the dashboard language to match the tree, or point the project at the configuration file of the tree it should build.

## Adding a language

1. Add `docs/<language>/` with the same relative pages as the other trees and a `conf.py` that calls `apply_shared_settings(globals(), language=..., title_suffix=..., rtd_project=...)`.
2. Add its Read the Docs build file next to the existing ones and register the language in `tests/unit/test_docs_projects.py`.
3. Create the Read the Docs project, set its language and `Build configuration file`, and add it to the parent project's Translations page.

A language maintained through gettext catalogs instead of a second Markdown tree declares `language = "auto"` in its `conf.py`; the build then takes its language from Read the Docs instead of the declaration. `docs/locales/README.md` describes the catalog layout and how to extract it.
