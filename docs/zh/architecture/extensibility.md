# 扩展边界

## 扩展边界

当前内置来源适配器先覆盖 NCBI Datasets；ENA 等来源仍属于后续扩展边界。taxonomy
coverage 当前只支持 NCBI Taxonomy；GTDB 及 NCBI↔GTDB crosswalk 尚未实现。内置 QC
覆盖文件级、reads 基础、assembly 结构与 annotation 结构。BUSCO 已通过目录输出和
JSON summary parser 原生接入；QUAST、Merqury、Kraken2、CheckM2 等尚未提供 parser
的工具仍可通过 `run-external` + `import-qc` 接入。下游比较基因组分析在 `analysis/`
中由外部工作流完成，`operon` 负责数据准入、provenance 与发布。

下游流程与数据库之间的契约入口是 `operon export`：它把所选实体按文件身份物化为
`data/<entity_type>/<entity_id>/<文件名>` 布局，并附带 `manifest.tsv`（含物化后复算的
SHA-256）、`qc.tsv` QC 长表快照、`checksums.sha256` 和 `provenance.json`。下游流程应
消费这组产物而不是直接读数据库。export 与 release 语义互补：release 面向发布（QC
准入、不可变快照），export 面向分析输入（任意选择条件、按需物化）。

去重按层实现：字节级重复已由 SHA-256 幂等保证（同一实体同角色、相同字节返回同一
`FIL_`，不同字节明确拒绝）；序列级重复（规范化序列 digest / refget 风格摘要）与
生物学近重复（Mash/ANI/k-mer 相似度、duplicate cluster 与代表选择）属于扩展方向，
可在 `analysis/` 中以外部工具完成并把结果写回 `qc_results`，代表选择规则本身也应
版本化。

规模方面，SQLite WAL + 索引适合百万级元数据行，序列解析全部流式；如明确接受
inode 共享，可对 `standardized/` 或 release 显式使用硬链接。

执行后端按 `execution.py` 的抽象扩展：当前提供 `local`、`slurm` 与 `ssh` 三种，
新增后端只需实现同一 executor 接口即可接入 `run-external`/`analyze`。暂不支持
云厂商 SDK（AWS Batch、GCP Batch 等）与 Slurm 数组作业；远程存储目前仅有 SFTP
镜像，对象存储（S3 等）同样属于扩展方向。
