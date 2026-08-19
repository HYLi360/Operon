# Operon

一个基于 Python 的、面向大规模基因组数据的**基于文件的数据库**，用于归档、质控、分析与确定性自动化处理。设计理念是：把基因组文件视为不可变对象，把元数据视为受约束的关系数据，把质控视为版本化规则，把所有处理视为可验证的状态机，把一次研究数据集视为可发布、可审计、可重建的 release。

## 特色

- **基于文件的数据库**：单个 SQLite 文件（`operon.sqlite`），元数据交换格式为 TSV，字段契约由 YAML schema 定义
- **NCBI Datasets 适配器**：离线优先导入 JSON/JSONL、ZIP 或解包目录，也可在线下载 genome package 并自动归档
- **纯 Python 流式解析与内置 QC**：FASTA / FASTQ / GFF3 / protein FASTA 不整体读入内存；指标写入长表，判定交给版本化 YAML profile 规则引擎
- **封装式外部分析**：`config/tools.yaml` 声明 BLAST/HMMER/BUSCO 的启动方式、artifact 类型、版本探测、缓存与结果回写；`analyze` 一键执行全库或指定类目
- **通用执行器**：结构化命令执行器与 `import-qc` 可接入 QUAST、FastQC、fastp、CheckM2 等任意外部工具
- **不可变 release**：带 manifest、checksum、排除报告与 provenance 的数据集快照，可 `sha256sum -c` 验证

## 依赖

- Python 3.10 及以上版本
- 运行时依赖：`PyYAML`、`requests`、`aiohttp`、`Biopython`
- 可选 extras：`test`（pytest）、`build`（cx_Freeze）、`dev`（两者）

## 安装

```bash
# 创建并激活标准 Python 虚拟环境
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

如需构建不依赖 Python 环境的独立可执行应用（cx_Freeze），见
[架构说明 §15 应用发布文件结构](docs/architecture.md)。

## 文档

完整中文文档位于 [`docs/`](docs/index.md)：

- [入门指南](docs/getting-started.md)：安装、5 分钟演示、从零建立第一个真实项目
- [How-to 操作手册](docs/howto.md)：按任务组织的日常操作步骤与排错
- [命令参考](docs/cli-reference.md)：全部 CLI 命令与参数速查
- [Recipe 配置参考](docs/recipe-reference.md)：外部分析 `tools.yaml` 的完整字段契约
- [架构说明](docs/architecture.md)：设计原则、数据模型、QC 流水线、状态机与正确性保证
- [数据库兼容代码清单](docs/database-compatibility.md)：开发期数据库迁移代码与 1.0 删除边界
