# 元数据流与数据模型

## 元数据流

```text
交互式 import / import table / 专用 adapter / add
        │
        ▼
Draft 或输入表预览                 不修改项目
        │
        ▼
schema + 交叉引用 + 冲突预检       类型、必填、允许值、外键、既有主键
        │
        ▼
用户确认                           汇总审阅或表格 diff
        │
        ▼
受控事务写入 SQLite + changes      SQLite 是唯一可写事实来源
        │
        ├─> ingest ─> raw/files manifest
        └─> report metadata ─> 派生只读 TSV 快照
```

- `operon import dataset` 使用纯英文 questionary 向导建立 Draft；已有 organism 以 scientific
  name 自动补全选择。来源章节区分 INSDC 与非 INSDC，收集 database/repository、provider、
  record URL、citation 和 License；非 INSDC 的 citation 与 License 为强制准入条件。最终确认
  前不写项目，汇总页进入任一章节修改后直接回到汇总页。
- `operon import table` 只接受人工管理的 metadata 表，支持 CSV/XLSX 模板、预览、碰撞策略与逐字段审计；不允许导入系统管理的 `files` manifest。
- `operon report metadata` 从 SQLite 生成带行数和 SHA-256 manifest 的只读 TSV 快照，包含
  `data_sources.tsv` 与 `source_links.tsv`。修改这些 report 不会改变数据库。
- `metadata/` 目录仅为旧布局保留，不自动读写 TSV。

### 6.1 NCBI Datasets adapter

`ncbi-datasets` 在通用 TSV 流程之前增加来源适配层，但不建立第二套数据模型：

```text
已有 JSON/JSONL/TSV/ZIP/目录 ─┐
                              ├─> report parser ─> 规范化映射 ─> schema 校验 ─> SQLite
NCBI Datasets v2 下载 ────────┘                         │
                                                       └─> ingest ─> files manifest/raw
```

在线下载和离线导入使用完全相同的后半段。下载层使用 aiohttp 并发下载多个
accession 批次（`--download-workers`），在后台 asyncio 线程中运行，完成后通过队列
把批次交回调用线程导入，避免跨线程使用 SQLite。SSL record layer failure、连接中断、
超时、429/5xx 等瞬时错误按指数退避自动重试；单批次兼容接口
`download_ncbi_dataset()` 同样具有外层 SSL/网络重试。下载使用流式写入、临时文件、
磁盘空间预检和 ZIP 完整性验证；当 package 异常缺少 assembly report 且配置了 NCBI
email 时，Biopython Entrez 可作为元数据回退。NCBI 对无效/撤回 accession 可能
返回只有 README 的“空 package”（ZIP 无中央目录）；下载层会解析 local file header
识别这种非瞬时错误，报告具体 accession，而其他批次的下载与导入继续执行。

身份与关系策略：

- taxon ID、BioSample 和完整版本化 GCA/GCF 用于复用实体；
- paired GCA/GCF 指向同一个 `ASM_`；canonical 不由到达顺序改写，新实体有 GCF 时
  确定性优先 GCF；
- `.1` → `.2` 被视为新的不可变 assembly 版本；
- BioProject 是一对多普通字段，不进入唯一 accession 映射表；
- 没有 BioSample 的记录使用 assembly 专属 sample；
- annotation 身份包含来源 accession、provider、version 与 release date，文件自动归属到
  对应 `ANN_`；pre-2.6 行用严格相同元数据接续，避免 provider 不是 `NCBI *` 时重复分配。

在写元数据前，适配器会计算待归档文件 SHA-256，检查同一实体/角色的包内冲突和
现有 manifest 冲突。paired 来源的 alternate genome/report 使用带 `_genbank`/`_refseq`
后缀的受控角色，因此不同来源字节可以并存而不放宽同一实体同一角色的不可覆盖约束。
原始 report/ZIP 按 SHA-256 保存到
`raw/metadata/ncbi_datasets/`；导入摘要写入 `changes` 和 workflow provenance。
旧项目在正式导入时会以合并方式补齐 adapter 自有字段和来源文件角色并把 metadata
schema 升级为 1.4；自定义字段保留，dry-run 只使用内存中的升级后 schema。

adapter run 在开始处理前写入 `running` workflow；每个 accession 的状态保存在
`adapter_run_items`。失败或中断运行保持原状态，恢复运行使用新的 run ID 和
`resumes_run_id`，请求 SHA-256 不一致时拒绝恢复。元数据 upsert 的字段级 before/after
通过 `changes.workflow_run_id` 关联具体运行。旧 adapter 异常由显式 `ncbi-reconcile`
生成和应用补偿计划，使用 `entity_supersessions` 保留所有旧行和文件。

### 6.2 NCBI Taxonomy coverage 快照

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

详细表结构见[数据模型参考](../reference/data-model.md)。
