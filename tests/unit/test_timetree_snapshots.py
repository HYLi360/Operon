"""Snapshot/calibration tests for :mod:`operon.timetree`.

Covers the project-independent halves of the TimeTree CLI group: the atomic
snapshot publisher (``new_directory``), pair parsing (``pairs_from_tsv``),
``fetch_snapshot``, ``load_snapshot``, ``calibrate_tree`` and the
``run_cli`` fetch/calibrate dispatch plus the query-command taxon guards.

Every HTTP interaction is mocked with a URL-routed fake ``requests.Session``;
no test touches the network and no test really sleeps (``time.sleep`` is
replaced by a recorder so the retry backoff schedule is asserted directly).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from operon import timetree
from operon.cli import main
from operon.config import Project
from operon.errors import ValidationError
from operon.schema import read_tsv, write_tsv
from operon.utils import sha256_file

API = timetree.API

SUMMARY_3702_9606 = {
    "taxon_a_id": 3702,
    "taxon_b_id": 9606,
    "scientific_name_a": "Arabidopsis thaliana",
    "scientific_name_b": "Homo sapiens",
    "precomputed_age": 1496.0,
    "precomputed_ci_low": 1350.0,
    "precomputed_ci_high": 1650.0,
    "adjusted_age": 1496.0,
    "all_total": 42,
}
STUDY_EVIDENCE = {"hit_records": [{"study_id": 1, "reference": "Kumar et al. 2022"}]}


# --- HTTP fakes --------------------------------------------------------------

class FakeResponse:
    """Minimal requests.Response stand-in carrying verbatim bytes."""

    def __init__(self, body, status: int = 200, url: str | None = None):
        self.body = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.status_code = status
        self.url = url

    @property
    def content(self) -> bytes:
        return self.body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """URL-routed ``requests.Session`` stand-in recording every call.

    A route value that is a list is replayed in order (the last item repeats),
    which is how retry schedules are exercised.
    """

    def __init__(self, routes: dict[str, object]):
        self.headers: dict[str, str] = {}
        self.routes = dict(routes)
        self.calls: list[tuple[str, float | None]] = []
        self.closed = False

    def get(self, url: str, timeout: float | None = None) -> FakeResponse:
        self.calls.append((url, timeout))
        if url not in self.routes:
            raise AssertionError(f"unexpected URL: {url}")
        item = self.routes[url]
        if isinstance(item, list):
            item = item.pop(0) if len(item) > 1 else item[0]
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def patch_network(monkeypatch):
    """Route every ``requests.Session`` created under test to one fake."""
    state: dict[str, FakeSession] = {}

    def install(routes: dict[str, object]) -> FakeSession:
        session = FakeSession(routes)
        state["session"] = session
        monkeypatch.setattr(requests, "Session", lambda: session)
        return session

    return install


@pytest.fixture
def sleep_calls(monkeypatch):
    """Record ``time.sleep`` arguments instead of sleeping."""
    calls: list[float] = []
    monkeypatch.setattr(timetree.time, "sleep", calls.append)
    return calls


def pair_url(a: int, b: int, flag: str) -> str:
    return f"{API}/pairwise/{a}/{b}/{flag}"


def pair_routes(a: int = 3702, b: int = 9606, *, summary=None, studies=None) -> dict[str, object]:
    summary = {**SUMMARY_3702_9606, "taxon_a_id": a, "taxon_b_id": b} if summary is None else summary
    studies = STUDY_EVIDENCE if studies is None else studies
    return {
        pair_url(a, b, "summaryjson"): FakeResponse(json.dumps(summary), url=pair_url(a, b, "summaryjson")),
        pair_url(a, b, "json"): FakeResponse(json.dumps(studies), url=pair_url(a, b, "json")),
    }


def write_pairs(path: Path, rows) -> Path:
    write_tsv(path, ["taxon_a", "taxon_b"], [list(row) for row in rows])
    return path


def write_snapshot(root: Path, records=None) -> Path:
    """Hand-write a snapshot directory with correct per-file sha256 values."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "raw").mkdir()
    entries = []
    for a, b in records if records is not None else [(3702, 9606), (3702, 7227), (9606, 10090)]:
        responses = {}
        for flag, payload in (("summaryjson", {**SUMMARY_3702_9606, "taxon_a_id": a, "taxon_b_id": b}),
                              ("json", STUDY_EVIDENCE)):
            name = f"raw/{a}_{b}.{flag}.json"
            (root / name).write_bytes(json.dumps(payload).encode("utf-8"))
            responses[flag] = {
                "path": name,
                "sha256": sha256_file(root / name),
                "url": pair_url(a, b, flag),
                "response_url": pair_url(a, b, flag),
                "retrieved_at": "2026-01-01T00:00:00+00:00",
            }
        entries.append({"taxon_a": a, "taxon_b": b, "responses": responses})
    document = {
        "schema": "operon-timetree-snapshot-1",
        "source": "TimeTree",
        "source_version": "unreported-by-api",
        "api_publication": timetree.CITATION,
        "age_unit": "Ma",
        "calibration_type": "secondary",
        "pairs_sha256": "0" * 64,
        "records": entries,
    }
    (root / "snapshot.json").write_text(json.dumps(document, indent=2) + "\n")
    return root


