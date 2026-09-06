"""Interactive dataset-import wizard (Textual port of the questionary wizard).

The pages mirror ``operon import dataset`` section by section: Source →
Organism → Sample → Sequencing → Assembly → Annotation → Files → Summary.
The draft dict has exactly the shape :mod:`operon.import_wizard` produces, and
the final commit goes through the same single-transaction
:func:`operon.import_wizard._commit` (via :func:`operon.tui.actions.import_dataset`),
so a TUI import and a CLI import record identical provenance.  The summary
page renders the wizard's own ``_summary``/``_warnings`` output and offers
non-linear "Edit <section>" jumps, like the questionary review loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, ContentSwitcher, Input, Select, Static

from operon.config import Project
from operon.import_wizard import _source_validation_errors, _synchronize_new_entity_links
from operon.tui import actions, data

CREATE_NEW = "__new__"

SECTIONS = ("source", "organism", "sample", "sequencing", "assembly", "annotation", "files")
PAGES = (*SECTIONS, "summary")

SOURCE_TYPE_OPTIONS = [
    ("INSDC (GenBank / ENA / DDBJ)", "insdc"),
    ("Non-INSDC database, repository, or institution", "non_insdc"),
]
TAXONOMY_SOURCE_OPTIONS = [("NCBI", "NCBI"), ("GTDB", "GTDB"), ("other", "other")]
LIBRARY_STRATEGY_OPTIONS = [(value, value) for value in
                            ("WGS", "WGA", "RNA-Seq", "Amplicon", "Hi-C", "ATAC-seq", "other")]
LIBRARY_SOURCE_OPTIONS = [(value, value) for value in
                          ("GENOMIC", "TRANSCRIPTOMIC", "METAGENOMIC", "OTHER")]
LIBRARY_LAYOUT_OPTIONS = [(value, value) for value in ("PAIRED", "SINGLE", "unknown")]
PLATFORM_OPTIONS = [(value, value) for value in
                    ("ILLUMINA", "PACBIO_SMRT", "OXFORD_NANOPORE", "BGISEQ", "ION_TORRENT", "other")]
ASSEMBLY_LEVEL_OPTIONS = [(value, value) for value in
                          ("complete_genome", "chromosome", "scaffold", "contig")]
SOURCE_DATABASE_OPTIONS = [(value, value) for value in ("RefSeq", "GenBank", "other")]

# (label, role, entity_type, widget id); annotation/run entries are only shown
# when the draft records an annotation release / sequencing run.
FILE_ENTRIES = [
    ("Genome FASTA", "genome_fasta", "assembly", "iw-file-genome-fasta"),
    ("GFF3", "annotation_gff3", "annotation", "iw-file-annotation-gff3"),
    ("CDS FASTA", "cds_fasta", "annotation", "iw-file-cds-fasta"),
    ("Protein FASTA", "protein_fasta", "annotation", "iw-file-protein-fasta"),
    ("Reads R1", "reads_r1", "run", "iw-file-reads-r1"),
    ("Reads R2", "reads_r2", "run", "iw-file-reads-r2"),
    ("Single-end reads", "reads_single", "run", "iw-file-reads-single"),
]


def _input(screen: Screen, widget_id: str) -> str:
    return screen.query_one(f"#{widget_id}", Input).value.strip()


def _select_value(screen: Screen, widget_id: str) -> str:
    value = screen.query_one(f"#{widget_id}", Select).value
    return "" if value is Select.NULL else str(value)


class ImportWizardScreen(Screen):
    """Full-screen wizard that builds and commits an import draft."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.draft: dict[str, Any] = {}
        self.reserved_ids: dict[str, str] = {}
        self.organisms: list[dict[str, Any]] = []
        self.page_index = 0
        self._return_to_summary = False
        self._executing = False

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="wizard-layout"):
            with VerticalScroll(id="wizard-box"):
                yield Static("Import dataset", id="wizard-title")
                yield Static(
                    "No project data is changed until you execute the final review. "
                    "Equivalent CLI command: operon import dataset",
                    classes="modal-info",
                )
                with ContentSwitcher(initial="page-source", id="wizard-pages"):
                    with VerticalScroll(id="page-source"):
                        yield Static("[1] Source", classes="wizard-heading")
                        yield Static("Source classification", classes="modal-label")
                        yield Select(SOURCE_TYPE_OPTIONS, value="insdc",
                                     id="iw-source-type", allow_blank=False)
                        yield Input(placeholder="source database or repository (required)",
                                    id="iw-database-name")
                        yield Input(placeholder="data provider or institution (required)",
                                    id="iw-provider")
                        yield Input(placeholder="source record URL (optional)", id="iw-record-url")
                        yield Input(placeholder="reference citation or DOI (required for non-INSDC)",
                                    id="iw-citation")
                        yield Input(placeholder="license name or SPDX identifier (required for non-INSDC)",
                                    id="iw-license-name")
                        yield Input(placeholder="license URL (optional)", id="iw-license-url")
                    with VerticalScroll(id="page-organism"):
                        yield Static("[2] Organism", classes="wizard-heading")
                        yield Select([("Create a new organism", CREATE_NEW)],
                                     value=CREATE_NEW, id="iw-organism-choice", allow_blank=False)
                        with VerticalScroll(id="iw-organism-form"):
                            yield Input(placeholder="scientific name (required)", id="iw-organism-name")
                            yield Input(placeholder="taxonomy ID (optional)", id="iw-organism-taxon-id")
                            yield Input(value="species", placeholder="taxonomic rank",
                                        id="iw-organism-rank")
                            yield Static("Taxonomy source", classes="modal-label")
                            yield Select(TAXONOMY_SOURCE_OPTIONS, id="iw-organism-taxonomy-source")
                            yield Input(placeholder="taxonomy version (optional)",
                                        id="iw-organism-taxonomy-version")
                    with VerticalScroll(id="page-sample"):
                        yield Static("[3] Sample", classes="wizard-heading")
                        yield Select([("Create a new sample", CREATE_NEW)],
                                     value=CREATE_NEW, id="iw-sample-choice", allow_blank=False)
                        with VerticalScroll(id="iw-sample-form"):
                            yield Input(placeholder="BioSample accession (optional)",
                                        id="iw-sample-biosample")
                            yield Input(placeholder="strain (optional)", id="iw-sample-strain")
                            yield Input(placeholder="isolate (optional)", id="iw-sample-isolate")
                    with VerticalScroll(id="page-sequencing"):
                        yield Static("[4] Sequencing", classes="wizard-heading")
                        yield Checkbox("Record sequencing information", id="iw-run-enabled")
                        with VerticalScroll(id="iw-run-form"):
                            yield Input(placeholder="run accession (optional)", id="iw-run-accession")
                            yield Input(placeholder="experiment accession (optional)",
                                        id="iw-run-experiment")
                            yield Static("Library strategy", classes="modal-label")
                            yield Select(LIBRARY_STRATEGY_OPTIONS, id="iw-run-strategy")
                            yield Static("Library source", classes="modal-label")
                            yield Select(LIBRARY_SOURCE_OPTIONS, id="iw-run-source")
                            yield Static("Library layout", classes="modal-label")
                            yield Select(LIBRARY_LAYOUT_OPTIONS, id="iw-run-layout")
                            yield Static("Sequencing platform", classes="modal-label")
                            yield Select(PLATFORM_OPTIONS, id="iw-run-platform")
                            yield Input(placeholder="instrument model (optional)",
                                        id="iw-run-instrument")
                    with VerticalScroll(id="page-assembly"):
                        yield Static("[5] Assembly", classes="wizard-heading")
                        yield Select([("Create a new assembly", CREATE_NEW)],
                                     value=CREATE_NEW, id="iw-assembly-choice", allow_blank=False)
                        with VerticalScroll(id="iw-assembly-form"):
                            yield Input(placeholder="assembly accession (optional)",
                                        id="iw-assembly-accession")
                            yield Input(placeholder="assembly name (optional)", id="iw-assembly-name")
                            yield Input(value="1", placeholder="assembly version",
                                        id="iw-assembly-version")
                            yield Static("Assembly level", classes="modal-label")
                            yield Select(ASSEMBLY_LEVEL_OPTIONS, id="iw-assembly-level")
                            yield Input(placeholder="assembly software and parameters (optional)",
                                        id="iw-assembly-method")
                            yield Static("Source database", classes="modal-label")
                            yield Select(SOURCE_DATABASE_OPTIONS, id="iw-assembly-source-db")
                    with VerticalScroll(id="page-annotation"):
                        yield Static("[6] Annotation", classes="wizard-heading")
                        yield Checkbox("Record an annotation release", id="iw-annotation-enabled")
                        with VerticalScroll(id="iw-annotation-form"):
                            yield Select([("Create a new annotation", CREATE_NEW)],
                                         value=CREATE_NEW, id="iw-annotation-choice", allow_blank=False)
                            yield Input(placeholder="annotation pipeline or source (optional)",
                                        id="iw-annotation-source")
                            yield Input(value="1", placeholder="annotation version",
                                        id="iw-annotation-version")
                            yield Input(placeholder="annotation date, YYYY-MM-DD (optional)",
                                        id="iw-annotation-date")
                    with VerticalScroll(id="page-files"):
                        yield Static("[7] Files", classes="wizard-heading")
                        yield Static("Leave a path empty to skip that file.", classes="modal-info")
                        for label, _role, _entity_type, widget_id in FILE_ENTRIES:
                            with Vertical(id=f"{widget_id}-row"):
                                yield Static(label, classes="modal-label")
                                yield Input(placeholder=f"{label} path (optional)", id=widget_id)
                    with VerticalScroll(id="page-summary"):
                        yield Static("Import plan", classes="wizard-heading")
                        yield Static("loading…", id="iw-summary", classes="body")
                        with Horizontal(id="wizard-edit-bar"):
                            for name in SECTIONS:
                                yield Button(f"Edit {name}", id=f"wizard-edit-{name}",
                                             classes="wizard-edit")
            yield Static("", id="wizard-error")
            with Horizontal(id="wizard-bar"):
                yield Button("Back", id="wizard-back")
                yield Button("Next", id="wizard-next", variant="primary")
                yield Button("Execute import", id="wizard-execute", variant="success")
                yield Button("Cancel", id="wizard-cancel")

    def on_mount(self) -> None:
        self.query_one("#wizard-execute", Button).display = False
        self.query_one("#wizard-edit-bar", Horizontal).display = False
        self.query_one("#wizard-back", Button).disabled = True
        self._startup()

    # -- startup / page loading --------------------------------------------

    @work(thread=True)
    def _startup(self) -> None:
        try:
            payload: Any = {
                "ids": actions.reserve_entity_ids(self.project),
                "organisms": data.list_organisms_for_picker(self.project),
            }
        except Exception as exc:  # noqa: BLE001 - surfaced in the wizard
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._startup_done, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _startup_done(self, payload: Any) -> None:
        if isinstance(payload, BaseException):
            self.show_error(payload)
            return
        self.reserved_ids = payload["ids"]
        self.organisms = payload["organisms"]
        self._set_choice_options("#iw-organism-choice", self._organism_options())

    def _organism_options(self) -> list[tuple[str, str]]:
        names: dict[str, int] = {}
        for row in self.organisms:
            name = str(row["scientific_name"])
            names[name] = names.get(name, 0) + 1
        options = [("Create a new organism", CREATE_NEW)]
        for row in self.organisms:
            name = str(row["scientific_name"])
            label = name if names[name] == 1 else f"{name} [{row['organism_id']}]"
            options.append((f"{label}  ({row['organism_id']})", str(row["organism_id"])))
        return options

    def _set_choice_options(self, widget_id: str, options: list[tuple[str, str]],
                            value: str = CREATE_NEW) -> None:
        select = self.query_one(widget_id, Select)
        select.set_options(options)
        select.value = value

    def _goto(self, index: int) -> None:
        self.page_index = index
        self.clear_error()
        self._load_page(PAGES[index])

    @work(thread=True)
    def _load_page(self, page: str) -> None:
        """Fetch the picklist/summary a page needs, off the UI thread."""
        try:
            payload: Any = {}
            if page == "sample":
                organism_id = (self.draft.get("organism") or {}).get("id", "")
                payload["samples"] = data.list_samples_for_picker(self.project, organism_id)
            elif page == "assembly":
                sample_id = (self.draft.get("sample") or {}).get("id", "")
                payload["assemblies"] = data.list_assemblies_for_picker(self.project, sample_id)
            elif page == "annotation":
                assembly_id = (self.draft.get("assembly") or {}).get("id", "")
                payload["annotations"] = data.list_annotations_for_picker(
                    self.project, assembly_id)
            elif page == "summary":
                payload["summary"] = data.import_summary(self.project, self.draft)
        except Exception as exc:  # noqa: BLE001 - surfaced in the wizard
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._show_page, page, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _show_page(self, page: str, payload: Any) -> None:
        if isinstance(payload, BaseException):
            self.show_error(payload)
            return
        if page == "sample":
            self._set_choice_options(
                "#iw-sample-choice",
                [("Create a new sample", CREATE_NEW)] + [
                    (
                        f"{row['sample_id']}  "
                        f"{row.get('isolate') or row.get('strain') or row.get('biosample_accession') or 'sample'}",
                        str(row["sample_id"]),
                    )
                    for row in payload["samples"]
                ],
            )
        elif page == "assembly":
            self._set_choice_options(
                "#iw-assembly-choice",
                [("Create a new assembly", CREATE_NEW)] + [
                    (
                        f"{row['assembly_id']}  "
                        f"{row.get('assembly_accession') or row.get('assembly_name') or 'assembly'}",
                        str(row["assembly_id"]),
                    )
                    for row in payload["assemblies"]
                ],
            )
        elif page == "annotation":
            self._set_choice_options(
                "#iw-annotation-choice",
                [("Create a new annotation", CREATE_NEW)] + [
                    (
                        f"{row['annotation_id']}  "
                        f"{row.get('annotation_source') or 'annotation'} "
                        f"v{row.get('annotation_version') or '?'}",
                        str(row["annotation_id"]),
                    )
                    for row in payload["annotations"]
                ],
            )
        elif page == "files":
            self._update_file_visibility()
        elif page == "summary":
            self.query_one("#iw-summary", Static).update(payload["summary"])
        self._populate(page)
        self.query_one("#wizard-pages", ContentSwitcher).current = f"page-{page}"
        is_summary = page == "summary"
        self.query_one("#wizard-back", Button).disabled = self.page_index == 0
        self.query_one("#wizard-next", Button).display = not is_summary
        self.query_one("#wizard-execute", Button).display = is_summary
        self.query_one("#wizard-edit-bar", Horizontal).display = is_summary

    def _update_file_visibility(self) -> None:
        has_annotation = bool(self.draft.get("annotation"))
        has_run = bool(self.draft.get("run"))
        for _label, _role, entity_type, widget_id in FILE_ENTRIES:
            row = self.query_one(f"#{widget_id}-row")
            row.display = (
                entity_type == "assembly"
                or (entity_type == "annotation" and has_annotation)
                or (entity_type == "run" and has_run)
            )

    # -- repopulating a page from the draft (Back / Edit jumps) -------------

    def _populate(self, page: str) -> None:
        draft = self.draft
        if page == "source" and draft.get("source"):
            source = draft["source"]
            self.query_one("#iw-source-type", Select).value = source.get("source_type") or "insdc"
            for key, widget_id in (
                    ("database_name", "iw-database-name"), ("provider", "iw-provider"),
                    ("record_url", "iw-record-url"), ("citation", "iw-citation"),
                    ("license_name", "iw-license-name"), ("license_url", "iw-license-url")):
                self.query_one(f"#{widget_id}", Input).value = str(source.get(key) or "")
        elif page == "organism" and draft.get("organism"):
            organism = draft["organism"]
            if organism.get("action") == "reuse":
                self.query_one("#iw-organism-choice", Select).value = organism["id"]
            else:
                row = organism.get("row", {})
                self.query_one("#iw-organism-choice", Select).value = CREATE_NEW
                self.query_one("#iw-organism-name", Input).value = str(row.get("scientific_name") or "")
                self.query_one("#iw-organism-taxon-id", Input).value = str(row.get("taxon_id") or "")
                self.query_one("#iw-organism-rank", Input).value = str(row.get("taxonomic_rank") or "species")
                self.query_one("#iw-organism-taxonomy-version", Input).value = str(
                    row.get("taxonomy_version") or "")
        elif page == "sample" and draft.get("sample"):
            sample = draft["sample"]
            if sample.get("action") == "reuse":
                self.query_one("#iw-sample-choice", Select).value = sample["id"]
            else:
                row = sample.get("row", {})
                self.query_one("#iw-sample-choice", Select).value = CREATE_NEW
                self.query_one("#iw-sample-biosample", Input).value = str(row.get("biosample_accession") or "")
                self.query_one("#iw-sample-strain", Input).value = str(row.get("strain") or "")
                self.query_one("#iw-sample-isolate", Input).value = str(row.get("isolate") or "")
        elif page == "sequencing":
            run = draft.get("run")
            self.query_one("#iw-run-enabled", Checkbox).value = bool(run)
            if run:
                row = run.get("row", {})
                for key, widget_id in (
                        ("run_accession", "iw-run-accession"),
                        ("experiment_accession", "iw-run-experiment"),
                        ("instrument_model", "iw-run-instrument")):
                    self.query_one(f"#{widget_id}", Input).value = str(row.get(key) or "")
        elif page == "assembly" and draft.get("assembly"):
            assembly = draft["assembly"]
            if assembly.get("action") == "reuse":
                self.query_one("#iw-assembly-choice", Select).value = assembly["id"]
            else:
                row = assembly.get("row", {})
                self.query_one("#iw-assembly-choice", Select).value = CREATE_NEW
                for key, widget_id in (
                        ("assembly_accession", "iw-assembly-accession"),
                        ("assembly_name", "iw-assembly-name"),
                        ("assembly_version", "iw-assembly-version"),
                        ("assembly_method", "iw-assembly-method")):
                    self.query_one(f"#{widget_id}", Input).value = str(row.get(key) or "")
        elif page == "annotation":
            annotation = draft.get("annotation")
            self.query_one("#iw-annotation-enabled", Checkbox).value = bool(annotation)
            if annotation:
                if annotation.get("action") == "reuse":
                    self.query_one("#iw-annotation-choice", Select).value = annotation["id"]
                else:
                    row = annotation.get("row", {})
                    self.query_one("#iw-annotation-choice", Select).value = CREATE_NEW
                    for key, widget_id in (
                            ("annotation_source", "iw-annotation-source"),
                            ("annotation_version", "iw-annotation-version"),
                            ("annotation_date", "iw-annotation-date")):
                        self.query_one(f"#{widget_id}", Input).value = str(row.get(key) or "")
        elif page == "files":
            current = {item["role"]: item["path"] for item in draft.get("files", [])}
            for _label, role, _entity_type, widget_id in FILE_ENTRIES:
                self.query_one(f"#{widget_id}", Input).value = current.get(role, "")

    # -- collecting a page into the draft ------------------------------------

    def _collect(self, page: str) -> str | None:
        """Validate the current page and merge it into the draft; error or None."""
        if page == "source":
            source = {
                "source_type": _select_value(self, "iw-source-type"),
                "database_name": _input(self, "iw-database-name"),
                "provider": _input(self, "iw-provider"),
                "record_url": _input(self, "iw-record-url"),
                "citation": _input(self, "iw-citation"),
                "license_name": _input(self, "iw-license-name"),
                "license_url": _input(self, "iw-license-url"),
            }
            errors = _source_validation_errors({"source": source})
            if errors:
                return "\n".join(errors)
            self.draft["source"] = source
        elif page == "organism":
            choice = _select_value(self, "iw-organism-choice")
            if choice != CREATE_NEW:
                self.draft["organism"] = {"action": "reuse", "id": choice}
            else:
                name = _input(self, "iw-organism-name")
                if not name:
                    return "Scientific name is required."
                row = {
                    "organism_id": self.reserved_ids["organism"],
                    "scientific_name": name,
                    "taxon_id": _input(self, "iw-organism-taxon-id"),
                    "taxonomic_rank": _input(self, "iw-organism-rank"),
                    "taxonomy_source": _select_value(self, "iw-organism-taxonomy-source"),
                    "taxonomy_version": _input(self, "iw-organism-taxonomy-version"),
                }
                self.draft["organism"] = {"action": "create", "id": row["organism_id"], "row": row}
        elif page == "sample":
            choice = _select_value(self, "iw-sample-choice")
            if choice != CREATE_NEW:
                self.draft["sample"] = {"action": "reuse", "id": choice}
            else:
                row = {
                    "sample_id": self.reserved_ids["sample"],
                    "organism_id": self.draft["organism"]["id"],
                    "biosample_accession": _input(self, "iw-sample-biosample"),
                    "strain": _input(self, "iw-sample-strain"),
                    "isolate": _input(self, "iw-sample-isolate"),
                    "source_record": self.draft.get("source", {}).get("record_url", ""),
                }
                self.draft["sample"] = {"action": "create", "id": row["sample_id"], "row": row}
        elif page == "sequencing":
            if not self.query_one("#iw-run-enabled", Checkbox).value:
                self.draft["run"] = None
            else:
                row = {
                    "run_id": self.reserved_ids["run"],
                    "sample_id": self.draft["sample"]["id"],
                    "run_accession": _input(self, "iw-run-accession"),
                    "experiment_accession": _input(self, "iw-run-experiment"),
                    "library_strategy": _select_value(self, "iw-run-strategy"),
                    "library_source": _select_value(self, "iw-run-source"),
                    "library_layout": _select_value(self, "iw-run-layout"),
                    "platform": _select_value(self, "iw-run-platform"),
                    "instrument_model": _input(self, "iw-run-instrument"),
                }
                self.draft["run"] = {"action": "create", "id": row["run_id"], "row": row}
        elif page == "assembly":
            choice = _select_value(self, "iw-assembly-choice")
            if choice != CREATE_NEW:
                self.draft["assembly"] = {"action": "reuse", "id": choice}
            else:
                row = {
                    "assembly_id": self.reserved_ids["assembly"],
                    "sample_id": self.draft["sample"]["id"],
                    "assembly_accession": _input(self, "iw-assembly-accession"),
                    "assembly_name": _input(self, "iw-assembly-name"),
                    "assembly_version": _input(self, "iw-assembly-version"),
                    "assembly_level": _select_value(self, "iw-assembly-level"),
                    "assembly_method": _input(self, "iw-assembly-method"),
                    "submitter": self.draft.get("source", {}).get("provider", ""),
                    "source_database": _select_value(self, "iw-assembly-source-db"),
                }
                self.draft["assembly"] = {"action": "create", "id": row["assembly_id"], "row": row}
        elif page == "annotation":
            if not self.query_one("#iw-annotation-enabled", Checkbox).value:
                self.draft["annotation"] = None
            else:
                choice = _select_value(self, "iw-annotation-choice")
                if choice != CREATE_NEW:
                    self.draft["annotation"] = {"action": "reuse", "id": choice}
                else:
                    row = {
                        "annotation_id": self.reserved_ids["annotation"],
                        "assembly_id": self.draft["assembly"]["id"],
                        "annotation_source": _input(self, "iw-annotation-source"),
                        "annotation_version": _input(self, "iw-annotation-version"),
                        "annotation_date": _input(self, "iw-annotation-date"),
                    }
                    self.draft["annotation"] = {
                        "action": "create", "id": row["annotation_id"], "row": row,
                    }
        elif page == "files":
            files: list[dict[str, str]] = []
            for label, role, entity_type, widget_id in FILE_ENTRIES:
                if entity_type == "annotation" and not self.draft.get("annotation"):
                    continue
                if entity_type == "run" and not self.draft.get("run"):
                    continue
                value = _input(self, widget_id)
                if not value:
                    continue
                path = Path(value).expanduser()
                if not path.is_file():
                    return f"File does not exist: {value}"
                files.append({
                    "label": label, "role": role, "entity_type": entity_type,
                    "path": str(path.resolve()),
                })
            self.draft["files"] = files
        return None

    # -- navigation ----------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "wizard-cancel":
            self.action_cancel()
        elif button_id == "wizard-back":
            self._return_to_summary = False
            if self.page_index > 0:
                self._goto(self.page_index - 1)
        elif button_id == "wizard-next":
            self._advance()
        elif button_id == "wizard-execute":
            self._execute_import()
        elif button_id.startswith("wizard-edit-"):
            self._return_to_summary = True
            self._goto(SECTIONS.index(button_id[len("wizard-edit-"):]))

    def _advance(self) -> None:
        error = self._collect(PAGES[self.page_index])
        if error:
            self.show_error(error)
            return
        _synchronize_new_entity_links(self.draft)
        if self._return_to_summary:
            self._return_to_summary = False
            self._goto(PAGES.index("summary"))
        elif self.page_index < len(PAGES) - 1:
            self._goto(self.page_index + 1)

    def action_cancel(self) -> None:
        self.dismiss(None)

    # -- execution -------------------------------------------------------------

    def _execute_import(self) -> None:
        if self._executing:
            return
        self._executing = True
        self.clear_error()
        self.query_one("#wizard-execute", Button).disabled = True
        self._run_import()

    @work(thread=True)
    def _run_import(self) -> None:
        try:
            payload: Any = actions.import_dataset(self.project, self.draft)
        except Exception as exc:  # noqa: BLE001 - shown inline; staged files rolled back
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._import_done, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _import_done(self, payload: Any) -> None:
        self._executing = False
        self.query_one("#wizard-execute", Button).disabled = False
        if isinstance(payload, BaseException):
            self.show_error(payload)
            return
        entities = ", ".join(f"{name} {entity_id}" for name, entity_id in payload["entities"].items())
        self.app.notify(
            f"imported {entities} + {len(payload['files'])} file(s); source {payload['source_id']}"
        )
        self.dismiss(payload)
        self.app.reload_after_write()

    # -- shared bits -------------------------------------------------------------

    def show_error(self, exc: BaseException | str) -> None:
        self.query_one("#wizard-error", Static).update(Text(str(exc), style="red"))

    def clear_error(self) -> None:
        self.query_one("#wizard-error", Static).update("")
