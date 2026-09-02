# 项目概述

`operon` 将基因组文件、元数据、QC 指标、分析结果和发布记录组织为一个可审计的项目。它使用单个 SQLite 文件保存结构化状态，使用项目目录保存原始文件、派生文件和报告。

## 主要能力

- **元数据与来源管理**：受 YAML schema 约束的 organism、sample、run、assembly 和 annotation；外部 accession 与内部稳定 ID 分离；来源、引用和 License 可追踪。
- **不可变文件归档**：`ingest` 计算 SHA-256、原子写入 raw，并防止同实体同角色被不同字节覆盖。
- **NCBI Datasets 适配器**：支持离线 report/package 和在线 accession 下载，自动建立元数据关系并归档包内文件。
- **流式内置 QC**：支持 FASTA、FASTQ、GFF3 和 protein FASTA；结果写入统一长表。
- **规则引擎与人工策展**：YAML profile 产生追加式 decision；人工覆盖写入审计表。
- **封装式外部分析**：通过 `config/tools.yaml` 运行 BLAST、HMMER、BUSCO 或其他命令，并记录版本、输入、数据库和输出身份。
- **远程存储与执行**：支持 SFTP 镜像、REMOTE_ONLY 文件、本地/Slurm/SSH 执行后端。
- **taxonomy 覆盖率**：显式导入 NCBI Taxonomy 版本并编译冻结分母，分别审计当前元数据和历史 release。
- **可验证 release**：发布目录包含成员清单、排除报告、metadata 快照、provenance 和 `checksums.sha256`。

## 适用边界

Operon 负责数据准入、身份校验、provenance、规则判定和发布。它不替代下游比较基因组分析流程；此类分析可在 `analysis/` 中由外部工具完成，并将结果回写到 Operon。当前内置来源适配器覆盖 NCBI Datasets，taxonomy 覆盖率仅支持 NCBI Taxonomy。

## 推荐工作流

```text
metadata import/add
  -> ingest
  -> verify
  -> standardize
  -> qc / analyze
  -> evaluate / curate
  -> release
```

详见[日常操作顺序](getting-started/daily-workflow.md)。
