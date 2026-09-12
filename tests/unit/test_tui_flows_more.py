"""Deeper TUI flows: import-wizard page wiring, publish builders, write-action edges.

Companion to ``test_tui_writes.py`` / ``test_tui_publish.py`` / ``test_tui_config.py``.
Where a screen method is only reachable through a long interactive walk (Back /
Edit jumps, inline validation, worker failures) the tests drive the screen's own
navigation entry points (``_goto`` / ``_advance``) and assert the resulting screen
state, draft dict, database rows, or files on disk.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

import yaml
from rich.text import Text
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    DataTable,
    Input,
    Select,
    Static,
    TabbedContent,
)

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ValidationError
from operon.tools import get_recipe
from operon.tui import actions, data
from operon.tui.app import OperonApp
from operon.tui.screens import import_wizard as wizard_screen
from operon.tui.screens import publish as publish_screen
from operon.tui.screens.import_wizard import CREATE_NEW, ImportWizardScreen
from operon.tui.screens.publish import CreateReleaseModal, ExportModal, PublishPanel


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-flows-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    """Each test gets its own copy of the demo project."""
    target = tmp_path / "project"
    shutil.copytree(demo_template.root, target)
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


def _queried_text(screen, selector: str) -> str:
    """Text of the first matching Static, or '' while the screen is still composing."""
    nodes = list(screen.query(selector))
    return _static_text(nodes[0]) if nodes else ""


def _queried_visible(screen, selector: str) -> bool:
    nodes = list(screen.query(selector))
    return bool(nodes) and bool(nodes[0].display)


SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 15.0


def _run(coroutine) -> None:
    """Drive a Textual headless scenario without requiring pytest-asyncio."""
    asyncio.run(asyncio.wait_for(coroutine, timeout=SCENARIO_TIMEOUT))


async def _settled(app, timeout: float = SETTLE_TIMEOUT) -> None:
    """Wait until the splash screen startup and every worker have finished."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while app.workers or getattr(app, "_starting", False):
        if loop.time() > deadline:
            states = [worker.state.name for worker in app.workers]
            raise TimeoutError(f"workers did not finish within {timeout}s: {states}")
        await asyncio.sleep(0.05)


