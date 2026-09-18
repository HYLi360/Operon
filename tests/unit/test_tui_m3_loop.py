"""M3 acceptance loop, driven through the TUI actions end to end.

``analyze`` (a fake tool emitting BLAST-style hits) → ``classify`` →
``select-sequences`` → ``adopt`` → ``fanout`` → a second ``analyze`` whose
recipe carries ``file_role_prefix`` and therefore runs over exactly the
fan-out unit files.  Every step is asserted on its provenance — run rows,
per-label audit rows, ``file_lineage`` edges — and re-run to prove the
idempotency contracts (0 label changes, reused registrations, reused units).

The CLI versions of these steps are covered by
``tests/integration/test_fanout_analyze.py``; this file proves the TUI's
actions drive the identical core paths.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("textual")

import yaml

from operon.config import Project
from operon.database import Database
from operon.files import ingest_file
from operon.profiles import load_profile
from operon.tui import actions

PROTEINS = {"p1": "M" * 60, "p2": "M" * 40, "p3": "M" * 30, "p4": "K" * 50, "p5": "K" * 20}
LOOP_PROFILE = {
    "kind": "sequence_classification",
    "version": 1,
    "description": "loop tiers",
    "applies_to": {"entity_type": "annotation", "file_role": "protein_fasta"},
    "sources": {"hits": {"analysis": "cdd", "filter": []}},
    "rules": [
        {"label": "STRONG", "source": "hits",
         "when": [{"field": "evalue", "operator": "<=", "value": 1e-8}]},
        {"label": "WEAK", "source": "hits",
         "when": [{"field": "evalue", "operator": ">", "value": 1e-8}]},
        {"label": "NONE", "source": "hits", "absent": True},
    ],
}


@pytest.fixture
def loop_project(tmp_path: Path) -> Project:
    """A project with a protein FASTA, a sequences registry, a fake tool and a profile."""
    project = Project.init(tmp_path / "loop")
    script = tmp_path / "fakeblast.py"
    script.write_text(textwrap.dedent("""
        import sys
        args = sys.argv[1:]
        if '-version' in args:
            print('fakeblast: 9.8.7')
            raise SystemExit(0)
        out = args[args.index('-out') + 1]
        with open(out, 'w') as handle:
            handle.write('p1\\ts1\\t99.0\\t30\\t1e-10\\t500\\n')
            handle.write('p2\\ts2\\t95.0\\t20\\t1e-4\\t100\\n')
    """).strip(), encoding="utf-8")
    tools = {
        "version": 1,
        "tools": {
            "fakeblast": {
                "executable": str(script),
                "run_method": sys.executable,
                "version_args": ["-version"],
                "version_pattern": r"fakeblast:\s*([^\s]+)",
                "recipes": {
                    "cdd": {
                        "description": "fake domain hits",
                        "entity_type": "annotation",
                        "file_role": "protein_fasta",
                        "format": "fasta",
                        "output_subdir": "cdd",
                        "output_suffix": ".hits.tsv",
                        "arguments": ["-query", "${input}", "-out", "${output}"],
                        "result_parser": "blast_tabular",
                        "result_columns": ["qseqid", "sseqid", "pident", "length",
                                           "evalue", "bitscore"],
                        "hit_metric_columns": ["pident", "length", "evalue", "bitscore"],
                        "numeric_columns": ["pident", "length", "evalue", "bitscore"],
                        "query_column": "qseqid",
                        "subject_column": "sseqid",
                    },
                    "unit_scan": {
                        "description": "second pass over the fan-out units",
                        "entity_type": "annotation",
                        "file_role_prefix": "units",
                        "format": "fasta",
                        "output_subdir": "unit_scan",
                        "output_suffix": ".scan.tsv",
                        "arguments": ["-query", "${input}", "-out", "${output}"],
                        "result_parser": "blast_tabular",
                        "result_columns": ["qseqid", "sseqid", "pident", "length",
                                           "evalue", "bitscore"],
                        "hit_metric_columns": ["pident", "length", "evalue", "bitscore"],
                        "numeric_columns": ["pident", "length", "evalue", "bitscore"],
                        "query_column": "qseqid",
                        "subject_column": "sseqid",
                    },
                },
            },
        },
    }
    project.tools_config_path.write_text(yaml.safe_dump(tools, sort_keys=False), encoding="utf-8")
    project.profiles_dir.mkdir(parents=True, exist_ok=True)
    (project.profiles_dir / "loop_tiers.yaml").write_text(
        yaml.safe_dump(LOOP_PROFILE, sort_keys=False), encoding="utf-8")

    db = Database(project.db_path)
    try:
        db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Testus", "taxonomy_source": "NCBI",
        })
        db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "contig", "assembly_version": 1,
        })
        db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_source": "test", "annotation_version": 1,
        })
        fasta = tmp_path / "proteins.faa"
        fasta.write_text(
            "".join(f">{seqid}\n{sequence}\n" for seqid, sequence in PROTEINS.items()),
            encoding="utf-8",
        )
        row = ingest_file(db, project, fasta, "annotation", "ANN_000001", "protein_fasta")
        for seqid, sequence in PROTEINS.items():
            db.insert_row("sequences", {
                "file_id": row["file_id"], "file_sha256": row["sha256"],
                "entity_type": "annotation", "entity_id": "ANN_000001",
                "seqid": seqid, "length": len(sequence),
            })
        assignments = tmp_path / "units.tsv"
        assignments.write_text("unit\tseqid\nunitA\tp1\nunitA\tp2\nunitB\tp3\n", encoding="utf-8")
        ingest_file(db, project, assignments, "annotation", "ANN_000001", "assignments",
                    fmt="tsv", compression="none")
    finally:
        db.close()
    return project


def _query(project: Project, sql: str, params: tuple = ()) -> list:
    db = Database(project.db_path)
    try:
        return db.query(sql, params)
    finally:
        db.close()


def _file_id(project: Project, role: str) -> str:
    return _query(project, "SELECT file_id FROM files WHERE file_role=?", (role,))[0]["file_id"]


def test_m3_acceptance_loop(loop_project: Project, tmp_path: Path) -> None:
    """analyze → classify → select → adopt → fanout → analyze the unit files."""
    project = loop_project
    source = _file_id(project, "protein_fasta")
    assignments = _file_id(project, "assignments")

    # 1. analyze: the fake tool emits two hits; the recipe parses them.
    analysis = actions.run_analysis(project, "cdd")
    jobs = _query(project, "SELECT job_id, status, file_id FROM analysis_jobs WHERE analysis_name='cdd'")
    assert [job["status"] for job in jobs] == ["completed"]
    assert jobs[0]["file_id"] == source
    alignments = _query(project, "SELECT query_id, evalue FROM analysis_alignments ORDER BY query_id")
    assert [(row["query_id"], row["evalue"]) for row in alignments] == [("p1", 1e-10), ("p2", 1e-4)]
    assert analysis["messages"] is not None

    # 2. classify: every sequence gets a tier, each change is audited.
    classified = actions.run_classify(project, "loop_tiers")
    assert classified["profile"] == "loop_tiers"
    assert classified["labels_written"] == len(PROTEINS)
    labels = {
        row["seqid"]: row["label"]
        for row in _query(project, "SELECT seqid, label FROM sequence_labels ORDER BY seqid")
    }
    assert labels == {"p1": "STRONG", "p2": "WEAK", "p3": "NONE", "p4": "NONE", "p5": "NONE"}
    audits = _query(project, "SELECT object_type, object_id, new_value, workflow_run_id "
                             "FROM changes WHERE object_type='sequence_label'")
    assert len(audits) == 5
    assert all(audit["workflow_run_id"] for audit in audits)
    rerun = actions.run_classify(project, "loop_tiers")
    assert (rerun["labels_written"], rerun["labels_removed"]) == (0, 0)

    # 3. select-sequences: with a hit keeps p1/p2; without a hit keeps the rest.
    hit_out = tmp_path / "hit.faa"
    selected = actions.select_sequences(project, file_id=source, out=str(hit_out),
                                        analyses=["cdd"], require_hit=True)
    assert selected["selected"] == 2
    nohit_out = tmp_path / "nohit.faa"
    nohit = actions.select_sequences(project, file_id=source, out=str(nohit_out),
                                     analyses=["cdd"], require_hit=False)
    assert nohit["selected"] == len(PROTEINS) - 2

    # 4. adopt: the subset re-enters the manifest with a lineage edge.
    adopted = actions.adopt(project, items=[{
        "path": str(hit_out), "entity_type": "annotation", "entity_id": "ANN_000001",
        "role": "selected_proteins", "derived_from": [source],
    }])
    assert adopted["registered"] == 1 and adopted["reused"] == 0
    adopted_file = adopted["file_ids"][0]
    edges = _query(project, "SELECT derived_file_id, input_file_id FROM file_lineage "
                            "WHERE derived_file_id=?", (adopted_file,))
    assert [edge["input_file_id"] for edge in edges] == [source]
    readopt = actions.adopt(project, items=[{
        "path": str(hit_out), "entity_type": "annotation", "entity_id": "ANN_000001",
        "role": "selected_proteins", "derived_from": [source],
    }])
    assert readopt["reused"] == 1 and readopt["file_ids"] == [adopted_file]

    # 5. fanout: per-unit files with lineage to the source and the assignments.
    expected_roles = ["units:unitA", "units:unitB"]
    preview = actions.fanout(project, assignments_file_id=assignments,
                             source_file_ids=[source], entity_type="annotation",
                             entity_id="ANN_000001", role_prefix="units", dry_run=True)
    assert sorted(unit["status"] for unit in preview["units"]) == ["would_create", "would_create"]
    fanned = actions.fanout(project, assignments_file_id=assignments, source_file_ids=[source],
                            entity_type="annotation", entity_id="ANN_000001",
                            role_prefix="units", dry_run=False)
    assert (fanned["created"], fanned["reused"]) == (2, 0)
    roles = sorted(row["file_role"] for row in
                   _query(project, "SELECT file_role FROM files WHERE file_role LIKE 'units:%'"))
    assert roles == expected_roles
    unit_ids = [row["file_id"] for row in
                _query(project, "SELECT file_id FROM files WHERE file_role LIKE 'units:%'")]
    for unit_id in unit_ids:
        inputs = {
            edge["input_file_id"]
            for edge in _query(project, "SELECT input_file_id FROM file_lineage "
                                        "WHERE derived_file_id=?", (unit_id,))
        }
        assert inputs == {source, assignments}
    refined = actions.fanout(project, assignments_file_id=assignments, source_file_ids=[source],
                             entity_type="annotation", entity_id="ANN_000001",
                             role_prefix="units", dry_run=False)
    assert (refined["created"], refined["reused"]) == (0, 2)

    # 6. the acceptance step: a file_role_prefix recipe runs over the units.
    actions.run_analysis(project, "unit_scan")
    scan_jobs = _query(project, "SELECT file_id, status FROM analysis_jobs "
                                "WHERE analysis_name='unit_scan'")
    assert len(scan_jobs) == 2
    assert {job["file_id"] for job in scan_jobs} == set(unit_ids)
    assert all(job["status"] == "completed" for job in scan_jobs)

    # 7. provenance: one run row per stage, in order.
    steps = [row["step"] for row in
             _query(project, "SELECT step FROM workflow_runs ORDER BY rowid")]
    # The fixture's own ingests come first; the loop then appends one row per stage.
    assert [step for step in steps if step != "ingest"] == [
        "analysis:cdd", "classify-sequences", "classify-sequences",
        "select-sequences", "select-sequences", "adopt", "adopt",
        "fanout", "fanout", "analysis:unit_scan", "analysis:unit_scan",
    ]

    # 8. the saved profile is exactly what classify consumed.
    profile = load_profile(project.profiles_dir, "loop_tiers",
                           expected_kind="sequence_classification")
    assert profile["version"] == 1
    assert json.loads(
        _query(project, "SELECT profile_document FROM qc_profiles "
                        "WHERE profile_name='loop_tiers'")[-1]["profile_document"]
    )["description"] == "loop tiers"
