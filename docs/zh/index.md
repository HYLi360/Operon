# Operon 文档

Operon 是一个面向大规模基因组数据的文件型数据库，覆盖元数据管理、文件归档、质量控制、规则判定、外部分析、远程存算和版本化发布。

本文档对应 `operon` 0.6.2、数据库内部 schema 2.9、metadata schema 1.4。文档按使用者任务分层，中文与英文页面保持相同的目录结构。

## 阅读路径

1. 新用户：先读[项目概述](overview.md)，再完成[安装](getting-started/installation.md)和[快速开始](getting-started/quickstart.md)。
2. 日常使用者：按任务查阅[操作指南](guides/index.md)。
3. 配置或命令排查：查阅[命令与配置参考](reference/index.md)。
4. 维护者：阅读[架构说明](architecture/index.md)、[运维手册](operations/index.md)和[贡献者指南](contributor/index.md)。

## 文档目录

- [项目概述](overview.md)
- [入门](getting-started/index.md)
- [操作指南](guides/index.md)
- [命令与配置参考](reference/index.md)
- [架构说明](architecture/index.md)
- [运维手册](operations/index.md)
- [贡献者指南](contributor/index.md)

```{toctree}
:hidden:
:maxdepth: 2

overview
getting-started/index
guides/index
reference/index
architecture/index
operations/index
contributor/index
```

## 核心概念

- SQLite 中的结构化元数据是唯一可写事实来源。
- 原始文件不可变；派生文件必须可重建。
- 文件身份由 `file_id + sha256 + size_bytes` 定义，路径只表示当前位置。
- QC 程序只产生指标；准入判定由版本化 YAML profile 执行。
- 所有处理都记录到状态机、SQLite 和 JSONL provenance。
- release 是带 manifest、checksum、排除清单和 provenance 的不可变快照。
