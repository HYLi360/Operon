# 人工策展、查询与实体生命周期

## 人工覆盖判定

```bash
operon curate \
  --entity-type assembly --entity-id ASM_000003 \
  --profile assembly_production_v1 \
  --decision PASS \
  --reviewer "zhang.san" \
  --reason "N 含量偏高来自已知着丝粒，不影响本研究" \
  --evidence "见 cytology report 2026-08-01"
```

规则：

- 只修改该 entity/profile 的**最新** decision 的 `curated_*` 字段。
- 自动判定与更早的历史 decision 保持原样。
- 修改同时写入 `changes` 审计表。
- 状态机按策展后的 decision 更新为 ACCEPTED/REJECTED/REVIEW。

## 只读 SQL 查询

`query` 是只读的。常用查询示例：

```bash
# 所有 PASS/FAIL 状态
operon query "SELECT entity_type, entity_id, decision, reason_codes FROM current_decisions"

# 一个 assembly 及其文件
operon query "
SELECT a.assembly_id, f.file_id, f.file_role, f.relative_path, f.sha256
FROM assemblies a JOIN files f ON f.entity_id=a.assembly_id
WHERE a.assembly_id='ASM_000001'
"

# QC 指标宽表式查看
operon query "
SELECT entity_id, metric_name, metric_numeric, metric_unit, evaluated_at
FROM qc_results
WHERE entity_type='assembly' AND qc_stage='assembly_basic'
ORDER BY entity_id, metric_name
"

# 查看数据库内 schema 版本标记
operon query "SELECT entity_id, state, message FROM entity_state WHERE entity_type='database'"
```

`SELECT`、`PRAGMA table_info` 等只读操作可用；`UPDATE`、`INSERT`、`DROP`、`PRAGMA user_version=...`、`ATTACH` 等会被拒绝。

如果目标是从一个 organism 根 accession 查看完整数据树，不必手写 JOIN：

```bash
operon show NCBI_Taxonomy:3702
operon show ORG_000001
operon show GCF_000001405.40 --json
```

`show` 会把匹配到的任意实体向上解析到 organism。默认 `--scope matched` 只列出命中实体的
上游 lineage 与自己的 subtree，避免查询一个 assembly 时把同一 organism 的其他 assembly
计入数量；需要完整 organism 图时使用 `--scope organism`。默认隐藏已 supersede 和已退役的
后代，分别可用 `--include-superseded`、`--include-retired` 审计完整历史。裸 accession 有
歧义时使用 `namespace:accession`。

## 退役与恢复实体

当 assembly、annotation 或整棵 organism 数据是误导入、来源不合适或上游已撤回时，先做
逻辑退役，不要删除数据库行或 raw 文件。第一步永远是只读预览：

```bash
operon retire GCA_000751015.1 \
  --reason-code accidental_import \
  --reason "误混入本项目，等待按正确来源重新导入"
```

JSON 计划会列出命中目标、所有权 subtree、文件数量和路径，以及 accession、QC、decision、
analysis、workflow、source、remote location、release 引用。确认 `physical_changes` 全部为零，
并重点检查已有 release/远端引用；这些引用不会被退役删除。

计划无误后显式应用：

```bash
operon retire GCA_000751015.1 \
  --reason-code accidental_import \
  --reason "误混入本项目，等待按正确来源重新导入" \
  --evidence "2026-09-01 导入批次复核记录" \
  --actor hyli360 --apply --yes
```

退役只追加 lifecycle event、`changes` 和 workflow provenance，不移动/删除文件，也不改写已有
QC、analysis 或 release。父实体状态会沿所有权传播：退役 organism 会隔离其全部 sample、
run、assembly、annotation；退役 sample 隔离自己的下游；退役 assembly 隔离 annotation。
活动 `show` 数量、status/report、批量 QC/evaluate/analyze、定向 `run-external`、新 release
和 NCBI 复用默认排除这些实体。查看当前状态：

```bash
operon retired
operon retired --direct-only
operon show GCA_000751015.1 --include-retired
```

如果复核后确认应重新启用，先预览再恢复：

```bash
operon restore GCA_000751015.1 --reason "确认来源映射正确"
operon restore GCA_000751015.1 --reason "确认来源映射正确" \
  --actor hyli360 --apply --yes
```

恢复是严格逆过程：它追加 `RESTORE` 并指回目标最近的直接 `RETIRE`，不删除历史。继承祖先
退役的子实体不能单独恢复，应恢复计划指出的根；如果子实体还有独立直接退役，恢复父实体后
它仍保持退役。若库版本早于 database schema 2.7，先运行 `operon migrate`。

当前没有 `purge`。不要用手工 SQL、`rm` 或删除远端对象代替：物理清除还需要单独设计保留期、
release/远端引用保护、恢复窗口和不可逆确认。在此之前，“退役 + 可审计恢复”就是完整的安全
处置路径。