def rewrite_manifest(root: Path, mutate) -> None:
    document = json.loads((root / "snapshot.json").read_text())
    mutate(document)
    (root / "snapshot.json").write_text(json.dumps(document, indent=2) + "\n")


# --- new_directory -----------------------------------------------------------

def test_new_directory_publishes_complete_artifact(tmp_path):
    destination = tmp_path / "nested" / "deeper" / "artifact"
    with timetree.new_directory(destination) as work:
        assert work.parent == destination.parent
        assert work.name.startswith(".artifact-")
        assert not destination.exists()
        (work / "payload.txt").write_text("complete")
    assert (destination / "payload.txt").read_text() == "complete"
    assert [entry.name for entry in destination.parent.iterdir()] == ["artifact"]


def test_new_directory_refuses_existing_destination(tmp_path):
    destination = tmp_path / "artifact"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep")
    with pytest.raises(ValidationError, match=f"output already exists: {destination}"):
        with timetree.new_directory(destination):
            raise AssertionError("body must not run for an existing destination")
    assert (destination / "keep.txt").read_text() == "keep"


def test_new_directory_refuses_destination_created_during_run(tmp_path):
    destination = tmp_path / "artifact"
    with pytest.raises(ValidationError, match="output already exists"):
        with timetree.new_directory(destination) as work:
            (work / "partial.txt").write_text("partial")
            destination.mkdir()  # a racing publisher won the rename
    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    assert [entry.name for entry in tmp_path.iterdir() if entry.name.startswith(".artifact-")] == []


def test_new_directory_removes_temporary_after_body_failure(tmp_path):
    destination = tmp_path / "artifact"
    with pytest.raises(RuntimeError, match="boom"):
        with timetree.new_directory(destination) as work:
            (work / "partial.txt").write_text("partial")
            raise RuntimeError("boom")
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


# --- positive ----------------------------------------------------------------

def test_positive_accepts_numbers_and_numeric_strings():
    assert timetree.positive(3, "timeout") == 3.0
    assert timetree.positive("2.5", "timeout") == 2.5


@pytest.mark.parametrize("value", [0, -1, "abc", None, float("nan"), float("inf")])
def test_positive_rejects_nonpositive_or_nonfinite(value):
    with pytest.raises(ValidationError, match="timeout must be a positive finite number"):
        timetree.positive(value, "timeout")


# --- pairs_from_tsv ----------------------------------------------------------

def test_pairs_from_tsv_returns_sorted_unique_pairs(tmp_path):
    path = write_pairs(tmp_path / "pairs.tsv", [(9606, 3702), (3702, 9606), (3702, 7227)])
    assert timetree.pairs_from_tsv(path) == [(3702, 7227), (3702, 9606)]


@pytest.mark.parametrize("row, message", [
    (("3702", "abc"), "require NCBI taxonomy integer IDs"),
    (("0", "5"), "require two different positive NCBI IDs"),
    (("-3", "5"), "require two different positive NCBI IDs"),
    (("5", "5"), "require two different positive NCBI IDs"),
])
def test_pairs_from_tsv_rejects_invalid_rows(tmp_path, row, message):
    path = write_pairs(tmp_path / "pairs.tsv", [row])
    with pytest.raises(ValidationError, match=message):
        timetree.pairs_from_tsv(path)


def test_pairs_from_tsv_rejects_empty_table(tmp_path):
    path = write_pairs(tmp_path / "pairs.tsv", [])
    with pytest.raises(ValidationError, match="TimeTree pairs table is empty"):
        timetree.pairs_from_tsv(path)


def test_pairs_from_tsv_requires_both_columns(tmp_path):
    path = tmp_path / "pairs.tsv"
    path.write_text("taxon_a\n3702\n")
    with pytest.raises(ValidationError, match="missing columns"):
        timetree.pairs_from_tsv(path)


# --- fetch_snapshot ----------------------------------------------------------

