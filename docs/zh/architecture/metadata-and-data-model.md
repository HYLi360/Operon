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

### NCBI Datasets adapter

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
schema 升级为 {{ metadata_schema }}；自定义字段保留，dry-run 只使用内存中的升级后 schema。

adapter run 在开始处理前写入 `running` workflow；每个 accession 的状态保存在
`adapter_run_items`。失败或中断运行保持原状态，恢复运行使用新的 run ID 和
`resumes_run_id`，请求 SHA-256 不一致时拒绝恢复。元数据 upsert 的字段级 before/after
通过 `changes.workflow_run_id` 关联具体运行。旧 adapter 异常由显式 `ncbi-reconcile`
生成和应用补偿计划，使用 `entity_supersessions` 保留所有旧行和文件。

### NCBI Taxonomy coverage 快照

taxonomy 快照与覆盖率是独立于 NCBI genome adapter 的子系统：其来源包、profile
类型、分母与报告历史见 [Taxonomy 覆盖率架构](taxonomy-coverage.md)。

详细表结构见[数据模型参考](../reference/data-model.md)。
