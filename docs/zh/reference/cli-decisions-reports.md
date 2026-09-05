# 判定、发布与报告命令

## evaluate

```bash
operon evaluate [--profile NAME] [--entity-type TYPE] [--entity-id ID] [--yes]
```

- 默认 profile 来自 `project.yaml` 的 `qc.default_profile`。
- 指定 `--entity-id` 时必须同时指定 `--entity-type`。
- 保存 profile SHA-256 快照，追加 decision；状态机按判定更新。
- 若本次会重新评估已有人工判定的实体，CLI 会在执行前一次性列出受影响的实体并请求确认；非交互运行必须显式给出 `--yes`，取消发生在写入任何 decision 之前。
- 重评估会追加新的自动判定，但会沿用当前 decision 的 `curated_*` 字段；人工决定的生命周期只有在显式执行 `curate` 时才会改变。
- 规则支持 `value_by.metric + value_by.values` 动态选择门限，并用 `unknown` 指定未知
  selector 的策略（`warning`/`fail`/`ignore`，缺省视为缺少门限即 `NOT_EVALUATED`）；
  `source.qc_stage` 可把规则绑定到一个明确的 QC/analysis 来源。

## curate

```bash
operon curate \
  --entity-type TYPE --entity-id ID --profile NAME \
  --decision DECISION --reviewer REVIEWER --reason REASON [--evidence TEXT]
```

修改该 entity/profile 最新 decision 的 `curated_*` 字段并写入 `changes` 审计表。

## release

```bash
operon release --version VERSION --profile NAME \
  [--link {copy|hardlink}] [--copy-files]
```

- 默认 `copy`，生成与 raw/standardized 不共享 inode 的 release。
- `--copy-files` 是 `--link copy` 的兼容别名。
- 已存在的 version 目录会拒绝重复创建。
- 仅纳入 `current_decisions` 中 PASS、PASS_WITH_WARNINGS、ACCEPT_WITH_WARNING 的文件；其余实体写入 `exclusions.tsv`。
- release 的 metadata 快照包含 `data_sources.tsv` 与 `source_links.tsv`，冻结来源、引用、
  License 及对象关联，并纳入 release checksum/provenance。

## export

```bash
operon export --output DIR \
  [--entity-type TYPE] [--entity-id ID ...] [--file-id FIL_... ...] \
  [--file-role ROLE] [--format FMT] [--state STATE] \
  [--decision DECISION --profile NAME] \
  [--link {copy,hardlink,symlink}] [--no-qc]
```

把数据库实体按文件身份物化为目录，供外部分析工具消费。

- `--output` 必填；目标目录必须不存在或为空。
- 至少提供一个选择条件：`--entity-type`、`--entity-id`（可重复）、`--file-id`
  （可重复）、`--file-role`、`--format`、`--state` 或 `--decision`。
- `--decision` 必须同时给出 `--profile`，匹配 `current_decisions` 中该 profile 下的
  有效决策（如 PASS、FAIL），大小写不敏感。
- 有效退役实体始终被排除。
- `--link` 默认 `copy`；`hardlink` 失败时自动回退 `copy`。
- 布局为 `data/<entity_type>/<entity_id>/<文件名>`；物化前校验源文件 SHA-256 与
  manifest 一致，不一致即拒绝。
- 产物：
  - `manifest.tsv`：列为 `file_id`、`entity_type`、`entity_id`、`file_role`、
    `format`、`compression`、`export_relative_path`、`original_relative_path`、
    `source_url`、`size_bytes`、`sha256`；其中 `sha256` 是物化后对导出字节复算的值；
  - `qc.tsv`：默认生成，导出实体的 QC 长表快照；`--no-qc` 跳过；
  - `checksums.sha256`：导出字节的校验和；
  - `provenance.json`：记录全部选择条件、`created_at`、`file_count`、`operon` 版本、
    `link_kind` 和 manifest SHA-256。
- 每次导出写入一行 `workflow_runs`（step 为 `export`，`output_sha256` 为 manifest
  哈希，`execution_details` 包含选择条件）。
- 语义上与 release 互补：release 面向发布（QC 准入、不可变快照），export 面向分析
  输入（任意选择条件、按需物化）。

## adopt

```bash
operon adopt --file PATH --entity-type TYPE --entity-id ID --role ROLE \
  [--format FMT] [--compression C] \
  --derived-from FILE_ID [--derived-from FILE_ID ...] [--workflow-run-id RID] [--actor NAME]
operon adopt --from-manifest FILE [--actor NAME]
```

把外部分析/工作流产生的派生 artifact 注册回数据库，成为一等公民文件：进入
`files` manifest，获得 QC、evaluate、export、release 资格，并可被 `analyze` 的
候选选择（`entity_type + file_role + format`）选为下游 recipe 的输入，从而打通
级联分析。与 export 构成契约闭环：export 提供输入侧 manifest，外部工作流消费后由
adopt 回注册输出侧 manifest。

- 两种模式互斥：`--file` 单文件模式要求 `--entity-type`/`--entity-id`/`--role` 与至少
  一个 `--derived-from`（可重复）；`--from-manifest` 批量模式供 snakemake/nextflow
  在 rule 末尾一次回注册整批产出，manifest 格式见
  [外部分析操作指南](../guides/external-analysis.md)。
- 产物物化到 `analysis/adopted/<entity_id>/`，不进入不可变的 `raw/` 归档。
- 继承 ingest 的幂等/冲突不变量：同实体同 role 相同字节幂等复用同一 `FIL_`，
  不同字节抛 `ConflictError`。
