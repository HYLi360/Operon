"""A standalone standard-library producer; demonstration only, no Operon import."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--version", action="version", version="toy-analysis 1")
for name in ("input", "out", "file-id", "entity-type", "entity-id"):
    parser.add_argument(f"--{name}", required=True)
parser.add_argument("--events")
parser.add_argument("--job-id", type=int)
args = parser.parse_args()
source, out = Path(args.input), Path(args.out)
data = source.read_bytes()
identity = {
    "file_id": args.file_id,
    "sha256": hashlib.sha256(data).hexdigest(),
    "size_bytes": len(data),
}
count = sum(line.startswith(b">") for line in data.splitlines())
metric = {
    "entity_type": args.entity_type,
    "entity_id": args.entity_id,
    "qc_stage": "toy",
    "metric_name": "toy_sequence_count",
    "metric_value": str(count),
    "metric_numeric": count,
    "tool": "toy-analysis",
    "tool_version": "1",
    "parameter_set": "toy-v1",
    "file": identity,
}
out.parent.mkdir(parents=True, exist_ok=True)
derived = out.with_suffix(".fasta")
derived.write_bytes(data)
artifact = {
    "path": str(derived.absolute()),
    "entity_type": args.entity_type,
    "entity_id": args.entity_id,
    "role": "toy_copy",
    "format": "fasta",
    "derived_from": [args.file_id],
}
events = [
    {"schema_version": 1, "event_id": "count", "type": "metric", "metric": metric},
    {"schema_version": 1, "event_id": "copy", "type": "artifact", "artifact": artifact},
]
event_path = Path(args.events or (str(out) + ".events.jsonl"))
event_path.parent.mkdir(parents=True, exist_ok=True)
event_path.write_text(
    "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"
)
columns = [
    "entity_type",
    "entity_id",
    "file_id",
    "qc_stage",
    "metric_name",
    "metric_value",
    "tool",
    "tool_version",
    "parameter_set",
]
with open(str(out) + ".qc.tsv", "w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, columns, delimiter="\t")
    writer.writeheader()
    writer.writerow(
        {key: (args.file_id if key == "file_id" else metric[key]) for key in columns}
    )
hits, alignments = [], []
if count:
    first = data.split(b">", 2)[1].splitlines()
    seqid = first[0].split()[0].decode("utf-8")
    length = sum(len(line.strip()) for line in first[1:])
    hits = [
        {
            "query_id": seqid,
            "subject_id": seqid,
            "hit_rank": 1,
            "metric_name": "copied_length",
            "metric_value": str(length),
            "metric_numeric": length,
        }
    ]
    if length:
        alignments = [
            {
                "query_id": seqid,
                "subject_id": seqid,
                "hit_rank": 1,
                "query_start": 1,
                "query_end": length,
                "subject_start": 1,
                "subject_end": length,
            }
        ]
payload = {
    "schema_version": 1,
    "file": identity,
    "results": [
        {
            "metric_name": "toy_sequence_count",
            "metric_value": str(count),
            "metric_numeric": count,
        }
    ],
    "hits": hits,
    "alignments": alignments,
}
if args.job_id is not None:
    payload["job_id"] = args.job_id
out.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
print(f"measured {count} toy sequences; output {out}")