def test_fetch_snapshot_writes_manifest_raw_files_and_candidates(tmp_path, patch_network):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    session = patch_network(pair_routes())
    destination = tmp_path / "snapshot"

    result = timetree.fetch_snapshot(pairs, destination, timeout=12, retries=1, delay=0)

    assert result == {
        "output": str(destination),
        "pairs": 1,
        "snapshot_sha256": sha256_file(destination / "snapshot.json"),
    }
    assert [call for call, _ in session.calls] == [
        pair_url(3702, 9606, "summaryjson"), pair_url(3702, 9606, "json")]
    assert all(timeout == 12 for _, timeout in session.calls)
    assert session.closed is True
    assert session.headers["User-Agent"] == "Operon-TimeTree-snapshot (research; selected taxon pairs)"

    manifest = json.loads((destination / "snapshot.json").read_text())
    assert manifest["schema"] == "operon-timetree-snapshot-1"
    assert manifest["source"] == "TimeTree"
    assert manifest["source_version"] == "unreported-by-api"
    assert manifest["api_publication"] == timetree.CITATION
    assert manifest["age_unit"] == "Ma"
    assert manifest["calibration_type"] == "secondary"
    assert manifest["pairs_sha256"] == sha256_file(pairs)
    [record] = manifest["records"]
    assert (record["taxon_a"], record["taxon_b"]) == (3702, 9606)
    assert record["responses"]["summaryjson"]["path"] == "raw/3702_9606.summaryjson.json"
    assert record["responses"]["json"]["path"] == "raw/3702_9606.json.json"
    for flag in ("summaryjson", "json"):
        item = record["responses"][flag]
        assert item["sha256"] == sha256_file(destination / item["path"])
        assert item["url"] == pair_url(3702, 9606, flag)
        assert item["response_url"] == pair_url(3702, 9606, flag)
        assert item["retrieved_at"]
    assert (destination / "raw/3702_9606.summaryjson.json").read_bytes() == json.dumps(
        SUMMARY_3702_9606).encode("utf-8")
    assert (destination / "raw/3702_9606.json.json").read_bytes() == json.dumps(
        STUDY_EVIDENCE).encode("utf-8")

    header = (destination / "candidates.tsv").read_text().splitlines()[0].split("\t")
    assert header == [
        "taxon_a", "taxon_b", "name_a", "name_b", "age_ma", "reported_ci_low_ma",
        "reported_ci_high_ma", "adjusted_age", "studies", "calibration_type", "approved"]
    assert read_tsv(destination / "candidates.tsv") == [{
        "taxon_a": "3702", "taxon_b": "9606",
        "name_a": "Arabidopsis thaliana", "name_b": "Homo sapiens",
        "age_ma": "1496.0", "reported_ci_low_ma": "1350.0", "reported_ci_high_ma": "1650.0",
        "adjusted_age": "1496.0", "studies": "42",
        "calibration_type": "secondary", "approved": "no",
    }]
    assert [entry.name for entry in tmp_path.iterdir() if entry.name.startswith(".snapshot-")] == []


def test_fetch_snapshot_deduplicates_and_sorts_pairs(tmp_path, patch_network):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(9606, 3702), (3702, 9606), (3702, 7227)])
    routes = {**pair_routes(3702, 9606), **pair_routes(3702, 7227)}
    patch_network(routes)
    destination = tmp_path / "snapshot"

    result = timetree.fetch_snapshot(pairs, destination, timeout=5, retries=1, delay=0)

    assert result["pairs"] == 2
    manifest = json.loads((destination / "snapshot.json").read_text())
    assert [(r["taxon_a"], r["taxon_b"]) for r in manifest["records"]] == [(3702, 7227), (3702, 9606)]
    assert len(read_tsv(destination / "candidates.tsv")) == 2


def test_fetch_snapshot_retries_transient_failure_with_backoff(tmp_path, patch_network, sleep_calls):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    session = patch_network({
        **pair_routes(),
        pair_url(3702, 9606, "summaryjson"): [
            requests.exceptions.Timeout("slow"), FakeResponse(json.dumps(SUMMARY_3702_9606))],
    })
    destination = tmp_path / "snapshot"

    result = timetree.fetch_snapshot(pairs, destination, retries=3, delay=1.0)

    assert result["pairs"] == 1
    assert sleep_calls == [1.0, 1.0, 1.0]
    assert [url for url, _ in session.calls] == [
        pair_url(3702, 9606, "summaryjson"),
        pair_url(3702, 9606, "summaryjson"),
        pair_url(3702, 9606, "json"),
    ]
    assert (destination / "raw/3702_9606.summaryjson.json").exists()


def test_fetch_snapshot_backoff_doubles_with_attempt(tmp_path, patch_network, sleep_calls):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    patch_network({
        **pair_routes(),
        pair_url(3702, 9606, "summaryjson"): [
            requests.exceptions.Timeout("first"),
            FakeResponse("boom", status=503),
            FakeResponse(json.dumps(SUMMARY_3702_9606)),
        ],
    })

    timetree.fetch_snapshot(pairs, tmp_path / "snapshot", retries=3, delay=1.0)

    assert sleep_calls == [1.0, 2.0, 1.0, 1.0]


