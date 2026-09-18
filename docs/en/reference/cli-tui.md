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

Scientific-name displays use italics, with standalone rank abbreviations
`subsp.`, `ssp.`, `var.`, `subvar.`, `f.`, and `subf.` in upright type.
Original text and spacing are preserved; other components retain italics.

## Installation

The TUI is built on [Textual](https://textual.textualize.io/) and is included
in the standard `OperonDBS` installation.

## Usage

```bash
operon [--project PATH] tui
```

The project is selected with the global `--project` option, exactly like
every other command.

## Startup screen

On startup, `operon` shows a night-lake illustration while the eight panels
load in background workers.

The screen stays visible for at least 2 second after its first paint and
until every initial panel load has completed or reported an error. Failed
loads remain visible in the affected panels; press `r` there to retry.
During startup, navigation is disabled and `ctrl+q` still quits.

The lower-left loading status and lower-right installed application version
are live English text, white on black. The source artwork is 1024 × 768 pixels.
The startup screen automatically chooses a renderer:

- **Text:** Linux TTY, unknown/16-color terminals, or `NO_COLOR` use a centered
  rounded-square block-letter OPERON logo and a spaced subtitle. Basic ANSI
  cyan/white keeps the original palette without RGB escape codes; `NO_COLOR`
  uses monochrome. Non-UTF-8 output uses ASCII `#` blocks; narrow windows use
  ordinary text.
- **Color blocks:** 256-color and true-color terminal hints select the existing
  night-lake half-block illustration, with a readable terminal-text subtitle.
- **Kitty:** `TERM=xterm-kitty` outside tmux/screen selects native PNG display
  using the [Kitty graphics protocol](https://sw.kovidgoyal.net/kitty/graphics-protocol/).
  PNG bytes travel through the terminal connection in base64 chunks, so SSH
  needs no shared filesystem or remote `kitten` command. The image is fitted
  above the footer, repositioned on resize, and removed on dismissal, quit,
  or when another modal covers the splash. Only this splash's image is deleted.

SSH alone does not determine color/image support; the remote terminal hints
must describe the local emulator. Automatic Kitty selection is conservative
and based on `TERM`, not an active protocol probe. tmux/screen use character
rendering automatically. Unsupported graphics requests leave the character
illustration underneath; image-resource or output errors fall back to text.
No new runtime dependency is required. Fit assumes cells twice as tall as wide.

Override detection with `OPERON_SPLASH=auto|text|blocks|kitty`, for example:

```bash
OPERON_SPLASH=text operon --project PATH tui
OPERON_SPLASH=kitty operon --project PATH tui
```

Use `kitty` only when the terminal and any intermediary support the protocol;
this explicit override also bypasses `NO_COLOR`. An unknown value uses auto
selection. The minimum startup duration and data-readiness gate are unchanged.

## Screens

The left sidebar (or the number keys) switches between eight screens:

| Screen | Key | Contents |
|--------|-----|----------|
| Home | `1` | Project identity, entity counts, file count and total size, decision distribution, latest release, the 10 most recent workflow runs, and an "Attention needed" section (failed/interrupted runs, REVIEW/FAIL current decisions, files whose status is not healthy). Press `i` to open the import wizard (see below). |
| Entities | `2` | Hierarchy tree (organisms → samples → runs and assemblies → annotations) with the current state of each entity. Selecting a node shows its metadata fields, accessions, state, files, and the latest built-in QC and external-analysis (e.g. BUSCO/QUAST) metrics. Logically retired entities are shown by default, dimmed and struck-through; press `t` to hide them. Press `x` for the lifecycle dialog (see below). |
| Files | `3` | Filterable manifest table (substring filter plus status selector). Moving the cursor shows the full file record, its `file_locations` residency list, and a *Sequence labels* section (the `classify-sequences` output aggregated per label and profile). Statuses are color-coded: verified green, `REMOTE_ONLY` blue, `MISSING`/`CHECKSUM_FAILED` red. Press `i`/`v`/`q`/`l` for ingest, verify, QC, and the label browser (see below). |
| Tasks | `4` | Workflow-run monitor (processing tasks, not sequencing runs) fed by the same read-only query as `operon workflow list`, with status/step/entity/limit filters plus an advanced row mirroring the CLI's `--from`/`--to` (ISO-8601; an invalid value or `--from` ≥ `--to` is an inline error), `--run-id`, `--parent-run-id`, `--tool`, `--executor`, `--offset` and `--oldest-first` (`--resumes-run-id` and machine formats stay CLI-only). The table loads on entry and refreshes on demand with `r` (no background polling); the cursor and scroll position survive each refresh. Press `enter` on a row for the full run record (the same sections as `operon workflow show`); press `esc` to go back. On a *running* run's detail screen, *Follow logs* appends the local `logs/<run_id>.stdout.log`/`.stderr.log` tails once per second until the run leaves `running`, then reports the final status — it only observes and never cancels (with the SSH backend logs are pulled back at completion, so nothing streams until then). The *Analysis jobs* button opens a read-only `analysis_jobs` browser (analysis/status/limit filters, with the selected row's full error and artifact paths) that also lists tasks interrupted inside a job array — rows that never get a `workflow_runs` row. The *Environments* button browses captured execution environments (the same list as `operon environments list`) and renders *View JSON*, *Export explicit* and *Export yaml* read-only — writing a conda spec to a file stays a CLI redirection (errors such as a snapshot without a package inventory are shown inline). The *New analysis* and *Run external* buttons open the analysis and external-command dialogs; the *Analysis hits* button opens the alignment-hit browser (`report analysis --hits` columns and filters, with *Export* matching the CLI's `--out` byte for byte) (see below). |
| Decisions | `5` | Current decisions from the `current_decisions` view (effective decision = curated override when present, marked `✎curated`), with profile/decision/text filters. Press `e` to evaluate, `c` to curate the selected row (see below). |
| Config | `6` | Structured, control-based editors for the project's configuration files (no free-text YAML editing): **QC Profiles** (both `kind: qc` and `kind: sequence_classification` profiles, each with its own form) and **Tools & Recipes**. See below. |
| Publish | `7` | Immutable release builder and selective export builder (two tabs), each with a read-only preview before anything is written. See below. |
| Coverage | `8` | Imported NCBI Taxonomy snapshots, compiled reference sets, coverage report generation (`operon report coverage`), and a browser for existing `reports/coverage/COV_*` reports. See below. |

Global keys: `1`–`8` switch screens, `r` refreshes the current screen, `i`
opens the import dataset wizard (except when focus is inside the Files
screen, where `i` is ingest), `?` shows the key help, `ctrl+q` quits.

## Write operations

Every dialog shows the equivalent CLI command, kept in sync with the form as
you type, and records exactly what that command would record.

| Key | Screen | Operation | Equivalent command |
|-----|--------|-----------|--------------------|
| `e` | Decisions | Evaluate decisions for all entities or the selected row's entity under a chosen profile; reports "N decisions evaluated". | `operon evaluate --profile … [--entity-type … --entity-id …]` |
| `c` | Decisions | Curate the selected decision: pick the new decision, reviewer (prefilled from `$USER`), required reason, optional evidence. Validation errors (retired entity, no automatic decision) appear inline without closing. | `operon curate --entity-type … --entity-id … --profile … --decision … --reviewer … --reason …` |
| `l` | Files | Browse `sequence_labels` (`classify-sequences` output): the project-wide label × sequences/files summary plus the selected file's label rows (500-row window). There is no CLI reader for labels — this view is the read side of `classify-sequences`, and `report analysis --hits` shows the alignments behind it. | — (no CLI counterpart) |
| — | Tasks | Browse alignment hits (the `report analysis --hits` view): analysis/entity/query/subject/evalue-max/limit filters over the same read-only query and column order. *Export* writes the current query through the CLI's own renderer, so `text`/`tsv`/`json` files are byte-identical to the CLI's; the job-summary view (without `--hits`) stays CLI-only. | `operon report analysis --hits [--analysis … --entity-type … --entity-id … --query-id … --subject-id … --evalue-max … --limit … --include-retired --format {text,tsv,json} --out PATH]` |
| `x` | Entities | Retire (or restore, for a retired entity) the selected entity. The dialog first loads the read-only impact plan (affected entities/files/references, physical changes — always zero for logical retirement) and blocks Confirm when the plan reports no change; a reason code is required for RETIRE. | `operon retire\|restore <id> --reason … [--reason-code …] --apply --yes` |
| `i` | Files | Ingest a file (local path or `sftp://`/`remote://` URL) into `raw/`, prefilled from the selected row. Format/compression auto-detect when left blank. A checksum conflict (same entity+role, different bytes) is shown inline in red and never overwrites. | `operon ingest --source … --entity-type … --entity-id … --role …` |
| `v` | Files | Verify the selected file, or all files after a "verify all N files?" confirm. Failures (`MISSING`, `CHECKSUM_FAILED`, …) are listed in an error dialog. | `operon verify [--file-id …]` |
| `q` | Files | Run built-in QC for the selected file or all files, with a live progress bar ("k/n · current file_id"). The completion notification mirrors the CLI text ("QC complete: ok/total file(s) passed built-in stages"); failures are listed in an error dialog. Cancel stops the batch cooperatively *between* files — results for files already processed are kept. | `operon qc [--file-id …] [--sample-size …] [--phred-offset …] [--rehash]` |
| `i` | global | Open the import dataset wizard (also via the Home button; on the Files screen `i` stays ingest). | `operon import dataset` |
| — | Publish | Create an immutable release after a members/exclusions preview; a duplicate version is rejected inline. | `operon release --version … --profile … [--copy-files\|--link hardlink]` |
| — | Publish | Materialize a selective export after a count/bytes preview; a non-empty output directory is rejected inline. | `operon export --output … [--entity-type … --entity-id … --file-id … --file-role … --format … --state … --decision … --profile …] [--link …] [--no-qc]` |
| — | Coverage | Generate a taxonomy coverage report; a result below the profile thresholds is a warning notification (FAIL), never a crash. | `operon report coverage --reference-set … [--release …]` |
| — | Config / Tasks | Run an analysis recipe over matching manifest files (*Run analysis* on the selected recipe, or *New analysis* with a recipe picker). Runtime parameters are rendered from the recipe's declared spec and validated exactly like the CLI; entity-type/entity-id/limit/threads filters, the execution backend (project default / local / slurm / ssh, preflighted before the worker starts — a missing `sbatch` or an incomplete `execution.ssh` block is an inline error), dry-run (a read-only plan shown inside the dialog), force, and keep-partial are supported, with a live progress bar and cooperative cancellation. Cancel stops the batch at the next file/planning/collection boundary and cancels the submitted work as a whole (one `scancel` for a Slurm job or job array; a direct SSH payload is terminated on the remote host); files already completed keep their results. Per-file failures are listed in an error dialog; a finished run lands on the Tasks screen. | `operon analyze --analysis … [--param NAME=VALUE …] [--entity-type …] [--entity-id …] [--limit …] [--threads …] [--backend {local,slurm,ssh}] [--dry-run] [--force] [--keep-partial]` |
| — | Tasks | Run one external command with structured provenance (*Run external*): step, a shlex-split command line (shell quoting; no pipes or redirections), optional entity/tool/parameter-set, comma-separated declared inputs (hashed and staged like the CLI) and expected outputs, threads, working directory, timeout, and the execution backend (preflighted like the analysis dialog). The preview shows the equivalent CLI command and notes that a fresh run id is allocated at submission; a command that ran but failed is a *recorded* outcome, so its full run record (command, exit code, error, log paths) opens on the Tasks screen either way. A running command cannot be interrupted from the TUI — the CLI's Ctrl+C can. | `operon run-external --step … --command … [--entity-type … --entity-id … --parameter-set … --tool … --input … --threads … --expected-output … --cwd … --timeout … --backend {local,slurm,ssh}]` |

All of these append the same `changes` audit rows and `workflow_runs`
provenance records as the CLI, so operations performed in the TUI are
indistinguishable from command-line ones in reports and exports.

### QC options

The Files screen's QC dialog accepts a positive FASTQ sample size (default
1,000,000 reads), Phred offset `33` (default), `64` or `auto`, and **Recompute
input SHA-256** (`--rehash`). Automatic Phred detection assumes 33 when
ambiguous, just like the CLI. Invalid sample sizes are rejected before the
worker starts. The command preview updates with these options; Confirm
captures them and locks the controls until the run ends. After a failure,
the controls are enabled again so the inputs can be corrected and retried.

## Import dataset wizard

The wizard (opened with the global `i` key) is a Textual
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

Navigation waits for initialization and page loading. If initialization fails,
the error stays inline and **Retry initialization** retries it. Picklists are
refreshed when their page opens; a draft ID no longer available in the list
is left unselected with an error, so you must explicitly select another
entity or choose to create one. Disabling Sequencing or Annotation and
pressing Next discards that section's draft; revisiting it clears its
inputs and selections before you enable it again.

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

**Export tab.** Filter form (entity type, entity id, file id, file role, format,
state, decision + profile — a decision filter without a profile is rejected
inline, mirroring the CLI), link kind (copy/hardlink/symlink), an
`include_qc` checkbox, and the output directory. **Preview** counts the
matching files and their total bytes without writing anything (using the
same `_select_files` selection as the export itself). **Run export**
confirms with the equivalent CLI command and materializes the export through
`operon.export.export_files`; an output directory that exists and is not
empty is rejected before the dialog opens.

Entity IDs and file IDs accept comma-separated lists; whitespace and empty
entries are ignored. File IDs alone are a valid selection criterion and are
combined with other filters using the CLI's selection rules. The same IDs
are passed to Preview and execution, and the confirmation shows one
`--file-id` argument per ID.

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

Every TSV row is checked against the header width, including rows beyond
the display limit. A blank header or malformed row produces an inline error with the filename
and line number instead of crashing the viewer. After repairing the file,
select the report again to load it. This validation does not modify reports.

## Config screen

The Config screen edits the two versioned configuration files with structured
forms whose values are re-composed into valid YAML by the backend — there is
no free-text editor, so a save can never produce a syntactically invalid
file. Keys the forms do not model (`value_by`, `source`, `unknown`,
parameter spec details, …) are **preserved verbatim** and
shown as dim read-only notes, never silently dropped.

**Save-as-version semantics.** Every save writes a *new version*: the
`version` field becomes one greater than the highest version in the current
file, recorded snapshots, or an existing file previously loaded by the editor
for that name (`1` for a name with no known history), and a
content-addressed snapshot is recorded — with exactly the same canonical
document the CLI records, so a TUI save and a later `operon evaluate` /
`operon analyze` of identical content map to the same snapshot row. Saving
content unchanged from an existing file is a no-op: the version is not bumped and no snapshot is
recorded. Every save is confirmed in a dialog that shows the effect
("writes `config/profiles/<name>.yaml` as version N + records snapshot"),
and validation errors are shown inline without touching the file (a failed
write is rolled back to the previous file bytes). Configuration publication
uses atomic file replacement. Rollback also covers database opening, snapshot
insertion/commit, and handled interruptions: existing bytes (including line
endings) are restored, or a newly created configuration file is removed.

Deleting or renaming a configuration file does not erase its recorded version
history: recreating its original name continues above that history. The
editor also remembers versions read from files that have no snapshots yet. A file
externally replaced with an older version also uses this version floor on
the next content change. The confirmation dialog uses the same calculation.

**History and restore.** The History dialog lists the recorded snapshots
(snapshot id, version, sha256 prefix, recording time, usage count) like
`operon profiles history` / `operon recipes history`. *View* renders a
snapshot document read-only as YAML; *Restore* loads the snapshot into the
editor — saving it then creates the **next** version. Snapshots are never
overwritten in place.

**QC Profiles tab.** Left: every `kind: qc` and
`kind: sequence_classification` profile found in `config/profiles/` (name +
version; classification entries carry a tag). Right: the editor for the
selected profile's kind — the qc and classification editors are swapped based
on the document's own `kind`, never merged. *New profile* prompts for a name
**and a kind** and starts from a minimal skeleton of that kind.
`taxonomy_coverage` profiles are not editable here.

**Classification profiles** (`kind: sequence_classification`). The editor
mirrors `classify.py`'s grammar: `applies_to` as an `entity_type` +
`file_role` pair, *sources* (name → analysis, a filter-condition list, and
`best_by` entries of field / direction / `Value=rank` map / default), and
*rules* (a label plus one of: a source with a `when` condition list,
`absent: true`, or `default: true`). Condition rows offer the core's whole
operator set — `>=`, `<=`, `>`, `<`, `==`, `!=`, `in`, `not_in`, `between`,
`exists` and the case-insensitive `like` — and each row is either a flat
condition, one `any of` group, or one `not` negation. Rule rows pick their
source from the declared source names, and a rule marked `default` or
`absent` hides the fields it must not carry. Operands that look numeric are
stored as numbers, `best_by` emits `direction` only when the file had one or
it differs from the core default (`asc`), and *Save profile* goes through the
same version + snapshot + rollback machinery as the qc editor, so a later
`operon classify-sequences` consumes exactly the snapshot saved here.
Structure the manual form cannot represent (conditions nested deeper than one
`any:`/`not:` level, sources or rules that are not mappings) opens
**read-only**: the editor names the reason, disables saving, and never
rewrites the file — edit the YAML instead. Keys the form does not model are
preserved verbatim at every level (document, source, rule, condition and
`best_by` entry).

**Tools & Recipes tab.** A tools table (name, executable, run method) with a
*Check tools* button — the equivalent of `operon tools-check`, run in a
background worker with per-row live updates (detected version in green,
`MISSING` in red) and a summary notification; one broken tool never breaks
the batch. Below, a recipes table (name, version, tool, entity type, file
role, format); selecting a recipe opens its editor: description, entity type
(Select, blank = `*`), file role or file role prefix (mutually exclusive —
setting both is rejected inline), format, input/output artifact kind
(Selects over file/directory, blank = key absent), database, database
version, environment policy (Select over `ignore`/`warn`/`strict`, blank =
the core default `warn`), output
subdirectory and suffix inputs, `arguments` as one-per-line text
(placeholders like `${input}` stay visible), runtime `parameters` as
`name=default` lines (other spec keys are preserved), the result parser
Select (`none`, `blast_tabular`, `hmmer_tblout`, `hmmer_domtblout`,
`rpsbproc_tabular`, `busco_json`), `result_glob`, the HMMER mode Select
(`hmmsearch`/`hmmscan`, blank = key absent),
`result_columns` / `hit_metric_columns` / `numeric_columns` as
comma-separated inputs, the parser column-mapping inputs (`query_column`,
`subject_column`, `qstart_column`, `qend_column`, `sstart_column`,
`send_column`, `evalue_column`, `bitscore_column`, `pident_column`), and
`max_hits_per_query`. Clearing this optional limit removes the key on save
and restores the core default (5); it does not mean unlimited hits. Leaving
an already absent limit blank remains a no-op. With a recipe selected, the
*Run analysis* button opens the analysis dialog prefilled with that recipe
(see below).

> **Note (tools.yaml formatting):** saving a recipe from the TUI rewrites
> `config/tools.yaml` with normalized YAML formatting and drops hand-written
> comments. No content is lost: every saved version is preserved verbatim in
> the `recipe_snapshots` table (`operon recipes history` / `operon recipes
> show`). Hand-editing the file remains fully supported — the TUI is the
> audited alternative.
