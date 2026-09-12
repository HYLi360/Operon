# 数据模型

## 核心实体

```text
organisms (ORG_)
    └── samples (SMP_)
            ├── runs (RUN_)         测序 run，产生 reads
            └── assemblies (ASM_)   组装版本
                    └── annotations (ANN_)   注释版本
                            ├── GFF3
                            ├── CDS FASTA
                            └── protein FASTA
```

外部 accession 放在独立的 `accessions` 表中，且从不作为实体的主键：

```text
internal_type   internal_id    namespace        accession         version   is_primary
assembly        ASM_000001     NCBI_Assembly    GCA_000000001     1         1
sample          SMP_000001     NCBI_BioSample   SAMN0000001       1         1
```

该表自身的主键是 `(namespace, accession)`，因此一个 accession 至多映射到一个内部实体；
当一个实体有多个 accession 时，`is_primary` 标记其中的主 accession。

## files：文件清单

`files` 是归档文件的 manifest。关键字段：

```text
file_id, entity_type, entity_id, file_role, format, compression,
relative_path, source_url, size_bytes, sha256, downloaded_at, status
```

文件身份由 `file_id + sha256 + size_bytes` 定义。`relative_path` 只表示文件当前位于项目中的位置。

## qc_results：QC 长表

内置 QC 和外部 QC 都写入同一张长表。当前版本每条结果额外绑定：

```text
file_id        该指标对应的 manifest 文件（可为空）
file_sha256    该输入文件的 SHA-256（可为空）
input_identity 唯一输入标识：
               file:{file_id}:{sha256} 或 entity:{entity_type}:{entity_id}
               读取关联输入的内置 annotation QC 使用 input-set:v1:{sha256}
```

`input-set:v1` 的摘要由主 GFF3、assembly FASTA 和实际读取的 protein FASTA 的
`kind + file_id + sha256 + size_bytes` 规范化后计算。校验缓存命中状态和长度索引路径不
参与身份，因此同一组内容无论首次构建还是后续命中都 upsert 到同一结果；任一关联
文件内容身份变化则生成新的 QC 输入身份，旧结果仍保留。assembly 长度索引作为可重建
派生物，除绑定上述 manifest 内容身份外，还校验索引行自身的 SHA-256 摘要。

唯一约束为：

```text
(input_identity, qc_stage, metric_name, tool, tool_version, parameter_set)
```

这保证同一实体的 R1、R2、GFF3、蛋白 FASTA 等不同输入文件的同名指标不会互相覆盖。查询 `latest_metrics()` 时，对 `file_exists`、`sha256_match`、`parseable`、`paired_read_count_match` 这些“任一文件失败即失败”的指标取多个输入中的最小值（保守值）。

外部分析的不同 recipe/运行参数以不同 `qc_stage` 和 `parameter_set` 共存。例如固定
BUSCO lineage 使用 `analysis:busco_lineage:lineage_dataset=<name>`。长表完整保留这些
结果；宽表因每个 metric 只能有一列，仅提供最近值的浏览视图。规则引擎可通过
`source.qc_stage` 只读取指定 stage，避免正式判定被另一个分析变体的“最新值”改变。

## qc_profiles 与 decisions：可追溯判定

规则引擎每次 `evaluate` 都会：

1. 对 YAML profile 内容做规范化 JSON 序列化并计算 SHA-256；
2. 把 profile 快照写入 `qc_profiles`（同名同版本同内容去重）；
3. 把新的自动判定**追加**到 `decisions`，不覆盖旧判定；
4. `current_decisions` 视图返回每个 `(entity_type, entity_id, profile)` 的最新一条 decision。

因此修改 profile 阈值后重新 evaluate 会形成新的 decision 历史，release 和 `report decisions` 默认读取 `current_decisions`；需要表格快照时使用对应的 `report` 子命令。

规则的阈值既可由标量 `value` 给出，也可通过 `value_by` 根据同一来源中的另一个指标
选择。例如 BUSCO complete 门限由 `busco_lineage_dataset` 映射。selector 未出现在映射
中时，profile 显式规定 `warning`、`fail` 或 `ignore`；缺省按缺少可用门限处理
（`NOT_EVALUATED`），`ignore` 会在 decision 的 reason_codes 中留下持久化痕迹；
没有隐式分类回退。

## 其他系统表

下表中除标注“(view)”的条目外都是 `CREATE TABLE` 定义；`current_entity_lifecycle` 与
`effective_retired_entities` 是 `CREATE VIEW` 定义。