def test_fetch_snapshot_fails_after_exhausting_retries(tmp_path, patch_network, sleep_calls):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    session = patch_network({
        **pair_routes(),
        pair_url(3702, 9606, "summaryjson"): requests.exceptions.ConnectionError("down"),
    })
    destination = tmp_path / "snapshot"

    with pytest.raises(ValidationError,
                       match=f"TimeTree request failed: {pair_url(3702, 9606, 'summaryjson')}"):
        timetree.fetch_snapshot(pairs, destination, retries=2, delay=1.0)

    assert [url for url, _ in session.calls] == [pair_url(3702, 9606, "summaryjson")] * 2
    assert sleep_calls == [1.0]
    assert not destination.exists()
    assert [entry.name for entry in tmp_path.iterdir() if entry.name.startswith(".snapshot-")] == []


def test_fetch_snapshot_reports_http_error_status(tmp_path, patch_network):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    patch_network({
        **pair_routes(),
        pair_url(3702, 9606, "summaryjson"): FakeResponse("gone", status=404),
    })
    with pytest.raises(ValidationError, match="TimeTree request failed: .*HTTP 404"):
        timetree.fetch_snapshot(pairs, tmp_path / "snapshot", retries=1, delay=0)


def test_fetch_snapshot_requires_a_json_object(tmp_path, patch_network):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    patch_network({
        **pair_routes(),
        pair_url(3702, 9606, "summaryjson"): FakeResponse("[1, 2, 3]"),
    })
    with pytest.raises(ValidationError, match="expected a JSON object"):
        timetree.fetch_snapshot(pairs, tmp_path / "snapshot", retries=1, delay=0)


def test_fetch_snapshot_rejects_taxon_id_mismatch(tmp_path, patch_network):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    patch_network(pair_routes(summary={**SUMMARY_3702_9606, "taxon_b_id": 7227}))
    with pytest.raises(ValidationError,
                       match="TimeTree returned different/missing taxon IDs for 3702/9606"):
        timetree.fetch_snapshot(pairs, tmp_path / "snapshot", delay=0)


def test_fetch_snapshot_rejects_missing_age(tmp_path, patch_network):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    patch_network(pair_routes(summary={k: v for k, v in SUMMARY_3702_9606.items()
                                      if k != "precomputed_age"}))
    with pytest.raises(ValidationError,
                       match="TimeTree age for 3702/9606 must be a positive finite number"):
        timetree.fetch_snapshot(pairs, tmp_path / "snapshot", delay=0)


@pytest.mark.parametrize("studies", [{"hit_records": []}, {"hit_records": "nope"}, {}])
def test_fetch_snapshot_rejects_missing_study_evidence(tmp_path, patch_network, studies):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    session = patch_network(pair_routes(studies=studies))
    destination = tmp_path / "snapshot"
    with pytest.raises(ValidationError, match="TimeTree returned no study evidence for 3702/9606"):
        timetree.fetch_snapshot(pairs, destination, delay=0)
    assert not destination.exists()
    assert session.closed is True


def test_fetch_snapshot_validates_pairs_before_network(tmp_path, patch_network):
    pairs = write_pairs(tmp_path / "pairs.tsv", [])
    session = patch_network({})
    destination = tmp_path / "snapshot"
    with pytest.raises(ValidationError, match="pairs table is empty"):
        timetree.fetch_snapshot(pairs, destination, delay=0)
    assert session.calls == []
    assert not destination.exists()


def test_fetch_snapshot_rejects_nonpositive_timeout(tmp_path, patch_network):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    session = patch_network(pair_routes())
    with pytest.raises(ValidationError, match="timeout must be a positive finite number"):
        timetree.fetch_snapshot(pairs, tmp_path / "snapshot", timeout=0, delay=0)
    assert session.calls == []


@pytest.mark.parametrize("kwargs, message", [
    ({"retries": 0}, "retries must be 1..10"),
    ({"retries": 11}, "retries must be 1..10"),
    ({"retries": 1, "delay": -1.0}, "retries must be 1..10"),
    ({"retries": 1, "delay": float("nan")}, "retries must be 1..10"),
])
def test_fetch_snapshot_rejects_invalid_retry_policy(tmp_path, patch_network, kwargs, message):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    session = patch_network(pair_routes())
    with pytest.raises(ValidationError, match=message):
        timetree.fetch_snapshot(pairs, tmp_path / "snapshot", **kwargs)
    assert session.calls == []


def test_fetch_snapshot_refuses_existing_output_directory(tmp_path, patch_network):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    session = patch_network(pair_routes())
    destination = tmp_path / "snapshot"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep")
    with pytest.raises(ValidationError, match=f"output already exists: {destination}"):
        timetree.fetch_snapshot(pairs, destination, delay=0)
    assert session.calls == []
    assert (destination / "keep.txt").read_text() == "keep"


# --- load_snapshot -----------------------------------------------------------

