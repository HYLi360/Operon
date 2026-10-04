"""Guard the published measurement Schema without changing import-qc."""

import copy
import json

import pytest

from operon.contracts import read_json, validate_payload
from operon.errors import ValidationError
from operon.qc.measure import measure_file
from operon.utils import sha256_file


@pytest.fixture
def payload(tmp_path):
    source = tmp_path / "input.fa"
    source.write_text(">q1\nACGT\n")
    return measure_file(
        source,
        file_format="fasta",
        file_role="genome_fasta",
        sha256=sha256_file(source),
        size_bytes=source.stat().st_size,
        file_id="FIL_000001",
    )


def test_measurement_output_satisfies_schema(payload):
    before = json.dumps(payload, sort_keys=True)
    validate_payload(payload, "measure")
    assert json.dumps(payload, sort_keys=True) == before
    validate_payload(
        {**payload, "file": {**payload["file"], "file_id": None}}, "measure"
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(schema_version=2),
        lambda p: p.update(schema_version=True),
        lambda p: p.pop("metrics"),
        lambda p: p.update(tool="toy"),
        lambda p: p["file"].update(sha256="invalid"),
        lambda p: p["file"].update(size_bytes=-1),
        lambda p: p["metrics"][0].pop("metric_name"),
        lambda p: p["metrics"][0].update(metric_numeric=float("nan")),
        lambda p: p.update(extra="unknown"),
        lambda p: p.update(tool_version=""),
        lambda p: p.update(metrics={}),
        lambda p: p.update(sequences={"q1": -1}),
    ],
)
def test_schema_rejects_invalid_payload(payload, change):
    candidate = copy.deepcopy(payload)
    change(candidate)
    with pytest.raises(ValidationError):
        validate_payload(candidate, "measure")


def test_json_and_contract_errors(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{")
    with pytest.raises(ValidationError, match="invalid JSON"):
        read_json(path)
    with pytest.raises(ValidationError, match="unknown file contract"):
        validate_payload({}, "absent")
