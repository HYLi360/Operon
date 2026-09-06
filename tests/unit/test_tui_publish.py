"""Phase-3 TUI: import wizard, publish (release/export), coverage screens."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("textual")

import yaml
from rich.text import Text
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Select, Static, TabbedContent

from operon import taxonomy
from operon.cli import main
from operon.config import Project, load_project
from operon.database import Database
from operon.demo import init_demo
from operon.export import _select_files
from operon.release import release_exclusions_for, release_files_for
from operon.tui import actions, data
from operon.tui.app import OperonApp
from operon.tui.screens.coverage import CoverageModal, CoveragePanel
from operon.tui.screens.import_wizard import ImportWizardScreen
from operon.tui.screens.publish import CreateReleaseModal, ExportModal, PublishPanel


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-publish-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    """Each write test gets its own copy of the demo project."""
    target = tmp_path / "project"
    shutil.copytree(demo_template.root, target)
    return Project.find(target)


def _coverage_project(root: Path) -> Project:
    """A fresh project with a minimal taxonomy snapshot and reference set."""
    assert main(["--project", str(root), "init", str(root)]) == 0
    project = load_project(root)
    db = Database(project.db_path)
    try:
        source = root / "taxonomy.jsonl"
        records = [
            {"taxId": 1, "rank": "no rank", "taxName": "root"},
            {"taxId": 10, "parents": [1], "rank": "family", "taxName": "Fam"},
            {"taxId": 20, "parents": [10], "rank": "genus", "taxName": "Gen"},
        ]
        source.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        taxonomy.import_ncbi_taxonomy(db, project, source, "cov.1")
        profile = {
            "kind": "taxonomy_coverage",
            "version": 1,
            "name": "cov",
            "taxonomy": {"source": "NCBI"},
            "scope": {"root_taxids": [1]},
            "targets": {"ranks": ["family", "genus"]},
            "thresholds": {
                "family": {"min_coverage_percent": 0},
                "genus": {"min_coverage_percent": 0},
            },
        }
        (project.profiles_dir / "cov.yaml").write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8"
        )
        taxonomy.compile_reference_set(db, project, "cov", "cov.1")
    finally:
        db.close()
    return project


@pytest.fixture(scope="module")
def coverage_template(tmp_path_factory) -> Project:
    return _coverage_project(tmp_path_factory.mktemp("tui-coverage"))


@pytest.fixture
def coverage_project(tmp_path: Path, coverage_template: Project) -> Project:
    target = tmp_path / "cov-project"
    shutil.copytree(coverage_template.root, target)
    return Project.find(target)


def _query(project: Project, sql: str, params: tuple = ()) -> list[dict]:
    db = Database(project.db_path, read_only=True)
    try:
        return [dict(row) for row in db.query(sql, params)]
    finally:
        db.close()


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 15.0


def _run(coroutine) -> None:
    """Drive a Textual headless scenario without requiring pytest-asyncio."""
    asyncio.run(asyncio.wait_for(coroutine, timeout=SCENARIO_TIMEOUT))


async def _settled(app, timeout: float = SETTLE_TIMEOUT) -> None:
    """Wait until no workers are running, with a diagnostic timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # The splash screen blocks key bindings until the startup worker finishes;
    # that worker is scheduled via call_after_refresh, so the worker set can
    # be momentarily empty before it starts — gate on _starting as well.
    while app.workers or getattr(app, "_starting", False):
        if loop.time() > deadline:
            states = [worker.state.name for worker in app.workers]
            raise TimeoutError(f"workers did not finish within {timeout}s: {states}")
        await asyncio.sleep(0.05)


async def _click(pilot, selector: str) -> None:
    assert await pilot.click(selector), f"click did not land on {selector}"


async def _button_click(pilot, app, selector: str) -> None:
    """Click a button after outlasting the Button ``-active`` debounce window."""
    await asyncio.sleep(0.35)
    await _click(pilot, selector)
    await _settled(app)
    await pilot.pause()


