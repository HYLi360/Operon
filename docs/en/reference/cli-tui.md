# Terminal UI (`operon tui`)

`operon tui` opens an interactive terminal user interface (TUI) for a project.
Read access uses short-lived read-only database connections, so the TUI is
safe to leave open while CLI commands run against the same project. Write
operations (phase 2) run the **same core functions as the CLI**, so audit
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

## Screens

The left sidebar (or the number keys) switches between six screens:

| Screen | Key | Contents |
|--------|-----|----------|
| Home | `1` | Project identity, entity counts, file count and total size, decision distribution, latest release, the 10 most recent workflow runs, and an "Attention needed" section (failed/interrupted runs, REVIEW/FAIL current decisions, files whose status is not healthy). |
| Entities | `2` | Hierarchy tree (organisms → samples → runs and assemblies → annotations) with the current state of each entity. Selecting a node shows its metadata fields, accessions, state, files, and the latest built-in QC and external-analysis (e.g. BUSCO/QUAST) metrics. Logically retired entities are shown by default, dimmed and struck-through; press `t` to hide them. Press `x` for the lifecycle dialog (see below). |
| Files | `3` | Filterable manifest table (substring filter plus status selector). Moving the cursor shows the full file record and its `file_locations` residency list. Statuses are color-coded: verified green, `REMOTE_ONLY` blue, `MISSING`/`CHECKSUM_FAILED` red. Press `i`/`v`/`q` for ingest, verify, and QC (see below). |
| Tasks | `4` | Workflow-run monitor (processing tasks, not sequencing runs) fed by the same read-only query as `operon workflow list`, with status/step/entity/limit filters. The table auto-refreshes every 2 seconds so running jobs update live; the cursor and scroll position survive each refresh. Press `enter` on a row for the full run record (the same sections as `operon workflow show`); press `esc` to go back. |
| Decisions | `5` | Current decisions from the `current_decisions` view (effective decision = curated override when present, marked `✎curated`), with profile/decision/text filters. Press `e` to evaluate, `c` to curate the selected row (see below). |
| Config | `6` | Structured, control-based editors for the project's configuration files (no free-text YAML editing): **QC Profiles** and **Tools & Recipes**. See below. |

Global keys: `1`–`6` switch screens, `r` refreshes the current screen, `?`
shows the key help, `q` quits (when the Files table is focused, `q` starts a
QC run instead — move focus elsewhere or use the sidebar to leave).

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

All of these append the same `changes` audit rows and `workflow_runs`
provenance records as the CLI, so operations performed in the TUI are
indistinguishable from command-line ones in reports and exports.

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
write is rolled back to the previous file bytes).

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