def test_load_snapshot_returns_manifest_and_sorted_pairs(tmp_path):
    root = write_snapshot(tmp_path / "snapshot")
    document, pairs = timetree.load_snapshot(root)
    assert document["schema"] == "operon-timetree-snapshot-1"
    assert pairs == {(3702, 7227), (3702, 9606), (9606, 10090)}


@pytest.mark.parametrize("field, value", [
    ("schema", "operon-timetree-snapshot-2"),
    ("age_unit", "years"),
])
def test_load_snapshot_rejects_unsupported_schema_or_unit(tmp_path, field, value):
    root = write_snapshot(tmp_path / "snapshot")
    rewrite_manifest(root, lambda document: document.__setitem__(field, value))
    with pytest.raises(ValidationError, match="unsupported TimeTree snapshot schema or time unit"):
        timetree.load_snapshot(root)


def test_load_snapshot_detects_tampered_raw_file(tmp_path):
    root = write_snapshot(tmp_path / "snapshot")
    (root / "raw/3702_9606.summaryjson.json").write_text('{"tampered": true}')
    with pytest.raises(ValidationError, match="checksum mismatch or path escape"):
        timetree.load_snapshot(root)


@pytest.mark.parametrize("escape", ["../outside.json", "/nonexistent/outside.json"])
def test_load_snapshot_rejects_path_escape(tmp_path, escape):
    root = write_snapshot(tmp_path / "snapshot")
    rewrite_manifest(
        root,
        lambda document: document["records"][0]["responses"]["summaryjson"].__setitem__("path", escape),
    )
    with pytest.raises(ValidationError, match="checksum mismatch or path escape"):
        timetree.load_snapshot(root)


def test_load_snapshot_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside.json"
    root = write_snapshot(tmp_path / "snapshot")
    outside.write_bytes((root / "raw/3702_9606.summaryjson.json").read_bytes())
    link = root / "raw/3702_9606.summaryjson.json"
    link.unlink()
    link.symlink_to(outside)
    # The checksum still matches (same bytes through the link); only the escape check can fire.
    rewrite_manifest(
        root,
        lambda document: document["records"][0]["responses"]["summaryjson"].__setitem__(
            "sha256", sha256_file(outside)),
    )
    with pytest.raises(ValidationError, match="checksum mismatch or path escape"):
        timetree.load_snapshot(root)


# --- calibrate_tree ----------------------------------------------------------

DEFAULT_TREE = "((A:1,B:1):1,C:1);"
CONSTRAINT_COLUMNS = ["taxon_a", "taxon_b", "members", "min_ma", "max_ma", "approved", "rationale"]
DEFAULT_TAXA = [
    {"leaf": "A", "taxon_id": 3702},
    {"leaf": "B", "taxon_id": 9606},
    {"leaf": "C", "taxon_id": 7227},
]
DEFAULT_CONSTRAINT = {
    "taxon_a": 3702, "taxon_b": 9606, "members": "A,B",
    "min_ma": 100, "max_ma": 200, "approved": "yes",
    "rationale": "reviewed fossil calibration from the primary literature",
}


def constraint(**overrides) -> dict:
    row = dict(DEFAULT_CONSTRAINT)
    row.update(overrides)
    return row


def write_calibration_inputs(tmp_path, *, tree=DEFAULT_TREE, taxa=None, constraints=None):
    tree_file = tmp_path / "tree.nwk"
    tree_file.write_text(tree if tree.endswith("\n") else tree + "\n")
    taxa_file = tmp_path / "taxa.tsv"
    write_tsv(taxa_file, ["leaf", "taxon_id"], list(DEFAULT_TAXA if taxa is None else taxa))
    constraints_file = tmp_path / "constraints.tsv"
    write_tsv(constraints_file, CONSTRAINT_COLUMNS,
              list([DEFAULT_CONSTRAINT] if constraints is None else constraints))
    return tree_file, taxa_file, constraints_file


@pytest.fixture
def snapshot_dir(tmp_path):
    return write_snapshot(tmp_path / "snapshot")


def test_calibrate_tree_writes_paml_tree_and_provenance(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path)
    destination = tmp_path / "calibrated"

    result = timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file, destination)

    assert result == {"output": str(destination), "calibrations": 1, "unit_ma": 100.0}
    calibrated = (destination / "calibrated.tree").read_text()
    assert calibrated == "3 1\n((A,B)'B(1,2)',C);\n"
    assert ":" not in calibrated  # branch lengths are stripped for MCMCTree
    assert (destination / "constraints.tsv").read_bytes() == constraints_file.read_bytes()
    evidence = json.loads((destination / "provenance.json").read_text())
    assert evidence == {
        "calibration_type": "secondary",
        "unit_ma": 100.0,
        "snapshot_sha256": sha256_file(snapshot_dir / "snapshot.json"),
        "tree_sha256": sha256_file(tree_file),
        "taxa_sha256": sha256_file(taxa_file),
        "constraints_sha256": sha256_file(constraints_file),
        "calibrated_tree_sha256": sha256_file(destination / "calibrated.tree"),
    }


