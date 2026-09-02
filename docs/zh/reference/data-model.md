# 数据模型

## 数据模型

### 5.1 核心实体

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

外部 accession 放在独立的 `accessions` 表中，不作为主键：

```text
internal_type   internal_id    namespace        accession         version
assembly        ASM_000001     NCBI_Assembly    GCA_000000001     1
sample          SMP_000001     NCBI_BioSample   SAMN0000001       1
```

### 5.2 files：文件清单

`files` 是归档文件的 manifest。关键字段：

```text
file_id, entity_type, entity_id, file_role, format, compression,
relative_path, source_url, size_bytes, sha256, downloaded_at, status
```

文件身份由 `file_id + sha256 + size_bytes` 定义。`relative_path` 只表示文件当前位于项目中的位置。

### 5.3 qc_results：QC 长表

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

### 5.4 qc_profiles 与 decisions：可追溯判定

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

### 5.5 其他系统表

| 表 | 用途 |
|---|---|
| `entity_state` | 实体级状态机，含数据库 schema 标记行 |
| `workflow_runs` | 结构化运行记录（与 `logs/workflow.jsonl` 对应） |
| `execution_environments` | 内容寻址的执行环境文档（hostname、OS/kernel、Python/operon 版本、相关环境变量、docker 探测）；`workflow_runs` 与 `analysis_jobs` 经 `environment_id` 引用 |
| `data_sources` | 外部数据库/仓库、提供者、记录 URL、引用文献、License 与规范化内容身份 |
| `source_links` | 来源与 organism/sample/run/assembly/annotation/file 的多对多关联及导入 provenance |
| `schema_migrations` | 已应用数据库迁移的稳定 ID、脚本身份和应用时间 |
| `adapter_run_items` | 可恢复 adapter 的 accession/item 级状态、尝试、错误与结果 write-set |
| `ncbi_assembly_records` | GCA/GCF 来源记录到稳定 `ASM_` 的映射、canonical 标记及来源文件指针 |
| `ncbi_annotation_records` | 来源 accession/provider/version/date 规范化得到的 annotation 身份 |
| `entity_supersessions` | 不删除旧行的逻辑替代关系及 repair provenance |
| `entity_lifecycle_events` | 实体的 append-only `RETIRE`/`RESTORE` 历史、原因、证据、操作者、workflow 与反向事件指针 |
| `current_entity_lifecycle` | 每个实体最新直接生命周期事件；只表达该实体自身，不传播祖先状态 |
| `effective_retired_entities` | 当前有效退役集合；沿 organism → sample → run/assembly → annotation 传播，并保留根退役事件身份 |
| `file_locations` | `file_id` 在各远程镜像上的 URI、身份副本、可用状态与最近校验时间；可由远端清单重建 |
| `local_file_verifications` | 最近一次完整本地 SHA-256 通过时的 stat 指纹；仅为可重建的 QC 加速缓存，不改变 manifest 文件身份 |
| `releases` / `release_members` | release 元数据与成员文件清单 |
| `analysis_jobs` | 外部分析作业：命令、版本、参数指纹、输入/数据库指纹、输出 checksum、缓存状态 |
| `analysis_results` / `analysis_hits` | 同步到数据库的分析汇总指标与 top hits 长表 |
| `taxonomy_snapshots` | NCBI Taxonomy 版本、来源 manifest 身份、节点数与导入状态 |
| `taxonomy_nodes` / `taxonomy_aliases` | 冻结的分类树节点与 secondary/merged TaxID 映射 |
| `taxonomy_reference_sets` | coverage profile 与 taxonomy 版本编译出的分母 TSV 身份和各 rank 行数 |
| `coverage_reports` / `coverage_report_metrics` | 不可变输入身份对应的覆盖率报告历史与 family/genus 指标 |
| `changes` | 人工修改审计日志 |

### 5.6 实体退役与恢复：先隔离，再决定是否物理清除

`retire` 是控制面状态变化，不是文件操作。它向 `entity_lifecycle_events` 追加一个直接
`RETIRE` 事件，同时向 `changes` 追加审计行；不会删除数据库行、移动文件、修改 checksum、
撤销既有 QC/analysis/workflow，也不会改写已经创建的 release。退役一个父实体会在
`effective_retired_entities` 中使其所有权后代有效退役：organism 覆盖 sample、run、assembly
和 annotation，sample 覆盖自己的 run、assembly 和 annotation，assembly 覆盖 annotation。

`restore` 只反转目标自身最近的直接 `RETIRE`，并追加一个指回原事件/原审计行的
`RESTORE`，不删除历史。由祖先继承退役的子实体不能单独恢复，必须先恢复造成隔离的根；
反之，子实体若另有自己的直接退役，即使父实体恢复也仍保持退役。这使逆过程与原过程严格
对应，不会把独立的人工决定一起抹掉。

活动数据消费者默认排除有效退役实体，包括 `show` 的后代计数、status/report、批量 QC、
规则判定、外部分析候选、metadata coverage、NCBI 重导入复用和新 release。显式查询历史时
可用对应的 `--include-retired`；`retired` 列出当前直接及继承状态。备份、校验、远程驻留、
只读 SQL、已有 release 和审计历史仍保留完整归档视角。

当前架构没有 `purge`。退役计划会列出后代、文件及 QC/decision/analysis/workflow/source/
remote/release 引用，并明确 `physical_changes` 全为零。未来若增加物理清除，必须以这份可审计
状态和引用图为前置条件，另行定义保留期、release/远端引用保护、可恢复窗口和不可逆确认。
