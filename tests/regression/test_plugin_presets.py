"""Declarative preset conflicts, no-op imports and exact-byte rollback."""

import copy
from contextlib import closing

import pytest
import yaml

from operon.cli import main
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ConflictError, ValidationError
from operon.tools._presets import add_preset, plan_preset_import


@pytest.fixture
def setup(tmp_path):
    project = init_demo(tmp_path / "project")
    source = tmp_path / "preset.yaml"
    recipe = {
        "version": 1,
        "entity_type": "assembly",
        "file_role": "genome_fasta",
        "format": "fasta",
        "arguments": ["${input}", "${output}"],
        "result_parser": "plugin:toy",
    }
    payload = {
        "version": 1,
        "tools": {
            "toy": {
                "executable": "python",
                "run_method": "",
                "version_args": ["--version"],
                "recipes": {"toy_probe": recipe},
            }
        },
    }
    source.write_text(yaml.safe_dump(payload))
    return project, source, payload


def test_preset_preview_merge_snapshot_and_noop(setup):
    project, source, payload = setup
    before = project.tools_config_path.read_bytes()
    assert plan_preset_import(project, source)["recipes"] == ["toy_probe"]
    assert project.tools_config_path.read_bytes() == before
    assert (
        main(
            [
                "--project",
                str(project.root),
                "tools",
                "add-preset",
                "--file",
                str(source),
                "--dry-run",
            ]
        )
        == 0
    )
    with closing(Database(project.db_path)) as db:
        outcome = add_preset(project, db, source)
        assert outcome["recipes"] == ["toy_probe"]
        assert outcome["snapshots"]["toy_probe"]
        saved = project.tools_config_path.read_bytes()
        audit_count = db.conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        assert add_preset(project, db, source)["unchanged"]
        assert project.tools_config_path.read_bytes() == saved
        assert (
            db.conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == audit_count
        )
        payload["tools"]["toy"]["recipes"]["toy_second"] = copy.deepcopy(
            payload["tools"]["toy"]["recipes"]["toy_probe"]
        )
        source.write_text(yaml.safe_dump(payload))
        result = add_preset(project, db, source)
        assert result["recipes"] == ["toy_probe", "toy_second"]
    config = yaml.safe_load(project.tools_config_path.read_text())
    assert "blastn" in config["tools"]


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(version=2),
        lambda p: p.update(version=True),
        lambda p: p.update(conda={}),
        lambda p: p["tools"].update({"../bad": {}}),
        lambda p: p["tools"].update({"invalid": []}),
        lambda p: p["tools"]["toy"].update(recipes=[]),
        lambda p: p["tools"]["toy"]["recipes"].update({"bad/name": {}}),
        lambda p: p["tools"]["toy"]["recipes"]["toy_probe"].update(version=0),
        lambda p: p["tools"].update({"other": copy.deepcopy(p["tools"]["toy"])}),
    ],
)
def test_invalid_preset_changes_nothing(setup, change):
    project, source, payload = setup
    before = project.tools_config_path.read_bytes()
    change(payload)
    source.write_text(yaml.safe_dump(payload))
    with pytest.raises((ValidationError, ConflictError)):
        plan_preset_import(project, source)
    assert project.tools_config_path.read_bytes() == before


def test_conflicts_and_failure_restore_original_bytes(setup, monkeypatch):
    project, source, payload = setup
    before = project.tools_config_path.read_bytes()
    with closing(Database(project.db_path)) as db:
        before_count = db.conn.execute(
            "SELECT COUNT(*) FROM recipe_snapshots"
        ).fetchone()[0]
        with monkeypatch.context() as patch:
            patch.setattr(
                db,
                "record_recipe",
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("snapshot failure")),
            )
            with pytest.raises(RuntimeError):
                add_preset(project, db, source)
        assert project.tools_config_path.read_bytes() == before
        assert (
            db.conn.execute("SELECT COUNT(*) FROM recipe_snapshots").fetchone()[0]
            == before_count
        )
        add_preset(project, db, source)
        saved = project.tools_config_path.read_bytes()
        payload["tools"]["toy"]["executable"] = "different"
        source.write_text(yaml.safe_dump(payload))
        with pytest.raises(ConflictError):
            add_preset(project, db, source)
        assert project.tools_config_path.read_bytes() == saved
        payload["tools"]["toy"]["executable"] = "python"
        payload["tools"]["toy"]["recipes"]["toy_probe"]["arguments"] = []
        source.write_text(yaml.safe_dump(payload))
        with pytest.raises(ConflictError):
            add_preset(project, db, source)


def test_missing_config_preview_and_rollback(setup, monkeypatch):
    project, source, _payload = setup
    project.tools_config_path.unlink()
    assert not plan_preset_import(project, source)["unchanged"]
    assert not project.tools_config_path.exists()
    with closing(Database(project.db_path)) as db:
        monkeypatch.setattr(
            db,
            "record_recipe",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("snapshot failure")),
        )
        with pytest.raises(RuntimeError):
            add_preset(project, db, source)
    assert not project.tools_config_path.exists()


def test_round_trip_failure_rolls_back(setup, monkeypatch):
    import operon.tools._presets as presets

    project, source, _payload = setup
    before = project.tools_config_path.read_bytes()
    monkeypatch.setattr(presets, "load_tools_config", lambda project: {})
    with (
        closing(Database(project.db_path)) as db,
        pytest.raises(ValidationError, match="round-trip"),
    ):
        add_preset(project, db, source)
    assert project.tools_config_path.read_bytes() == before


@pytest.mark.parametrize("text", ["[", "[]", "tools: []", "tools: {bad: []}"])
def test_malformed_yaml_or_existing_config_writes_nothing(setup, text):
    project, source, _payload = setup
    source.write_text(text)
    before = project.tools_config_path.read_bytes()
    with pytest.raises(ValidationError):
        plan_preset_import(project, source)
    assert project.tools_config_path.read_bytes() == before
    if text == "tools: {bad: []}":
        project.tools_config_path.write_text(text)
        source.write_text("version: 1\ntools: {}\n")
        with pytest.raises(ValidationError):
            plan_preset_import(project, source)
