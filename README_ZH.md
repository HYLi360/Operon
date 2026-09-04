# Operon

[![deploy status](https://github.com/HYLi360/Operon/actions/workflows/deploy.yml/badge.svg)](https://github.com/HYLi360/Operon/actions/workflows/deploy.yml) [![codecov](https://codecov.io/gh/HYLi360/Operon/graph/badge.svg?token=BC4LD8UPL2)](https://codecov.io/gh/HYLi360/Operon)

一个基于 Python 的、面向大规模基因组数据的**基于文件的数据库**，用于归档、质控、分析与确定性自动化处理。

## 特色

- **基于文件的数据库**：单个 SQLite 文件（`operon.sqlite`）是唯一可写事实来源；CSV/XLSX 用于受控导入，TSV report 用于只读交换，字段契约由 YAML schema 定义
- **NCBI Datasets 适配器**：离线优先导入 JSON/JSONL、ZIP 或解包目录，也可在线下载 genome package 并自动归档
- **冻结的 NCBI Taxonomy 覆盖率**：版本化 YAML profile 编译为带 SHA-256 的 family/genus 分母，可分别审计当前 metadata 与不可变 release，并输出缺失采样清单
- **流式解析与内置 QC**：FASTA / FASTQ / GFF3 / protein FASTA 不整体读入内存；指标写入长表，判定交给版本化 YAML profile 规则引擎；`value_by` 可按 BUSCO auto-lineage 等分类指标选择门限
- **封装式外部分析**：`config/tools.yaml` 声明 BLAST/HMMER/BUSCO 的启动方式、artifact 类型、受约束运行参数、版本探测、缓存与结果回写；`analyze` 一键执行全库或指定类目
- **本地控制、远程存算**：本地保留 SQLite/配置/provenance，原始大文件可驻留在经校验的 SFTP 镜像；执行后端支持本地、Slurm、SSH 与远端 Slurm
- **通用执行器**：结构化命令执行器与 `import-qc` 可接入 QUAST、FastQC、fastp、CheckM2 等任意外部工具
- **不可变 release**：带 manifest、checksum、排除报告与 provenance 的数据集快照，可 `sha256sum -c` 验证

## 依赖

- Python 3.10 及以上版本
- 运行时依赖：`PyYAML`、`requests`、`aiohttp`、`Biopython`、`Cython`（构建并默认使用内置 QC 加速扩展）
- 可选 extras：`test`（pytest）、`remote`（Paramiko）、`build`（cx_Freeze 与远程功能）、`dev`（全部开发/构建依赖）

## 安装

```bash
# 创建并激活标准 Python 虚拟环境
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# 需要 SSH/SFTP 远程存算时
python -m pip install -e '.[remote]'
```

或者，使用构建脚本构建独立可执行应用：

```bash
python -m pip install -e '.[build]'
python tools/build.py
```

## 文档

完整文档同时维护[中文](docs/zh/index.md)和[英文](docs/en/index.md)版本。本地构建 Sphinx 站点：

```bash
python -m pip install -e '.[docs]'
sphinx-build -W --keep-going -b html docs docs/_build/html
```

Read the Docs 使用仓库根目录的 `.readthedocs.yaml`，发布带语言选择入口及 `/zh/`、`/en/` 镜像目录的文档站点。
