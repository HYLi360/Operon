# Runnable toy plugin examples

Two standard-library samples live in `examples/plugins/`: `toy_analysis.py`
produces copied FASTA, external QC TSV, events and optional hits JSON;
`toy_viz.py` checks bundle hashes and renders a small SVG. They import no Operon
code and are not separately published extensions. These are intentionally toy
measurements, without biological thresholds or decisions.

From a source checkout with the project environment active:

```bash
operon init-demo /tmp/operon-plugin-demo
operon --project /tmp/operon-plugin-demo query \
  "SELECT file_id, entity_type, entity_id, relative_path FROM files WHERE entity_type='assembly' ORDER BY file_id LIMIT 1"
```

Use that row's `FILE_ID`, `ENTITY_ID` and absolute `INPUT_PATH` below. A
producer can consume an export's `manifest.tsv` in the same way.

```bash
python examples/plugins/toy_analysis.py --input INPUT_PATH \
  --out /tmp/operon-plugin-demo/analysis/toy.json \
  --file-id FILE_ID --entity-type assembly --entity-id ENTITY_ID
operon --project /tmp/operon-plugin-demo run-external --step toy \
  --entity-type assembly --entity-id ENTITY_ID --input INPUT_PATH \
  --events analysis/toy.json.events.jsonl --expected-output analysis/toy.json \
  --command 'python /ABSOLUTE/OPERON/examples/plugins/toy_analysis.py --input INPUT_PATH --out /tmp/operon-plugin-demo/analysis/toy.json --file-id FILE_ID --entity-type assembly --entity-id ENTITY_ID'
```

The second command prints its `RUN_ID`. Import facts and explicitly adopt the
artifact draft; external QC TSV also works without the events channel:

```bash
operon --project /tmp/operon-plugin-demo import-events --run RUN_ID \
  --file /tmp/operon-plugin-demo/analysis/toy.json.events.jsonl --dry-run
operon --project /tmp/operon-plugin-demo import-events --run RUN_ID \
  --file /tmp/operon-plugin-demo/analysis/toy.json.events.jsonl --out /tmp/toy-adopt.json
operon --project /tmp/operon-plugin-demo adopt --from-manifest /tmp/toy-adopt.json
operon --project /tmp/operon-plugin-demo import-qc \
  --file /tmp/operon-plugin-demo/analysis/toy.json.qc.tsv
operon --project /tmp/operon-plugin-demo report view --out /tmp/toy-view
python examples/plugins/toy_viz.py --bundle /tmp/toy-view --out /tmp/toy-view.svg
```

For recipes, create an additive preset fragment (substitute absolute interpreter
and script paths):

```yaml
version: 1
tools:
  toy_analysis:
    executable: /ABSOLUTE/PYTHON
    run_method: ""
    version_args: [/ABSOLUTE/OPERON/examples/plugins/toy_analysis.py, --version]
    recipes:
      toy_analysis:
        version: 1
        entity_type: assembly
        file_role: genome_fasta
        format: fasta
        output_suffix: .json
        result_parser: plugin:toy-analysis
        arguments:
          - /ABSOLUTE/OPERON/examples/plugins/toy_analysis.py
          - --input
          - ${input}
          - --out
          - ${output}
          - --file-id
          - ${file_id}
          - --entity-type
          - ${entity_type}
          - --entity-id
          - ${entity_id}
          - --events
          - ${output}.${file_id}.events.jsonl
```

```bash
operon --project /tmp/operon-plugin-demo tools add-preset --file toy-preset.yaml --dry-run
operon --project /tmp/operon-plugin-demo tools add-preset --file toy-preset.yaml
operon --project /tmp/operon-plugin-demo analyze --analysis toy_analysis --limit 1 \
  --events '${output}.${file_id}.events.jsonl'
operon --project /tmp/operon-plugin-demo query \
  "SELECT job_id, file_id, workflow_run_id FROM analysis_jobs WHERE analysis_name='toy_analysis' ORDER BY job_id DESC LIMIT 1"
python examples/plugins/toy_analysis.py --input INPUT_PATH --out /tmp/toy-hits.json \
  --file-id FILE_ID --entity-type assembly --entity-id ENTITY_ID --job-id JOB_ID
operon --project /tmp/operon-plugin-demo import-hits --file /tmp/toy-hits.json --dry-run
operon --project /tmp/operon-plugin-demo import-hits --file /tmp/toy-hits.json
```

For built-in measurements, the producer can call `operon qc-measure` as a
separate command using manifest SHA-256 and size, then feed that unchanged JSON
to `import-qc`. A third-party producer keeps its own name in external QC TSV or
metric events; it never labels its measurements `operon.builtin`.

`tests/regression/test_plugin_contract.py` executes these file loops on fresh
demo projects, including recipe sequential/array dispatch, linked provenance,
identity preservation, reimport rules and checksum refusal. The samples add no
runtime dependency and contain no direct SQLite writes.