async def _wait_until(
    predicate: Callable[[], bool],
    description: str,
    timeout: float = SETTLE_TIMEOUT,
) -> None:
    """Wait for an observable UI result after a thread worker completes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        await asyncio.sleep(0.05)


async def _click(pilot, selector: str) -> None:
    """Click a widget, scrolling it into view first (clipped clicks return False)."""
    pilot.app.screen.query_one(selector).scroll_visible(animate=False)
    await pilot.pause()
    assert await pilot.click(selector), f"click did not land on {selector}"


async def _button_click(pilot, app, selector: str) -> None:
    """Click a button after outlasting the Button ``-active`` debounce window."""
    await asyncio.sleep(0.35)
    await _click(pilot, selector)
    await _settled(app)
    await pilot.pause()


def _source_dict() -> dict:
    return {
        "source_type": "insdc", "database_name": "TestDB", "provider": "TestProvider",
        "record_url": "", "citation": "", "license_name": "", "license_url": "",
    }


def _draft(project: Project, fasta: Path) -> dict:
    ids = actions.reserve_entity_ids(project)
    return {
        "source": _source_dict(),
        "organism": {
            "action": "create", "id": ids["organism"],
            "row": {
                "organism_id": ids["organism"], "scientific_name": "Syntheticus gamma",
                "taxon_id": "12345", "taxonomic_rank": "species",
                "taxonomy_source": "NCBI", "taxonomy_version": "v1",
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


async def _open_wizard(pilot, app, project: Project) -> ImportWizardScreen:
    """Push the wizard over the running app and wait for its startup worker."""
    wizard = ImportWizardScreen(project)
    app.push_screen(wizard)
    await pilot.pause()
    await _wait_until(lambda: bool(wizard.reserved_ids), "reserve the wizard entity ids")
    return wizard


# ---------------------------------------------------------------------------
# actions.py: profile / recipe validation edges
# ---------------------------------------------------------------------------


def test_profile_document_validation_rejects_malformed_documents(project: Project) -> None:
    """The document guard rails save_profile relies on reject each defect."""
    with pytest.raises(ValidationError, match="document must be a mapping"):
        actions._validate_profile_document("demo", ["not", "a", "mapping"])
    with pytest.raises(ValidationError, match="only kind 'qc'"):
        actions._validate_profile_document(
            "demo", {"kind": "taxonomy_coverage", "version": 1}
        )
    with pytest.raises(ValidationError, match="'version' is required"):
        actions._validate_profile_document("demo", {"kind": "qc"})

    document = data.get_profile_document(project, "assembly_production_v1")
    document["applies_to"] = "assembly"
    with pytest.raises(ValidationError, match="'applies_to' must be a list"):
        actions.save_profile(project, "assembly_production_v1", document)

    document = data.get_profile_document(project, "assembly_production_v1")
    document["required"] = "not-a-list"
    with pytest.raises(ValidationError, match="'required' must be a list of rules"):
        actions.save_profile(project, "assembly_production_v1", document)

    document = data.get_profile_document(project, "assembly_production_v1")
    document["warnings"] = ["not-a-mapping"]
    with pytest.raises(ValidationError, match="warnings rule 1 must be a mapping"):
        actions.save_profile(project, "assembly_production_v1", document)

    # Nothing above may have touched the on-disk profile.
    still = yaml.safe_load(
        (project.profiles_dir / "assembly_production_v1.yaml").read_text(encoding="utf-8")
    )
    assert still["version"] == 1
    assert still["applies_to"] == ["assembly"]


def test_save_profile_rejects_unusable_existing_files(project: Project) -> None:
    broken = project.profiles_dir / "broken_v1.yaml"
    broken.write_text("- just\n- a list\n", encoding="utf-8")
    qc_document = {
        "kind": "qc", "applies_to": ["assembly"], "required": [], "warnings": [],
    }
    with pytest.raises(ValidationError, match="existing file is not a YAML mapping"):
        actions.save_profile(project, "broken_v1", qc_document)
    assert broken.read_text(encoding="utf-8") == "- just\n- a list\n"

    coverage_path = project.profiles_dir / "coverage_viridiplantae_v1.yaml"
    original = coverage_path.read_bytes()
    with pytest.raises(ValidationError, match="on-disk kind is 'taxonomy_coverage'"):
        actions.save_profile(project, "coverage_viridiplantae_v1", qc_document)
    assert coverage_path.read_bytes() == original


def test_save_profile_coerces_numeric_rule_values(project: Project) -> None:
    """Numeric-looking rule strings are stored as numbers, other strings verbatim."""
    document = {
        "kind": "qc",
        "applies_to": ["assembly"],
        "required": [
            {"metric": "total_length", "operator": ">=", "value": "2500", "code": "SHORT"},
            {"metric": "gc_percent", "operator": "between", "min": "40.5",
             "max": "not-a-number", "code": "GC_ODD"},
            {"metric": "contig_count", "operator": "in",
             "values": ["", "1", "2.5", "plain", 7], "code": "COUNT"},
        ],
        "warnings": [],
    }
    result = actions.save_profile(project, "coerced_v1", document)
    assert result["version"] == 1 and result["unchanged"] is False

    loaded = yaml.safe_load(
        (project.profiles_dir / "coerced_v1.yaml").read_text(encoding="utf-8")
    )
    required = loaded["required"]
    assert required[0]["value"] == 2500 and isinstance(required[0]["value"], int)
    assert required[1]["min"] == 40.5 and isinstance(required[1]["min"], float)
    assert required[1]["max"] == "not-a-number"
    assert required[2]["values"] == ["", 1, 2.5, "plain", 7]


def test_save_recipe_rejects_malformed_tools_config(project: Project) -> None:
    path = project.tools_config_path
    original = path.read_bytes()
    info = data.get_recipe_document(project, "blastn_nt")
    document = dict(info["document"])

    with pytest.raises(ValidationError, match="document must be a mapping"):
        actions.save_recipe(project, info["tool"], "tui_new", ["not", "a", "mapping"])
    assert path.read_bytes() == original

    path.write_text(
        yaml.safe_dump({"tools": {"blastn": "not-a-mapping"}}, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="tool 'blastn' in tools.yaml must be a mapping"):
        actions.save_recipe(project, "blastn", "tui_new", document)

    path.write_text(
        yaml.safe_dump({"tools": {"blastn": {"recipes": "not-a-mapping"}}}, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="recipes must be a mapping"):
        actions.save_recipe(project, "blastn", "tui_new", document)

    path.write_text(
        yaml.safe_dump(
            {"tools": {"blastn": {"recipes": {"tui_new": "not-a-mapping"}}}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="recipe 'tui_new' in tools.yaml must be a mapping"):
        actions.save_recipe(project, "blastn", "tui_new", document)


def test_save_recipe_new_recipe_starts_at_version_one(project: Project) -> None:
    info = data.get_recipe_document(project, "blastn_nt")
    document = dict(info["document"])

    result = actions.save_recipe(project, info["tool"], "tui_new_recipe", document)
    assert result == {
        "name": "tui_new_recipe", "tool": "blastn", "version": 1,
        "snapshot_id": result["snapshot_id"], "unchanged": False,
    }
    assert result["snapshot_id"] is not None

    recipe = get_recipe(project, "tui_new_recipe")
    assert recipe.version == 1
    history = data.recipe_history(project, "tui_new_recipe")
    assert [row["version"] for row in history] == [1]


def test_check_tools_skips_non_mapping_tool_entries(project: Project, tmp_path: Path) -> None:
    fake = tmp_path / "faketool"
    fake.write_text("#!/bin/sh\necho 'faketool 1.2.3'\n", encoding="utf-8")
    fake.chmod(0o755)
    project.tools_config_path.write_text(
        yaml.safe_dump({
            "tools": {
                "broken": "not-a-mapping",
                "faketool": {
                    "executable": str(fake), "run_method": "",
                    "version_args": ["--version"],
                    "version_pattern": r"faketool\s+([^\s]+)",
                    "recipes": {},
                },
            }
        }, sort_keys=False),
        encoding="utf-8",
    )

    seen: list[str] = []
    results = actions.check_tools(
        project, timeout=30, on_result=lambda row: seen.append(row["name"])
    )
    assert [row["name"] for row in results] == ["faketool"]
    assert results[0]["ok"] is True and results[0]["version"] == "1.2.3"
    assert seen == ["faketool"]


def test_ingest_remote_url_fetches_to_temp_and_cleans_up(
    project: Project, tmp_path: Path, monkeypatch
) -> None:
    """A remote source is staged locally, recorded with its URL, then removed."""
    fetched = tmp_path / "remote.fasta"
    fetched.write_text(">remote\nACGTACGTACGT\n", encoding="utf-8")
    requested: list[str] = []

    def fake_fetch(_project, url):
        requested.append(url)
        return fetched

    monkeypatch.setattr("operon.remotes.fetch_url_to_temp", fake_fetch)
    row = actions.ingest(
        project, "sftp://host/remote.fasta", "assembly", "ASM_000001", "remote_fasta",
    )
    assert requested == ["sftp://host/remote.fasta"]
    assert row["file_role"] == "remote_fasta"
    stored = _query(
        project, "SELECT source_url FROM files WHERE file_id=?", (row["file_id"],)
    )[0]
    assert stored["source_url"] == "sftp://host/remote.fasta"
    assert not fetched.exists()  # the staged copy is always removed

    # An explicit --source-url wins over the transport URL...
    fetched.write_text(">remote2\nACGTACGTACGTAC\n", encoding="utf-8")
    other = actions.ingest(
        project, "remote://mirror/other.fasta", "assembly", "ASM_000001", "remote_other",
        source_url="https://example.invalid/record",
    )
    stored = _query(
        project, "SELECT source_url FROM files WHERE file_id=?", (other["file_id"],)
    )[0]
    assert stored["source_url"] == "https://example.invalid/record"
    assert not fetched.exists()

    # ...and a fetched directory bundle is archived and then removed too.
    staging = tmp_path / "staging"
    (staging / "nested").mkdir(parents=True)
    (staging / "nested" / "x.fasta").write_text(">x\nACGT\n", encoding="utf-8")
    monkeypatch.setattr("operon.remotes.fetch_url_to_temp", lambda _project, _url: staging)
    bundle = actions.ingest(
        project, "sftp://host/bundle", "assembly", "ASM_000001", "remote_bundle",
    )
    assert bundle["file_role"] == "remote_bundle"
    assert bundle["format"] == "directory"
    assert not staging.exists()


# ---------------------------------------------------------------------------
# import_wizard.py: repopulating pages from the draft
# ---------------------------------------------------------------------------


def test_import_wizard_populates_every_page_from_draft(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            wizard = await _open_wizard(pilot, app, project)
            pages = wizard.query_one("#wizard-pages", ContentSwitcher)
            draft = wizard.draft

            draft["source"] = {
                "source_type": "non_insdc", "database_name": "TestDB", "provider": "Provider",
                "record_url": "https://record", "citation": "doi:1", "license_name": "CC0",
                "license_url": "https://license",
            }
            wizard._goto(0)
            await _wait_until(
                lambda: wizard.query_one("#iw-database-name", Input).value == "TestDB",
                "repopulate the source page",
            )
            assert wizard.query_one("#iw-source-type", Select).value == "non_insdc"
            assert wizard.query_one("#iw-provider", Input).value == "Provider"
            assert wizard.query_one("#iw-record-url", Input).value == "https://record"
            assert wizard.query_one("#iw-citation", Input).value == "doi:1"
            assert wizard.query_one("#iw-license-name", Input).value == "CC0"
            assert wizard.query_one("#iw-license-url", Input).value == "https://license"

            # Organism: reuse an existing row, then a draft row that does not exist.
            draft["organism"] = {"action": "reuse", "id": "ORG_000002"}
            wizard._goto(1)
            await _wait_until(
                lambda: wizard.query_one("#iw-organism-choice", Select).value == "ORG_000002",
                "repopulate the reused organism",
            )
            draft["organism"] = {
                "action": "create", "id": "ORG_000099",
                "row": {
                    "scientific_name": "Populated species", "taxon_id": 42,
                    "taxonomic_rank": "strain", "taxonomy_version": "v7",
                },
            }
            wizard._goto(1)
            await _wait_until(
                lambda: wizard.query_one("#iw-organism-name", Input).value == "Populated species",
                "repopulate the new organism",
            )
            assert wizard.query_one("#iw-organism-choice", Select).value == CREATE_NEW
            assert wizard.query_one("#iw-organism-taxon-id", Input).value == "42"
            assert wizard.query_one("#iw-organism-rank", Input).value == "strain"
            assert wizard.query_one("#iw-organism-taxonomy-version", Input).value == "v7"

            # Sample: reuse, then create.
            draft["organism"] = {"action": "reuse", "id": "ORG_000001"}
            draft["sample"] = {"action": "reuse", "id": "SMP_000003"}
            wizard._goto(2)
            await _wait_until(
                lambda: wizard.query_one("#iw-sample-choice", Select).value == "SMP_000003",
                "repopulate the reused sample",
            )
            draft["sample"] = {
                "action": "create", "id": "SMP_000099",
                "row": {"biosample_accession": "SAMN9", "strain": "S9", "isolate": "iso-1"},
            }
            wizard._goto(2)
            await _wait_until(
                lambda: wizard.query_one("#iw-sample-isolate", Input).value == "iso-1",
                "repopulate the new sample",
            )
            assert wizard.query_one("#iw-sample-choice", Select).value == CREATE_NEW
            assert wizard.query_one("#iw-sample-biosample", Input).value == "SAMN9"
            assert wizard.query_one("#iw-sample-strain", Input).value == "S9"

            # Sequencing: without a run, then with one.
            draft["sample"] = {"action": "reuse", "id": "SMP_000001"}
            draft["run"] = None
            wizard._goto(3)
            await _wait_until(
                lambda: wizard.query_one("#iw-run-enabled", Checkbox).value is False,
                "clear the sequencing section",
            )
            draft["run"] = {
                "action": "create", "id": "RUN_000099",
                "row": {
                    "run_accession": "SRR9", "experiment_accession": "ERX9",
                    "instrument_model": "NovaSeq",
                },
            }
            wizard._goto(3)
            await _wait_until(
                lambda: wizard.query_one("#iw-run-accession", Input).value == "SRR9",
                "repopulate the sequencing run",
            )
            assert wizard.query_one("#iw-run-enabled", Checkbox).value is True
            assert wizard.query_one("#iw-run-experiment", Input).value == "ERX9"
            assert wizard.query_one("#iw-run-instrument", Input).value == "NovaSeq"

            # Assembly: reuse, then create.
            draft["assembly"] = {"action": "reuse", "id": "ASM_000001"}
            wizard._goto(4)
            await _wait_until(
                lambda: wizard.query_one("#iw-assembly-choice", Select).value == "ASM_000001",
                "repopulate the reused assembly",
            )
            draft["assembly"] = {
                "action": "create", "id": "ASM_000099",
                "row": {
                    "assembly_accession": "GCA_9", "assembly_name": "populated-asm",
                    "assembly_version": "5", "assembly_method": "assembler 2",
                },
            }
            wizard._goto(4)
            await _wait_until(
                lambda: wizard.query_one("#iw-assembly-name", Input).value == "populated-asm",
                "repopulate the new assembly",
            )
            assert wizard.query_one("#iw-assembly-choice", Select).value == CREATE_NEW
            assert wizard.query_one("#iw-assembly-accession", Input).value == "GCA_9"
            assert wizard.query_one("#iw-assembly-version", Input).value == "5"
            assert wizard.query_one("#iw-assembly-method", Input).value == "assembler 2"

            # Annotation: disabled, reuse, then create.
            draft["assembly"] = {"action": "reuse", "id": "ASM_000001"}
            draft["annotation"] = None
            wizard._goto(5)
            await _wait_until(
                lambda: wizard.query_one("#iw-annotation-enabled", Checkbox).value is False,
                "clear the annotation section",
            )
            draft["annotation"] = {"action": "reuse", "id": "ANN_000001"}
            wizard._goto(5)
            await _wait_until(
                lambda: wizard.query_one("#iw-annotation-choice", Select).value == "ANN_000001",
                "repopulate the reused annotation",
            )
            assert wizard.query_one("#iw-annotation-enabled", Checkbox).value is True
            draft["annotation"] = {
                "action": "create", "id": "ANN_000099",
                "row": {
                    "annotation_source": "Pipe", "annotation_version": "3",
                    "annotation_date": "2025-02-03",
                },
            }
            wizard._goto(5)
            await _wait_until(
                lambda: wizard.query_one("#iw-annotation-source", Input).value == "Pipe",
                "repopulate the new annotation",
            )
            assert wizard.query_one("#iw-annotation-choice", Select).value == CREATE_NEW
            assert wizard.query_one("#iw-annotation-version", Input).value == "3"
            assert wizard.query_one("#iw-annotation-date", Input).value == "2025-02-03"

            # Files: role -> path mapping survives a jump back to the files page.
            draft["files"] = [{"role": "genome_fasta", "path": "/tmp/populated.fasta"}]
            wizard._goto(6)
            await _wait_until(
                lambda: wizard.query_one("#iw-file-genome-fasta", Input).value
                == "/tmp/populated.fasta",
                "repopulate the files page",
            )
            assert wizard.query_one("#iw-file-reads-r1", Input).value == ""
            assert pages.current == "page-files"

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ImportWizardScreen)

    _run(scenario())


# ---------------------------------------------------------------------------
# import_wizard.py: collecting pages into the draft
# ---------------------------------------------------------------------------


def test_import_wizard_collects_reuse_new_and_optional_sections(
    project: Project, tmp_path: Path
) -> None:
    fasta = tmp_path / "collect.fasta"
    fasta.write_text(">ctg1\nACGTACGTACGT\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            wizard = await _open_wizard(pilot, app, project)
            pages = wizard.query_one("#wizard-pages", ContentSwitcher)
            wizard.draft = {"source": _source_dict()}

            wizard._goto(1)
            await _wait_until(lambda: pages.current == "page-organism", "show organism")
            wizard.query_one("#iw-organism-choice", Select).value = "ORG_000002"
            assert wizard._collect("organism") is None
            assert wizard.draft["organism"] == {"action": "reuse", "id": "ORG_000002"}

            wizard._goto(2)
            await _wait_until(lambda: pages.current == "page-sample", "show sample")
            wizard.query_one("#iw-sample-choice", Select).value = "SMP_000002"
            assert wizard._collect("sample") is None
            assert wizard.draft["sample"] == {"action": "reuse", "id": "SMP_000002"}

            wizard._goto(3)
            await _wait_until(lambda: pages.current == "page-sequencing", "show sequencing")
            wizard.query_one("#iw-run-enabled", Checkbox).value = True
            wizard.query_one("#iw-run-accession", Input).value = "SRR9"
            wizard.query_one("#iw-run-experiment", Input).value = "ERX9"
            wizard.query_one("#iw-run-strategy", Select).value = "WGS"
            wizard.query_one("#iw-run-source", Select).value = "GENOMIC"
            wizard.query_one("#iw-run-layout", Select).value = "PAIRED"
            wizard.query_one("#iw-run-platform", Select).value = "ILLUMINA"
            wizard.query_one("#iw-run-instrument", Input).value = "NovaSeq"
            assert wizard._collect("sequencing") is None
            run = wizard.draft["run"]
            assert run["action"] == "create" and run["id"] == wizard.reserved_ids["run"]
            assert run["row"]["sample_id"] == "SMP_000002"
            assert run["row"]["run_accession"] == "SRR9"
            assert run["row"]["library_layout"] == "PAIRED"
            assert run["row"]["platform"] == "ILLUMINA"
            assert run["row"]["instrument_model"] == "NovaSeq"

            wizard._goto(4)
            await _wait_until(lambda: pages.current == "page-assembly", "show assembly")
            wizard.query_one("#iw-assembly-choice", Select).value = "ASM_000002"
            assert wizard._collect("assembly") is None
            assert wizard.draft["assembly"] == {"action": "reuse", "id": "ASM_000002"}

            # Jump back to the assembly holding an annotation for the picker.
            wizard.draft["assembly"] = {"action": "reuse", "id": "ASM_000001"}
            wizard._goto(5)
            await _wait_until(lambda: pages.current == "page-annotation", "show annotation")
            wizard.query_one("#iw-annotation-enabled", Checkbox).value = True
            wizard.query_one("#iw-annotation-choice", Select).value = "ANN_000001"
            assert wizard._collect("annotation") is None
            assert wizard.draft["annotation"] == {"action": "reuse", "id": "ANN_000001"}

            wizard.query_one("#iw-annotation-choice", Select).value = CREATE_NEW
            wizard.query_one("#iw-annotation-source", Input).value = "Pipe"
            wizard.query_one("#iw-annotation-version", Input).value = "3"
            wizard.query_one("#iw-annotation-date", Input).value = "2025-02-03"
            assert wizard._collect("annotation") is None
            annotation = wizard.draft["annotation"]
            assert annotation["action"] == "create"
            assert annotation["id"] == wizard.reserved_ids["annotation"]
            assert annotation["row"]["assembly_id"] == "ASM_000001"
            assert annotation["row"]["annotation_date"] == "2025-02-03"

            wizard._goto(6)
            await _wait_until(lambda: pages.current == "page-files", "show files")
            # Every visible entry is empty -> skipped, not an error.
            assert wizard._collect("files") is None
            assert wizard.draft["files"] == []

            wizard.query_one("#iw-file-genome-fasta", Input).value = str(fasta)
            assert wizard._collect("files") is None
            assert [item["role"] for item in wizard.draft["files"]] == ["genome_fasta"]
            assert wizard.draft["files"][0]["path"] == str(fasta.resolve())
            assert wizard.draft["files"][0]["entity_type"] == "assembly"

            # The summary page has nothing to merge into the draft.
            assert wizard._collect("summary") is None

            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())


# ---------------------------------------------------------------------------
# import_wizard.py: navigation, re-entrancy, worker failures
# ---------------------------------------------------------------------------


def test_import_wizard_navigation_buttons_and_execute_guard(
    project: Project, tmp_path: Path
) -> None:
    fasta = tmp_path / "nav.fasta"
    fasta.write_text(">ctg1\nACGTACGTACGT\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            wizard = await _open_wizard(pilot, app, project)
            pages = wizard.query_one("#wizard-pages", ContentSwitcher)
            back = wizard.query_one("#wizard-back", Button)
            assert back.disabled is True

            wizard.query_one("#iw-database-name", Input).value = "TestDB"
            wizard.query_one("#iw-provider", Input).value = "TestProvider"
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-organism"
            assert back.disabled is False

            await _button_click(pilot, app, "#wizard-back")
            assert pages.current == "page-source"
            assert back.disabled is True
            # A press that arrives while already on the first page must not move.
            wizard.post_message(Button.Pressed(back))
            await pilot.pause()
            assert pages.current == "page-source"
            assert wizard.page_index == 0

            wizard.draft = _draft(project, fasta)
            wizard._goto(7)
            await _wait_until(lambda: pages.current == "page-summary", "show summary")
            assert wizard.query_one("#wizard-execute", Button).display is True
            assert wizard.query_one("#wizard-edit-bar").display is True

            # "Edit source" jumps back; the next Next returns to the summary.
            await _button_click(pilot, app, "#wizard-edit-source")
            assert pages.current == "page-source"
            assert wizard._return_to_summary is True
            await _button_click(pilot, app, "#wizard-next")
            assert pages.current == "page-summary"
            assert wizard._return_to_summary is False

            # Next on the summary page collects nothing and stays put.
            wizard._advance()
            await pilot.pause()
            assert pages.current == "page-summary"

            # A press from a button the wizard does not handle is ignored.
            wizard.mount(Button("Ghost", id="wizard-ghost"))
            await pilot.pause()
            await _click(pilot, "#wizard-ghost")
            await pilot.pause()
            assert isinstance(app.screen, ImportWizardScreen)
            assert pages.current == "page-summary"

            # The re-entrancy guard keeps a second Execute from starting a commit.
            execute = wizard.query_one("#wizard-execute", Button)
            wizard._executing = True
            wizard._execute_import()
            await pilot.pause()
            assert execute.disabled is False
            assert wizard._executing is True
            wizard._executing = False

            await _button_click(pilot, app, "#wizard-cancel")
            await pilot.pause()
            assert not isinstance(app.screen, ImportWizardScreen)

    _run(scenario())
    assert _query(project, "SELECT COUNT(*) AS n FROM organisms")[0]["n"] == 2


def test_import_wizard_surfaces_worker_failures(project: Project, monkeypatch) -> None:
    """Startup and page-load worker failures are rendered inline, not raised."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()

            def broken_reserve(*_args, **_kwargs):
                raise ValidationError("id reservation offline")

            monkeypatch.setattr(wizard_screen.actions, "reserve_entity_ids", broken_reserve)
            orphan = ImportWizardScreen(project)
            app.push_screen(orphan)
            await _wait_until(
                lambda: "id reservation offline" in _queried_text(orphan, "#wizard-error"),
                "show the startup failure",
            )
            assert orphan.reserved_ids == {}
            app.pop_screen()
            await pilot.pause()
            monkeypatch.undo()

            def broken_summary(*_args, **_kwargs):
                raise ValidationError("summary service unavailable")

            monkeypatch.setattr(wizard_screen.data, "import_summary", broken_summary)
            wizard = await _open_wizard(pilot, app, project)
            wizard.draft = {"source": _source_dict(), "files": []}
            wizard._goto(7)
            await _wait_until(
                lambda: "summary service unavailable" in _queried_text(wizard, "#wizard-error"),
                "show the summary failure",
            )
            assert isinstance(app.screen, ImportWizardScreen)

            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())


