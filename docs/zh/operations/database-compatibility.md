# 数据库兼容代码清单

本清单记录仅用于兼容开发期旧数据库或旧项目 schema、并计划在 1.0 移除的代码。

本文集中记录正式版（1.0）发布时计划废除的、仅用于读取开发期旧数据库或旧项目
schema 的兼容代码。这里的“删除”不包括当前 schema 所需的建表、索引、视图或
`ensure_metadata_columns()` 动态扩展能力。

## SQLite 数据库迁移

文件：`operon/database.py`

| 位置 | 当前用途 | 1.0 处理方式 |
|---|---|---|
| `Database.__init__()` 对 `_migrate_pre_1_0_schema()` 的调用 | 每次打开可写数据库时检查开发期旧结构 | 删除调用 |
| `Database._migrate_pre_1_0_schema()` 的 assembly 列补齐段 | 给旧 `assemblies` 表增加 NCBI adapter 后来引入的 5 列 | 删除 |
| 同一方法的 `qc_results` 迁移段 | 把没有 `input_identity` 的 v1 表迁移为 file-aware 结构，并用 `legacy:` 保存旧记录身份 | 删除 |
| 同一方法的 `decisions` 迁移段 | 把没有 `profile_snapshot_id` 的 v1 表迁移为可追加历史结构 | 删除 |

`Database._ensure_current_schema_objects()` 不属于兼容代码。它负责当前版本仍需要的
索引，以及 `current_decisions`、`current_entity_lifecycle`、
`effective_retired_entities` 视图，1.0 中必须保留。

`Database._migrate_remote_schema_2_2()` 也不属于上述“开发期 v1 兼容层”。它把 2.1
数据库按纯加法升级为 2.2：给 `workflow_runs` 增加 `executor`、
`scheduler_job_id`、`execution_details`，并创建 `file_locations`。只要仍支持打开 2.1
项目就必须保留；若未来停止兼容，应通过正式数据库迁移策略取代，不能随
`_migrate_pre_1_0_schema()` 一起删除。对应测试为
`test_schema_2_2_adds_remote_location_and_executor_provenance`。

`Database._migrate_taxonomy_schema_2_3()` 同样是当前功能所需的纯加法迁移，不属于
`_migrate_pre_1_0_schema()`：它为 2.2 项目创建 `taxonomy_snapshots`、
`taxonomy_nodes`、`taxonomy_aliases`、`taxonomy_reference_sets`、
`coverage_reports` 与 `coverage_report_metrics` 及相关索引，不修改既有业务行。
只要仍支持打开 2.2 项目就必须保留。对应回归测试为
`test_schema_2_3_adds_taxonomy_coverage_history`。

`Database._migrate_source_schema_2_4()` 是另一项当前功能所需的纯加法迁移：它为 2.3
项目创建 `data_sources` 与 `source_links`，保存规范化的外部数据库/仓库、引用文献、
License 和其关联对象。非 INSDC 来源必须同时包含 citation 与 License；来源内容通过
SHA-256 身份去重。只要仍支持打开 2.3 项目就必须保留。对应回归测试为
`test_schema_2_4_adds_normalized_source_provenance`。

`Database._migrate_integrity_cache_schema_2_5()` 为 2.4 项目增加
`local_file_verifications`。该表只保存最近一次完整本地 SHA-256 通过时的 stat 指纹，
可随时清空并由 ingest、`verify` 或 QC 重建；它不改变 `files` 中的内容身份。只要仍
支持打开 2.4 项目就必须保留。对应回归测试为
`test_schema_2_5_adds_local_file_verification_cache`。

`Database._migrate_recovery_schema_2_6()` 为 2.5 项目纯加法增加：

- `workflow_runs.resumes_run_id`；
- `changes.workflow_run_id` 与 `changes.reverts_change_id`；
- `schema_migrations`、`adapter_run_items`；
- `ncbi_assembly_records`、`ncbi_annotation_records`；
- `entity_supersessions`。

