"""Sequence classification engine: rule evaluation, best-hit ranking, idempotency."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from tests.helpers import PytestAssertions

from operon.classify import (
    _best_sort_key,
    _compare,
    _condition_holds,
    _row_context,
    classify_sequences,
    validate_classification_profile,
)
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ValidationError


class TestConditions(PytestAssertions):
    def test_compare_numeric_operators(self):
        self.assertTrue(_compare(40, ">=", 40))
        self.assertTrue(_compare(30, "<=", 30))
        self.assertTrue(_compare(5, ">", 4))
        self.assertTrue(_compare(3, "<", 4))
        self.assertTrue(_compare("1e-5", "==", 0.00001))
        self.assertFalse(_compare(1, "!=", 1))

    def test_compare_string_equality_fallback(self):
        self.assertTrue(_compare("Specific", "==", "Specific"))
        self.assertFalse(_compare("Specific", "==", "Motif"))
        self.assertTrue(_compare("-", "!=", "NC"))
        self.assertFalse(_compare("NC", "!=", "NC"))

    def test_compare_ordering_requires_numbers(self):
        with self.assertRaises(ValidationError):
            _compare("abc", ">=", 1)
        with self.assertRaises(ValidationError):
            _compare(1, "=~", 1)

    def test_condition_semantics(self):
        context = {"span": 45, "hit_type": "Specific"}
        self.assertTrue(_condition_holds(
            context, {"field": "span", "operator": "between", "min": 40, "max": 50}))
        self.assertFalse(_condition_holds(
            context, {"field": "span", "operator": "between", "min": 46, "max": 50}))
        self.assertTrue(_condition_holds(
            context, {"field": "hit_type", "operator": "in", "values": ["Specific", "Motif"]}))
        self.assertTrue(_condition_holds(
            context, {"field": "hit_type", "operator": "not_in", "values": ["Motif"]}))
        self.assertTrue(_condition_holds(context, {"field": "hit_type", "operator": "exists"}))
        self.assertTrue(_condition_holds(
            {"short_name": "bhlh_1"},
            {"field": "short_name", "operator": "like", "value": "bhlh_%"}))
        # `_` is a single-character wildcard: "bhlh_%" also matches bhlh-myc_n.
        self.assertTrue(_condition_holds(
            {"short_name": "bhlh-myc_n"},
            {"field": "short_name", "operator": "like", "value": "bhlh_%"}))
        self.assertFalse(_condition_holds(
            {"short_name": "bhlh-myc_n"},
            {"field": "short_name", "operator": "like", "value": "bhlh"}))
        # `any` groups OR their members; `not` negates one condition.
        group = {"any": [
            {"field": "subject_id", "operator": "==", "value": "cl00081"},
            {"field": "short_name", "operator": "like", "value": "bhlh_%"},
        ]}
        self.assertTrue(_condition_holds({"subject_id": "cl00081"}, group))
        self.assertTrue(_condition_holds({"short_name": "bhlh_2"}, group))
        self.assertFalse(_condition_holds({"subject_id": "cd00001"}, group))
        negated = {"not": {"field": "short_name", "operator": "==", "value": "bhlh-myc_n"}}
        self.assertFalse(_condition_holds({"short_name": "bhlh-myc_n"}, negated))
        self.assertTrue(_condition_holds({"subject_id": "cl00081"}, negated))
        # A missing field never satisfies any condition, including exists/not_in/!=.
        for condition in (
                {"field": "incomplete", "operator": "exists"},
                {"field": "incomplete", "operator": "not_in", "values": ["NC"]},
                {"field": "incomplete", "operator": "!=", "value": "NC"},
                {"field": "incomplete", "operator": "==", "value": "NC"},
        ):
            self.assertFalse(_condition_holds(context, condition))

    def test_row_context_span_seqid_and_extras(self):
        row = {
            "alignment_id": 7, "job_id": 3, "query_id": "gene1 description here",
            "subject_id": "cl00081", "hit_rank": 1, "query_start": 10, "query_end": 54,
            "evalue": 1e-12, "bitscore": 88.5, "extra_json": json.dumps(
                {"hit_type": "Specific", "incomplete": "-"}),
        }
        context = _row_context(row)
        self.assertEqual(context["seqid"], "gene1")
        self.assertEqual(context["span"], 45)
        self.assertEqual(context["hit_type"], "Specific")
        self.assertEqual(context["incomplete"], "-")
        self.assertEqual(context["subject_id"], "cl00081")

    def test_row_context_missing_span_and_bad_extra_json(self):
        context = _row_context({
            "query_id": "g2", "query_start": None, "query_end": 9, "extra_json": "{broken",
        })
        self.assertIsNone(context["span"])
        self.assertEqual(context["seqid"], "g2")
        context = _row_context({"query_id": "g3", "query_start": 1, "query_end": 9,
                                "extra_json": '["not-a-mapping"]'})
        self.assertEqual(context["span"], 9)

    def test_best_sort_key_rank_maps_and_directions(self):
        best_by = [
            {"field": "hit_type", "direction": "asc",
             "rank": {"Specific": 0.0, "Motif": 1.0}, "default": 9.0},
            {"field": "evalue", "direction": "asc", "rank": None, "default": None},
            {"field": "bitscore", "direction": "desc", "rank": None, "default": None},
        ]
        key = _best_sort_key(best_by)
        specific = {"hit_type": "Specific", "evalue": 1e-3, "bitscore": 50, "alignment_id": 1}
        motif = {"hit_type": "Motif", "evalue": 1e-30, "bitscore": 900, "alignment_id": 2}
        unknown = {"hit_type": "Other", "evalue": 1e-50, "bitscore": 5, "alignment_id": 3}
        missing = {"evalue": 1e-99, "bitscore": 1, "alignment_id": 4}
        ordered = sorted([missing, unknown, motif, specific], key=key)
        self.assertEqual(
            [row["alignment_id"] for row in ordered], [1, 2, 3, 4])
        # Within the same rank: evalue asc, then bitscore desc, then alignment_id.
        low_evalue = {"hit_type": "Specific", "evalue": 1e-9, "bitscore": 10, "alignment_id": 5}
        high_score = {"hit_type": "Specific", "evalue": 1e-3, "bitscore": 99, "alignment_id": 6}
        ordered = sorted([specific, high_score, low_evalue], key=key)
        self.assertEqual([row["alignment_id"] for row in ordered], [5, 6, 1])
        # Missing values and non-numeric values sort last.
        non_numeric = {"hit_type": "Specific", "evalue": "n/a", "bitscore": 1, "alignment_id": 7}
        ordered = sorted([non_numeric, specific], key=key)
        self.assertEqual([row["alignment_id"] for row in ordered], [1, 7])

    def test_best_sort_key_unmapped_rank_without_default(self):
        key = _best_sort_key([
            {"field": "incomplete", "direction": "asc",
             "rank": {"-": 0.0, "NC": 1.0}, "default": None},
        ])
        known = {"incomplete": "-", "alignment_id": 1}
        unmapped = {"incomplete": "P", "alignment_id": 2}
        self.assertTrue(key(known) < key(unmapped))


class TestProfileValidation(PytestAssertions):
    def _profile(self, **overrides):
        profile = {
            "kind": "sequence_classification",
            "version": 1,
            "applies_to": {"entity_type": "annotation", "file_role": "protein_fasta"},
            "sources": {
                "core": {
                    "analysis": "cdd_scan",
                    "filter": [{"field": "hit_type", "operator": "exists"}],
                    "best_by": [{"field": "evalue", "direction": "asc"}],
                },
            },
            "rules": [
                {"label": "HIT", "source": "core",
                 "when": [{"field": "span", "operator": ">=", "value": 40}]},
                {"label": "NONE", "source": "core", "absent": True},
                {"label": "OTHER", "default": True},
            ],
        }
        profile.update(overrides)
        return profile

    def test_valid_profile_normalizes(self):
        spec = validate_classification_profile(self._profile(), "tier")
        self.assertEqual(spec["entity_type"], "annotation")
        self.assertEqual(spec["file_role"], "protein_fasta")
        self.assertEqual(spec["sources"]["core"]["best_by"],
                         [{"field": "evalue", "direction": "asc", "rank": None, "default": None}])
        self.assertEqual([rule["label"] for rule in spec["rules"]], ["HIT", "NONE", "OTHER"])

    def test_source_without_best_by_uses_sortable_default(self):
        profile = self._profile()
        del profile["sources"]["core"]["best_by"]
        spec = validate_classification_profile(profile, "tier")
        default = [{"field": "hit_rank", "direction": "asc", "rank": None, "default": None}]
        self.assertEqual(spec["sources"]["core"]["best_by"], default)
        key = _best_sort_key(spec["sources"]["core"]["best_by"])
        low = {"hit_rank": 1, "alignment_id": 1}
        high = {"hit_rank": 2, "alignment_id": 2}
        missing = {"alignment_id": 3}
        ordered = sorted([missing, high, low], key=key)
        self.assertEqual([row["alignment_id"] for row in ordered], [1, 2, 3])

    def test_validation_errors(self):
        bad_profiles = [
            self._profile(applies_to=["annotation"]),
            self._profile(applies_to={"file_role": "protein_fasta"}),
            self._profile(applies_to={"entity_type": "annotation"}),
            self._profile(sources={}),
            self._profile(sources={"core": "cdd_scan"}),
            self._profile(sources={"core": {"filter": []}}),
            self._profile(sources={"core": {"analysis": "x", "filter": "not-a-list"}}),
            self._profile(sources={"core": {"analysis": "x", "filter": [{"operator": "=="}]}}),
            self._profile(sources={"core": {"analysis": "x", "filter": [
                {"field": "evalue"}]}}),
            self._profile(sources={"core": {"analysis": "x", "filter": [
                {"field": "hit_type", "operator": "in"}]}}),
            self._profile(sources={"core": {"analysis": "x", "filter": [
                {"field": "span", "operator": "between", "min": 1}]}}),
            self._profile(sources={"core": {"analysis": "x", "filter": [
                {"field": "span", "operator": ">=", "min": 1}]}}),
            self._profile(sources={"core": {"analysis": "x", "filter": [
                {"any": []}]}}),
            self._profile(sources={"core": {"analysis": "x", "filter": [
                {"any": [{"operator": "==", "value": 1}]}]}}),
            self._profile(sources={"core": {"analysis": "x", "filter": [{"not": "x"}]}}),
            self._profile(sources={"core": {"analysis": "x", "filter": ["not-a-dict"]}}),
            self._profile(sources={"core": {"analysis": "x", "filter": [
                {"field": "short_name", "operator": "like"}]}}),
            self._profile(sources={"core": {"analysis": "x", "best_by": []}}),
            self._profile(sources={"core": {"analysis": "x", "best_by": [{"direction": "asc"}]}}),
            self._profile(sources={"core": {"analysis": "x", "best_by": [
                {"field": "evalue", "direction": "sideways"}]}}),
            self._profile(sources={"core": {"analysis": "x", "best_by": [
                {"field": "hit_type", "rank": ["Specific"]}]}}),
            self._profile(rules=[]),
            self._profile(rules=[{"source": "core", "absent": True}]),
            self._profile(rules=[{"label": "X", "default": True, "source": "core"}]),
            self._profile(rules=[{"label": "X", "source": "missing", "absent": True}]),
            self._profile(rules=[{"label": "X", "source": "core"}]),
            self._profile(rules=[{"label": "X", "source": "core", "absent": True,
                                  "when": [{"field": "span", "operator": ">=", "value": 1}]}]),
            self._profile(rules=[{"label": "X", "source": "core", "when": "not-a-list"}]),
            self._profile(rules=[{"label": "X", "source": "core", "when": [
                {"field": "span", "operator": ">=", "value": 1, "extra": 1},
                {"field": "span"}]}]),
        ]
        for profile in bad_profiles:
            with self.assertRaises(ValidationError):
                validate_classification_profile(profile, "tier")


class TestClassifySequences(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root),
                               "--project-id", "PRJ_CLS_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Testus",
            "taxonomy_source": "NCBI"})
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        self.db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "contig", "assembly_version": 1})
        self.db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_source": "test", "annotation_version": 1})
        self.db.insert_row("files", {
            "file_id": "FILE_PROTEINS", "entity_type": "annotation",
            "entity_id": "ANN_000001", "file_role": "protein_fasta", "format": "fasta",
            "compression": "none", "relative_path": "data/proteins.faa",
            "size_bytes": 10, "sha256": "abc123", "status": "ACTIVE"})
        for index in range(1, 7):
            self.db.insert_row("sequences", {
                "file_id": "FILE_PROTEINS", "file_sha256": "abc123",
                "entity_type": "annotation", "entity_id": "ANN_000001",
                "seqid": f"g{index}", "length": 100})

    def _write_profile(self, profile, name="tier"):
        (self.project.profiles_dir / f"{name}.yaml").write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    def _profile(self, rules):
        return {
            "kind": "sequence_classification",
            "version": 1,
            "description": "generic test classification",
            "applies_to": {"entity_type": "annotation", "file_role": "protein_fasta"},
            "sources": {
                "core": {
                    "analysis": "cdd_scan",
                    "filter": [{"field": "hit_type", "operator": "in",
                                "values": ["Specific", "Motif"]}],
                    "best_by": [
                        {"field": "hit_type", "rank": {"Specific": 0, "Motif": 1}},
                        {"field": "evalue", "direction": "asc"},
                        {"field": "bitscore", "direction": "desc"},
                        {"field": "span", "direction": "desc"},
                    ],
                },
                "rescue": {
                    "analysis": "pfam_hmm",
                    "filter": [{"field": "subject_id", "operator": "==", "value": "PF00010"}],
                    "best_by": [{"field": "evalue", "direction": "asc"}],
                },
            },
            "rules": rules,
        }

    def _rules(self, with_default=True):
        rules = [
            {"label": "A", "source": "core", "when": [
                {"field": "hit_type", "operator": "==", "value": "Specific"},
                {"field": "incomplete", "operator": "==", "value": "-"},
                {"field": "span", "operator": ">=", "value": 40},
            ]},
            {"label": "B", "source": "core", "when": [
                {"field": "span", "operator": ">=", "value": 30},
                {"field": "incomplete", "operator": "!=", "value": "NC"},
            ]},
            {"label": "R", "source": "rescue", "when": [
                {"field": "i_evalue", "operator": "<=", "value": 1e-5},
                {"field": "span", "operator": ">=", "value": 30},
            ]},
            {"label": "U", "source": "core", "absent": True},
        ]
        if with_default:
            rules.append({"label": "C", "default": True})
        return rules

    def _add_job(self, analysis, alignments, job_id=None):
        sequence_number = self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"]
        self.db.insert_row("analysis_jobs", {
            "analysis_name": analysis, "entity_type": "annotation",
            "entity_id": "ANN_000001", "file_id": "FILE_PROTEINS",
            "tool": "fake", "tool_version": "1.0", "parameter_set": "{}",
            "parameter_sha256": f"params-{sequence_number}", "input_sha256": "abc123",
            "database_identity": "db1", "status": "completed",
            "started_at": "2026-09-11T00:00:00+00:00"})
        if job_id is None:
            job_id = self.db.query("SELECT MAX(job_id) AS j FROM analysis_jobs")[0]["j"]
        for row in alignments:
            self.db.insert_row("analysis_alignments", {
                "job_id": job_id, "entity_type": "annotation",
                "entity_id": "ANN_000001", "file_id": "FILE_PROTEINS",
                "analysis_name": analysis, "hit_rank": 1, **row})
        return job_id

    def _labels(self):
        return {
            row["seqid"]: row["label"]
            for row in self.db.query(
                "SELECT seqid, label FROM sequence_labels WHERE profile_name='tier'")
        }

    def _changes(self):
        return self.db.query(
            "SELECT * FROM changes WHERE object_type='sequence_label' ORDER BY change_id")

    def _seed_hits(self):
        self._add_job("cdd_scan", [
            # g1: Specific, complete, span 45 -> A
            {"query_id": "g1 some description", "subject_id": "cl00081",
             "query_start": 10, "query_end": 54, "evalue": 1e-12, "bitscore": 90.0,
             "extra_json": json.dumps({"hit_type": "Specific", "incomplete": "-"})},
            # g2: best hit is Specific but span 35 -> B (incomplete P != NC)
            {"query_id": "g2", "subject_id": "cl00081",
             "query_start": 1, "query_end": 35, "evalue": 1e-8, "bitscore": 60.0,
             "extra_json": json.dumps({"hit_type": "Specific", "incomplete": "P"})},
            # g3: best hit Motif span 50 incomplete NC -> falls through A and B -> C
            {"query_id": "g3", "subject_id": "cd00001",
             "query_start": 5, "query_end": 54, "evalue": 1e-6, "bitscore": 40.0,
             "extra_json": json.dumps({"hit_type": "Motif", "incomplete": "NC"})},
            # g4: Specific span 45 but incomplete NC -> falls through A and B -> C
            {"query_id": "g4", "subject_id": "cl00081",
             "query_start": 1, "query_end": 45, "evalue": 1e-9, "bitscore": 70.0,
             "extra_json": json.dumps({"hit_type": "Specific", "incomplete": "NC"})},
            # filtered out (hit_type not in values): must not rescue g5 from absent
            {"query_id": "g5", "subject_id": "cl99999",
             "query_start": 1, "query_end": 60, "evalue": 1e-20, "bitscore": 99.0,
             "extra_json": json.dumps({"hit_type": "Partial", "incomplete": "-"})},
        ])
        self._add_job("pfam_hmm", [
            # g6: no core hit, rescue PF00010 i_evalue 1e-8 span 40 -> R
            {"query_id": "g6", "subject_id": "PF00010",
             "query_start": 3, "query_end": 42, "evalue": 1e-8, "bitscore": 55.0,
             "extra_json": json.dumps({"i_evalue": 1e-8})},
        ])

    def test_all_rule_branches(self):
        self._write_profile(self._profile(self._rules()))
        self._seed_hits()
        result = classify_sequences(
            self.db, self.project, profile_name="tier",
            command="operon classify-sequences --profile tier")
        self.assertEqual(self._labels(), {
            "g1": "A", "g2": "B", "g3": "C", "g4": "C", "g5": "U", "g6": "R",
        })
        self.assertEqual(result["label_counts"], {"A": 1, "B": 1, "C": 2, "R": 1, "U": 1})
        self.assertEqual(result["unlabeled"], 0)
        self.assertEqual(result["sequences"], 6)
        details = {
            row["seqid"]: json.loads(row["details_json"])
            for row in self.db.query("SELECT seqid, details_json FROM sequence_labels")
        }
        self.assertEqual(details["g1"]["subject_id"], "cl00081")
        self.assertEqual(details["g1"]["observed"]["span"], 45)
        self.assertTrue(details["g5"]["absent"])
        self.assertTrue(details["g3"]["default"])
        self.assertIsNotNone(details["g1"]["job_id"])
        changes = self._changes()
        self.assertEqual(len(changes), 6)
        self.assertTrue(all(row["workflow_run_id"] == result["run_id"] for row in changes))
        snapshots = self.db.query(
            "SELECT * FROM qc_profiles WHERE profile_name='tier'")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["profile_sha256"], result["profile_sha256"])
        runs = self.db.query(
            "SELECT * FROM workflow_runs WHERE step='classify-sequences'")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")
        counts = json.loads(runs[0]["execution_details"])["label_counts"]
        self.assertEqual(counts, {"A": 1, "B": 1, "C": 2, "R": 1, "U": 1})

    def test_unlabeled_sequences_without_default(self):
        self._write_profile(self._profile(self._rules(with_default=False)))
        self._seed_hits()
        result = classify_sequences(
            self.db, self.project, profile_name="tier",
            command="operon classify-sequences --profile tier")
        self.assertEqual(self._labels(), {
            "g1": "A", "g2": "B", "g5": "U", "g6": "R",
        })
        self.assertEqual(result["unlabeled"], 2)  # g3, g4 match no rule

    def test_idempotent_rerun_writes_nothing(self):
        self._write_profile(self._profile(self._rules()))
        self._seed_hits()
        classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        first_labels = self._labels()
        first_decided = {
            row["seqid"]: row["decided_at"]
            for row in self.db.query("SELECT seqid, decided_at FROM sequence_labels")
        }
        result = classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        self.assertEqual(result["labels_written"], 0)
        self.assertEqual(result["labels_removed"], 0)
        self.assertEqual(self._labels(), first_labels)
        self.assertEqual(len(self._changes()), 6)
        self.assertEqual(
            {row["seqid"]: row["decided_at"]
             for row in self.db.query("SELECT seqid, decided_at FROM sequence_labels")},
            first_decided)

    def test_profile_change_updates_labels_with_audit(self):
        self._write_profile(self._profile(self._rules()))
        self._seed_hits()
        classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        # Raising the A span threshold re-demotes g1 to B; everything else holds.
        profile = self._profile(self._rules())
        profile["rules"][0]["when"][2]["value"] = 50
        profile["version"] = 2
        self._write_profile(profile)
        result = classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        self.assertEqual(self._labels()["g1"], "B")
        self.assertEqual(result["labels_written"], 1)
        changes = self._changes()
        self.assertEqual(len(changes), 7)
        latest = changes[-1]
        self.assertEqual(latest["old_value"], "A")
        self.assertEqual(latest["new_value"], "B")
        snapshots = self.db.query(
            "SELECT * FROM qc_profiles WHERE profile_name='tier' ORDER BY profile_snapshot_id")
        self.assertEqual(len(snapshots), 2)

    def test_label_removed_when_sequence_falls_out_of_rules(self):
        self._write_profile(self._profile(self._rules()))
        self._seed_hits()
        classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        # Keeping only the A/B rules leaves g3, g4, g5 and g6 unlabeled; their
        # labels are removed with an audit row each.
        profile = self._profile(self._rules()[:2])
        profile["version"] = 2
        self._write_profile(profile)
        result = classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        self.assertEqual(self._labels(), {"g1": "A", "g2": "B"})
        self.assertEqual(result["labels_removed"], 4)
        g6_changes = self.db.query(
            "SELECT * FROM changes WHERE object_type='sequence_label' "
            "AND object_id LIKE '%:g6:%'")
        self.assertEqual(len(g6_changes), 2)
        self.assertIsNone(g6_changes[-1]["new_value"])
        self.assertEqual(g6_changes[-1]["old_value"], "R")

    def test_only_latest_completed_job_counts(self):
        self._write_profile(self._profile(self._rules()))
        first = self._add_job("cdd_scan", [
            {"query_id": "g1", "subject_id": "cd00001",
             "query_start": 1, "query_end": 50, "evalue": 1e-9, "bitscore": 10.0,
             "extra_json": json.dumps({"hit_type": "Motif", "incomplete": "-"})},
        ])
        # A failed newer job must not shadow the completed one.
        self.db.insert_row("analysis_jobs", {
            "analysis_name": "cdd_scan", "entity_type": "annotation",
            "entity_id": "ANN_000001", "file_id": "FILE_PROTEINS",
            "tool": "fake", "tool_version": "1.0", "parameter_set": "{}",
            "parameter_sha256": "p2", "input_sha256": "abc123",
            "database_identity": "db1", "status": "failed",
            "started_at": "2026-09-11T01:00:00+00:00"})
        self._add_job("cdd_scan", [
            {"query_id": "g1", "subject_id": "cl00081",
             "query_start": 1, "query_end": 45, "evalue": 1e-9, "bitscore": 80.0,
             "extra_json": json.dumps({"hit_type": "Specific", "incomplete": "-"})},
        ])
        classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        labels = self._labels()
        self.assertEqual(labels["g1"], "A")
        details = json.loads(self.db.query(
            "SELECT details_json FROM sequence_labels WHERE seqid='g1'")[0]["details_json"])
        self.assertNotEqual(details["job_id"], first)

    def test_best_by_rank_map_selects_specific_over_motif(self):
        self._write_profile(self._profile(self._rules()))
        self._add_job("cdd_scan", [
            {"query_id": "g1", "subject_id": "cd00001", "hit_rank": 1,
             "query_start": 1, "query_end": 60, "evalue": 1e-40, "bitscore": 200.0,
             "extra_json": json.dumps({"hit_type": "Motif", "incomplete": "-"})},
            {"query_id": "g1", "subject_id": "cl00081", "hit_rank": 2,
             "query_start": 5, "query_end": 49, "evalue": 1e-4, "bitscore": 30.0,
             "extra_json": json.dumps({"hit_type": "Specific", "incomplete": "-"})},
        ])
        classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        labels = self._labels()
        self.assertEqual(labels["g1"], "A")
        details = json.loads(self.db.query(
            "SELECT details_json FROM sequence_labels WHERE seqid='g1'")[0]["details_json"])
        self.assertEqual(details["subject_id"], "cl00081")

    def test_superseded_entity_files_are_skipped(self):
        self._write_profile(self._profile(self._rules()))
        self._seed_hits()
        self.db.insert_row("entity_supersessions", {
            "object_type": "annotation", "object_id": "ANN_000001",
            "superseded_by_type": "annotation", "superseded_by_id": "ANN_000002",
            "reason": "test", "superseded_at": "2026-09-11T00:00:00+00:00"})
        result = classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["sequences"], 0)
        self.assertEqual(self._labels(), {})

    def test_file_without_sequence_rows_is_counted_not_labeled(self):
        self._write_profile(self._profile(self._rules()))
        self.db.insert_row("annotations", {
            "annotation_id": "ANN_000002", "assembly_id": "ASM_000001",
            "annotation_source": "test", "annotation_version": 1})
        self.db.insert_row("files", {
            "file_id": "FILE_EMPTY", "entity_type": "annotation",
            "entity_id": "ANN_000002", "file_role": "protein_fasta", "format": "fasta",
            "compression": "none", "relative_path": "data/empty.faa",
            "size_bytes": 1, "sha256": "def456", "status": "ACTIVE"})
        result = classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["sequences"], 6)
        details = json.loads(self.db.query(
            "SELECT execution_details FROM workflow_runs WHERE step='classify-sequences'"
        )[0]["execution_details"])
        self.assertEqual(details["files_without_sequences"], 1)

    def test_wrong_profile_kind_is_rejected(self):
        self._write_profile({"kind": "qc", "version": 1, "applies_to": ["annotation"]})
        with self.assertRaises(ValidationError):
            classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        runs = self.db.query("SELECT * FROM workflow_runs WHERE step='classify-sequences'")
        self.assertEqual(runs, [])

    def test_failed_run_is_recorded(self):
        # Validation passes, but a numeric operator on string data fails mid-run.
        profile = self._profile([
            {"label": "A", "source": "core", "when": [
                {"field": "hit_type", "operator": ">=", "value": 5}]},
        ])
        self._write_profile(profile)
        self._seed_hits()
        with self.assertRaises(ValidationError):
            classify_sequences(self.db, self.project, profile_name="tier", command="cmd")
        runs = self.db.query("SELECT * FROM workflow_runs WHERE step='classify-sequences'")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[0]["exit_code"], 1)
        self.assertEqual(self._labels(), {})
