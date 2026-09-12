# Operon 文档

[项目概述](overview.md)介绍 Operon 的能力与适用边界：一个面向大规模基因组数据的文件型数据库。

本文档对应 `operon` {{ operon_version }}、数据库内部 schema {{ db_schema }}、metadata schema {{ metadata_schema }}。文档按使用者任务分层，中文与英文页面保持相同的目录结构。

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

设计不变量统一在[架构总览](architecture/overview.md)中说明。