迁移不删除或重写已有 assembly、annotation、file、QC、analysis、release、workflow 或
changes 行。旧 NCBI adapter 的业务异常由显式 `operon ncbi-reconcile` 处理，不能藏进
schema migration。对应回归测试为
`test_schema_2_6_adds_resumable_adapter_and_repair_history`。

`Database._migrate_lifecycle_schema_2_7()` 为 2.6 项目纯加法增加
`entity_lifecycle_events`、`current_entity_lifecycle` 与
`effective_retired_entities`。事件表只追加 `RETIRE`/`RESTORE`，恢复事件通过
`reverts_event_id` 和对应的 `changes.reverts_change_id` 指回被撤销的直接退役；有效退役
视图则沿 organism → sample → run/assembly → annotation 所有权关系传播状态。迁移不删除、
移动或改写 metadata、file、QC、analysis、release、workflow 或归档字节。只要仍支持打开
2.6 项目就必须保留。对应回归测试为
`test_schema_2_7_adds_append_only_entity_lifecycle`。

对应回归测试位于 `tests/regression/test_correctness.py` 的
`test_v1_qc_and_decisions_migrate_without_data_loss`。删除迁移代码时应同时删除该测试，
并把不兼容旧数据库写入 1.0 发布说明。

## 项目 metadata schema 自动升级

文件：`operon/adapters/ncbi_datasets.py`

`_adapter_schema()` 当前会把 adapter 自有 assembly 字段合并进旧项目的
`config/schemas.yaml`，追加 paired-source 文件角色并把旧版本提升到 `1.4`。正式版应改为明确
要求受支持的 schema 版本，给出可操作的错误信息，不再静默修改旧项目 schema。

`_validate_plan_rows()` 中按旧 schema 已知列投影 adapter 行的 `compatible_rows` 逻辑
也属于同一兼容层。要求 schema 1.1+ 后应删除该投影，让未知或缺失字段直接触发校验
错误，避免正式版继续静默丢弃 adapter 字段。

对应测试是 `tests/integration/test_ncbi_datasets_adapter.py` 中所有调用
`_make_schema_legacy()` 的用例；移除兼容层时需一并改成“旧 schema 被明确拒绝”的
测试。

metadata schema 1.2 在 `files.status` 中增加 `REMOTE_ONLY`。当前新项目直接生成 1.4，
并包含该受控词汇；
旧项目第一次执行通过远端/本地身份预检的 `operon evict`，或在本地字节缺失时由
`operon verify` 实时确认远端副本，都会以保留自定义字段的方式只追加该允许值并把
版本提升到 1.2。该升级属于当前远程驻留功能的必要契约，不是 NCBI adapter 的
1.0/1.1 兼容投影。

文件：`operon/taxonomy.py`

metadata schema 1.3 为 taxonomy 原包 manifest 增加 `taxonomy_snapshot` entity type、
`TAX_` ID 前缀和 `taxonomy_package` role。新项目直接生成 1.4；旧项目第一次成功执行
`operon taxonomy import` 前，`_ensure_taxonomy_metadata_schema()` 会保留自定义字段，
只追加这些受控词汇并把版本提升到 1.3。这是 taxonomy 快照文件身份所需的当前契约，
不能随 NCBI genome adapter 的 1.0/1.1 兼容投影一并删除。

metadata schema 1.4 为 paired GCA/GCF 来源文件增加
`genome_fasta_genbank/refseq` 与 `assembly_report_genbank/refseq` 角色。NCBI adapter 或
`ncbi-reconcile --apply` 只追加这些受控值并保留项目自定义字段；taxonomy 的 1.3 升级逻辑
只在当前版本低于 1.3 时运行，不能把 1.4 降级回 1.3。

## 不在本清单中的兼容行为

- `--copy-files` 是命令行参数别名，不是数据库兼容代码。
- `standardize` 的 hardlink/symlink 模式是存储策略，不是数据库迁移。
- `upsert_decision()` 的旧方法名属于 Python API 兼容，不影响数据库文件格式。
- `ensure_metadata_columns()` 是当前自定义 schema 功能，不能随旧数据库迁移一起删除。
