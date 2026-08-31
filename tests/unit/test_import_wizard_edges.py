"""Branch-oriented tests for the interactive dataset import workflow."""

from __future__ import annotations

from pathlib import Path

import pytest

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon import import_wizard as wizard


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


class _Prompt:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


def test_prompt_wrappers_and_choice_validation(monkeypatch):
    assert wizard._required(" x ") is True
    assert wizard._required(" ") == "A value is required."
    assert wizard._answer(_Prompt("answer")) == "answer"
    with pytest.raises(KeyboardInterrupt):
        wizard._answer(_Prompt(None))

    captured = {}
    monkeypatch.setattr(
        wizard.questionary,
        "text",
        lambda message, **kwargs: captured.update(message=message, **kwargs) or _Prompt("  value "),
    )
    assert wizard._text("Message", "default", required=True) == "value"
    assert captured["validate"] is wizard._required

    monkeypatch.setattr(wizard.questionary, "path", lambda *a, **k: _Prompt("/tmp/x"))
    monkeypatch.setattr(wizard.questionary, "confirm", lambda *a, **k: _Prompt(1))
    monkeypatch.setattr(wizard.questionary, "select", lambda *a, **k: _Prompt("selected"))
    assert wizard._path("Path") == "/tmp/x"
    assert wizard._confirm("Confirm") is True
    assert wizard._select("Select", ["selected"]) == "selected"

    def autocomplete(_message, **kwargs):
        assert kwargs["validate"]("one") is True
        assert kwargs["validate"]("bad") == "Choose one of the completion options."
        return _Prompt(" one ")

    monkeypatch.setattr(wizard.questionary, "autocomplete", autocomplete)
    assert wizard._autocomplete("Auto", ["one"], meta_information={"one": "1"}) == "one"


def test_ask_source_builds_both_source_classifications(monkeypatch):
    draft = {"source": {"database_name": "old", "source_type": "insdc"}}
    monkeypatch.setattr(wizard, "_select", lambda *_a, **_k: "non_insdc")
    answers = iter(["DB", "Provider", "https://record", "doi:1", "CC0", "https://license"])
    required_flags = []

    def text(_message, default="", *, required=False):
        required_flags.append(required)
        return next(answers)

    monkeypatch.setattr(wizard, "_text", text)
    wizard._ask_source(None, draft)
    assert draft["source"]["source_type"] == "non_insdc"
    assert draft["source"]["citation"] == "doi:1"
    assert required_flags == [True, True, False, True, True, False]


