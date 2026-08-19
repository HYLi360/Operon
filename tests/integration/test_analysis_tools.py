"""Encapsulated external analysis tools (BLAST/HMMER-style recipes)."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
from pathlib import Path

from tests.helpers import PytestAssertions

import yaml

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file
from operon.tools import ToolSpec, launcher_prefix


class TestAnalysisTools(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_TOOL_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)

    def _write_fake_blast(self) -> Path:
        script = self.root / "fakeblast.py"
        script.write_text(textwrap.dedent(f"""
            import sys
            args = sys.argv[1:]
            if '-version' in args:
                print('fakeblast: 9.8.7')
                raise SystemExit(0)
            out = None
            for i, arg in enumerate(args):
                if arg == '-out':
                    out = args[i + 1]
            with open(out, 'w') as handle:
                handle.write('q1\\ts1\\t99.0\\t100\\t1e-10\\t500\\n')
                handle.write('q1\\ts2\\t95.0\\t90\\t1e-5\\t300\\n')
                handle.write('q2\\ts3\\t80.0\\t80\\t0.01\\t100\\n')
        """).strip(), encoding="utf-8")
        return script

    def _write_fake_hmmsearch(self) -> Path:
        script = self.root / "fakehmm.py"
        script.write_text(textwrap.dedent(f"""
            import sys
            args = sys.argv[1:]
            if '-h' in args:
                print('hmmsearch :: HMMER 3.4')
                raise SystemExit(0)
            out = args[args.index('--tblout') + 1]
            with open(out, 'w') as handle:
                handle.write('# HMMER tblout comment\\n')
                handle.write('PF00001 - query1 - 1.2e-10 123.4 0.1 1.2e-10 123.4 0.1 1 1 0 0 1 1 1 -\\n')
                handle.write('PF00002 - query2 - 0.01 34.5 0.2 0.01 34.5 0.2 1 1 0 0 1 1 1 -\\n')
        """).strip(), encoding="utf-8")
        return script

    def _write_fake_busco(self) -> Path:
        script = self.root / "fakebusco.py"
        script.write_text(textwrap.dedent("""
            import json
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            if '--version' in args:
                print('BUSCO 6.1.0')
                raise SystemExit(0)
            input_path = Path(args[args.index('-i') + 1])
            run_name = args[args.index('-o') + 1]
            out_path = Path(args[args.index('--out_path') + 1])
            download_path = Path(args[args.index('--download_path') + 1])
            assert input_path.is_file()
            assert '/' not in run_name
            assert 'fasta' not in run_name
            download_path.mkdir(parents=True, exist_ok=True)
            output = out_path / run_name
            output.mkdir(parents=True, exist_ok=False)
            generic = {
                'results': {'Complete percentage': 1.0, 'n_markers': 1}
            }
            (output / 'short_summary.generic.eukaryota.json').write_text(json.dumps(generic))
            summary = {
                'parameters': {
                    'datasets_version': 'odb12.2', 'orthodb_version': '12.2',
                    'dataset_version': '01', 'ncbi_taxid': '3699'
                },
                'lineage_dataset': {
                    'name': 'brassicales_odb12.2', 'creation_date': '2026-05-13',
                    'number_of_buscos': '7083', 'number_of_species': '10'
                },
                'versions': {'busco': '6.1.0', 'hmmsearch': 3.4},
                'results': {
                    'one_line_summary': 'C:98.5%[S:21.3%,D:77.2%],F:0.2%,M:1.3%,n:7083',
                    'Complete percentage': 98.5, 'Complete BUSCOs': 6976,
                    'Single copy percentage': 21.3, 'Single copy BUSCOs': 1511,
                    'Multi copy percentage': 77.2, 'Multi copy BUSCOs': 5465,
                    'Fragmented percentage': 0.2, 'Fragmented BUSCOs': 14,
                    'Missing percentage': 1.3, 'Missing BUSCOs': 93,
                    'n_markers': 7083, 'domain': 'eukaryota'
                }
            }
            (output / 'short_summary.specific.brassicales_odb12.2.json').write_text(
                json.dumps(summary)
            )
            (output / 'logs').mkdir()
            (output / 'logs' / 'busco.log').write_text('completed\\n')
        """).strip(), encoding="utf-8")
        return script

    def _write_fake_directory_tool(self) -> Path:
        script = self.root / "fakedir.py"
        script.write_text(textwrap.dedent("""
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            if '-version' in args:
                print('fakedir: 1.2.3')
                raise SystemExit(0)
            source = Path(args[args.index('--input') + 1])
            output = Path(args[args.index('--output') + 1])
            assert source.is_dir()
            output.mkdir(parents=True, exist_ok=False)
            text = ''.join(p.read_text() for p in sorted(source.glob('*.txt')))
            (output / 'combined.txt').write_text(text)
        """).strip(), encoding="utf-8")
        return script

    def _write_tool_config(self, executable: Path, tool_name: str, recipe_name: str,
                           entity_type: str, file_role: str, parser: str, database: Path,
                           version_args: list[str] | None = None, version_pattern: str = ""):
        tool_config = {
            "version": 1,
            "conda": {"bin": "conda", "run_args": ["run", "--no-capture-output"]},
            "tools": {
                tool_name: {
                    "description": "fake tool for tests",
                    "executable": str(executable),
                    "run_method": sys.executable,
                    "version_args": version_args if version_args is not None else ["-version"],
                    "version_pattern": version_pattern or rf"{tool_name}:\s*([^\s]+)",
                    "recipes": {
                        recipe_name: {
                            "description": "fake recipe",
                            "entity_type": entity_type,
                            "file_role": file_role,
                            "format": "fasta",
                            "database": str(database),
                            "database_version": "test-db-1",
                            "output_subdir": recipe_name,
                            "output_suffix": ".out.tsv",
                            "arguments": ["-db", "${database}", "-query", "${input}", "-out", "${output}", "-num_threads", "${threads}"],
                            "result_parser": parser,
                            "result_columns": ["qseqid", "sseqid", "pident", "length", "evalue", "bitscore"],
                            "hit_metric_columns": ["pident", "length", "evalue", "bitscore"],
                            "max_hits_per_query": 2,
                        }
                    },
                }
            },
        }
        self.project.tools_config_path.write_text(yaml.safe_dump(tool_config, sort_keys=False), encoding="utf-8")

    def _add_assembly(self):
        self.db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Testus", "taxonomy_source": "NCBI"})
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        self.db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_level": "contig", "assembly_version": 1})
        fasta = self.root / "asm.fa"
        fasta.write_text(">ctg1\n" + "ACGT" * 600 + "\n", encoding="utf-8")
        return ingest_file(self.db, self.project, fasta, "assembly", "ASM_000001", "genome_fasta")

    def _add_annotation(self):
        self._add_assembly()
        self.db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_source": "test", "annotation_version": 1,
        })
        proteins = self.root / "proteins.faa"
        proteins.write_text(">p1\nMPEPTIDE\n", encoding="utf-8")
        return ingest_file(
            self.db, self.project, proteins, "annotation", "ANN_000001", "protein_fasta"
        )

    def test_blast_recipe_runs_caches_and_syncs_results(self):
        database = self.root / "nt"
        database.write_text(">ref1\nACGT\n", encoding="utf-8")
        self._write_fake_blast()
        self._write_tool_config(
            self.root / "fakeblast.py", "fakeblast", "fake_nt",
            "assembly", "genome_fasta", "blast_tabular", database,
            version_pattern=r"fakeblast:\s*([^\s]+)",
        )
        file_row = self._add_assembly()

        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "fake_nt"]), 0)
        jobs = self.db.query("SELECT * FROM analysis_jobs ORDER BY job_id")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "completed")
        self.assertEqual(jobs[0]["tool"], "fakeblast")
        self.assertEqual(jobs[0]["tool_version"], "9.8.7")
        self.assertTrue(jobs[0]["output_sha256"])

        hits = self.db.query("SELECT * FROM analysis_hits ORDER BY query_id, subject_id, metric_name")
        self.assertEqual(len(hits), 12)  # 3 query/subject pairs x 4 metrics
        summaries = {r["metric_name"]: r["metric_value"] for r in self.db.query("SELECT * FROM analysis_results")}
        self.assertEqual(summaries["query_count"], "2")
        self.assertEqual(summaries["hit_count"], "3")
        self.assertEqual(summaries["query_with_hit_count"], "2")

        qc = self.db.query(
            "SELECT * FROM qc_results WHERE qc_stage='analysis:fake_nt' AND metric_name='query_count'"
        )
        self.assertEqual(len(qc), 1)
        self.assertEqual(qc[0]["file_id"], file_row["file_id"])

        # Identical second run is a cache hit, not a new execution.
        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "fake_nt"]), 0)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"], 1)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs WHERE status='completed'")[0]["n"], 1)

        # --force supersedes the cached row and creates a new completed job.
        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "fake_nt", "--force"]), 0)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"], 2)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs WHERE status='completed'")[0]["n"], 1)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs WHERE status='superseded'")[0]["n"], 1)

    def test_analysis_refuses_modified_raw_input(self):
        database = self.root / "nt"
        database.write_text(">ref1\nACGT\n", encoding="utf-8")
        self._write_fake_blast()
        self._write_tool_config(
            self.root / "fakeblast.py", "fakeblast", "fake_nt",
            "assembly", "genome_fasta", "blast_tabular", database,
            version_pattern=r"fakeblast:\s*([^\s]+)",
        )
        file_row = self._add_assembly()
        (self.project.root / file_row["relative_path"]).write_text(">ctg1\nTTTT\n", encoding="utf-8")

        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "fake_nt"]), 1)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"], 0)

    def test_hmmsearch_recipe_parses_tblout(self):
        database = self.root / "Pfam-A.hmm"
        database.write_text("HMMER3/f fake hmm\n", encoding="utf-8")
        script = self._write_fake_hmmsearch()
        self._write_tool_config(
            script, "fakehmm", "fake_pfam", "assembly", "genome_fasta", "hmmer_tblout", database,
            version_args=["-h"], version_pattern=r"HMMER\s+([^\s]+)",
        )
        # Replace the recipe arguments: this fake expects --tblout / --cpu / db / input.
        doc = yaml.safe_load(self.project.tools_config_path.read_text(encoding="utf-8"))
        doc["tools"]["fakehmm"]["recipes"]["fake_pfam"]["arguments"] = [
            "--tblout", "${output}", "--cpu", "${threads}", "${database}", "${input}",
        ]
        self.project.tools_config_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        self._add_assembly()

        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "fake_pfam"]), 0)
        hits = self.db.query(
            "SELECT query_id, subject_id, metric_name, metric_value FROM analysis_hits "
            "ORDER BY query_id, subject_id, metric_name"
        )
        self.assertEqual([(r["query_id"], r["subject_id"], r["metric_name"]) for r in hits][:4], [
            ("query1", "PF00001", "evalue"),
            ("query1", "PF00001", "score"),
            ("query2", "PF00002", "evalue"),
            ("query2", "PF00002", "score"),
        ])

    def test_conda_run_method_is_supported(self):
        tool = ToolSpec(
            name="blastn", executable="blastn",
            run_method="conda run --no-capture-output -n blast",
            version_args=["-version"], version_pattern="x",
            description="", recipes={}, raw={},
        )
        config = {"conda": {"bin": "/opt/conda/bin/conda", "run_args": ["run", "--no-capture-output"]}}
        prefix = launcher_prefix(tool, config)
        self.assertEqual(prefix, ["/opt/conda/bin/conda", "run", "--no-capture-output", "-n", "blast"])

    def test_directory_input_and_output_are_hashed_cached_and_verified(self):
        script = self._write_fake_directory_tool()
        source = self.root / "input_tree"
        source.mkdir()
        (source / "a.txt").write_text("alpha\n", encoding="utf-8")
        (source / "b.txt").write_text("beta\n", encoding="utf-8")
        self.db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Testus",
            "taxonomy_source": "NCBI",
        })
        file_row = ingest_file(
            self.db, self.project, source, "organism", "ORG_000001", "other",
            fmt="directory",
        )
        config = {
            "version": 1,
            "tools": {
                "fakedir": {
                    "executable": str(script), "run_method": sys.executable,
                    "version_args": ["-version"], "version_pattern": r"fakedir:\s*([^\s]+)",
                    "recipes": {
                        "directory_roundtrip": {
                            "entity_type": "organism", "file_role": "other",
                            "format": "directory", "input_kind": "directory",
                            "output_kind": "directory", "output_subdir": "directory_roundtrip",
                            "output_suffix": ".results",
                            "arguments": ["--input", "${input}", "--output", "${output}"],
                            "result_parser": "none",
                        }
                    },
                }
            },
        }
        self.project.tools_config_path.write_text(yaml.safe_dump(config, sort_keys=False))

        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "directory_roundtrip"]), 0)
        job = self.db.query("SELECT * FROM analysis_jobs")[0]
        output = self.project.root / job["output_relative_path"]
        self.assertTrue(output.is_dir())
        self.assertEqual((output / "combined.txt").read_text(), "alpha\nbeta\n")
        self.assertTrue(job["output_sha256"])

        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "directory_roundtrip"]), 0)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"], 1)

        archived = self.project.root / file_row["relative_path"]
        (archived / "a.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "directory_roundtrip"]), 1)

    def test_busco_directory_output_and_json_metrics(self):
        script = self._write_fake_busco()
        file_row = self._add_annotation()
        config = {
            "version": 1,
            "tools": {
                "busco": {
                    "executable": str(script), "run_method": sys.executable,
                    "version_args": ["--version"], "version_pattern": r"BUSCO\s+([^\s]+)",
                    "recipes": {
                        "busco_autolineage": {
                            "entity_type": "annotation", "file_role": "protein_fasta",
                            "format": "fasta", "input_kind": "file",
                            "database": "resources/busco_downloads",
                            "database_version": "odb12.2", "database_mode": "mutable_cache",
                            "output_subdir": "busco", "output_kind": "directory",
                            "output_name": "${file_id}.busco",
                            "arguments": [
                                "-m", "protein", "-i", "${input}", "-o", "${output_name}",
                                "--out_path", "${output_parent}",
                                "--download_path", "${database}", "-c", "${threads}",
                                "--auto-lineage",
                            ],
                            "result_parser": "busco_json",
                            "result_glob": "short_summary*.json",
                        }
                    },
                }
            },
        }
        self.project.tools_config_path.write_text(yaml.safe_dump(config, sort_keys=False))

        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "busco_autolineage", "--threads", "8"]), 0)
        job = self.db.query("SELECT * FROM analysis_jobs")[0]
        output = self.project.root / job["output_relative_path"]
        self.assertTrue(output.is_dir())
        self.assertEqual(output.name, f"{file_row['file_id']}.busco")
        self.assertTrue((self.root / "resources" / "busco_downloads").is_dir())

        metrics = {
            row["metric_name"]: row
            for row in self.db.query("SELECT * FROM analysis_results WHERE job_id=?", (job["job_id"],))
        }
        self.assertEqual(metrics["busco_complete_percent"]["metric_numeric"], 98.5)
        self.assertEqual(metrics["busco_complete_percent"]["metric_unit"], "percent")
        self.assertEqual(metrics["busco_duplicated_count"]["metric_numeric"], 5465.0)
        self.assertEqual(metrics["busco_lineage_dataset"]["metric_value"], "brassicales_odb12.2")
        self.assertEqual(metrics["busco_domain"]["metric_value"], "eukaryota")
        self.assertEqual(metrics["busco_n_markers"]["metric_numeric"], 7083.0)
        qc = self.db.query(
            "SELECT * FROM qc_results WHERE file_id=? AND metric_name='busco_missing_percent'",
            (file_row["file_id"],),
        )
        self.assertEqual(len(qc), 1)
        self.assertEqual(qc[0]["metric_numeric"], 1.3)

        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "busco_autolineage", "--threads", "8"]), 0)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"], 1)

        (output / "stale.txt").write_text("stale", encoding="utf-8")
        self.assertEqual(main(["--project", str(self.root), "analyze", "--analysis", "busco_autolineage", "--threads", "8", "--force"]), 0)
        self.assertFalse((output / "stale.txt").exists())
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"], 2)

        # SEPP derives jplace names by replacing "fasta" in the full path.
        # Reject the old default <file_id>.protein_fasta.busco name up front.
        config["tools"]["busco"]["recipes"]["busco_autolineage"].pop("output_name")
        config["tools"]["busco"]["recipes"]["busco_autolineage"]["output_suffix"] = ".busco"
        self.project.tools_config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        self.assertEqual(main([
            "--project", str(self.root), "analyze", "--analysis", "busco_autolineage",
            "--threads", "8", "--dry-run",
        ]), 1)

