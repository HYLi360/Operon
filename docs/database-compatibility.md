# Operon 数据库向后兼容代码清单

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
索引与 `current_decisions` 视图，1.0 中必须保留。

对应回归测试位于 `tests/regression/test_correctness.py` 的
`test_v1_qc_and_decisions_migrate_without_data_loss`。删除迁移代码时应同时删除该测试，
并把不兼容旧数据库写入 1.0 发布说明。

## 项目 metadata schema 自动升级

文件：`operon/adapters/ncbi_datasets.py`

`_adapter_schema()` 当前会把 adapter 自有 assembly 字段合并进旧项目的
`config/schemas.yaml`，并把 schema `1.0`/缺失版本提升到 `1.1`。正式版应改为明确
要求受支持的 schema 版本，给出可操作的错误信息，不再静默修改旧项目 schema。

`_validate_plan_rows()` 中按旧 schema 已知列投影 adapter 行的 `compatible_rows` 逻辑
也属于同一兼容层。要求 schema 1.1+ 后应删除该投影，让未知或缺失字段直接触发校验
错误，避免正式版继续静默丢弃 adapter 字段。

对应测试是 `tests/integration/test_ncbi_datasets_adapter.py` 中所有调用
`_make_schema_legacy()` 的用例；移除兼容层时需一并改成“旧 schema 被明确拒绝”的
测试。

## 不在本清单中的兼容行为

- `--copy-files` 是命令行参数别名，不是数据库兼容代码。
- `standardize` 的 hardlink/symlink 模式是存储策略，不是数据库迁移。
- `upsert_decision()` 的旧方法名属于 Python API 兼容，不影响数据库文件格式。
- `ensure_metadata_columns()` 是当前自定义 schema 功能，不能随旧数据库迁移一起删除。
