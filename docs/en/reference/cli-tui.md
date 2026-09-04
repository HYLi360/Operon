# Terminal UI (`operon tui`)

`operon tui` opens an interactive terminal user interface (TUI) for a project.
Phase 1 of the TUI is strictly read-only: it never writes to `operon.sqlite`,
never appends workflow records, and never touches archived files. Each panel
opens its own short-lived read-only database connection, so the TUI is safe
to leave open while CLI commands run against the same project.

## Installation

The TUI is an optional feature built on
[Textual](https://textual.textualize.io/). Install the extra:

```bash
pip install 'operon[tui]'
```

The frozen standalone build does not bundle Textual; there `operon tui`
prints the install hint above and exits with code 2.

## Usage

```bash
operon [--project PATH] tui
```

The project is selected with the global `--project` option, exactly like
every other command.

## Screens

The left sidebar (or the number keys) switches between four screens:

| Screen | Key | Contents |
|--------|-----|----------|
| Home | `1` | Project identity, entity counts, file count and total size, decision distribution, latest release, the 10 most recent workflow runs, and an "Attention needed" section (failed/interrupted runs, REVIEW/FAIL current decisions, files whose status is not healthy). |
| Entities | `2` | Hierarchy tree (organisms → samples → runs and assemblies → annotations) with the current state of each entity. Selecting a node shows its metadata fields, accessions, state, and files. Press `t` to include logically retired entities (shown dimmed/struck-through). |
| Files | `3` | Filterable manifest table (substring filter plus status selector). Moving the cursor shows the full file record and its `file_locations` residency list. Statuses are color-coded: verified green, `REMOTE_ONLY` blue, `MISSING`/`CHECKSUM_FAILED` red. |
| Runs | `4` | Workflow-run monitor fed by the same read-only query as `operon workflow list`, with status/step/entity/limit filters. The table auto-refreshes every 2 seconds so running jobs update live. Press `enter` on a row for the full run record (the same sections as `operon workflow show`); press `esc` to go back. |

Global keys: `1`–`4` switch screens, `r` refreshes the current screen, `?`
shows the key help, `q` quits.
