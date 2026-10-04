# 插件文件契约

分析插件是独立 CLI 程序，由 recipe 或 `run-external` 以子进程调用；不导入
Operon、不直接写 SQLite。可视化插件读取 `report view` bundle。内置解析器和
软件预设继续可用；进程内 hook 与 `operon.api` 暂缓。插件沿用 Operon 的开发者
和 BSD-2-Clause 许可证，发行名与托管位置不在此指定。

## 版本与验证

measurement、hits、events 与 bundle 的初始版本均为整数 `1`，独立于程序
{{ operon_version }}、数据库 {{ db_schema }} 和元数据 {{ metadata_schema }}。
未知载荷版本在任何写入前报错。JSON Schema 随包发布在 `operon/contracts/`，
测量格式在 `operon/qc/measure.schema.json`；不新增校验依赖。

未来 bundle 的成员或字段增加须记录演进说明；消费者按列名读取、可忽略增加项。
删成员、改列名或改语义须提升整数主版本并提供迁移说明。当前生产者仅输出 `1`，
当前导入器不承诺接受未来版本。第三方直读 SQLite 的兼容窗口仍未承诺。

## 输入、产物、日志与退出码

输入来自 `operon export`：`manifest.tsv` 用 `file_id + sha256 + size_bytes`
标识物化文件，另有 `checksums.sha256`、`qc.tsv`、`provenance.json`。路径仅定位字节。
选择方法见[外部分析指南](../guides/external-analysis.md)。插件仅在产物完整后返回
0，非零为失败；用 `--expected-output` 或 recipe 字段声明产物。Operon 捕获
`logs/<run_id>.stdout.log` / `.stderr.log`，在 `workflow_runs` 登记命令、后端、
版本、输入与状态。

adopt 清单是 JSON list，每项必有 `path`、`entity_type`、`entity_id`、`role`、
`derived_from`（非空 file ID list）；可选 `format`、`compression`、
`workflow_run_id`。通过 `operon adopt --from-manifest PATH` 显式采纳；同字节幂等，
同实体/role 的不同字节冲突。事件只产生清单草稿，不自动采纳。

## 测量载荷

`qc-measure` 保留[文件与 QC 命令](cli-files-qc.md)所述格式，`tool` 必为
`operon.builtin`，不冒充第三方工具。必需 `schema_version`、`tool`、
`tool_version`、`parameter_set`、`file`、`metrics`；文件含 `sha256` 和
`size_bytes`，可用 `file_id` 消歧。每项指标含 `qc_stage`、`metric_name`、
`metric_value`，可选 `metric_numeric`、`metric_unit`、`parameter_set`。
可选 `sequences` 是序列 ID 到长度的映射。

独立分析程序输出 `import-qc` 的外部 TSV 或 metric 事件，保留自己的工具名和
版本；需要内置测量时可另行调用 `operon qc-measure`。Schema 描述已有输出，
不改变 `import-qc` 的接受范围或输出。

## hits 载荷与预览

`operon import-hits --file hits.json [--dry-run]` 读取 JSON object：

```json
{
  "schema_version": 1,
  "job_id": 42,
  "file": {"file_id": "FIL_000001", "sha256": "<64 hex digits>", "size_bytes": 120},
  "results": [{"metric_name": "query_count", "metric_value": "1", "metric_numeric": 1}],
  "hits": [{"query_id": "q1", "subject_id": "s1", "hit_rank": 1,
            "metric_name": "evalue", "metric_value": "0.001", "metric_numeric": 0.001}],
  "alignments": [{"query_id": "q1", "subject_id": "s1", "hit_rank": 1,
                  "query_start": 1, "query_end": 12, "subject_start": 2, "subject_end": 13}]
}
```

三个数组必需且可为空，分别写入 `analysis_results`、`analysis_hits`、
`analysis_alignments`。既有 completed `analysis_jobs` 提供实体、分析名、工具、
参数与父 workflow；输入哈希和三段文件身份必须与清单一致。`analyze` 完成后可用
`operon query --sql 'SELECT job_id FROM analysis_jobs ...'` 取得 job ID。

rank 是插件提供的每 query 正整数；summary 键为 metric_name，hit 键为
`(query_id, hit_rank, metric_name)`，alignment 键为 `(query_id, hit_rank)`，
均须唯一，同 query/rank 的 subject 必须一致。坐标可选，为 1-based inclusive
正整数；反向比对可 start 大于 end。可选数值字段 `evalue`、`bitscore`、
`percent_identity`，可选 `extra` object 存为规范 JSON；数值须有限。

导入只保存提供的数据，不重排 rank、不派生 summary、不写 QC、不改实体状态。
完全相同的重导入连审计也为 no-op，既有 job 的不同证据冲突，需重新运行生成新 job。
事务内写 `changes` 和以产出 run 为父的 `import-hits` workflow。预览以只读连接
执行相同校验和冲突检查。