def test_calibrate_tree_scales_bounds_by_unit_ma(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path)
    destination = tmp_path / "calibrated"

    result = timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                     destination, unit_ma=10)

    assert result["unit_ma"] == 10.0
    assert (destination / "calibrated.tree").read_text() == "3 1\n((A,B)'B(10,20)',C);\n"
    assert json.loads((destination / "provenance.json").read_text())["unit_ma"] == 10.0


def test_calibrate_tree_labels_root_and_internal_nodes(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(
        tmp_path,
        constraints=[
            constraint(taxon_a=3702, taxon_b=9606, members="A,B", min_ma=100, max_ma=200),
            constraint(taxon_a=3702, taxon_b=7227, members="A,B,C", min_ma=300, max_ma=400),
        ],
    )
    destination = tmp_path / "calibrated"

    result = timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file, destination)

    assert result["calibrations"] == 2
    assert (destination / "calibrated.tree").read_text() == \
        "3 1\n((A,B)'B(1,2)',C)'B(3,4)';\n"


def test_calibrate_tree_refuses_existing_output(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path)
    destination = tmp_path / "calibrated"
    destination.mkdir()
    with pytest.raises(ValidationError, match=f"output already exists: {destination}"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file, destination)


@pytest.mark.parametrize("unit_ma", [0, -1, "abc", float("inf")])
def test_calibrate_tree_rejects_invalid_unit_ma(tmp_path, unit_ma):
    # Validation happens before the snapshot is even opened.
    with pytest.raises(ValidationError, match="unit_ma must be a positive finite number"):
        timetree.calibrate_tree(tmp_path / "missing-snapshot", tmp_path / "missing.nwk",
                                tmp_path / "missing.tsv", tmp_path / "missing.tsv",
                                tmp_path / "out", unit_ma=unit_ma)
    assert not (tmp_path / "out").exists()


def test_calibrate_tree_rejects_duplicate_leaf_labels(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path, tree="((A:1,A:1):1,C:1);")
    destination = tmp_path / "calibrated"
    with pytest.raises(ValidationError, match="dating tree must have unique nonempty leaf labels"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file, destination)
    assert not destination.exists()


def test_calibrate_tree_rejects_missing_leaf_labels(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path, tree="((:1,B:1):1,C:1);")
    with pytest.raises(ValidationError, match="dating tree must have unique nonempty leaf labels"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                tmp_path / "calibrated")


def test_calibrate_tree_rejects_non_bifurcating_tree(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(
        tmp_path, tree="((A:1,B:1,C:1):1,D:1);")
    with pytest.raises(ValidationError, match="dating tree must be rooted and strictly bifurcating"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                tmp_path / "calibrated")


def test_calibrate_tree_rejects_duplicate_leaf_in_taxa_table(tmp_path, snapshot_dir):
    taxa = [{"leaf": "A", "taxon_id": 3702}, {"leaf": "A", "taxon_id": 9606},
            {"leaf": "B", "taxon_id": 9606}, {"leaf": "C", "taxon_id": 7227}]
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path, taxa=taxa)
    with pytest.raises(ValidationError, match="duplicate leaf in taxa table"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                tmp_path / "calibrated")


@pytest.mark.parametrize("taxa, message", [
    ([{"leaf": "A", "taxon_id": 3702}, {"leaf": "B", "taxon_id": 9606}],
     "taxa table must map every tree leaf to one unique positive NCBI ID"),
    ([{"leaf": "A", "taxon_id": 3702}, {"leaf": "B", "taxon_id": 3702}, {"leaf": "C", "taxon_id": 7227}],
     "taxa table must map every tree leaf to one unique positive NCBI ID"),
    ([{"leaf": "A", "taxon_id": 0}, {"leaf": "B", "taxon_id": 9606}, {"leaf": "C", "taxon_id": 7227}],
     "taxa table must map every tree leaf to one unique positive NCBI ID"),
    ([{"leaf": "Z", "taxon_id": 3702}, {"leaf": "B", "taxon_id": 9606}, {"leaf": "C", "taxon_id": 7227}],
     "taxa table must map every tree leaf to one unique positive NCBI ID"),
])
def test_calibrate_tree_requires_taxa_table_to_match_leaves(tmp_path, snapshot_dir, taxa, message):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path, taxa=taxa)
    with pytest.raises(ValidationError, match=message):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                tmp_path / "calibrated")


@pytest.mark.parametrize("row, message", [
    (constraint(taxon_a=9606, taxon_b=7227, members="B,C"),
     "constraint pair is absent from snapshot or target taxa"),
    (constraint(taxon_a=9606, taxon_b=10090, members="B,D"),
     "constraint pair is absent from snapshot or target taxa"),
])
def test_calibrate_tree_rejects_constraint_pairs_outside_snapshot_or_taxa(tmp_path, snapshot_dir, row, message):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path, constraints=[row])
    with pytest.raises(ValidationError, match=message):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                tmp_path / "calibrated")