def _draft(project: Project, fasta: Path) -> dict:
    ids = actions.reserve_entity_ids(project)
    return {
        "source": {
            "source_type": "insdc", "database_name": "TestDB", "provider": "TestProvider",
            "record_url": "", "citation": "", "license_name": "", "license_url": "",
        },
        "organism": {
            "action": "create", "id": ids["organism"],
            "row": {
                "organism_id": ids["organism"], "scientific_name": "Syntheticus gamma",
                "taxon_id": "", "taxonomic_rank": "species",
                "taxonomy_source": "", "taxonomy_version": "",
            },
        },
        "sample": {
            "action": "create", "id": ids["sample"],
            "row": {
                "sample_id": ids["sample"], "organism_id": ids["organism"],
                "biosample_accession": "", "strain": "T1", "isolate": "",
                "source_record": "",
            },
        },
        "run": None,
        "assembly": {
            "action": "create", "id": ids["assembly"],
            "row": {
                "assembly_id": ids["assembly"], "sample_id": ids["sample"],
                "assembly_accession": "", "assembly_name": "gamma-asm",
                "assembly_version": "1", "assembly_level": "", "assembly_method": "",
                "submitter": "TestProvider", "source_database": "",
            },
        },
        "annotation": None,
        "files": [
            {"label": "Genome FASTA", "role": "genome_fasta",
             "entity_type": "assembly", "path": str(fasta)},
        ],
    }


# ---------------------------------------------------------------------------
# Data layer additions (read-only)
# ---------------------------------------------------------------------------


def test_picker_queries(demo_template: Project) -> None:
    organisms = data.list_organisms_for_picker(demo_template)
    assert [row["organism_id"] for row in organisms] == ["ORG_000001", "ORG_000002"]
    samples = data.list_samples_for_picker(demo_template, "ORG_000001")
    assert [row["sample_id"] for row in samples] == ["SMP_000001", "SMP_000003"]
    assemblies = data.list_assemblies_for_picker(demo_template, "SMP_000001")
    assert [row["assembly_id"] for row in assemblies] == ["ASM_000001"]
    annotations = data.list_annotations_for_picker(demo_template, "ASM_000001")
    assert [row["annotation_id"] for row in annotations] == ["ANN_000001"]
    assert data.list_samples_for_picker(demo_template, "ORG_999999") == []


def test_import_summary_uses_wizard_renderer(project: Project, tmp_path: Path) -> None:
    fasta = tmp_path / "new.fasta"
    fasta.write_text(">ctg1\nACGT\n", encoding="utf-8")
    draft = _draft(project, fasta)
    summary = data.import_summary(project, draft)
    assert "Import plan" in summary
    assert "Syntheticus gamma" in summary
    assert "Sequencing provenance will not be recorded." in summary


def test_list_releases_and_release_preview(demo_template: Project) -> None:
    releases = data.list_releases(demo_template)
    assert [row["version"] for row in releases] == ["2026.08.demo"]
    assert releases[0]["summary"]["accepted_file_count"] > 0

    preview = data.release_preview(demo_template, "assembly_production_v1")
    db = Database(demo_template.db_path, read_only=True)
    try:
        members = release_files_for(db, "assembly_production_v1")
        exclusions = release_exclusions_for(db, "assembly_production_v1")
    finally:
        db.close()
    assert [row["file_id"] for row in preview["members"]] == [row["file_id"] for row in members]
    assert preview["member_bytes"] == sum(int(row["size_bytes"]) for row in members)
    assert [row["entity_id"] for row in preview["exclusions"]] == [
        row["entity_id"] for row in exclusions
    ]
    assert preview["exclusions"], "demo project should have excluded entities"


def test_export_preview_matches_core_selection(demo_template: Project) -> None:
    preview = data.export_preview(demo_template, entity_type="assembly")
    db = Database(demo_template.db_path, read_only=True)
    try:
        rows = _select_files(
            db, entity_type="assembly", entity_ids=(), file_ids=(),
            file_role=None, fmt=None, state=None, decision=None, profile=None,
        )
    finally:
        db.close()
    assert preview["count"] == len(rows)
    assert preview["bytes"] == sum(int(row["size_bytes"]) for row in rows)
    assert [row["file_id"] for row in preview["files"]] == [row["file_id"] for row in rows]

    with pytest.raises(Exception, match="selection criterion"):
        data.export_preview(demo_template)
    with pytest.raises(Exception, match="requires --profile"):
        data.export_preview(demo_template, decision="PASS")