def test_import_wizard_execute_failure_keeps_the_screen_open(
    project: Project, tmp_path: Path, monkeypatch
) -> None:
    fasta = tmp_path / "failing.fasta"
    fasta.write_text(">ctg1\nACGTACGTACGT\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            wizard = await _open_wizard(pilot, app, project)
            wizard.draft = _draft(project, fasta)
            wizard._goto(7)
            await _wait_until(
                lambda: _queried_visible(wizard, "#wizard-execute"),
                "show the summary page",
            )

            def broken_commit(*_args, **_kwargs):
                raise ValidationError("commit exploded")

            monkeypatch.setattr(wizard_screen.actions, "import_dataset", broken_commit)
            await _button_click(pilot, app, "#wizard-execute")
            await _wait_until(
                lambda: "commit exploded" in _queried_text(wizard, "#wizard-error"),
                "show the commit failure",
            )
            assert isinstance(app.screen, ImportWizardScreen)
            assert wizard._executing is False
            assert wizard.query_one("#wizard-execute", Button).disabled is False
            assert wizard.draft["organism"]["row"]["scientific_name"] == "Syntheticus gamma"
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())
    assert _query(project, "SELECT COUNT(*) AS n FROM organisms")[0]["n"] == 2
    assert _query(
        project,
        "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='interactive_dataset_import'",
    )[0]["n"] == 0


