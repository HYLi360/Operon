# 可运行 toy 插件示例

`examples/plugins/` 提供两个仅用标准库的样板：`toy_analysis.py` 生成复制 FASTA、
外部 QC TSV、events 和可选 hits JSON；`toy_viz.py` 验证 bundle 哈希后绘制 SVG。
它们不导入 Operon、不单独发布扩展，也没有生物学阈值或判定。

以下命令在激活项目环境的源码 checkout 中运行：

```bash
operon init-demo /tmp/operon-plugin-demo
operon --project /tmp/operon-plugin-demo query --sql \
  "SELECT file_id, entity_type, entity_id, relative_path FROM files WHERE entity_type='assembly' ORDER BY file_id LIMIT 1"
```

用查询结果替换 `FILE_ID`、`ENTITY_ID`、绝对 `INPUT_PATH`；生产者也可从 export
的 `manifest.tsv` 取相同身份。完整命令如下，第二步会打印 `RUN_ID`：

```bash
python examples/plugins/toy_analysis.py --input INPUT_PATH \
  --out /tmp/operon-plugin-demo/analysis/toy.json \
  --file-id FILE_ID --entity-type assembly --entity-id ENTITY_ID
operon --project /tmp/operon-plugin-demo run-external --step toy \
  --entity-type assembly --entity-id ENTITY_ID --input INPUT_PATH \
  --events analysis/toy.json.events.jsonl --expected-output analysis/toy.json \
  --command 'python /ABSOLUTE/OPERON/examples/plugins/toy_analysis.py --input INPUT_PATH --out /tmp/operon-plugin-demo/analysis/toy.json --file-id FILE_ID --entity-type assembly --entity-id ENTITY_ID'
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

recipe 接入使用增量片段（替换 Python 和脚本的绝对路径）：

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
operon --project /tmp/operon-plugin-demo query --sql \
  "SELECT job_id, file_id, workflow_run_id FROM analysis_jobs WHERE analysis_name='toy_analysis' ORDER BY job_id DESC LIMIT 1"
python examples/plugins/toy_analysis.py --input INPUT_PATH --out /tmp/toy-hits.json \
  --file-id FILE_ID --entity-type assembly --entity-id ENTITY_ID --job-id JOB_ID
operon --project /tmp/operon-plugin-demo import-hits --file /tmp/toy-hits.json --dry-run
operon --project /tmp/operon-plugin-demo import-hits --file /tmp/toy-hits.json
```

内置测量可另行调用 `operon qc-measure`，传入清单 SHA-256、size，保持其 JSON
不变供 `import-qc` 使用。第三方测量保留自己的工具名，输出外部 TSV 或 metric
事件，不标成 `operon.builtin`。

`tests/regression/test_plugin_contract.py` 在新 demo 上实跑上述文件闭环，覆盖
recipe 顺序/array 分派、provenance、身份、重导入和篡改哈希拒绝。样板不新增
运行时依赖，也不直接写 SQLite。