def test_coverage_data_wrappers(coverage_project: Project) -> None:
    snapshots = data.list_taxonomy_snapshots(coverage_project)
    assert [row["taxonomy_snapshot_id"] for row in snapshots] == ["TAX_000001"]
    reference_sets = data.list_reference_sets(coverage_project)
    assert [row["reference_set_id"] for row in reference_sets] == ["cov@cov.1"]
    assert data.list_coverage_reports(coverage_project) == []


def test_read_coverage_report(coverage_project: Project) -> None:
    result = actions.run_coverage(coverage_project, "cov@cov.1")
    assert result["decision"] == "PASS"
    assert result["exit_code"] == 0

    reports = data.list_coverage_reports(coverage_project)
    assert [row["report_id"] for row in reports] == [result["report_id"]]
    assert reports[0]["scope_kind"] == "metadata"
    assert reports[0]["reference_set_id"] == "cov@cov.1"

    report = data.read_coverage_report(coverage_project, result["report_id"])
    assert report["provenance"]["reference_set_id"] == "cov@cov.1"
    summary = report["tables"]["coverage_summary"]
    assert summary["columns"] == [
        "rank", "numerator", "denominator", "coverage_percent",
        "min_coverage_percent", "decision",
    ]
    assert summary["total"] == 2
    assert summary["truncated"] is False
    assert set(report["tables"]) == set(data.COVERAGE_TABLE_NAMES)

    with pytest.raises(Exception, match="invalid coverage report id"):
        data.read_coverage_report(coverage_project, "../../etc")
    with pytest.raises(Exception, match="not found"):
        data.read_coverage_report(coverage_project, "COV_FFFFFF")


# ---------------------------------------------------------------------------
# Actions layer
# ---------------------------------------------------------------------------


def test_reserve_entity_ids(project: Project) -> None:
    ids = actions.reserve_entity_ids(project)
    assert ids == {
        "organism": "ORG_000003", "sample": "SMP_000004", "run": "RUN_000002",
        "assembly": "ASM_000004", "annotation": "ANN_000004",
    }


def test_import_dataset_action(project: Project, tmp_path: Path) -> None:
    fasta = tmp_path / "new.fasta"
    fasta.write_text(">ctg1\n" + "ACGT" * 250 + "\n", encoding="utf-8")
    draft = _draft(project, fasta)
    result = actions.import_dataset(project, draft)

    assert result["entities"]["organism"] == draft["organism"]["id"]
    assert len(result["files"]) == 1
    assert result["files"][0]["role"] == "genome_fasta"
    assert "Sequencing provenance will not be recorded." in result["warnings"]
    assert "Taxonomy ID is missing." in result["warnings"]

    organism = _query(
        project, "SELECT scientific_name FROM organisms WHERE organism_id=?",
        (draft["organism"]["id"],),
    )
    assert organism[0]["scientific_name"] == "Syntheticus gamma"
    state = _query(
        project, "SELECT state FROM entity_state WHERE entity_id=?",
        (draft["organism"]["id"],),
    )
    assert state[0]["state"] == "METADATA_VALIDATED"
    sources = _query(
        project, "SELECT source_id FROM data_sources WHERE database_name='TestDB'")
    assert sources[0]["source_id"] == result["source_id"]
    runs = _query(
        project,
        "SELECT status FROM workflow_runs WHERE step='interactive_dataset_import'",
    )
    assert [row["status"] for row in runs] == ["completed"]
    changes = _query(
        project,
        "SELECT COUNT(*) AS n FROM changes WHERE reason LIKE '%interactive dataset import%'",
    )
    assert changes[0]["n"] >= 3
    files = _query(
        project, "SELECT file_role FROM files WHERE entity_id=?", (draft["assembly"]["id"],))
    assert [row["file_role"] for row in files] == ["genome_fasta"]