# ---------------------------------------------------------------------------
# publish.py: release builder
# ---------------------------------------------------------------------------


def test_release_modal_command_text_variants(project: Project) -> None:
    copy_modal = CreateReleaseModal(project, "2099.1", "assembly_production_v1", False, "copy")
    assert copy_modal.command_text() == (
        "operon release --version 2099.1 --profile assembly_production_v1"
    )

    link_modal = CreateReleaseModal(project, "2099.1", "assembly_production_v1", False, "hardlink")
    assert link_modal.command_text().endswith("--link hardlink")

    files_modal = CreateReleaseModal(project, "2099.1", "assembly_production_v1", True, "hardlink")
    assert files_modal.command_text().endswith("--copy-files")


def test_publish_release_validation_hardlink_and_cancel(
    project: Project, monkeypatch
) -> None:
    reloads: list[int] = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            reload_now = app.reload_after_write
            monkeypatch.setattr(
                app, "reload_after_write",
                lambda: (reloads.append(1), reload_now())[1],
            )
            app.action_switch_screen("publish")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(PublishPanel)
            table = panel.query_one("#releases-table", DataTable)
            assert table.row_count == 1

            # Preview without a profile is refused inline.
            await _button_click(pilot, app, "#release-preview-btn")
            assert "select a profile first" in _static_text(
                panel.query_one("#release-error", Static)
            )

            # Version is validated before the profile.
            await _button_click(pilot, app, "#release-create")
            assert "version is required" in _static_text(
                panel.query_one("#release-error", Static)
            )
            panel.query_one("#release-version", Input).value = "2099.05.link"
            await pilot.pause()
            await _button_click(pilot, app, "#release-create")
            assert "select a profile first" in _static_text(
                panel.query_one("#release-error", Static)
            )
            assert not isinstance(app.screen, CreateReleaseModal)

            panel.query_one("#release-profile", Select).value = "assembly_production_v1"
            await pilot.pause()
            await _settled(app)
            assert "member file(s)" in _static_text(
                panel.query_one("#release-preview-summary", Static)
            )

            # Preview can also be re-run from its button.
            await _button_click(pilot, app, "#release-preview-btn")
            assert "member file(s)" in _static_text(
                panel.query_one("#release-preview-summary", Static)
            )

            # Link instead of copy: the modal must preview the equivalent flag.
            panel.query_one("#release-copy-files", Checkbox).value = False
            panel.query_one("#release-link", Select).value = "hardlink"
            await pilot.pause()
            await _button_click(pilot, app, "#release-create")
            modal = app.screen
            assert isinstance(modal, CreateReleaseModal)
            command = _static_text(modal.query_one("#modal-command", Static))
            assert "--link hardlink" in command and "--copy-files" not in command
            await _button_click(pilot, app, "#confirm")
            await _wait_until(
                lambda: not isinstance(app.screen, CreateReleaseModal),
                "dismiss the release modal",
            )
            await _settled(app)
            assert table.row_count == 2
            assert reloads == [1]

            # Cancelling the modal must dismiss without touching the app data.
            panel.query_one("#release-version", Input).value = "2099.05.cancelled"
            await pilot.pause()
            await _button_click(pilot, app, "#release-create")
            assert isinstance(app.screen, CreateReleaseModal)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, CreateReleaseModal)
            assert reloads == [1]
            assert table.row_count == 2

    _run(scenario())

    release_root = project.releases_root / "2099.05.link"
    assert (release_root / "manifest.tsv").is_file()
    provenance = json.loads((release_root / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["storage_mode"] == "hardlink"
    assert not (project.releases_root / "2099.05.cancelled").exists()
    rows = _query(project, "SELECT version FROM releases ORDER BY version")
    assert [row["version"] for row in rows] == ["2026.08.demo", "2099.05.link"]


# ---------------------------------------------------------------------------
# publish.py: export builder
# ---------------------------------------------------------------------------


def test_publish_export_validation_and_symlink_command(project: Project, tmp_path: Path) -> None:
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

            # No selection criterion at all.
            await _button_click(pilot, app, "#export-run")
            assert "at least one selection criterion" in _static_text(
                panel.query_one("#export-error", Static)
            )

            # A criterion without an output directory.
            panel.query_one("#export-entity-type", Select).value = "assembly"
            await pilot.pause()
            await _button_click(pilot, app, "#export-run")
            assert "output directory is required" in _static_text(
                panel.query_one("#export-error", Static)
            )

            panel.query_one("#export-entity-id", Input).value = "ASM_000001, ASM_000002"
            panel.query_one("#export-link", Select).value = "symlink"
            panel.query_one("#export-include-qc", Checkbox).value = False
            panel.query_one("#export-output", Input).value = str(output)
            await pilot.pause()
            await _button_click(pilot, app, "#export-run")
            modal = app.screen
            assert isinstance(modal, ExportModal)
            command = _static_text(modal.query_one("#modal-command", Static))
            assert "--entity-id ASM_000001 --entity-id ASM_000002" in command
            assert "--link symlink" in command
            assert "--no-qc" in command
            await _button_click(pilot, app, "#confirm")
            await _wait_until(
                lambda: not isinstance(app.screen, ExportModal),
                "dismiss the export modal",
            )
            await _settled(app)
            await pilot.pause()

            # The now non-empty output directory is rejected before the modal.
            await _button_click(pilot, app, "#export-run")
            assert "not empty" in _static_text(panel.query_one("#export-error", Static))

    _run(scenario())

    assert (output / "manifest.tsv").is_file()
    assert not (output / "qc.tsv").exists()
    provenance = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["link_kind"] == "symlink"
    assert provenance["file_count"] == 2
    linked = sorted(path for path in (output / "data").rglob("*") if path.is_symlink())
    assert [path.name for path in linked] == ["ASM_000001.genome_fasta.fasta",
                                              "ASM_000002.genome_fasta.fasta"]


# ---------------------------------------------------------------------------
# publish.py: preview failures, malformed rows, ignored presses
# ---------------------------------------------------------------------------


def test_publish_preview_failures_and_reason_codes(project: Project, monkeypatch) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            app.action_switch_screen("publish")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(PublishPanel)

            # A release row whose summary is not a mapping renders placeholders.
            panel.render_data({
                "releases": [{
                    "version": "2099.09.corrupt", "created_at": "2099-09-01",
                    "profile": "assembly_production_v1", "summary": "corrupt",
                }],
                "profiles": ["assembly_production_v1"],
            })
            await pilot.pause()
            table = panel.query_one("#releases-table", DataTable)
            assert table.row_count == 1
            assert [str(cell) for cell in table.get_row_at(0)] == [
                "2099.09.corrupt", "2099-09-01", "assembly_production_v1", "?", "?",
            ]

            def broken_release_preview(*_args, **_kwargs):
                raise ValidationError("release preview exploded")

            monkeypatch.setattr(
                publish_screen.data, "release_preview", broken_release_preview
            )
            panel.query_one("#release-profile", Select).value = "assembly_production_v1"
            await _wait_until(
                lambda: "release preview exploded"
                in _static_text(panel.query_one("#release-error", Static)),
                "show the release preview failure",
            )
            assert _static_text(panel.query_one("#release-preview-summary", Static)) == ""

            def broken_export_preview(*_args, **_kwargs):
                raise ValidationError("export preview exploded")

            monkeypatch.setattr(publish_screen.data, "export_preview", broken_export_preview)
            panel.query_one("#publish-tabs", TabbedContent).active = "tab-export"
            await pilot.pause()
            panel.query_one("#export-entity-type", Select).value = "assembly"
            await pilot.pause()
            await _button_click(pilot, app, "#export-preview-btn")
            assert "export preview exploded" in _static_text(
                panel.query_one("#export-error", Static)
            )
            assert _static_text(panel.query_one("#export-preview-summary", Static)) == ""

            # Exclusion reason codes: JSON list, undecodable text, and missing.
            panel._apply_release_preview({
                "members": [], "member_bytes": 0,
                "exclusions": [
                    {"entity_type": "assembly", "entity_id": "ASM_000009",
                     "effective_decision": "FAIL", "exclusion_reason": "stale decision",
                     "reason_codes": json.dumps(["LOW_CONTIGUITY", "STALE"])},
                    {"entity_type": "annotation", "entity_id": "ANN_000009",
                     "effective_decision": "REVIEW", "exclusion_reason": "not a list",
                     "reason_codes": '{"broken": true}'},
                    {"entity_type": "run", "entity_id": "RUN_000009",
                     "effective_decision": None, "exclusion_reason": "no codes",
                     "reason_codes": "{not json"},
                    {"entity_type": "sample", "entity_id": "SMP_000009",
                     "effective_decision": "PASS", "exclusion_reason": "codes absent",
                     "reason_codes": None},
                ],
            })
            await pilot.pause()
            exclusions = panel.query_one("#release-exclusions-table", DataTable)
            assert exclusions.row_count == 4
            assert [str(cell) for cell in exclusions.get_row_at(0)][0] == "assembly:ASM_000009"
            assert [str(cell) for cell in exclusions.get_row_at(0)][3] == "LOW_CONTIGUITY, STALE"
            assert [str(cell) for cell in exclusions.get_row_at(1)][3] == ""
            assert [str(cell) for cell in exclusions.get_row_at(2)][3] == "{not json"
            assert [str(cell) for cell in exclusions.get_row_at(3)][3] == ""

            # A press from a button the panel does not handle is ignored.
            error_before = _static_text(panel.query_one("#release-error", Static))
            panel.mount(Button("Ghost", id="ghost-button"))
            await pilot.pause()
            await _click(pilot, "#ghost-button")
            await pilot.pause()
            assert type(app.screen).__name__ == "Screen"
            assert _static_text(panel.query_one("#release-error", Static)) == error_before

            # A failing panel load renders in both inline error areas.
            def broken_listing(*_args, **_kwargs):
                raise ValidationError("release listing offline")

            monkeypatch.setattr(publish_screen.data, "list_releases", broken_listing)
            panel.reload()
            await _wait_until(
                lambda: "release listing offline" in _queried_text(panel, "#release-error")
                and "release listing offline" in _queried_text(panel, "#export-error"),
                "show the panel load failure",
            )

            await pilot.press("escape")

    _run(scenario())