@pytest.mark.parametrize("row", [
    constraint(approved="no"),
    constraint(approved="YES "),  # trailing space is not "yes"
    constraint(approved="yes", rationale="   "),
])
def test_calibrate_tree_requires_approval_and_rationale(tmp_path, snapshot_dir, row):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path, constraints=[row])
    with pytest.raises(ValidationError,
                       match="each constraint requires approved=yes and a scientific rationale"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                tmp_path / "calibrated")


def test_calibrate_tree_accepts_approved_case_insensitively(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(
        tmp_path, constraints=[constraint(approved="Yes")])
    destination = tmp_path / "calibrated"
    assert timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                   destination)["calibrations"] == 1


def test_calibrate_tree_rejects_member_mismatch(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(
        tmp_path, constraints=[constraint(members="A,C")])
    with pytest.raises(ValidationError,
                       match="constraint members do not equal the target MRCA clade"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                tmp_path / "calibrated")


def test_calibrate_tree_rejects_duplicate_mrca_constraints(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(
        tmp_path, constraints=[constraint(), constraint(rationale="second row for the same MRCA")])
    with pytest.raises(ValidationError,
                       match="multiple constraints map to the same MRCA; review and consolidate them"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                tmp_path / "calibrated")


@pytest.mark.parametrize("row, message", [
    (constraint(min_ma=300, max_ma=200), "min_ma must be less than max_ma"),
    (constraint(min_ma=200, max_ma=200), "min_ma must be less than max_ma"),
    (constraint(min_ma=0), "min_ma must be a positive finite number"),
    (constraint(min_ma="soon"), "min_ma must be a positive finite number"),
    (constraint(max_ma=0), "max_ma must be a positive finite number"),
])
def test_calibrate_tree_rejects_invalid_bounds(tmp_path, snapshot_dir, row, message):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path, constraints=[row])
    with pytest.raises(ValidationError, match=message):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                tmp_path / "calibrated")


def test_calibrate_tree_requires_at_least_one_constraint(tmp_path, snapshot_dir):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path, constraints=[])
    destination = tmp_path / "calibrated"
    with pytest.raises(ValidationError, match="at least one reviewed calibration is required"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file, destination)
    assert not destination.exists()


def test_calibrate_tree_rejects_contradictory_ancestor_descendant_bounds(tmp_path, snapshot_dir):
    taxa = DEFAULT_TAXA + [{"leaf": "D", "taxon_id": 10090}]
    tree_file, taxa_file, constraints_file = write_calibration_inputs(
        tmp_path,
        tree="(((A:1,B:1):1,C:1):1,D:1);",
        taxa=taxa,
        constraints=[
            constraint(taxon_a=3702, taxon_b=9606, members="A,B", min_ma=300, max_ma=400),
            constraint(taxon_a=3702, taxon_b=7227, members="A,B,C", min_ma=100, max_ma=200),
        ],
    )
    with pytest.raises(ValidationError,
                       match="calibration bounds contradict ancestor/descendant time ordering"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file,
                                tmp_path / "calibrated")


def test_calibrate_tree_rejects_unsafe_paml_leaf_label(tmp_path, snapshot_dir):
    taxa = [{"leaf": "A*", "taxon_id": 3702}, {"leaf": "B", "taxon_id": 9606},
            {"leaf": "C", "taxon_id": 7227}]
    tree_file, taxa_file, constraints_file = write_calibration_inputs(
        tmp_path, tree="((A*:1,B:1):1,C:1);", taxa=taxa,
        constraints=[constraint(members="A*,B")])
    destination = tmp_path / "calibrated"
    with pytest.raises(ValidationError, match=r"unsafe PAML leaf label: A\*"):
        timetree.calibrate_tree(snapshot_dir, tree_file, taxa_file, constraints_file, destination)
    assert not destination.exists()


def test_calibrate_tree_loads_snapshot_written_by_fetch_snapshot(tmp_path, patch_network):
    """fetch_snapshot output is directly consumable by calibrate_tree."""
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    patch_network(pair_routes())
    snapshot = tmp_path / "fetched"
    timetree.fetch_snapshot(pairs, snapshot, retries=1, delay=0)
    _, available = timetree.load_snapshot(snapshot)
    assert available == {(3702, 9606)}

    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path)
    destination = tmp_path / "calibrated"
    result = timetree.calibrate_tree(snapshot, tree_file, taxa_file, constraints_file, destination)
    assert result["calibrations"] == 1
    assert (destination / "calibrated.tree").read_text() == "3 1\n((A,B)'B(1,2)',C);\n"


# --- run_cli dispatch --------------------------------------------------------