| 表 | 用途 |
|---|---|
| `entity_state` | 实体级状态机，含数据库 schema 标记行 |
| `id_counters` | 按实体类型分配的 ID 计数器（`entity_type` 主键、`next_number`），支撑稳定的 `ORG_`/`SMP_`/`RUN_`/`ASM_`/`ANN_`/`FIL_` 标识；ID 在写锁下从计数器预留，因此失败插入后会留下空号 |
| `workflow_runs` | 结构化运行记录（与 `logs/workflow.jsonl` 对应），含 executor、scheduler job ID、执行详情与资源使用列（`duration_seconds`、`max_rss_mb`、`avg_rss_mb`、`cpu_seconds`；采集不到留 NULL，不影响任务判定） |
| `execution_environments` | 内容寻址的执行环境文档（hostname、OS/kernel、Python/operon 版本、相关环境变量、docker 探测）；`workflow_runs.environment_id` 与 `analysis_jobs.environment_id` 指向它，但两列都没有声明外键，引用由应用层维护 |
| `file_lineage` | 派生文件到输入文件的谱系边（`derived_file_id`、`input_file_id`、可选 `workflow_run_id`、`created_at`），由 `operon adopt` 与 `operon fanout` 写入；`derived_file_id` 声明了指向 `files(file_id)` 的外键，`input_file_id` 没有；`UNIQUE(derived_file_id, input_file_id)` 使重复 adopt 幂等 |
| `data_sources` | 外部数据库/仓库、提供者、记录 URL、引用文献、License 与规范化内容身份；`source_type` 仅允许 `insdc`/`non_insdc`，且非 INSDC 行必须同时提供 citation 与 License 名称（CHECK 约束） |
| `source_links` | 来源与 organism/sample/run/assembly/annotation/file 的多对多关联及导入 provenance |
| `schema_migrations` | 已应用数据库迁移的稳定 ID、脚本身份和应用时间 |
| `adapter_run_items` | 可恢复 adapter 的 accession/item 级状态、尝试、错误与结果 write-set；`status` 仅允许 `pending`、`downloading`、`completed`、`skipped`、`failed`、`interrupted` |
| `ncbi_assembly_records` | GCA/GCF 来源记录到稳定 `ASM_` 的映射、canonical 标记及来源文件指针 |
| `ncbi_annotation_records` | 来源 accession/provider/version/date 规范化得到的 annotation 身份 |
| `entity_supersessions` | 不删除旧行的逻辑替代关系及 repair provenance |
| `entity_lifecycle_events` | 实体的 `RETIRE`/`RESTORE` 历史、原因、证据、操作者、workflow 与反向事件指针；`object_type` 仅允许 `organism`、`sample`、`run`、`assembly`、`annotation`，`action` 仅允许 `RETIRE`/`RESTORE`。该历史按约定保持 append-only：schema 未声明任何触发器，因此直接 SQL 写入不会被阻止 |
| `current_entity_lifecycle`（view） | 每个实体最新直接生命周期事件；只表达该实体自身，不传播祖先状态 |
| `effective_retired_entities`（view） | 当前有效退役集合；沿 organism → sample → run/assembly → annotation 传播，并保留根退役事件身份 |
| `file_locations` | `file_id` 在各远程镜像上的 URI、身份副本、可用状态与最近校验时间；可由远端清单重建 |
| `local_file_verifications` | 最近一次完整本地 SHA-256 通过时的 stat 指纹；仅为可重建的 QC 加速缓存，不改变 manifest 文件身份 |
| `releases` / `release_members` | release 元数据与成员文件清单 |
| `analysis_jobs` | 外部分析作业：命令、版本、参数指纹、输入/数据库指纹、输出 checksum、缓存状态；completed cache 由部分唯一索引 `idx_analysis_jobs_completed_cache` 表达，键为 `(analysis_name, file_id, parameter_sha256, input_sha256, database_identity)` 且带 `WHERE status='completed'`，因此 superseded 或失败的行永远不会满足缓存查找；`recipe_snapshot_id` 回指产生该作业的 recipe 快照 |
| `recipe_snapshots` | 内容寻址的 recipe 快照（recipe 原文 + 引用 tool spec 原文的规范化 JSON 及其 SHA-256），`UNIQUE(recipe_name, recipe_version, recipe_sha256)` 去重；由 `analyze` 记录 |
| `analysis_results` / `analysis_hits` | 同步到数据库的分析汇总指标与 top hits 长表 |
| `sequences` | 每条 FASTA 记录一行（`file_id`、`file_sha256`、`entity_type`、`entity_id`、`seqid`、`length`；`UNIQUE(file_id, seqid)`），由内置 FASTA QC 填充（annotation QC 还会同步其 assembly 的序列），`import-qc` 导入 `qc-measure` payload 时也会写入；支撑 `show` 与只读 SQL 的 seqid 反查 |
| `analysis_alignments` | 已完成分析作业解析出的每条比对命中各占一行（job/实体/文件身份、`query_id`、`subject_id`、`hit_rank`、query/subject 区间、`evalue`、`bitscore`、`percent_identity`、`extra_json`）；全量写入，不受 `max_hits_per_query` 截断，`report analysis --hits` 读取该表 |
| `sequence_labels` | 逐序列的分类标签（`file_id`、`seqid`、`label`、`profile_name`、`profile_sha256`、`details_json`、`decided_at`；主键 `file_id + seqid + profile_name`），由 `operon classify-sequences` 依据版本化的 `sequence_classification` profile 写入；每次标签变更都在 `changes` 中留有审计 |
| `taxonomy_snapshots` | NCBI Taxonomy 版本、来源 manifest 身份、节点数与导入状态 |
| `taxonomy_nodes` / `taxonomy_aliases` | 冻结的分类树节点与 secondary/merged TaxID 映射 |
| `taxonomy_reference_sets` | coverage profile 与 taxonomy 版本编译出的分母 TSV 身份和各 rank 行数 |
| `coverage_reports` / `coverage_report_metrics` | 不可变输入身份对应的覆盖率报告历史与 family/genus 指标 |
| `changes` | 人工修改审计日志 |

