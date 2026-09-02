# Release、生命周期与正确性保证

## 实体退役与恢复

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

## Release

`release --version <版本> --profile <profile>` 使用 `current_decisions` 挑选 PASS/PASS_WITH_WARNINGS/人工 ACCEPT_WITH_WARNING 的文件，并生成：

```text
manifest.tsv / decisions.tsv / exclusions.tsv / profile_history.tsv
qc_summary.tsv / provenance.json / checksums.sha256
software_versions.tsv / README.md / 元数据表快照 / data/ 成员文件
```

release 默认 `copy`，保证与 raw/standardized 不共享 inode；`--link hardlink` 是显式空间优化选项。
coverage 的 release 口径读取这里冻结的 metadata，而不是当前活动数据库中的 TaxID；
release summary/provenance 保存每张 metadata TSV 的 SHA-256，coverage 计算前复核这些
身份，因此 release 创建后的活动 metadata 修改不会重写历史覆盖率，release 目录内的
快照篡改也会被拒绝。

## 关键正确性保证

- **只读查询**：`query` 使用独立只读 SQLite 连接 + authorizer，拒绝 DML、DDL、写 PRAGMA、ATTACH/VACUUM 等副作用操作。
- **原子导入**：metadata import 在单事务内完成，失败整体回滚。
- **幂等**：相同输入重复执行不会产生重复文件或覆盖正确结果；不同输入被明确拒绝。
- **可追溯**：provenance 同时写入 `logs/workflow.jsonl` 和 `workflow_runs`。交互导入等事务型
  调用先在同一 SQLite 事务内写入 `workflow_runs` 并缓存 JSONL 记录，待事务最终提交后才
  append；事务失败时丢弃尚未提交的完成事件，并在回滚后记录父级失败事件，避免日志声称
  已完成的对象实际不存在。
- **冻结分母**：coverage 仅对带 SHA-256 的 reference-set TSV 计算；taxonomy 升级不能静默改变历史数字。
- **自动迁移**：打开旧版 v1 数据库时，`qc_results` 和 `decisions` 会自动迁移到 v2 结构，旧数据不丢失（旧 QC 以 `legacy:` 身份保留）。
