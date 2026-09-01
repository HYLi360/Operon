"""Configuration, identity, and result-parser edge cases for analysis tools."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from operon import tools
from operon.errors import ExternalToolError, ValidationError


def recipe(**overrides):
    base = tools.Recipe(
        name="analysis", tool_name="tool", description="", entity_type="assembly",
        file_role="genome_fasta", fmt="fasta", input_kind="file", database="",
        database_version="", output_subdir="analysis", output_kind="file",
        output_name_template="", output_suffix=".tsv", arguments=[], parameters={},
        result_parser="none", max_hits_per_query=2, raw={},
    )
    return replace(base, **overrides)


def project(tmp_path: Path):
    return SimpleNamespace(
        root=tmp_path,
        tools_config_path=tmp_path / "tools.yaml",
        logs_root=tmp_path / "logs",
        analysis_root=tmp_path / "analysis",
    )


def test_tool_config_loading_and_launcher_modes(tmp_path, monkeypatch):
    p = project(tmp_path)
    p.tools_config_path.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(tools, "ensure_tools_config", lambda _p: p.tools_config_path)
    with pytest.raises(ValidationError, match="invalid tools config"):
        tools.load_tools_config(p)

    configs = [
        ({"tools": {}}, "missing", "unknown tool"),
        ({"tools": {"t": []}}, "t", "must be a mapping"),
        ({"tools": {"t": {"run_method": {"mode": "conda"}}}}, "t", "requires 'env'"),
        ({"tools": {"t": {"run_method": {"mode": "other"}}}}, "t", "unsupported launcher"),
        ({"tools": {"t": {"run_method": 3}}}, "t", "must be a string or mapping"),
    ]
    for config, name, message in configs:
        monkeypatch.setattr(tools, "load_tools_config", lambda _p, config=config: config)
        with pytest.raises(ValidationError, match=message):
            tools.get_tool(p, name)

    config = {"conda": {"bin": "/conda", "run_args": ["run"]}, "tools": {"t": {
        "executable": "exe", "run_method": {"mode": "conda", "env": "env"},
        "version_args": ["--version"],
    }}}
    monkeypatch.setattr(tools, "load_tools_config", lambda _p: config)
    spec = tools.get_tool(p, "t")
    assert spec.run_method == "/conda run -n env"
    config["tools"]["t"]["run_method"] = {"mode": "prefix", "prefix": ["env", "run"]}
    assert tools.get_tool(p, "t").run_method == "env run"
    config["tools"]["t"]["run_method"] = {"mode": "path"}
    assert tools.get_tool(p, "t").run_method == ""
    config["tools"]["t"]["run_method"] = "conda run -n e"
    spec = tools.get_tool(p, "t")
    assert tools.launcher_prefix(spec, {"conda": {"bin": "/custom/conda"}})[0] == "/custom/conda"
    assert tools.tool_command(spec, {})[-1] == "exe"
    assert tools.launcher_prefix(replace(spec, run_method=""), {}) == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "must be a mapping"),
        ({"input_kind": "stream"}, "input_kind must be"),
        ({"output_kind": "stream"}, "output_kind must be"),
        ({"format": "directory", "input_kind": "file"}, "requires input_kind=directory"),
        ({"parameters": [1]}, "parameters must be a mapping"),
        ({"parameters": {"bad-name": {}}}, "invalid parameter name"),
        ({"parameters": {"good": []}}, "parameter 'good' must be a mapping"),
    ],
)
def test_recipe_config_validation(tmp_path, monkeypatch, raw, message):
    p = project(tmp_path)
    config = {"tools": {"t": {"recipes": {"a": raw}}}}
    monkeypatch.setattr(tools, "load_tools_config", lambda _p: config)
    with pytest.raises(ValidationError, match=message):
        tools.get_recipe(p, "a")


def test_recipe_defaults_listing_and_unknown_message(tmp_path, monkeypatch):
    p = project(tmp_path)
    config = {"tools": {
        "ignored": [],
        "t": {"recipes": {
            "directory": {"format": "directory", "output_kind": "directory", "parameters": {"x": None}},
            "file": {"format": "fasta", "arguments": [1]},
        }},
    }}
    monkeypatch.setattr(tools, "load_tools_config", lambda _p: config)
    directory = tools.get_recipe(p, "directory")
    assert directory.input_kind == "directory"
    assert directory.output_suffix == ""
    assert directory.parameters == {"x": {}}
    file_recipe = tools.get_recipe(p, "file")
    assert file_recipe.input_kind == "file" and file_recipe.output_suffix == ".tsv"
    assert [item.name for item in tools.list_analyses(p)] == ["directory", "file"]
    with pytest.raises(ValidationError, match="t.directory"):
        tools.get_recipe(p, "missing")


def test_runtime_parameter_validation_and_argument_rendering(tmp_path):
    r = recipe(parameters={
        "required": {"required": True, "pattern": r"\d+"},
        "choice": {"default": "a", "choices": ["a", "b"]},
        "optional": {},
    })
    with pytest.raises(ValidationError, match="undeclared"):
        tools.resolve_runtime_parameters(r, {"extra": "1"})
    with pytest.raises(ValidationError, match="missing required"):
        tools.resolve_runtime_parameters(r)
    with pytest.raises(ValidationError, match="scalar value"):
        tools.resolve_runtime_parameters(r, {"required": []})
    with pytest.raises(ValidationError, match="must be one of"):
        tools.resolve_runtime_parameters(r, {"required": "1", "choice": "c"})
    bad_choices = replace(r, parameters={"x": {"default": "a", "choices": "a"}})
    with pytest.raises(ValidationError, match="choices must be a list"):
        tools.resolve_runtime_parameters(bad_choices)
    with pytest.raises(ValidationError, match="does not match pattern"):
        tools.resolve_runtime_parameters(r, {"required": "x"})
    assert tools.resolve_runtime_parameters(r, {"required": "2"}) == {
        "required": "2", "choice": "a"
    }

    rendered_recipe = replace(r, arguments=[
        "${input}", "${input_parent}", "${input_name}", "${input_stem}",
        "${output}", "${output_parent}", "${output_name}", "${output_stem}",
        "${database}", "${threads}", "${file_id}", "${file_role}",
        "${entity_type}", "${entity_id}", "${required}",
    ])
    file_record = {"file_id": "F1", "file_role": "genome_fasta", "entity_type": "assembly", "entity_id": "A1"}
    args = tools.render_arguments(
        rendered_recipe, input_path=tmp_path / "in.fna", output_path=tmp_path / "out.tsv",
        database_path=None, threads=4, file_record=file_record, runtime_parameters={"required": "2"},
    )
    assert args[-1] == "2" and "4" in args
    with pytest.raises(ValidationError, match="unresolved placeholder"):
        tools.render_arguments(
            replace(r, arguments=["${missing}"]), input_path=tmp_path / "i",
            output_path=tmp_path / "o", database_path=None, threads=1,
            file_record=file_record,
        )
    fingerprint = tools.parameter_fingerprint(r, ["a"], 1, "v")
    assert fingerprint != tools.parameter_fingerprint(r, ["b"], 1, "v")
    assert fingerprint != tools.parameter_fingerprint(r, ["a"], 2, "v")
    assert fingerprint != tools.parameter_fingerprint(r, ["a"], 1, "w")


def test_database_identity_modes_and_directory_fingerprint(tmp_path, monkeypatch):
    p = project(tmp_path)
    tools._DATABASE_IDENTITY_CACHE.clear()
    directory = tmp_path / "db"
    directory.mkdir()
    (directory / "a").write_text("x", encoding="utf-8")
    before = tools._directory_fingerprint(directory)
    (directory / "b").write_text("y", encoding="utf-8")
    assert tools._directory_fingerprint(directory) != before
    identities = {
        tools.database_identity(p, recipe(database="")),
        tools.database_identity(p, recipe(database="missing")),
        tools.database_identity(p, recipe(database="db")),
    }
    assert len(identities) == 3
    file_path = tmp_path / "database.fa"
    file_path.write_text("ACGT", encoding="utf-8")
    assert tools.database_identity(p, recipe(database=str(file_path)))
    assert tools.database_identity(p, recipe(database="db", raw={"database_checksum": "ABC"}))
    assert tools.database_identity(p, recipe(database="db"), location_identity="remote")
    with pytest.raises(ValidationError, match="database_mode must be"):
        tools.database_identity(p, recipe(raw={"database_mode": "bad"}))
    with pytest.raises(ValidationError, match="requires an explicit database_version"):
        tools.database_identity(p, recipe(raw={"database_mode": "mutable_cache"}))
    assert tools.database_identity(
        p, recipe(database_version="v1", raw={"database_mode": "mutable_cache"})
    )


def test_output_name_must_render_to_a_single_safe_component(tmp_path):
    file_record = {"file_id": "F1", "file_role": "genome_fasta",
                   "entity_type": "assembly", "entity_id": "A1"}
    for template in ("sub/${file_id}.tsv", "../${file_id}.tsv", ".."):
        with pytest.raises(ValidationError, match="one safe path component"):
            tools._render_output_name(
                recipe(output_name_template=template), file_record, tmp_path / "in.fna",
            )


def test_remove_output_artifact_refuses_paths_outside_analysis_root(tmp_path):
    p = project(tmp_path)
    p.analysis_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ExternalToolError, match="outside analysis root"):
        tools._remove_output_artifact(p, outside)
    assert outside.read_text(encoding="utf-8") == "x"
    with pytest.raises(ExternalToolError, match="outside analysis root"):
        tools._remove_output_artifact(p, p.analysis_root)
    assert p.analysis_root.is_dir()


def tool_spec(**overrides):
    base = tools.ToolSpec(
        name="tool", executable="tool", run_method="", version_args=["--version"],
        version_pattern="", description="", recipes={}, raw={},
    )
    return replace(base, **overrides)


def test_tool_version_detection_success_cache_and_failures(monkeypatch):
    tools._VERSION_CACHE.clear()
    assert tools.detect_tool_version_record(tool_spec(version_args=[]), {}) == (
        "unknown", "version_args not configured"
    )
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="tool 1.2.3\n", stderr=""))
    assert tools.detect_tool_version(tool_spec(), {}) == "1.2.3"
    tools._VERSION_CACHE.clear()
    assert tools.detect_tool_version(tool_spec(version_pattern=r"tool\s+(\S+)"), {}) == "1.2.3"
    tools._VERSION_CACHE.clear()
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="development build", stderr=""))
    assert tools.detect_tool_version(tool_spec(), {}) == "development build"
    tools._VERSION_CACHE.clear()
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="", stderr=""))
    with pytest.raises(ExternalToolError, match="could not determine version"):
        tools.detect_tool_version(tool_spec(), {})
    tools._VERSION_CACHE.clear()
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("missing")))
    with pytest.raises(ExternalToolError, match="cannot launch"):
        tools.detect_tool_version(tool_spec(), {})
    tools._VERSION_CACHE.clear()
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 1)))
    with pytest.raises(ExternalToolError, match="timed out"):
        tools.detect_tool_version(tool_spec(), {}, timeout=1)


def test_remote_version_capture(tmp_path):
    class Executor:
        project = SimpleNamespace(logs_root=tmp_path / "logs")

        def describe(self):
            return "remote"

        def run(self, _command, **kwargs):
            kwargs["stdout_path"].write_text("tool 2.0", encoding="utf-8")
            kwargs["stderr_path"].write_text("warning", encoding="utf-8")
            return SimpleNamespace(exit_code=0, error=None)

    assert "tool 2.0" in tools._version_output_via_executor(Executor(), ["tool"], 1)

    class Failed(Executor):
        def run(self, *_a, **_k):
            return SimpleNamespace(exit_code=1, error="bad")

    with pytest.raises(ExternalToolError, match="failed"):
        tools._version_output_via_executor(Failed(), ["tool"], 1)


@pytest.mark.parametrize(
    ("value", "rendered", "numeric"),
    [
        (1, "1", 1.0),
        (True, "True", None),
        (None, "", None),
        ({"b": 1}, '{"b": 1}', None),
        (float("inf"), "inf", None),
        ("x", "x", None),
    ],
)
def test_result_metric_rendering(value, rendered, numeric):
    result = tools._result_metric("m", value, "unit")
    assert result["metric_value"] == rendered
    assert result["metric_numeric"] == numeric


def test_busco_selection_and_parsing(tmp_path):
    r = recipe(result_parser="busco_json")
    with pytest.raises(ValidationError, match="must stay within"):
        tools._select_busco_json(tmp_path, replace(r, raw={"result_glob": "../x"}))
    with pytest.raises(ExternalToolError, match="no BUSCO JSON"):
        tools._select_busco_json(tmp_path, r)
    (tmp_path / "short_summary.a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "short_summary.b.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ExternalToolError, match="multiple ambiguous"):
        tools._select_busco_json(tmp_path, r)
    specific = tmp_path / "short_summary.specific.x.json"
    specific.write_text("{}", encoding="utf-8")
    assert tools._select_busco_json(tmp_path, r) == specific

    document = {
        "results": {"Complete percentage": 95.0, "n_markers": 100, "domain": "bacteria"},
        "parameters": {"dataset_version": "v1"},
        "lineage_dataset": {"name": "lineage", "number_of_buscos": 100},
        "versions": {"busco": "5"},
    }
    direct = tmp_path / "direct.json"
    direct.write_text(json.dumps(document), encoding="utf-8")
    metrics = tools._parse_busco_json(direct, r)
    assert {m["metric_name"] for m in metrics} >= {"busco_complete_percent", "busco_n_markers"}
    direct.write_text("bad", encoding="utf-8")
    with pytest.raises(ExternalToolError, match="invalid BUSCO JSON"):
        tools._parse_busco_json(direct, r)
    direct.write_text("[]", encoding="utf-8")
    with pytest.raises(ExternalToolError, match="has no results object"):
        tools._parse_busco_json(direct, r)
    direct.write_text(json.dumps({"results": {"Complete percentage": 1}}), encoding="utf-8")
    with pytest.raises(ExternalToolError, match="missing required metrics"):
        tools._parse_busco_json(direct, r)


def test_blast_and_hmmer_parsers_cover_malformed_and_rank_limits(tmp_path):
    blast = tmp_path / "blast.tsv"
    blast.write_text(
        "# comment\nq1\ts1\t10\ttext\nq1\ts2\tbad\tx\nq1\ts3\t30\ty\n"
        "\ts4\t40\tz\nwrong\tcolumns\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="at least query and subject"):
        tools._parse_blast_tabular(blast, recipe(raw={"result_columns": ["q"]}))
    r = recipe(result_parser="blast_tabular", raw={
        "result_columns": ["query", "subject", "score", "label"],
        "hit_metric_columns": ["score", "label", "missing"],
        "numeric_columns": ["score"],
    })
    hits = list(tools.parse_hits(blast, r))
    assert len(hits) == 4
    assert any(hit["metric_numeric"] is None for hit in hits)

    hmmer = tmp_path / "hmmer.tbl"
    hmmer.write_text(
        "# comment\nshort row\nt1 x q1 x 1e-5 20\nt2 x q1 x bad score\nt3 x q1 x 1e-9 30\n",
        encoding="utf-8",
    )
    hits = list(tools.parse_hits(hmmer, recipe(result_parser="hmmer_tblout")))
    assert len(hits) == 4
    assert any(hit["metric_numeric"] is None for hit in hits)
    assert tools.parse_hits(hmmer, recipe(result_parser="none")) == []
    assert tools.parse_hits(hmmer, recipe(result_parser="busco_json")) == []
    with pytest.raises(ExternalToolError, match="unsupported result_parser"):
        tools.parse_hits(hmmer, recipe(result_parser="unknown"))


def test_print_tools_table_records_detection_errors(tmp_path, monkeypatch):
    p = project(tmp_path)
    config = {"tools": {"bad": {"recipes": {"a": {}}}, "ignored": []}}
    monkeypatch.setattr(tools, "load_tools_config", lambda _p: config)
    monkeypatch.setattr(tools, "get_tool", lambda *_a: tool_spec(name="bad"))
    monkeypatch.setattr(tools, "detect_tool_version", lambda *_a: (_ for _ in ()).throw(ExternalToolError("no")))
    table, ok = tools.print_tools_table(p)
    assert ok is False and "ERROR" in table and "a" in table