- 所有 `derived_from` 的 file_id 必须已在库中，且目标实体必须处于活动状态；整批先
  校验后落库，任一条目失败则整批不注册（原子）。
- 谱系边写入 `file_lineage(derived_file_id, input_file_id, workflow_run_id,
  created_at)`；重复 adopt 是 no-op。
- 每次 adopt 写入一行 `workflow_runs`（step 为 `adopt`，`execution_details` 含
  actor 与 items 摘要）；`--actor` 缺省取 `$USER` 或 `adopt`。
- 派生 role 由产生它的工作流自由命名，不受 `schemas.yaml` 内置 role 清单限制
  （该清单只在导入路径生效）。

## run-pipeline

```bash
operon run-pipeline \
  --source FILE --entity-type {run|assembly|annotation} --entity-id ID \
  --role ROLE [--format FMT] [--compression C] [--source-url URL] \
  [--profile NAME]
```

依次执行 `ingest -> standardize -> qc -> evaluate`。任一阶段失败返回非零。

## report

```bash
operon report qc [--entity-type TYPE] [--entity-id ID] [--export] [--include-retired]
operon report decisions [--profile NAME] [--include-retired]
operon report analysis [--analysis NAME] [--entity-type TYPE] [--entity-id ID] \
  [--hits] [--limit N] [--include-retired]
operon report coverage --reference-set NAME@TAXONOMY_VERSION [--scope metadata]
operon report coverage --reference-set NAME@TAXONOMY_VERSION --release VERSION
operon report metadata [--output DIRECTORY] [--include-retired]
```

- `qc`：打印 QC 长表；`--export` 额外写出 `qc/aggregate/qc_results.tsv` 与
  `qc_results.wide.tsv`。
- `decisions`：显示 `current_decisions`（每个 entity/profile 的最新判定）。
- `analysis`：显示同步到数据库的分析汇总；`--hits` 改为显示 top hits，`--limit`
  默认 20。
- `coverage`：只对指定的冻结 taxonomy reference set 计算 family/genus 覆盖率。
  默认 `--scope metadata` 审计当前 `organisms`；`--release VERSION` 改为沿
  `release_members` 和 release 内冻结元数据统计已发布数据集，并复核创建时保存的
  metadata SHA-256。二者互斥。
- coverage 报告写入 `reports/coverage/COV_<input-hash>/`，包括分子/分母、完整目标、
  缺失清单、纳入/排除观察和 provenance。完全相同输入会校验并复用既有报告。
- `metadata`：从当前 SQLite 导出 `organisms/samples/runs/assemblies/annotations/accessions/files`
  以及规范化来源 `data_sources/source_links` 的只读 TSV 快照，并生成包含行数与 SHA-256
  的 `manifest.json`；默认写入
  `reports/metadata/`。它是派生 report，不是备份，也不能反向覆盖数据库。
- `qc`、`decisions`、`analysis` 和 `metadata` 默认排除有效退役实体；审计完整历史时显式
  使用 `--include-retired`。metadata scope 的 coverage 同样默认只统计活动 organism；
  已有 release 使用创建时冻结的范围，不因后续退役而改变。

coverage 计算成功且达到 profile 中全部阈值时返回 0；报告成功生成但至少一个 rank
未达标时返回 1。阈值不写死在命令或代码中。

## query

```bash
operon query "SQL"
```

只读 SQL。允许 SELECT 与只读 PRAGMA（如 `table_info`、`foreign_key_list`）；拒绝 DML/DDL/写 PRAGMA/ATTACH/VACUUM 等。

## show

```bash
operon show ORG_000001
operon show LAB:HX-ROOT
operon show GCF_000001405.40 --json
operon show GCF_000001405.40 --scope organism
operon show ANN_000001 --include-superseded
operon show ASM_000001 --include-retired
```

解析内部稳定 ID、裸 accession 或 `NAMESPACE:ACCESSION`。默认的 `--scope matched` 会显示
命中实体的上游 lineage 和自己的下游 subtree，避免查询一个 assembly 时把同一 organism 下
其他 sample/assembly 的数量一起算入：

- organism：显示该 organism 的全部后代；
- sample：显示 organism、该 sample 及其 run、assembly、annotation；
- run：显示 organism、所属 sample 和该 run；
- assembly：显示 organism、所属 sample、该 assembly 及其 annotation；
- annotation：显示 organism、所属 sample、所属 assembly 和该 annotation。

需要旧式的完整物种关系图时使用 `--scope organism`。默认不把
`entity_supersessions` 中已经逻辑取代的后代计入各节数量及文件集合；输出仍列出相关
`Supersessions`，便于解释隐藏的历史记录。`--include-superseded` 可显式恢复完整历史视图；
直接按一个已 supersede 的实体查询时，该命中实体本身仍会显示。

默认也不把有效退役的后代计入各节数量及文件集合；`Retirements` 节会说明它们由哪个直接
退役根隔离。`--include-retired` 恢复完整历史视图。直接查询一个已退役目标时，目标本身与
它的 subtree 仍显示，避免退役后失去审计入口。

裸 accession 对应多个实体时拒绝并要求使用带 namespace 的写法。`--json` 输出完整机器可读
对象，并包含 `scope`、`include_superseded`、`include_retired`、`supersessions` 和
`retirements` 字段。`show` 使用 SQLite
只读连接，因此可安全检查只读挂载或只读数据库副本。若只读介质上仍有非空
`operon.sqlite-wal`，命令会拒绝 immutable 回退并要求先在可写挂载上 checkpoint，避免忽略
未合并事务而显示过期数据。