def test_create_release_action(project: Project) -> None:
    result = actions.create_release(
        project, "2099.01.tui", "assembly_production_v1", copy_files=True,
    )
    assert result["version"] == "2099.01.tui"
    assert (project.releases_root / "2099.01.tui" / "manifest.tsv").is_file()
    with pytest.raises(FileExistsError):
        actions.create_release(project, "2099.01.tui", "assembly_production_v1")


def test_export_action(project: Project, tmp_path: Path) -> None:
    output = tmp_path / "export-out"
    result = actions.export(project, str(output), entity_type="assembly")
    assert result["file_count"] == 3
    assert (output / "manifest.tsv").is_file()
    assert (output / "qc.tsv").is_file()
    assert (output / "provenance.json").is_file()
    with pytest.raises(FileExistsError, match="not empty"):
        actions.export(project, str(output), entity_type="assembly")


def test_run_coverage_fail_is_a_result_not_an_exception(coverage_project: Project) -> None:
    profile_path = coverage_project.profiles_dir / "covstrict.yaml"
    profile = {
        "kind": "taxonomy_coverage",
        "version": 1,
        "name": "covstrict",
        "taxonomy": {"source": "NCBI"},
        "scope": {"root_taxids": [1]},
        "targets": {"ranks": ["family", "genus"]},
        "thresholds": {
            "family": {"min_coverage_percent": 100},
            "genus": {"min_coverage_percent": 100},
        },
    }
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    db = Database(coverage_project.db_path)
    try:
        reference = taxonomy.compile_reference_set(db, coverage_project, "covstrict", "cov.1")
    finally:
        db.close()
    result = actions.run_coverage(coverage_project, reference["reference_set_id"])
    assert result["decision"] == "FAIL"
    assert result["exit_code"] == 1
    assert result["reason_codes"] == [
        "FAMILY_COVERAGE_BELOW_THRESHOLD", "GENUS_COVERAGE_BELOW_THRESHOLD",
    ]


# ---------------------------------------------------------------------------
# Headless UI: import wizard
# ---------------------------------------------------------------------------