def test_cli_fetch_dispatch_publishes_snapshot(tmp_path, patch_network, capsys):
    pairs = write_pairs(tmp_path / "pairs.tsv", [(3702, 9606)])
    patch_network(pair_routes())
    destination = tmp_path / "snapshot"

    exit_code = main(["timetree", "fetch", "--pairs", str(pairs), "--output", str(destination),
                      "--timeout", "5", "--retries", "1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "output": str(destination),
        "pairs": 1,
        "snapshot_sha256": sha256_file(destination / "snapshot.json"),
    }


def test_cli_fetch_dispatch_reports_validation_error(tmp_path, capsys):
    pairs = write_pairs(tmp_path / "pairs.tsv", [])
    exit_code = main(["timetree", "fetch", "--pairs", str(pairs),
                      "--output", str(tmp_path / "snapshot")])
    assert exit_code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "error: TimeTree pairs table is empty" in err
    assert not (tmp_path / "snapshot").exists()


def test_cli_calibrate_dispatch_writes_outputs(tmp_path, snapshot_dir, capsys):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path)
    destination = tmp_path / "calibrated"

    exit_code = main(["timetree", "calibrate", "--snapshot", str(snapshot_dir),
                      "--tree", str(tree_file), "--taxa", str(taxa_file),
                      "--constraints", str(constraints_file), "--output", str(destination),
                      "--unit-ma", "50"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "output": str(destination), "calibrations": 1, "unit_ma": 50.0}
    assert (destination / "calibrated.tree").read_text() == "3 1\n((A,B)'B(2,4)',C);\n"


def test_cli_calibrate_dispatch_reports_validation_error(tmp_path, snapshot_dir, capsys):
    tree_file, taxa_file, constraints_file = write_calibration_inputs(tmp_path, constraints=[])
    exit_code = main(["timetree", "calibrate", "--snapshot", str(snapshot_dir),
                      "--tree", str(tree_file), "--taxa", str(taxa_file),
                      "--constraints", str(constraints_file),
                      "--output", str(tmp_path / "calibrated")])
    assert exit_code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "error: at least one reviewed calibration is required" in err


# --- CLI query-command validation branches -----------------------------------

@pytest.fixture
def project(tmp_path):
    return Project.init(tmp_path / "proj")


def _run_cli(project: Project, *argv: str) -> int:
    return main(["--project", str(project.root), *argv])


def test_cli_pairwise_rejects_more_than_two_taxa(project, patch_network, capsys):
    session = patch_network({})
    exit_code = _run_cli(project, "timetree", "pairwise",
                         "--taxon-id", "3702", "--taxon-id", "9606", "--taxon-id", "7227")
    assert exit_code == 2
    assert "pairwise needs exactly two distinct taxa" in capsys.readouterr().err
    assert session.calls == []


def test_cli_timeline_rejects_more_than_one_taxon(project, patch_network, capsys):
    session = patch_network({})
    exit_code = _run_cli(project, "timetree", "timeline",
                         "--taxon-id", "3702", "--taxon-id", "9606")
    assert exit_code == 2
    assert "timeline needs exactly one taxon" in capsys.readouterr().err
    assert session.calls == []


def test_cli_requires_minimum_distinct_taxa(project, patch_network, capsys):
    session = patch_network({})
    exit_code = _run_cli(project, "timetree", "calibrations", "--taxon-id", "3702")
    assert exit_code == 2
    assert "at least 2 distinct taxa" in capsys.readouterr().err
    assert session.calls == []


def test_cli_mrca_deduplicates_repeated_taxon_ids(project, patch_network, capsys):
    session = patch_network({
        f"{API}/mrca/id/3702+9606/summaryjson": FakeResponse(json.dumps(SUMMARY_3702_9606)),
    })
    exit_code = _run_cli(project, "timetree", "mrca",
                         "--taxon-id", "3702", "--taxon-id", "3702", "--taxon-id", "9606")
    assert exit_code == 0
    assert [url for url, _ in session.calls] == [f"{API}/mrca/id/3702+9606/summaryjson"]
    assert "3702,9606" in capsys.readouterr().out


def test_cli_calibrations_without_out_does_not_write_or_announce_a_file(project, patch_network, capsys):
    session = patch_network({
        f"{API}/mrca/id/3702+7227+9606/summaryjson": FakeResponse(json.dumps(SUMMARY_3702_9606)),
    })
    exit_code = _run_cli(project, "timetree", "calibrations",
                         "--taxon-id", "3702", "--taxon-id", "9606", "--taxon-id", "7227")
    assert exit_code == 0
    out, err = capsys.readouterr()
    assert "mrca(3 taxa)" in out
    assert "wrote" not in err
    assert "cite: " in err
    assert [url for url, _ in session.calls] == [f"{API}/mrca/id/3702+7227+9606/summaryjson"]
