# Taxonomy 覆盖率架构

## NCBI Taxonomy coverage 快照

taxonomy coverage 与 NCBI genome adapter 分离：前者读取 NCBI Datasets
`taxonomy_report.jsonl`/package，或含 `nodes.dmp`、`names.dmp`（以及可选
`merged.dmp`/`delnodes.dmp`）的官方 taxdump archive，原包按 SHA-256 归档到
`raw/metadata/ncbi_taxonomy/`，再把树节点和 secondary TaxID 导入
`taxonomy_snapshots/nodes/aliases`。版本标签必须由调用者显式指定；同一版本不同字节
作为冲突拒绝。

`config/profiles/*.yaml` 由必填 `kind` 区分 `qc` 与 `taxonomy_coverage`。coverage
profile 声明一个或多个根 TaxID、family/genus 目标 rank、extinct/排除子树/名称正则和
各 rank 阈值。`taxonomy compile` 对一个具体 taxonomy 版本遍历后代，产生确定性排序的
`taxonomy/reference_sets/<profile>@<taxonomy_version>.tsv`。TSV、taxonomy 原包和
profile 均记录 SHA-256；相同输入幂等复用，不同内容绝不覆盖；首次编译进入
`changes` 审计。

Datasets JSON 的 extinct 布尔值可支持 `exclude_extinct`；传统 taxdump 没有该字段，
其节点以 unknown 保存。如果 profile 请求 extinct 排除，compiler 会拒绝这种组合，
要求使用明确的排除子树/名称规则或具有 extinct 标注的快照，避免静默改变计算口径。

`report coverage` 只读取该 TSV 分母：

- metadata 口径直接读取 `organisms`，表达“库中登记采了什么”；
- release 口径校验 `release_members` 与 release manifest，并沿 release 目录内冻结的
  metadata 表回溯 organism，表达“已发布数据集覆盖了什么”。

分子是投影到 reference set 后互异的 family/genus TaxID 数，而不是 organism 数。
secondary TaxID 可按同一 taxonomy 快照的 alias 映射；非 NCBI、缺失/未知 TaxID 和
profile 排除项进入排除清单，不进行名称猜测。报告输出汇总、完整目标、缺失目标、
纳入/排除观察与 provenance；输入身份相同时校验并复用，metadata/release 成员变化时
追加新报告。详细契约见 [NCBI Taxonomy 覆盖率](../guides/taxonomy-coverage.md)。