def test_import_wizard_walkthrough(project: Project, tmp_path: Path) -> None:
    fasta = tmp_path / "wizard.fasta"
    fasta.write_text(">ctg1\n" + "ACGT" * 250 + "\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            await _settled(app)
            wizard = app.screen
            assert isinstance(wizard, ImportWizardScreen)
            pages = wizard.query_one("#wizard-pages", ContentSwitcher)
            assert pages.current == "page-source"
            assert wizard.reserved_ids["organism"] == "ORG_000003"

            wizard.query_one("#iw-database-name", Input).value = "TestDB"
            wizard.query_one("#iw-provider", Input).value = "TestProvider"
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-organism"

            wizard.query_one("#iw-organism-name", Input).value = "Syntheticus gamma"
            wizard.query_one("#iw-organism-taxon-id", Input).value = "12345"
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-sample"

            wizard.query_one("#iw-sample-strain", Input).value = "T1"
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-sequencing"
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-assembly"
            wizard.query_one("#iw-assembly-name", Input).value = "gamma-asm"
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-annotation"
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-files"

            wizard.query_one("#iw-file-genome-fasta", Input).value = str(fasta)
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-summary"
            summary = _static_text(wizard.query_one("#iw-summary", Static))
            assert "Syntheticus gamma" in summary
            assert "ASM_000004" in summary

            await _button_click(pilot, app, "#wizard-execute")
            await _settled(app)
            assert not isinstance(app.screen, ImportWizardScreen)

    _run(scenario())

    organisms = _query(
        project, "SELECT organism_id FROM organisms WHERE scientific_name='Syntheticus gamma'")
    assert [row["organism_id"] for row in organisms] == ["ORG_000003"]
    samples = _query(project, "SELECT sample_id, strain FROM samples WHERE organism_id='ORG_000003'")
    assert samples == [{"sample_id": "SMP_000004", "strain": "T1"}]
    assemblies = _query(project, "SELECT assembly_id FROM assemblies WHERE sample_id='SMP_000004'")
    assert [row["assembly_id"] for row in assemblies] == ["ASM_000004"]
    files = _query(project, "SELECT file_role FROM files WHERE entity_id='ASM_000004'")
    assert [row["file_role"] for row in files] == ["genome_fasta"]
    runs = _query(
        project, "SELECT status FROM workflow_runs WHERE step='interactive_dataset_import'")
    assert [row["status"] for row in runs] == ["completed"]


def test_import_wizard_global_binding_and_cancel(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            await _settled(app)
            assert isinstance(app.screen, ImportWizardScreen)
            await pilot.press("i")  # ignored while the wizard is open
            await pilot.pause()
            assert isinstance(app.screen, ImportWizardScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ImportWizardScreen)

    _run(scenario())


def test_import_wizard_validation_blocks(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            await _settled(app)
            wizard = app.screen
            assert isinstance(wizard, ImportWizardScreen)
            pages = wizard.query_one("#wizard-pages", ContentSwitcher)
            error = wizard.query_one("#wizard-error", Static)

            # Empty source form blocks on the required fields.
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-source"
            assert "Source database or repository is missing." in _static_text(error)

            # Non-INSDC requires citation and license.
            wizard.query_one("#iw-database-name", Input).value = "TestDB"
            wizard.query_one("#iw-provider", Input).value = "TestProvider"
            wizard.query_one("#iw-source-type", Select).value = "non_insdc"
            await pilot.pause()
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-source"
            assert "Non-INSDC data requires a reference citation or DOI." in _static_text(error)

            wizard.query_one("#iw-citation", Input).value = "doi:10.0000/test"
            wizard.query_one("#iw-license-name", Input).value = "CC0"
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-organism"

            # Creating an organism requires a scientific name.
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-organism"
            assert "Scientific name is required." in _static_text(error)

            wizard.query_one("#iw-organism-name", Input).value = "Syntheticus gamma"
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-sample"
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())
    assert _query(project, "SELECT COUNT(*) AS n FROM organisms")[0]["n"] == 2


def test_import_wizard_bad_file_path_blocks(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            await _settled(app)
            wizard = app.screen
            assert isinstance(wizard, ImportWizardScreen)
            pages = wizard.query_one("#wizard-pages", ContentSwitcher)
            wizard.draft = _draft(project, Path("/does/not/matter"))
            wizard._goto(pages and 6)  # files page
            await _settled(app)
            await pilot.pause()
            assert pages.current == "page-files"
            wizard.query_one("#iw-file-genome-fasta", Input).value = "/no/such/file.fasta"
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-files"
            assert "File does not exist" in _static_text(
                wizard.query_one("#wizard-error", Static))
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())


# ---------------------------------------------------------------------------
# Headless UI: publish screen
# ---------------------------------------------------------------------------


def test_release_create_end_to_end(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            app.action_switch_screen("publish")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(PublishPanel)
            table = panel.query_one("#releases-table", DataTable)
            assert table.row_count == 1

            panel.query_one("#release-profile", Select).value = "assembly_production_v1"
            await pilot.pause()
            await _settled(app)
            preview = _static_text(panel.query_one("#release-preview-summary", Static))
            assert "member file(s)" in preview
            exclusions = panel.query_one("#release-exclusions-table", DataTable)
            assert exclusions.row_count >= 1

            # Duplicate version is rejected inline, without opening the modal.
            panel.query_one("#release-version", Input).value = "2026.08.demo"
            await pilot.pause()
            await _button_click(pilot, app, "#release-create")
            assert not isinstance(app.screen, CreateReleaseModal)
            assert "already exists" in _static_text(
                panel.query_one("#release-error", Static))

            panel.query_one("#release-version", Input).value = "2099.01.tui"
            await pilot.pause()
            await _button_click(pilot, app, "#release-create")
            modal = app.screen
            assert isinstance(modal, CreateReleaseModal)
            command = _static_text(modal.query_one("#modal-command", Static))
            assert "operon release --version 2099.01.tui" in command
            assert "--profile assembly_production_v1" in command
            await _button_click(pilot, app, "#confirm")
            await _settled(app)
            assert not isinstance(app.screen, CreateReleaseModal)
            assert table.row_count == 2

    _run(scenario())
    assert (project.releases_root / "2099.01.tui" / "manifest.tsv").is_file()
    rows = _query(project, "SELECT version FROM releases ORDER BY version")
    assert [row["version"] for row in rows] == ["2026.08.demo", "2099.01.tui"]


def test_export_end_to_end(project: Project, tmp_path: Path) -> None:
    output = tmp_path / "export-out"

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            app.action_switch_screen("publish")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(PublishPanel)
            panel.query_one("#publish-tabs", TabbedContent).active = "tab-export"
            await pilot.pause()

            # No filters → inline validation error.
            await _button_click(pilot, app, "#export-preview-btn")
            assert "at least one selection criterion" in _static_text(
                panel.query_one("#export-error", Static))

            # Decision filter without profile → inline error (mirrors the CLI).
            panel.query_one("#export-decision", Select).value = "PASS"
            await pilot.pause()
            await _button_click(pilot, app, "#export-preview-btn")
            assert "requires --profile" in _static_text(
                panel.query_one("#export-error", Static))

            panel.query_one("#export-decision", Select).value = Select.NULL
            panel.query_one("#export-entity-type", Select).value = "assembly"
            panel.query_one("#export-output", Input).value = str(output)
            await pilot.pause()
            await _button_click(pilot, app, "#export-preview-btn")
            assert "3 file(s)" in _static_text(
                panel.query_one("#export-preview-summary", Static))

            await _button_click(pilot, app, "#export-run")
            modal = app.screen
            assert isinstance(modal, ExportModal)
            command = _static_text(modal.query_one("#modal-command", Static))
            assert "operon export --output" in command
            assert "--entity-type assembly" in command
            await _button_click(pilot, app, "#confirm")
            await _settled(app)
            assert not isinstance(app.screen, ExportModal)

            # The now non-empty output directory is rejected inline.
            await _button_click(pilot, app, "#export-run")
            assert "not empty" in _static_text(panel.query_one("#export-error", Static))

    _run(scenario())
    assert (output / "manifest.tsv").is_file()
    assert (output / "provenance.json").is_file()


# ---------------------------------------------------------------------------
# Headless UI: coverage screen
# ---------------------------------------------------------------------------


def test_coverage_screen_generate_and_browse(coverage_project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(coverage_project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            app.action_switch_screen("coverage")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(CoveragePanel)
            assert panel.query_one("#taxonomy-snapshots-table", DataTable).row_count == 1
            assert panel.query_one("#reference-sets-table", DataTable).row_count == 1
            reports = panel.query_one("#coverage-reports-table", DataTable)
            assert reports.row_count == 0

            panel.query_one("#coverage-reference-set", Select).value = "cov@cov.1"
            await pilot.pause()
            await _button_click(pilot, app, "#coverage-generate")
            modal = app.screen
            assert isinstance(modal, CoverageModal)
            command = _static_text(modal.query_one("#modal-command", Static))
            assert "operon report coverage --reference-set cov@cov.1" in command
            await _button_click(pilot, app, "#confirm")
            await _settled(app)
            assert not isinstance(app.screen, CoverageModal)
            assert reports.row_count == 1

            reports.focus()
            reports.move_cursor(row=0, animate=False)
            await pilot.pause()
            await pilot.press("enter")
            await _settled(app)
            await pilot.pause()
            headline = _static_text(panel.query_one("#coverage-report-headline", Static))
            assert "cov@cov.1" in headline
            assert "metadata" in headline
            summary = panel.query_one("#coverage-table-coverage_summary", DataTable)
            assert summary.row_count == 2

    _run(scenario())
    rows = _query(coverage_project, "SELECT decision FROM coverage_reports")
    assert [row["decision"] for row in rows] == ["PASS"]


def test_coverage_screen_empty_project(tmp_path: Path) -> None:
    """Without taxonomy data the screen renders empty tables, not a crash."""
    project = Project.init(tmp_path / "empty-coverage")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            app.action_switch_screen("coverage")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(CoveragePanel)
            assert panel.query_one("#taxonomy-snapshots-table", DataTable).row_count == 0
            generate = panel.query_one("#coverage-generate", Button)
            assert generate.disabled
            assert "no reference sets" in _static_text(
                panel.query_one("#coverage-error", Static))

    _run(scenario())
