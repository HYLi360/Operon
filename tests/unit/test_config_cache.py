"""Freshness and isolation contracts for the configuration read caches.

``load_tools_config``, ``load_profile`` and ``load_profiles`` memoize their
parsed documents against the ``(mtime_ns, size)`` of the file they came from,
because one Config-screen render used to re-parse the same unchanged YAML tens
of times.  The cache is only allowed to be invisible: an edit must be visible on
the very next read, a caller must own its result, and an explicit invalidation
must force a re-read even when the file's identity has not moved.

Every assertion here is on observable behaviour — a value a loader returns, or
the error it raises — and never on the cache's internal shape, so the storage
can change without rewriting them.  Where a test has to show that an entry
*survived* an invalidation, it does so by freezing the file's identity
(:func:`_write_same_size`) and asserting the read still returns the stale value:
that is only reachable through a cache hit, so the fact is visible from outside
without naming a private attribute.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from operon.errors import ValidationError
from operon.profiles import (
    invalidate_profile_cache,
    load_profile,
    load_profiles,
    write_default_profiles,
)
from operon.tools import (
    get_recipe,
    get_tool,
    invalidate_tools_config_cache,
    load_tools_config,
)

MINIMAL_TOOLS = {
    "tools": {
        "blast": {
            "executable": "blastn",
            "run_method": "",
            "version_args": ["-version"],
            "version_pattern": r"BLASTN (\S+)",
            "recipes": {
                "blastn": {
                    "tool": "blast",
                    "entity_type": "assembly",
                    "file_role": "genome_fasta",
                    "fmt": "fasta",
                    "arguments": ["-task", "blastn"],
                }
            },
        }
    }
}


def _project(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path,
        tools_config_path=tmp_path / "tools.yaml",
        profiles_dir=tmp_path / "profiles",
        logs_root=tmp_path / "logs",
        analysis_root=tmp_path / "analysis",
    )


def _write_yaml(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_profile(directory: Path, name: str, document: dict) -> Path:
    path = directory / f"{name}.yaml"
    _write_yaml(path, document)
    return path


def _identity(path: Path) -> tuple[int, int]:
    """The ``(mtime_ns, size)`` pair the cache keys a file on."""
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def _write_same_size(path: Path, document: dict, before: tuple[int, int]) -> None:
    """Write ``document`` to ``path`` while keeping the given ``(mtime, size)``.

    The cache keys on ``(mtime_ns, size)``, so a rewrite is only invisible to it
    when both still match.  This makes "the entry survived" observable from the
    outside: a read that still returns the *old* value after a same-size
    rewrite can only have come from the cache, never from the file.
    """
    mtime_ns, size = before
    body = yaml.safe_dump(document, sort_keys=False).encode("utf-8")
    assert len(body) == size, (
        f"freeze needs the same byte count ({size}), got {len(body)}: pad or "
        "shrink the document so the rewrite is byte-identical in length"
    )
    path.write_bytes(body)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    assert (path.stat().st_mtime_ns, path.stat().st_size) == before, (
        "the identity did not freeze; the filesystem ignored the restored mtime"
    )


# --- config/tools.yaml ----------------------------------------------------


def test_an_edited_tools_config_is_visible_on_the_next_read(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_yaml(project.tools_config_path, MINIMAL_TOOLS)

    first = load_tools_config(project)
    assert "blast" in first["tools"]

    renamed = {
        "tools": {
            "blast": {
                **MINIMAL_TOOLS["tools"]["blast"],
                "executable": "blastn-renamed",
            }
        }
    }
    _write_yaml(project.tools_config_path, renamed)

    second = load_tools_config(project)
    assert second["tools"]["blast"]["executable"] == "blastn-renamed"


def test_a_removed_tool_does_not_linger_in_the_cache(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_yaml(project.tools_config_path, MINIMAL_TOOLS)
    assert "blast" in load_tools_config(project)["tools"]

    _write_yaml(project.tools_config_path, {"tools": {}})
    assert load_tools_config(project)["tools"] == {}


def test_each_caller_owns_the_document_it_loaded(tmp_path: Path) -> None:
    """The save paths mutate the result in place before rewriting the file."""
    project = _project(tmp_path)
    _write_yaml(project.tools_config_path, MINIMAL_TOOLS)

    first = load_tools_config(project)
    first["tools"]["blast"]["recipes"]["injected"] = {"tool": "blast"}
    first["tools"]["blast"]["executable"] = "mutated"

    second = load_tools_config(project)
    assert "injected" not in second["tools"]["blast"]["recipes"]
    assert second["tools"]["blast"]["executable"] == "blastn"


def test_the_cache_does_not_bleed_between_projects(tmp_path: Path) -> None:
    """Each test gets its own tree; a shared key would leak one into the other."""
    left = _project(tmp_path / "left")
    right = _project(tmp_path / "right")
    _write_yaml(left.tools_config_path, MINIMAL_TOOLS)
    _write_yaml(
        right.tools_config_path,
        {"tools": {"other": {"executable": "other", "run_method": ""}}},
    )

    assert set(load_tools_config(left)["tools"]) == {"blast"}
    assert set(load_tools_config(right)["tools"]) == {"other"}


def test_invalidation_forces_a_reread_at_the_same_identity(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_yaml(project.tools_config_path, MINIMAL_TOOLS)
    load_tools_config(project)

    # Freeze the identity so identity alone cannot explain the re-read: only
    # the explicit invalidation below can make the new value visible.
    stat = project.tools_config_path.stat()
    _write_same_size(
        project.tools_config_path,
        {"tools": {"blast": {**MINIMAL_TOOLS["tools"]["blast"], "executable": "1234"}}},
        (stat.st_mtime_ns, stat.st_size),
    )
    assert load_tools_config(project)["tools"]["blast"]["executable"] == "blastn"

    invalidate_tools_config_cache(project)
    assert load_tools_config(project)["tools"]["blast"]["executable"] == "1234"


def test_an_invalid_tools_config_still_raises_and_is_not_cached(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _write_yaml(project.tools_config_path, {"not_tools": {}})

    with pytest.raises(ValidationError, match="invalid tools config"):
        load_tools_config(project)

    _write_yaml(project.tools_config_path, MINIMAL_TOOLS)
    assert "blast" in load_tools_config(project)["tools"]


def test_get_tool_and_get_recipe_read_through_the_same_cache(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_yaml(project.tools_config_path, MINIMAL_TOOLS)

    assert get_tool(project, "blast").executable == "blastn"
    assert get_recipe(project, "blastn").tool_name == "blast"

    _write_yaml(
        project.tools_config_path,
        {
            "tools": {
                "blast": {**MINIMAL_TOOLS["tools"]["blast"], "executable": "blastn2"}
            }
        },
    )
    assert get_tool(project, "blast").executable == "blastn2"


# --- config/profiles ------------------------------------------------------


def test_an_edited_profile_is_visible_on_the_next_read(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    path = _write_profile(
        directory, "strict", {"kind": "qc", "version": 1, "required": []}
    )
    assert load_profile(directory, "strict", expected_kind="qc")["version"] == 1

    _write_profile(directory, "strict", {"kind": "qc", "version": 2, "required": []})
    assert load_profile(directory, "strict", expected_kind="qc")["version"] == 2
    assert path.exists()


def test_each_caller_owns_the_profile_it_loaded(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write_profile(directory, "strict", {"kind": "qc", "version": 1, "required": []})

    first = load_profile(directory, "strict", expected_kind="qc")
    first["required"].append("mutated")

    assert load_profile(directory, "strict", expected_kind="qc")["required"] == []


def test_each_kind_is_listed_from_the_same_directory(tmp_path: Path) -> None:
    """One directory holds all three kinds, so a listing must not be shared."""
    directory = tmp_path / "profiles"
    _write_profile(directory, "a_qc", {"kind": "qc", "version": 1, "required": []})
    _write_profile(
        directory,
        "b_tiers",
        {"kind": "sequence_classification", "version": 1, "rules": []},
    )
    _write_profile(
        directory, "c_cov", {"kind": "taxonomy_coverage", "version": 1, "sets": []}
    )

    assert set(load_profiles(directory, kind="qc")) == {"a_qc"}
    assert set(load_profiles(directory, kind="sequence_classification")) == {"b_tiers"}
    assert set(load_profiles(directory, kind="taxonomy_coverage")) == {"c_cov"}
    assert set(load_profiles(directory, kind="qc")) == {"a_qc"}


def test_a_profile_added_after_the_first_listing_is_seen(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write_profile(directory, "a_qc", {"kind": "qc", "version": 1, "required": []})
    assert set(load_profiles(directory, kind="qc")) == {"a_qc"}

    _write_profile(directory, "b_qc", {"kind": "qc", "version": 1, "required": []})
    assert set(load_profiles(directory, kind="qc")) == {"a_qc", "b_qc"}

    (directory / "b_qc.yaml").unlink()
    assert set(load_profiles(directory, kind="qc")) == {"a_qc"}


def test_profile_listing_results_are_independent_objects(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write_profile(
        directory, "a_qc", {"kind": "qc", "version": 1, "required": ["seed_hits"]}
    )

    first = load_profiles(directory, kind="qc")
    first["a_qc"]["required"].append("mutated")

    assert load_profiles(directory, kind="qc")["a_qc"]["required"] == ["seed_hits"]


def test_profile_invalidation_forces_a_reread_at_the_same_identity(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "profiles"
    path = _write_profile(
        directory, "strict", {"kind": "qc", "version": 1, "required": []}
    )
    assert load_profile(directory, "strict", expected_kind="qc")["version"] == 1

    # Freeze the identity so only the explicit invalidation can expose the edit.
    _write_same_size(
        path, {"kind": "qc", "version": 5, "required": []}, _identity(path)
    )
    assert load_profile(directory, "strict", expected_kind="qc")["version"] == 1

    invalidate_profile_cache(directory)
    assert load_profile(directory, "strict", expected_kind="qc")["version"] == 5


def test_profile_invalidation_leaves_a_similarly_named_sibling_alone(
    tmp_path: Path,
) -> None:
    """``.../profiles`` must not clear ``.../profiles2``'s cached documents."""
    profiles_dir = tmp_path / "profiles"
    sibling_dir = tmp_path / "profiles2"
    for directory in (profiles_dir, sibling_dir):
        _write_profile(
            directory, "strict", {"kind": "qc", "version": 1, "required": []}
        )

    assert load_profile(profiles_dir, "strict", expected_kind="qc")["version"] == 1
    assert load_profile(sibling_dir, "strict", expected_kind="qc")["version"] == 1

    # Edit the sibling under a frozen identity, then invalidate only the other
    # directory.  A prefix-matching invalidation would drop the sibling's entry
    # and the read would return the file's new value; surviving the invalidation
    # with a *stale* read is what proves the entry was not touched.
    sibling_path = sibling_dir / "strict.yaml"
    _write_same_size(
        sibling_path,
        {"kind": "qc", "version": 9, "required": []},
        _identity(sibling_path),
    )
    invalidate_profile_cache(profiles_dir)
    assert load_profile(sibling_dir, "strict", expected_kind="qc")["version"] == 1

    # …while the directory that was named does re-read, proving the call
    # actually invalidated something.
    named_path = profiles_dir / "strict.yaml"
    _write_same_size(
        named_path,
        {"kind": "qc", "version": 4, "required": []},
        _identity(named_path),
    )
    invalidate_profile_cache(profiles_dir)
    assert load_profile(profiles_dir, "strict", expected_kind="qc")["version"] == 4