def test_ask_entity_sections_cover_create_reuse_and_skip(project_db, monkeypatch):
    _project, db = project_db
    db.insert_row("organisms", {
        "organism_id": "ORG_000001", "scientific_name": "Duplicatus",
        "taxon_id": 1, "taxonomy_source": "NCBI", "taxonomy_version": "v1",
    })
    db.insert_row("organisms", {
        "organism_id": "ORG_000002", "scientific_name": "Duplicatus",
    })
    db.insert_row("samples", {
        "sample_id": "SMP_000001", "organism_id": "ORG_000001", "strain": "S1",
    })
    db.insert_row("assemblies", {
        "assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_name": "A1",
    })
    db.insert_row("annotations", {
        "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
        "annotation_source": "Pipe", "annotation_version": "1",
    })

    draft = {"organism": {"id": "ORG_000002"}}
    monkeypatch.setattr(wizard, "_autocomplete", lambda *a, **k: "Duplicatus [ORG_000002]")
    wizard._ask_organism(db, draft)
    assert draft["organism"] == {"action": "reuse", "id": "ORG_000002"}

    monkeypatch.setattr(wizard, "_autocomplete", lambda *a, **k: "Create a new organism")
    texts = iter(["New species", "123", "species", "v2"])
    monkeypatch.setattr(wizard, "_text", lambda *a, **k: next(texts))
    monkeypatch.setattr(wizard, "_select", lambda *a, **k: "NCBI")
    wizard._ask_organism(db, draft)
    assert draft["organism"]["action"] == "create"
    assert draft["organism"]["row"]["scientific_name"] == "New species"

    draft = {
        "organism": {"id": "ORG_000001"},
        "source": {"record_url": "https://record", "provider": "Provider"},
    }
    monkeypatch.setattr(wizard, "_select", lambda *a, **k: "SMP_000001")
    wizard._ask_sample(db, draft)
    assert draft["sample"] == {"action": "reuse", "id": "SMP_000001"}
    monkeypatch.setattr(wizard, "_select", lambda *a, **k: "__new__")
    monkeypatch.setattr(wizard, "_text", lambda *a, **k: "value")
    wizard._ask_sample(db, draft)
    assert draft["sample"]["row"]["source_record"] == "https://record"

    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    wizard._ask_sequencing(db, draft)
    assert draft["run"] is None
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    selections = iter(["WGS", "GENOMIC", "PAIRED", "ILLUMINA"])
    monkeypatch.setattr(wizard, "_select", lambda *a, **k: next(selections))
    text_values = iter(["SRR1", "ERX1", "NovaSeq"])
    monkeypatch.setattr(wizard, "_text", lambda *a, **k: next(text_values))
    wizard._ask_sequencing(db, draft)
    assert draft["run"]["row"]["sample_id"] == draft["sample"]["id"]

    draft["sample"] = {"id": "SMP_000001"}
    monkeypatch.setattr(wizard, "_select", lambda *a, **k: "ASM_000001")
    wizard._ask_assembly(db, draft)
    assert draft["assembly"] == {"action": "reuse", "id": "ASM_000001"}
    assembly_selections = iter(["__new__", "chromosome", "RefSeq"])
    monkeypatch.setattr(wizard, "_select", lambda *a, **k: next(assembly_selections))
    assembly_texts = iter(["GCF_000000001.1", "A", "1", "assembler"])
    monkeypatch.setattr(wizard, "_text", lambda *a, **k: next(assembly_texts))
    wizard._ask_assembly(db, draft)
    assert draft["assembly"]["row"]["submitter"] == "Provider"

    draft["assembly"] = {"id": "ASM_000001"}
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    wizard._ask_annotation(db, draft)
    assert draft["annotation"] is None
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_select", lambda *a, **k: "ANN_000001")
    wizard._ask_annotation(db, draft)
    assert draft["annotation"] == {"action": "reuse", "id": "ANN_000001"}
    monkeypatch.setattr(wizard, "_select", lambda *a, **k: "__new__")
    annotation_texts = iter(["Pipe2", "2", "2025-01-01"])
    monkeypatch.setattr(wizard, "_text", lambda *a, **k: next(annotation_texts))
    wizard._ask_annotation(db, draft)
    assert draft["annotation"]["row"]["assembly_id"] == "ASM_000001"


def test_ask_path_files_and_summary_cover_optional_sections(project_db, tmp_path, monkeypatch, capsys):
    _project, db = project_db
    real_file = tmp_path / "genome.fna"
    real_file.write_text(">x\nACGT\n", encoding="utf-8")
    paths = iter([str(tmp_path / "missing"), str(real_file)])
    monkeypatch.setattr(wizard, "_path", lambda *a, **k: next(paths))
    assert wizard._ask_path("Genome") == str(real_file.resolve())
    assert "File does not exist" in capsys.readouterr().out
    monkeypatch.setattr(wizard, "_path", lambda *a, **k: "")
    assert wizard._ask_path("Genome") == ""

    values = iter([str(real_file), "", "", "", "", "", ""])
    monkeypatch.setattr(wizard, "_ask_path", lambda *a, **k: next(values))
    draft = {
        "source": {"source_type": "insdc", "database_name": "NCBI", "provider": "NCBI"},
        "organism": {"action": "create", "id": "ORG_1", "row": {"scientific_name": "X"}},
        "sample": {"action": "create", "id": "SMP_1", "row": {}},
        "run": {"action": "create", "id": "RUN_1", "row": {"run_id": "RUN_1"}},
        "assembly": {"action": "create", "id": "ASM_1", "row": {}},
        "annotation": {"action": "create", "id": "ANN_1", "row": {}},
    }
    wizard._ask_files(db, draft)
    assert [item["role"] for item in draft["files"]] == ["genome_fasta"]
    summary = wizard._summary(db, draft)
    assert "Import plan" in summary
    assert "Genome FASTA" in summary
    assert "Taxonomy ID is missing" in summary
    assert "GFF3 is missing" in summary

    minimal = {"source": {}, "files": []}
    summary = wizard._summary(db, minimal)
    assert "[skipped]" in summary
    assert "[none]" in summary


