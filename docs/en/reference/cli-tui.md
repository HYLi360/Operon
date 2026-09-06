# Terminal UI (`operon tui`)

`operon tui` opens an interactive terminal user interface (TUI) for a project.
Read access uses short-lived read-only database connections, so the TUI is
safe to leave open while CLI commands run against the same project. Write
operations run the **same core functions as the CLI**, so audit
rows (`changes`), workflow provenance (`workflow_runs`), and semantics are
identical to the equivalent command. Every write follows the same pattern:
a form or plan preview → the equivalent CLI command shown in the dialog →
explicit Confirm → the mutation runs in a background worker → a notification
and panel reload, or an inline error message (the dialog stays open).

## Installation

The TUI is built on [Textual](https://textual.textualize.io/) and is included
in the standard `OperonDBS` installation and the frozen standalone build.

## Usage

```bash
operon [--project PATH] tui
```

The project is selected with the global `--project` option, exactly like
every other command.

## Startup screen

On startup, `operon` shows a night-lake illustration while the eight panels
load in background workers. Lake reflections represent sequencing reads;
stars and three constellations represent analysis results and insights.
The screen stays visible for at least one second after its first paint and
until every initial panel load has completed or reported an error. Failed
loads remain visible in the affected panels; press `r` there to retry.
During startup, navigation is disabled and `q` still quits.

The lower-left loading status and lower-right installed application version
are live English text, white on black. The bundled source artwork is
1024 × 768 pixels. For compatibility with ordinary terminal emulators,
Textual renders a pre-sampled companion with colored Unicode half blocks,
fitting the 4:3 scene to the terminal (assuming cells twice as tall as wide).
The subtitle uses terminal text to stay readable at small sizes. True-color
terminals give the best result; limited-color terminals reduce the palette.
No Kitty/Sixel image support or additional imaging dependency is required.
Terminal resolution determines the visible detail; it is not a pixel-exact
1024 × 768 image display.

## Screens

The left sidebar (or the number keys) switches between eight screens:

| Screen | Key | Contents |
|--------|-----|----------|
| Home | `1` | Project identity, entity counts, file count and total size, decision distribution, latest release, the 10 most recent workflow runs, and an "Attention needed" section (failed/interrupted runs, REVIEW/FAIL current decisions, files whose status is not healthy). The **Import dataset** button opens the import wizard (see below). |
| Entities | `2` | Hierarchy tree (organisms → samples → runs and assemblies → annotations) with the current state of each entity. Selecting a node shows its metadata fields, accessions, state, files, and the latest built-in QC and external-analysis (e.g. BUSCO/QUAST) metrics. Logically retired entities are shown by default, dimmed and struck-through; press `t` to hide them. Press `x` for the lifecycle dialog (see below). |
| Files | `3` | Filterable manifest table (substring filter plus status selector). Moving the cursor shows the full file record and its `file_locations` residency list. Statuses are color-coded: verified green, `REMOTE_ONLY` blue, `MISSING`/`CHECKSUM_FAILED` red. Press `i`/`v`/`q` for ingest, verify, and QC (see below). |
| Tasks | `4` | Workflow-run monitor (processing tasks, not sequencing runs) fed by the same read-only query as `operon workflow list`, with status/step/entity/limit filters. The table auto-refreshes every 2 seconds so running jobs update live; the cursor and scroll position survive each refresh. Press `enter` on a row for the full run record (the same sections as `operon workflow show`); press `esc` to go back. |
| Decisions | `5` | Current decisions from the `current_decisions` view (effective decision = curated override when present, marked `✎curated`), with profile/decision/text filters. Press `e` to evaluate, `c` to curate the selected row (see below). |
| Config | `6` | Structured, control-based editors for the project's configuration files (no free-text YAML editing): **QC Profiles** and **Tools & Recipes**. See below. |
| Publish | `7` | Immutable release builder and selective export builder (two tabs), each with a read-only preview before anything is written. See below. |
| Coverage | `8` | Imported NCBI Taxonomy snapshots, compiled reference sets, coverage report generation (`operon report coverage`), and a browser for existing `reports/coverage/COV_*` reports. See below. |

Global keys: `1`–`8` switch screens, `r` refreshes the current screen, `i`
opens the import dataset wizard (except when focus is inside the Files
screen, where `i` is ingest), `?` shows the key help, `q` quits (when the
Files table is focused, `q` starts a QC run instead — move focus elsewhere
or use the sidebar to leave).

## Write operations

Every dialog shows the equivalent CLI command, kept in sync with the form as
you type, and records exactly what that command would record.

| Key | Screen | Operation | Equivalent command |
|-----|--------|-----------|--------------------|
| `e` | Decisions | Evaluate decisions for all entities or the selected row's entity under a chosen profile; reports "N decisions evaluated". | `operon evaluate --profile … [--entity-type … --entity-id …]` |
| `c` | Decisions | Curate the selected decision: pick the new decision, reviewer (prefilled from `$USER`), required reason, optional evidence. Validation errors (retired entity, no automatic decision) appear inline without closing. | `operon curate --entity-type … --entity-id … --profile … --decision … --reviewer … --reason …` |
| `x` | Entities | Retire (or restore, for a retired entity) the selected entity. The dialog first loads the read-only impact plan (affected entities/files/references, physical changes — always zero for logical retirement) and blocks Confirm when the plan reports no change; a reason code is required for RETIRE. | `operon retire\|restore <id> --reason … [--reason-code …] --apply --yes` |
| `i` | Files | Ingest a file (local path or `sftp://`/`remote://` URL) into `raw/`, prefilled from the selected row. Format/compression auto-detect when left blank. A checksum conflict (same entity+role, different bytes) is shown inline in red and never overwrites. | `operon ingest --source … --entity-type … --entity-id … --role …` |
| `v` | Files | Verify the selected file, or all files after a "verify all N files?" confirm. Failures (`MISSING`, `CHECKSUM_FAILED`, …) are listed in an error dialog. | `operon verify [--file-id …]` |
| `q` | Files | Run built-in QC for the selected file or all files, with a live progress bar ("k/n · current file_id"). The completion notification mirrors the CLI text ("QC complete: ok/total file(s) passed built-in stages"); failures are listed in an error dialog. Cancel stops the batch cooperatively *between* files — results for files already processed are kept. | `operon qc [--file-id …]` |
| `i` | global | Open the import dataset wizard (also via the Home button; on the Files screen `i` stays ingest). | `operon import dataset` |
| — | Publish | Create an immutable release after a members/exclusions preview; a duplicate version is rejected inline. | `operon release --version … --profile … [--copy-files\|--link hardlink]` |
| — | Publish | Materialize a selective export after a count/bytes preview; a non-empty output directory is rejected inline. | `operon export --output … [--entity-type … --entity-id … --file-role … --format … --state … --decision … --profile …] [--link …] [--no-qc]` |
| — | Coverage | Generate a taxonomy coverage report; a result below the profile thresholds is a warning notification (FAIL), never a crash. | `operon report coverage --reference-set … [--release …]` |

All of these append the same `changes` audit rows and `workflow_runs`
provenance records as the CLI, so operations performed in the TUI are
indistinguishable from command-line ones in reports and exports.

## Import dataset wizard

The wizard (Home → **Import dataset**, or the global `i` key) is a Textual
port of `operon import dataset`. It walks one form page per section —
**Source → Organism → Sample → Sequencing → Assembly → Annotation →
Files** — with Next/Back navigation and per-field validation identical to
the questionary flow (required source database/provider; non-INSDC sources
additionally require a citation and a license; file paths must exist).
Organism/sample/assembly/annotation pickers list the existing non-retired
entities for reuse, or "Create a new …" allocates a fresh internal ID
(`db.next_id`). Sequencing and Annotation are optional sections (checkbox);
the annotation file roles (GFF3/CDS/protein) and read roles (R1/R2/single)
only appear when the corresponding section is enabled.

The final **Summary** page renders the exact plan and warnings produced by
the questionary wizard's own `_summary`/`_warnings` helpers, offers
non-linear "Edit <section>" jumps, and executes only after an explicit
**Execute import**. The commit goes through the same single-transaction
`import_wizard._commit` as the CLI wizard — data-source registration, entity
rows, `entity_state`, `changes` audit, file ingest with staged-file rollback
on failure, and run logging are identical. On success a notification lists
the created entity IDs and file count; errors are shown inline and nothing
is left half-written.

## Publish screen

**Release tab.** A table of existing releases (version, creation time,
profile, accepted/excluded counts) above the builder form: version, profile
selector, "copy files" checkbox (maps to `--copy-files`) and link kind
(copy/hardlink). The **Preview** (also triggered by changing the profile)
runs the read-only `release_files_for`/`release_exclusions_for` core queries
and shows the member count and total bytes plus the exclusions table
(entity, effective decision, exclusion reason, reason codes) — the exact
content the release would publish. **Create release** asks for confirmation
with the equivalent CLI command, then builds the release in a background
worker through `operon.release.create_release`; a duplicate version and an
unevaluated/stale entity set are reported inline.

**Export tab.** Filter form (entity type, entity id, file role, format,
state, decision + profile — a decision filter without a profile is rejected
inline, mirroring the CLI), link kind (copy/hardlink/symlink), an
`include_qc` checkbox, and the output directory. **Preview** counts the
matching files and their total bytes without writing anything (using the
same `_select_files` selection as the export itself). **Run export**
confirms with the equivalent CLI command and materializes the export through
`operon.export.export_files`; an output directory that exists and is not
empty is rejected before the dialog opens.

## Coverage screen

The top half lists the imported NCBI Taxonomy snapshots (`taxonomy list`
data) and the compiled reference sets (`taxonomy reference-sets` data). The
**Generate report** form picks a reference set and a scope — project
metadata or a frozen release (release scope adds a release selector) — and
confirms with the equivalent `operon report coverage` command. Identical
inputs reuse the cached immutable report; when ranks miss their thresholds
the result is a warning notification with the per-rank coverage and the
report path (the CLI maps the same result to exit code 1), not an error
dialog.

The **Reports** table lists every `reports/coverage/COV_*` directory with
its provenance headline (reference set, scope, decision, creation time).
Selecting a row renders the report's TSVs — summary, targets, missing,
observations, excluded — as tabbed tables (parsed with the standard
library; large tables are capped at 500 rows).

## Config screen

The Config screen edits the two versioned configuration files with structured
forms whose values are re-composed into valid YAML by the backend — there is
no free-text editor, so a save can never produce a syntactically invalid
file. Keys the forms do not model (`value_by`, `source`, `unknown`,
`result_glob`, parameter spec details, …) are **preserved verbatim** and
shown as dim read-only notes, never silently dropped.

**Save-as-version semantics.** Every save writes a *new version*: the
`version` field is bumped (`old + 1`; `1` for a new profile/recipe) and a
content-addressed snapshot is recorded — with exactly the same canonical
document the CLI records, so a TUI save and a later `operon evaluate` /
`operon analyze` of identical content map to the same snapshot row. Saving
unchanged content is a no-op: the version is not bumped and no snapshot is
recorded. Every save is confirmed in a dialog that shows the effect
("writes `config/profiles/<name>.yaml` as version N + records snapshot"),
and validation errors are shown inline without touching the file (a failed
write is rolled back to the previous file bytes). Configuration publication
uses atomic file replacement. Rollback also covers database opening, snapshot
insertion/commit, and handled interruptions: existing bytes (including line
endings) are restored, or a newly created configuration file is removed.

**History and restore.** The History dialog lists the recorded snapshots
(snapshot id, version, sha256 prefix, recording time, usage count) like
`operon profiles history` / `operon recipes history`. *View* renders a
snapshot document read-only as YAML; *Restore* loads the snapshot into the
editor — saving it then creates the **next** version. Snapshots are never
overwritten in place.

**QC Profiles tab.** Left: the `kind: qc` profiles found in
`config/profiles/` (name + version). Right: the editor — description, the
five `applies_to` checkboxes, a read-only version note, and two rule sections
(required / warnings) where each rule is a row of metric, operator (Select
over the operators the rule engine supports), value, and code inputs plus a
remove button; "add rule" appends a row per section. *New profile* prompts
for a name and starts from a minimal skeleton. Numeric-looking values are
stored as numbers. `taxonomy_coverage` profiles are not editable here.

**Tools & Recipes tab.** A tools table (name, executable, run method) with a
*Check tools* button — the equivalent of `operon tools-check`, run in a
background worker with per-row live updates (detected version in green,
`MISSING` in red) and a summary notification; one broken tool never breaks
the batch. Below, a recipes table (name, version, tool, entity type, file
role, format); selecting a recipe opens its editor: description, entity type
(Select, blank = `*`), file role, format, database, database version, output
subdirectory and suffix inputs, `arguments` as one-per-line text
(placeholders like `${input}` stay visible), runtime `parameters` as
`name=default` lines (other spec keys are preserved), the result parser
Select (`none`, `blast_tabular`, `hmmer_tblout`, `busco_json`),
`result_columns` / `hit_metric_columns` as comma-separated inputs, and
`max_hits_per_query`.

> **Note (tools.yaml formatting):** saving a recipe from the TUI rewrites
> `config/tools.yaml` with normalized YAML formatting and drops hand-written
> comments. No content is lost: every saved version is preserved verbatim in
> the `recipe_snapshots` table (`operon recipes history` / `operon recipes
> show`). Hand-editing the file remains fully supported — the TUI is the
> audited alternative.