## metadata schema 层

上表通过版本化的项目 metadata schema 载入：项目初始化时把 `default_schemas()`
（`operon/schema.py`）写入 `config/schemas.yaml`，`Schema` 载入该文档并以其中的
`schema_version` 作为 metadata schema 标记。内置契约版本是 `operon/schema.py` 中的
`METADATA_SCHEMA_VERSION`，当前版本为 {{ metadata_schema }}。

- **表契约。** `default_schemas()` 为每张表（`organisms`、`samples`、`runs`、
  `assemblies`、`annotations`、`accessions`、`files`）声明 TSV `file`、`primary_key`、
  可选 `unique` 约束，以及逐字段契约：`type`、`required`、`pattern`、`allowed` 与
  `min`/`max`。
- **内部 ID 模式。** 内部 ID 匹配 `^ORG_\d{6}$`、`^SMP_\d{6}$`、`^RUN_\d{6}$`、
  `^ASM_\d{6}$`、`^ANN_\d{6}$` 与 `^FIL_\d{6}$`；taxonomy 快照使用 `TAX_` 前缀，
  `files.entity_id` 接受 `(ORG|SMP|RUN|ASM|ANN|TAX)_\d{6}`。ID 来自 `id_counters`，
  从不来自外部 accession。
- **受控词汇表。** 声明了 `allowed` 的字段会拒绝其他任何取值，例如
  `taxonomy_source`（`NCBI`/`GTDB`/`other`）、`sex`、`library_strategy`、
  `library_source`、`library_layout`、`platform`、`assembly_level`、
  `reference_status`、`source_database`、`accessions.internal_type`，以及
  `files.entity_type`/`file_role`/`format`/`compression`/`status`。
- **缺失值。** 原始取值 `""`（空串）、`na`、`n/a`、`null` 与 `none` 在去除空白并转小写后
  归一化为 NULL；必填字段出现这些取值会报校验错误，而显式列入 `allowed` 的取值会被保留
  （例如 `compression: none`）。

`Schema.validate_and_normalize()` 在写入任何行之前拒绝未知表/字段、类型不匹配、越界数值
以及不在受控词汇表内的取值。

## 实体退役与恢复：先隔离，再决定是否物理清除

`retire` 写入 `entity_lifecycle_events` 事件行与 `changes` 审计行；
`effective_retired_entities` 解析继承退役，`retired` / `show --include-retired` 暴露该状态。
事件配对规则、隐藏退役实体的消费者清单以及“不提供 `purge`”策略见
[Release、生命周期与正确性保证](../architecture/release-lifecycle.md)。