recipe 可用 `result_parser: plugin:<name>` 表示独立程序负责解析、随后显式导入
载荷。执行和产物检查仍由 Operon 完成。内置名称行为保留；`plugin:` 命名空间以外
的未知名称仍报错。

## 预设片段

`operon tools add-preset --file preset.yaml [--dry-run]` 导入带 `version: 1`
和 `tools` mapping 的声明式片段，不替换项目 defaults、不删除内置预设。允许
新工具，或为配置完全相同的既有工具添加新 recipe。相同定义 no-op，不同工具
字段或既有 recipe 冲突。recipe 名在全项目唯一，工具/recipe 名须安全。

每个受影响 recipe 经既有 loader 校验，留下 `{"recipe": ..., "tool": ...}`
内容寻址快照；原子替换、往返校验，失败恢复原字节。dry-run 不写配置或数据库。
该命令不导入 Python 模块、不安装插件。

## 事件 JSONL

每个非空行是 JSON object，含整数 `schema_version: 1`、非空 `event_id` 和
`type`。event ID 在产出 run 内唯一，为不透明字符串。事件只报告测量/产物事实，
不携带 verdict、state 或 decision。独立 Schema 在
`operon/contracts/events.schema.json`。

metric 事件的 `metric` 含 `entity_type`、`entity_id`、`qc_stage`、`metric_name`、
`metric_value`、`tool`、`tool_version`、`parameter_set`，可选 `metric_numeric`、
`metric_unit`、`file`（完整三段身份）。artifact 事件的 `artifact` 为上述 adopt
item，导入器补产出 `workflow_run_id`，拒绝冲突的 run。未知类型跳过计数，仍须
有效 envelope 和支持的版本。任一已知事件或后续行无效则全批在写入前拒绝。

`run-external --events PATH` 与 `analyze --events PATH` 把路径登记为
`execution_details.events_path` 和 expected output，SSH 随其他产物拉回。
多输入分析模板须含 `${file_id}`，也支持 `${output}`。参数仅登记路径，不替插件
增加命令参数，也不自动导入。请求事件使用独立缓存指纹，执行新任务而不复用
未登记的文件。

`operon import-events --run RUN_ID --file events.jsonl [--out adopt.json]
[--dry-run]` 校验产出 run，以 QC core 写 metric；artifact 转为 JSON list 草稿，
默认 `analysis/event-drafts/<run_id>.json`。去重键 `(run_id, event_id)` 保存在
该 run 的 `execution_details`：同内容 no-op，同 ID 不同内容冲突。ledger 保留
规范哈希及 artifact 草稿，可重建相同草稿。指标、ledger、子 workflow 与审计
同事务提交，草稿发布失败恢复原字节；不自动 adopt 或 evaluate。

## 只读 view bundle

`operon report view --out DIR` 用只读连接生成确定性快照，不新增 workflow/audit。
目标须不存在或已是完全相同 bundle，不同内容冲突。完整 staging 才 rename，
失败不留半成品。

| 成员 | 内容 |
| --- | --- |
| `manifest.json` | `bundle_schema_version`、各成员 SHA-256 与行数；无时钟/绝对项目路径 |
| `entities.tsv` | 实体身份、状态、有效退役信息 |
| `files.tsv` | 文件清单 |
| `qc_wide.tsv` | 共享 `qc_wide`，默认仅活跃实体 |
| `decisions.tsv` | 当前判定 |
| `analysis_results.tsv`、`analysis_hits.tsv`、`analysis_alignments.tsv` | 已存证据 |
| `lineage.tsv` | 文件谱系 |
| `coverage/reports.tsv`、`coverage/metrics.tsv` | 已存覆盖率证据，不触发计算 |

除 QC wide 外包含已存退役证据，可按 entities 的 retirement 列筛选。成员排序
确定，TSV 正确转义；`qc_wide.tsv` 与 `report qc --wide --format tsv` 字节一致。
消费者验证哈希再读取，不反写数据库；可视化依赖只属于插件发行版。
可运行示例随实现提供。

metric 事件的 parameter_set 添加稳定 `:events:<run/event hash>` 后缀，保留
不同事件的独立归属。分析 sidecar 须为 `analysis/` 下独立路径。

可实跑[独立 toy 插件文件闭环](plugin-examples.md)。

结构性 ID、大小、rank 和坐标须在 SQLite 有符号 64-bit 整数范围内；数值证据
存为有限的 SQLite REAL 值。

插件 JSON/JSONL 解码拒绝非有限数值，包括 `NaN`、`Infinity` 和超过有限 REAL
范围的十进制指数。
