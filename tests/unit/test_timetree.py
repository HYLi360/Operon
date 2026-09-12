"""TimeTree REST adapter tests; every HTTP interaction is mocked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from operon.adapters import timetree
from operon.adapters.timetree import TimeTreeClient
from operon.cli import main
from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError

BASE = timetree.TIMETREE_API

SUMMARY_3702_9606 = {
    "taxon_a_id": 3702,
    "taxon_b_id": 9606,
    "scientific_name_a": "Arabidopsis thaliana",
    "scientific_name_b": "Homo sapiens",
    "precomputed_age": 1496.0,
    "precomputed_ci_low": 1350.0,
    "precomputed_ci_high": 1650.0,
    "all_total": 42,
    "adjusted_age": 1496.0,
}


class FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self.text = body
        self.status_code = status


class FakeSession:
    """URL-routed stand-in for requests.Session; records every call."""

    def __init__(self, routes: dict[str, object]):
        self.headers: dict[str, str] = {}
        self.routes = routes
        self.calls: list[str] = []
        self.closed = False

    def get(self, url: str, timeout: float | None = None) -> FakeResponse:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected URL: {url}")
        item = self.routes[url]
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def make_session(routes: dict[str, object]) -> FakeSession:
    return FakeSession({
        url: FakeResponse(body) if isinstance(body, str) else body
        for url, body in routes.items()
    })


def make_client(tmp_path: Path, routes: dict[str, object], **kwargs) -> tuple[TimeTreeClient, FakeSession]:
    session = make_session(routes)
    kwargs.setdefault("delay", 0)
    client = TimeTreeClient(tmp_path / "cache", session=session, **kwargs)
    return client, session


@pytest.fixture
def project(tmp_path: Path):
    return Project.init(tmp_path / "proj")


@pytest.fixture
def patch_network(monkeypatch):
    """Route every requests.Session created by the adapter to a fake."""
    state: dict[str, FakeSession] = {}

    def install(routes: dict[str, object]) -> FakeSession:
        session = make_session(routes)
        state["session"] = session
        monkeypatch.setattr(requests, "Session", lambda: session)
        return session

    return install


# --- taxon resolution -------------------------------------------------------

def test_resolve_taxon_single(tmp_path):
    client, _ = make_client(tmp_path, {
        f"{BASE}/taxon/Arabidopsis%20thaliana": json.dumps(
            {"taxon_id": 3702, "scientific_name": "Arabidopsis thaliana", "rank": "species"}),
    })
    candidates = client.resolve_taxon("Arabidopsis thaliana")
    assert candidates == [
        {"taxon_id": 3702, "scientific_name": "Arabidopsis thaliana", "rank": "species"}]


def test_resolve_taxon_multiple(tmp_path):
    client, _ = make_client(tmp_path, {
        f"{BASE}/taxon/Apis": json.dumps([
            {"taxon_id": 7460, "scientific_name": "Apis mellifera", "rank": "species"},
            {"taxon_id": 7461, "scientific_name": "Apis cerana"},
        ]),
    })
    candidates = client.resolve_taxon("Apis")
    assert [item["taxon_id"] for item in candidates] == [7460, 7461]
    assert candidates[1]["rank"] == ""


def test_resolve_taxon_none(tmp_path):
    client, _ = make_client(tmp_path, {f"{BASE}/taxon/Nothingium": json.dumps({"error": "no match"})})
    with pytest.raises(ValidationError, match="no taxon"):
        client.resolve_taxon("Nothingium")


def test_resolve_taxon_rejects_empty_name(tmp_path):
    client, _ = make_client(tmp_path, {})
    with pytest.raises(ValidationError, match="must not be empty"):
        client.resolve_taxon("  ")


# --- pairwise / mrca summaries ----------------------------------------------

def test_pairwise_summaryjson(tmp_path):
    client, session = make_client(tmp_path, {
        f"{BASE}/pairwise/3702/9606/summaryjson": json.dumps(SUMMARY_3702_9606),
    })
    result = client.pairwise(3702, 9606)
    assert result["age_median"] == 1496.0
    assert result["ci_low"] == 1350.0
    assert result["ci_high"] == 1650.0
    assert result["study_count"] == 42
    assert result["taxon_ids"] == [3702, 9606]
    assert result["scientific_names"] == ["Arabidopsis thaliana", "Homo sapiens"]
    assert result["from_cache"] is False
    assert session.calls == [f"{BASE}/pairwise/3702/9606/summaryjson"]


def test_pairwise_field_variants(tmp_path):
    variant = {"Summary": {"Median Time": "100.5", "CI Low": 90, "CI High": 110, "Studies": 3}}
    client, _ = make_client(tmp_path, {
        f"{BASE}/pairwise/1/2/summaryjson": json.dumps(variant),
    })
    result = client.pairwise(1, 2)
    assert result["age_median"] == 100.5
    assert result["ci_low"] == 90
    assert result["ci_high"] == 110
    assert result["study_count"] == 3


def test_pairwise_missing_age_reports_raw_body(tmp_path):
    client, _ = make_client(tmp_path, {
        f"{BASE}/pairwise/1/2/summaryjson": json.dumps({"message": "no data available"}),
    })
    with pytest.raises(ValidationError, match="no divergence time.*no data available"):
        client.pairwise(1, 2)


def test_pairwise_rejects_invalid_ids(tmp_path):
    client, _ = make_client(tmp_path, {})
    with pytest.raises(ValidationError, match="different positive"):
        client.pairwise(3702, 3702)
    with pytest.raises(ValidationError, match="different positive"):
        client.pairwise(0, 3702)


def test_mrca_summaryjson(tmp_path):
    client, session = make_client(tmp_path, {
        f"{BASE}/mrca/id/3702+7227+9606/summaryjson": json.dumps(SUMMARY_3702_9606),
    })
    result = client.mrca([3702, 9606, 7227])
    assert result["taxon_ids"] == [3702, 7227, 9606]
    assert result["age_median"] == 1496.0
    assert session.calls == [f"{BASE}/mrca/id/3702+7227+9606/summaryjson"]


def test_mrca_requires_two_ids(tmp_path):
    client, _ = make_client(tmp_path, {})
    with pytest.raises(ValidationError, match="at least two"):
        client.mrca([3702])


# --- timeline ----------------------------------------------------------------

def test_timeline_csv(tmp_path):
    body = "node,node_name,adjusted_age\n1,cellular organisms,4200\n2,Eukaryota,1800\n"
    client, _ = make_client(tmp_path, {f"{BASE}/timeline/3702": body})
    rows = client.timeline(3702)
    assert len(rows) == 2
    assert rows[0]["node_name"] == "cellular organisms"
    assert rows[1]["adjusted_age"] == "1800"
    assert rows[0]["_queried_at"]


def test_timeline_empty_is_error(tmp_path):
    client, _ = make_client(tmp_path, {f"{BASE}/timeline/3702": ""})
    with pytest.raises(ValidationError, match="no timeline nodes"):
        client.timeline(3702)


# --- caching -------------------------------------------------------------------

def _pairwise_routes() -> dict[str, object]:
    return {f"{BASE}/pairwise/3702/9606/summaryjson": json.dumps(SUMMARY_3702_9606)}


def test_cache_write_and_replay(tmp_path):
    client, session = make_client(tmp_path, _pairwise_routes())
    first = client.pairwise(3702, 9606)
    cache_file = Path(first["cache_file"])
    assert cache_file.parent == tmp_path / "cache"
    record = json.loads(cache_file.read_text())
    assert record["url"] == f"{BASE}/pairwise/3702/9606/summaryjson"
    assert record["retrieved_at"]
    assert json.loads(record["body"])["precomputed_age"] == 1496.0

    second = client.pairwise(3702, 9606)
    assert second["from_cache"] is True
    assert second["age_median"] == 1496.0
    assert len(session.calls) == 1


def test_refresh_forces_new_request(tmp_path):
    client, session = make_client(tmp_path, _pairwise_routes(), refresh=True)
    client.pairwise(3702, 9606)
    client.pairwise(3702, 9606)
    assert len(session.calls) == 2


def test_unreadable_cache_is_clear_error(tmp_path):
    client, _ = make_client(tmp_path, _pairwise_routes())
    url = f"{BASE}/pairwise/3702/9606/summaryjson"
    cache_path = client._cache_path(url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("not json")
    with pytest.raises(ValidationError, match="cache record is unreadable"):
        client.pairwise(3702, 9606)


# --- error paths -----------------------------------------------------------------

def test_timeout_exhausts_retries(tmp_path):
    client, session = make_client(tmp_path, {
        f"{BASE}/pairwise/3702/9606/summaryjson": requests.exceptions.Timeout("slow"),
    }, retries=2)
    with pytest.raises(ValidationError, match="after 2 attempt"):
        client.pairwise(3702, 9606)
    assert len(session.calls) == 2


def test_http_500_retried_then_fails(tmp_path):
    client, session = make_client(tmp_path, {
        f"{BASE}/pairwise/3702/9606/summaryjson": FakeResponse("boom", status=500),
    }, retries=2)
    with pytest.raises(ValidationError, match="HTTP 500"):
        client.pairwise(3702, 9606)
    assert len(session.calls) == 2


def test_http_404_fails_without_retry(tmp_path):
    client, session = make_client(tmp_path, {
        f"{BASE}/pairwise/3702/9606/summaryjson": FakeResponse("missing", status=404),
    }, retries=3)
    with pytest.raises(ValidationError, match="HTTP 404"):
        client.pairwise(3702, 9606)
    assert len(session.calls) == 1


def test_non_json_body_is_error(tmp_path):
    client, _ = make_client(tmp_path, {
        f"{BASE}/pairwise/3702/9606/summaryjson": "<html>oops</html>",
    })
    with pytest.raises(ValidationError, match="non-JSON"):
        client.pairwise(3702, 9606)


def test_client_parameter_validation(tmp_path):
    session = make_session({})
    with pytest.raises(ValidationError, match="timeout"):
        TimeTreeClient(tmp_path, session=session, timeout=0)
    with pytest.raises(ValidationError, match="retries"):
        TimeTreeClient(tmp_path, session=session, retries=0)
    with pytest.raises(ValidationError, match="delay"):
        TimeTreeClient(tmp_path, session=session, delay=-1)


# --- build_calibrations ------------------------------------------------------------

def _calibration_routes() -> dict[str, object]:
    return {
        f"{BASE}/pairwise/3702/9606/summaryjson": json.dumps(SUMMARY_3702_9606),
        f"{BASE}/pairwise/3702/7227/summaryjson": json.dumps(
            {**SUMMARY_3702_9606, "taxon_b_id": 7227, "precomputed_age": 1400.0}),
        f"{BASE}/pairwise/7227/9606/summaryjson": json.dumps(
            {**SUMMARY_3702_9606, "taxon_a_id": 7227, "precomputed_age": 1000.0}),
        f"{BASE}/mrca/id/3702+7227+9606/summaryjson": json.dumps(SUMMARY_3702_9606),
    }


TAXA = [("Arabidopsis thaliana", 3702), ("Homo sapiens", 9606), ("Drosophila melanogaster", 7227)]


def test_build_calibrations_mrca(tmp_path):
    client, _ = make_client(tmp_path, _calibration_routes())
    rows = client.build_calibrations(TAXA)
    assert len(rows) == 1
    row = rows[0]
    assert row["node_label"] == "mrca(3 taxa)"
    assert row["taxa"] == "Arabidopsis thaliana,Homo sapiens,Drosophila melanogaster"
    assert row["taxon_ids"] == "3702,9606,7227"
    assert row["age_median"] == 1496.0
    assert row["ci_low"] == 1350.0
    assert row["ci_high"] == 1650.0
    assert row["study_count"] == 42
    assert row["source"] == "TimeTree v5 (Kumar et al. 2022, MBE)"
    assert row["queried_at"]
    assert row["cache_file"].endswith(".json")


def test_build_calibrations_pairs(tmp_path):
    client, _ = make_client(tmp_path, _calibration_routes())
    rows = client.build_calibrations(TAXA, pairs=True)
    assert [row["node_label"] for row in rows] == [
        "pair(3702,9606)", "pair(3702,7227)", "pair(7227,9606)"]
    assert all(row["source"] == "TimeTree v5 (Kumar et al. 2022, MBE)" for row in rows)


# --- CLI -------------------------------------------------------------------

_QUERY_COMMANDS = {"taxon", "pairwise", "mrca", "timeline", "calibrations"}


def _run_cli(project: Project, *argv: str) -> int:
    """Run a `timetree` query command without request pacing.

    Every HTTP interaction in this module is mocked, so the default 0.5 s
    inter-request pause is pure test latency.
    """
    args = list(argv)
    if len(args) >= 2 and args[0] == "timetree" and args[1] in _QUERY_COMMANDS \
            and "--delay" not in args:
        args[2:2] = ["--delay", "0"]
    return main(["--project", str(project.root), *args])


def _workflow_steps(project: Project) -> list[tuple[str, str]]:
    db = Database(project.db_path, read_only=True)
    try:
        return [
            (row["step"], row["command"])
            for row in db.conn.execute("SELECT step, command FROM workflow_runs")
        ]
    finally:
        db.close()


def test_cli_taxon_single(project, patch_network, capsys):
    patch_network({
        f"{BASE}/taxon/Arabidopsis%20thaliana": json.dumps(
            {"taxon_id": 3702, "scientific_name": "Arabidopsis thaliana", "rank": "species"}),
    })
    assert _run_cli(project, "timetree", "taxon", "--name", "Arabidopsis thaliana") == 0
    out, err = capsys.readouterr()
    assert "3702" in out and "Arabidopsis thaliana" in out
    assert "doi.org/10.1093/molbev/msac174" in err
    steps = _workflow_steps(project)
    assert steps == [("timetree:taxon", "operon timetree taxon --name 'Arabidopsis thaliana'")]


def test_cli_taxon_json_format(project, patch_network, capsys):
    patch_network({
        f"{BASE}/taxon/Apis": json.dumps([
            {"taxon_id": 7460, "scientific_name": "Apis mellifera"},
            {"taxon_id": 7461, "scientific_name": "Apis cerana"},
        ]),
    })
    assert _run_cli(project, "timetree", "taxon", "--name", "Apis", "--format", "json") == 0
    out, _ = capsys.readouterr()
    payload = json.loads(out)
    assert [item["taxon_id"] for item in payload["candidates"]] == [7460, 7461]


def test_cli_taxon_no_result_exit_code(project, patch_network, capsys):
    patch_network({f"{BASE}/taxon/Nothingium": json.dumps({"error": "no match"})})
    assert _run_cli(project, "timetree", "taxon", "--name", "Nothingium") == 2
    _, err = capsys.readouterr()
    assert "no taxon" in err


def test_cli_pairwise_by_ids(project, patch_network, capsys):
    session = patch_network(_pairwise_routes())
    exit_code = _run_cli(project, "timetree", "pairwise",
                         "--taxon-id", "3702", "--taxon-id", "9606")
    assert exit_code == 0
    out, _ = capsys.readouterr()
    assert "1496.0" in out and "1650.0" in out
    assert session.calls == [f"{BASE}/pairwise/3702/9606/summaryjson"]
    steps = _workflow_steps(project)
    assert [step for step, _ in steps] == ["timetree:pairwise"]


def test_cli_pairwise_ambiguous_name_needs_id(project, patch_network, capsys):
    patch_network({
        f"{BASE}/taxon/Apis": json.dumps([
            {"taxon_id": 7460, "scientific_name": "Apis mellifera"},
            {"taxon_id": 7461, "scientific_name": "Apis cerana"},
        ]),
    })
    exit_code = _run_cli(project, "timetree", "pairwise",
                         "--taxon", "Apis", "--taxon-id", "9606")
    assert exit_code == 2
    _, err = capsys.readouterr()
    assert "ambiguous" in err and "--taxon-id" in err


def test_cli_mrca_resolves_names_and_ids(project, patch_network, capsys):
    patch_network({
        f"{BASE}/taxon/Arabidopsis%20thaliana": json.dumps(
            {"taxon_id": 3702, "scientific_name": "Arabidopsis thaliana"}),
        f"{BASE}/mrca/id/3702+7227+9606/summaryjson": json.dumps(SUMMARY_3702_9606),
    })
    exit_code = _run_cli(project, "timetree", "mrca",
                         "--taxa", "Arabidopsis thaliana",
                         "--taxon-id", "9606", "--taxon-id", "7227")
    assert exit_code == 0
    out, _ = capsys.readouterr()
    assert "3702,7227,9606" in out


def test_cli_mrca_by_ids(project, patch_network, capsys):
    patch_network({
        f"{BASE}/mrca/id/3702+7227+9606/summaryjson": json.dumps(SUMMARY_3702_9606),
    })
    exit_code = _run_cli(project, "timetree", "mrca",
                         "--taxon-id", "3702", "--taxon-id", "9606", "--taxon-id", "7227")
    assert exit_code == 0
    out, _ = capsys.readouterr()
    assert "3702,7227,9606" in out


def test_cli_timeline(project, patch_network, capsys):
    patch_network({
        f"{BASE}/timeline/3702": "node,node_name,adjusted_age\n1,cellular organisms,4200\n",
    })
    assert _run_cli(project, "timetree", "timeline", "--taxon-id", "3702") == 0
    out, _ = capsys.readouterr()
    assert "cellular organisms" in out
    steps = _workflow_steps(project)
    assert [step for step, _ in steps] == ["timetree:timeline"]


def test_cli_calibrations_writes_tsv(project, patch_network, capsys, tmp_path):
    patch_network(_calibration_routes())
    out_path = tmp_path / "calibrations.tsv"
    exit_code = _run_cli(project, "timetree", "calibrations",
                         "--taxon-id", "3702", "--taxon-id", "9606", "--taxon-id", "7227",
                         "--out", str(out_path))
    assert exit_code == 0
    out, err = capsys.readouterr()
    lines = out_path.read_text().splitlines()
    header = lines[0].split("\t")
    assert header == timetree.CALIBRATION_COLUMNS
    values = dict(zip(header, lines[1].split("\t")))
    assert values["node_label"] == "mrca(3 taxa)"
    assert values["taxon_ids"] == "3702,9606,7227"
    assert values["source"] == "TimeTree v5 (Kumar et al. 2022, MBE)"
    assert values["age_median"] == "1496.0"
    assert values["cache_file"].endswith(".json")
    assert "adopt" in err and "doi.org/10.1093/molbev/msac174" in err
    assert "mrca(3 taxa)" in out
    steps = _workflow_steps(project)
    assert [step for step, _ in steps] == ["timetree:calibrations"]


def test_cli_calibrations_pairs(project, patch_network, capsys, tmp_path):
    patch_network(_calibration_routes())
    out_path = tmp_path / "pairs.tsv"
    exit_code = _run_cli(project, "timetree", "calibrations", "--pairs",
                         "--taxon-id", "3702", "--taxon-id", "9606", "--taxon-id", "7227",
                         "--out", str(out_path))
    assert exit_code == 0
    capsys.readouterr()
    lines = out_path.read_text().splitlines()
    assert len(lines) == 4


def test_cli_uses_cache_on_second_run(project, patch_network, capsys):
    session = patch_network(_pairwise_routes())
    assert _run_cli(project, "timetree", "pairwise",
                    "--taxon-id", "3702", "--taxon-id", "9606") == 0
    assert _run_cli(project, "timetree", "pairwise",
                    "--taxon-id", "3702", "--taxon-id", "9606") == 0
    capsys.readouterr()
    assert len(session.calls) == 1
    cache_files = list((project.root / "adapters_cache" / "timetree").glob("*.json"))
    assert len(cache_files) == 1
    assert _run_cli(project, "timetree", "pairwise",
                    "--taxon-id", "3702", "--taxon-id", "9606", "--refresh") == 0
    assert len(session.calls) == 2


def test_cli_network_failure_exit_code(project, patch_network, capsys):
    patch_network({
        f"{BASE}/pairwise/3702/9606/summaryjson": requests.exceptions.ConnectionError("down"),
    })
    exit_code = _run_cli(project, "timetree", "pairwise",
                         "--taxon-id", "3702", "--taxon-id", "9606", "--delay", "0")
    assert exit_code == 2
    _, err = capsys.readouterr()
    assert "failed" in err