def test_warning_relationships_and_link_synchronization(project_db):
    _project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_1", "scientific_name": "One"})
    db.insert_row("organisms", {"organism_id": "ORG_2", "scientific_name": "Two"})
    db.insert_row("samples", {"sample_id": "SMP_1", "organism_id": "ORG_1"})
    db.insert_row("samples", {"sample_id": "SMP_2", "organism_id": "ORG_2"})
    db.insert_row("assemblies", {"assembly_id": "ASM_1", "sample_id": "SMP_1"})
    draft = {
        "source": {"source_type": "non_insdc"},
        "organism": {"action": "reuse", "id": "ORG_2"},
        "sample": {"action": "reuse", "id": "SMP_1"},
        "assembly": {"action": "reuse", "id": "ASM_1"},
        "files": [],
    }
    warnings = wizard._warnings(db, draft)
    assert "The selected sample does not belong to the selected organism." in warnings
    assert "The selected assembly does not belong to the selected sample." not in warnings
    draft["sample"]["id"] = "SMP_2"
    warnings = wizard._warnings(db, draft)
    assert "The selected assembly does not belong to the selected sample." in warnings

    linked = {
        "source": {"record_url": "url", "provider": "provider"},
        "organism": {"id": "ORG_NEW"},
        "sample": {"action": "create", "id": "SMP_NEW", "row": {}},
        "run": {"action": "create", "id": "RUN_NEW", "row": {}},
        "assembly": {"action": "create", "id": "ASM_NEW", "row": {}},
        "annotation": {"action": "create", "id": "ANN_NEW", "row": {}},
    }
    wizard._synchronize_new_entity_links(linked)
    assert linked["sample"]["row"] == {"organism_id": "ORG_NEW", "source_record": "url"}
    assert linked["run"]["row"]["sample_id"] == "SMP_NEW"
    assert linked["assembly"]["row"]["submitter"] == "provider"
    assert linked["annotation"]["row"]["assembly_id"] == "ASM_NEW"


def test_preflight_rejects_missing_source_target_conflicts_and_wrong_bytes(
    project_db, tmp_path
):
    project, db = project_db
    with pytest.raises(ValidationError, match="Source classification"):
        wizard._preflight(db, project, {})

    base = {
        "source": {"source_type": "insdc", "database_name": "NCBI", "provider": "NCBI"},
        "organism": {"action": "create", "id": "ORG_000001", "row": {
            "organism_id": "ORG_000001", "scientific_name": "Example species",
        }},
        "sample": None,
        "run": None,
        "assembly": None,
        "annotation": None,
        "files": [],
    }
    assert wizard._preflight(db, project, base)[0][0] == "organism"
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Existing"})
    with pytest.raises(ConflictError, match="planned ID already exists"):
        wizard._preflight(db, project, base)

    base["organism"] = {"action": "reuse", "id": "ORG_000001"}
    source = tmp_path / "x.fna"
    source.write_text(">x\nA\n", encoding="utf-8")
    base["files"] = [{
        "label": "Genome FASTA", "entity_type": "assembly", "role": "genome_fasta",
        "path": str(source),
    }]
    with pytest.raises(ValidationError, match="has no target assembly"):
        wizard._preflight(db, project, base)


def test_wizard_execute_warning_decline_then_commit(project_db, monkeypatch):
    project, db = project_db

    class TTY:
        def isatty(self):
            return True

        def write(self, value):
            return len(value)

        def flush(self):
            return None

    monkeypatch.setattr(wizard.sys, "stdin", TTY())
    monkeypatch.setattr(wizard.sys, "stdout", TTY())
    monkeypatch.setattr(wizard, "_summary", lambda *_a: "summary")
    monkeypatch.setattr(wizard, "_preflight", lambda *_a: [])
    monkeypatch.setattr(wizard, "_warnings", lambda *_a: ["warning"])
    monkeypatch.setattr(wizard, "_commit", lambda *_a: {"ok": True})
    for name in ("_ask_source", "_ask_organism", "_ask_sample", "_ask_sequencing",
                 "_ask_assembly", "_ask_annotation", "_ask_files"):
        monkeypatch.setattr(wizard, name, lambda *_a: None)
    actions = iter(["execute", "execute"])
    confirms = iter([False, True])
    monkeypatch.setattr(wizard, "_select", lambda *_a, **_k: next(actions))
    monkeypatch.setattr(wizard, "_confirm", lambda *_a, **_k: next(confirms))
    assert wizard.run_dataset_wizard(db, project) == {"ok": True}


def test_wizard_rejects_non_terminal(project_db, monkeypatch):
    project, db = project_db

    class NotTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(wizard.sys, "stdin", NotTTY())
    with pytest.raises(ValidationError, match="requires a terminal"):
        wizard.run_dataset_wizard(db, project)
