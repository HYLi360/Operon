# Taxonomy、生命周期与管理命令

## taxonomy

```bash
operon taxonomy import --input PATH --version VERSION
operon taxonomy list
operon taxonomy compile --profile NAME --taxonomy-version VERSION
operon taxonomy reference-sets
```

- `import`：归档并导入 NCBI Datasets `taxonomy_report.jsonl`/package，或至少含
  `nodes.dmp`、`names.dmp` 的官方 NCBI taxdump ZIP/tar；可选的
  `merged.dmp`/`delnodes.dmp` 会转成 TaxID alias；`--version` 是显式、不可变的
  taxonomy 版本标签。
- `list`：列出来源文件身份、版本、节点数和导入状态。
- `compile`：读取 `config/profiles/<NAME>.yaml` 中 `kind: taxonomy_coverage` 的作用域、
  rank、排除规则与阈值，生成
  `taxonomy/reference_sets/<NAME>@<VERSION>.tsv` 及 provenance sidecar。
- `reference-sets`：列出已冻结分母的 family/genus 行数、SHA-256 和编译时间。
- 同一 taxonomy 版本不同字节、同一 reference-set 身份不同 profile/结果都作为冲突
  拒绝；相同输入重复执行则幂等复用。

完整 profile 格式与不变量见 [NCBI Taxonomy 覆盖率](../guides/taxonomy-coverage.md)。

## retire

```bash
operon retire IDENTIFIER \
  --reason-code {accidental_import,wrong_source,duplicate,withdrawn_upstream,policy_exclusion,metadata_error,other} \
  --reason TEXT [--evidence TEXT] [--actor NAME]

operon retire IDENTIFIER --reason-code accidental_import --reason TEXT \
  --apply [--yes] [--evidence TEXT] [--actor NAME]
```

默认只输出 JSON 计划，不修改项目。计划解析内部 ID/accession，列出目标所有权 subtree、
关联文件，以及 accession、QC、decision、analysis、workflow、source、remote location、
release member/version 等引用计数；`physical_changes` 明确为零。旧于 schema 2.7 的数据库
可先用 `operon migrate` 升级；只读旧副本不会被预览命令隐式地迁移。

只有 `--apply` 才追加直接 `RETIRE`、`changes` 审计和 lifecycle workflow；非交互执行还要
`--yes`。退役不删除/移动文件，不改 checksum，不删除 QC/analysis/workflow，也不改变已有
release。有效状态沿所有权传播：organism → sample → run/assembly → annotation，sample →
run/assembly → annotation，assembly → annotation。活动 ingest、QC、evaluate、analyze、
`run-external`、report、NCBI 复用和新 release 默认拒绝或排除这些实体。

## restore

```bash
operon restore IDENTIFIER --reason TEXT [--evidence TEXT] [--actor NAME]
operon restore IDENTIFIER --reason TEXT --apply [--yes] [--evidence TEXT] [--actor NAME]
```

默认同样只预览。`--apply` 追加 `RESTORE`，并由 `reverts_event_id` 与
`changes.reverts_change_id` 指回目标最近的直接退役；不会删除退役历史。它只恢复目标自己
的直接退役：如果目标只是继承祖先的状态，应恢复计划中指出的退役根。子实体另有独立直接
退役时，恢复父实体不会顺带恢复该子实体。

## retired

```bash
operon retired [--direct-only] [--json]
```

默认列出所有当前有效退役实体，同时显示 `retired_by_type/id`，从而区分直接退役根与继承
退役后代。`--direct-only` 只列直接退役根；`--json` 输出机器可读记录。该命令只读。

当前没有 `purge` 命令。退役/恢复只建立安全隔离与完整逆过程；物理清除需要以后另行定义
引用保护、保留期、远端副本和不可逆确认，不能用手工 SQL 或删除 raw 文件替代。

## backup

```bash
operon backup create --output /backups/project-2026-08-28 --scope control
operon backup create --output /backups/project-full --scope full
operon backup verify --input /backups/project-2026-08-28
```

`create` 使用只读数据库连接和 SQLite backup API 生成一致数据库快照，不会先触发新程序的
自动迁移；目标必须位于项目目录之外且不能已存在。
每个备份包含 `backup-manifest.json`，记录全部成员的 size 与 SHA-256。

- `control`（默认）：`project.yaml`、`config/`、一致的 `operon.sqlite`、`logs/`。
- `results`：control 加 `qc/analysis/reports/taxonomy/releases`，不复制 raw 与 standardized 大文件。
- `full`：results 加 `raw/standardized/.operon/metadata/examples`。

`verify` 不需要打开原项目，逐文件检查路径安全性、大小与 SHA-256，并比较目录中的实际文件
集合与 manifest：缺失、被修改或未列入 manifest 的额外文件都会使验证失败。远程镜像仍应
独立备份；control/results scope 只保存 `file_locations` 等控制面记录，不复制远端实际字节。

## set-state

```bash
operon set-state --entity-type TYPE --entity-id ID --state STATE \
  [--message TEXT] [--force]
```

- 校验合法迁移；非法迁移需 `--force`，且会写入 `changes` 审计表。
- 合法状态包括：`DISCOVERED`、`METADATA_FETCHED`、`METADATA_VALIDATED`、`DOWNLOAD_PENDING`、`DOWNLOADED`、`CHECKSUM_VERIFIED`、`STANDARDIZED`、`QC_RUNNING`、`QC_COMPLETE`、`ACCEPTED`、`REVIEW`、`REJECTED`、`RELEASED` 及 `DOWNLOAD_FAILED`、`CHECKSUM_FAILED`、`FORMAT_INVALID`、`METADATA_INVALID`、`STANDARDIZATION_FAILED`、`QC_FAILED`。

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 命令完成但检查未通过，或运行期失败（如 coverage 未达 YAML 阈值、verify/QC/外部命令失败） |
| `2` | `operon` 领域错误（配置错误、校验失败、实体不存在、冲突等） |