def test_profile_invalidation_without_an_argument_clears_every_directory(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    for directory in (left, right):
        _write_profile(
            directory, "strict", {"kind": "qc", "version": 1, "required": []}
        )

    assert load_profile(left, "strict", expected_kind="qc")["version"] == 1
    assert load_profile(right, "strict", expected_kind="qc")["version"] == 1

    # Freeze both identities, then clear everything: both directories must
    # re-read their edits, which they can only do if every entry went.
    for directory, version in ((left, 7), (right, 8)):
        path = directory / "strict.yaml"
        _write_same_size(
            path,
            {"kind": "qc", "version": version, "required": []},
            _identity(path),
        )
    invalidate_profile_cache()

    assert load_profile(left, "strict", expected_kind="qc")["version"] == 7
    assert load_profile(right, "strict", expected_kind="qc")["version"] == 8


def test_the_default_profiles_still_load_after_the_cache_is_warmed(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "profiles"
    write_default_profiles(directory)
    first = load_profiles(directory, kind="qc")
    assert first
    assert load_profiles(directory, kind="qc") == first


def test_an_invalid_profile_still_raises(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    _write_profile(directory, "broken", {"version": 1})

    with pytest.raises(ValidationError, match="explicit 'kind' and 'version'"):
        load_profiles(directory, kind="qc")
    with pytest.raises(ValidationError, match="explicit 'kind' and 'version'"):
        load_profile(directory, "broken", expected_kind="qc")


@pytest.mark.parametrize("body", ["42", "true", "- a\n- b", "just a string"])
def test_a_profile_file_that_is_not_a_mapping_raises_validation_error(
    tmp_path: Path, body: str
) -> None:
    """A scalar or sequence document is invalid, not a ``TypeError``.

    ``"kind" not in doc`` raises ``TypeError`` for a non-container document, so
    both loaders keep the explicit ``isinstance`` check: a hand-edited profile
    holding a bare number must report the profile contract, not fail deep in
    the membership test.
    """
    directory = tmp_path / "profiles"
    directory.mkdir(parents=True)
    (directory / "scalar.yaml").write_text(body, encoding="utf-8")

    with pytest.raises(ValidationError, match="explicit 'kind' and 'version'"):
        load_profiles(directory, kind="qc")
    with pytest.raises(ValidationError, match="explicit 'kind' and 'version'"):
        load_profile(directory, "scalar", expected_kind="qc")
