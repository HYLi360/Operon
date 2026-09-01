# Operon 帮助文档

`operon` 是一个基于 Python 的、面向大规模基因组数据的**基于文件的数据库**。它用于完成基因组数据的归档、元数据管理、质控（QC）、规则判定、自动化处理与版本化发布。其设计遵循五条核心原则：结构化元数据是唯一事实来源、原始数据不可修改、文件身份由校验和与稳定 ID 决定、指标与判定分离、所有处理由确定性工作流执行（详见[架构说明](architecture.md)）。

本目录中的文档均以中文编写，并已按当前代码库（`operon` 0.5.3、数据库内部 schema 2.7、metadata schema 1.4、pytest 测试套件）重新核对。

## 文档导航

| 文档 | 适合人群 | 内容 |
|---|---|---|
| [架构说明](architecture.md) | 开发者、维护者、需要理解系统边界的用户 | 设计原则与实现对应、模块划分、目录结构、数据模型、QC 流水线、规则引擎、状态机、release、安全保证、扩展边界与开发测试 |
| [数据库兼容代码清单](database-compatibility.md) | 1.0 发布维护者 | 开发期数据库/schema 迁移代码及正式版删除边界 |
| [入门指南](getting-started.md) | 第一次使用 `operon` 的用户 | 安装、5 分钟演示、从零建立第一个真实项目的完整步骤 |
| [How-to 操作手册](howto.md) | 日常使用者 | 针对具体任务的步骤：NCBI Datasets 导入/下载、批量元数据、归档测序数据、扩展 schema、外部 QC、profile、发布、逻辑退役/恢复、备份与排错 |
| [NCBI adapter 恢复与迁移手册](ncbi-recovery-migration.md) | 旧数据库维护者 | 停写、只读基线、备份、schema 2.7 迁移、补偿式修复、验收、恢复下载与回退步骤 |
| [NCBI Taxonomy 覆盖率](taxonomy-coverage.md) | 多物种项目维护者、release 审计者 | coverage profile、taxonomy 快照导入、冻结分母编译、metadata/release 两种覆盖率口径与缺失清单 |
| [Recipe 配置参考](recipe-reference.md) | 外部工具使用者、recipe 作者 | `tools.yaml` 心智模型、全部字段、占位符、文件/目录 artifact、输出命名、数据库身份、缓存、parser 与完整 BUSCO 示例 |
| [命令参考](cli-reference.md) | 所有用户 | 全部 CLI 命令与参数速查 |
| [内置 QC 性能诊断](qc-performance-diagnostics.md) | 开发者、性能测试人员 | workflow JSONL 分阶段计时字段、代表性实体清单、复测方法与热点判定 |

## 快速了解

```bash
# 安装
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# 生成并运行一个端到端演示项目
operon init-demo ./demo-project --project-id PRJ_DEMO_001

# 查看实体状态与判定
operon --project ./demo-project status
operon --project ./demo-project report decisions

# 导入已有 NCBI Datasets package（真实项目）
operon --project ./my-project ncbi-datasets --input /data/ncbi_dataset.zip

# 验证演示 release 的校验和
(cd ./demo-project/releases/2026.08.demo && sha256sum -c checksums.sha256)

# 检查外部分析程序配置（config/tools.yaml），并运行封装式 BLAST/HMMER/BUSCO
operon --project ./demo-project tools-check
operon --project ./demo-project analyze --analysis blastn_nt --dry-run
```

## 核心概念一句话版

- **元数据**：受 YAML schema 约束并保存在 SQLite 中的规范化记录；CSV/XLSX 是受控导入介质，TSV 是派生 report，不是第二事实来源。
- **来源适配**：NCBI Datasets report/package 可离线导入，也可按 accession 在线下载后自动归档。
- **内部稳定 ID**：`ORG_/SMP_/RUN_/ASM_/ANN_/FIL_` 前缀；外部 accession 只作为映射保存。
- **文件身份**：`file_id + sha256 + size_bytes`；`relative_path` 只是当前位置。
- **raw 不可变**：原始文件只归档、不修改；同名实体同角色不同字节会触发冲突。
- **QC 只测指标，不写死判定**：指标进入 `qc_results` 长表，YAML profile 规则负责判定。
- **分类覆盖率分母被冻结**：NCBI Taxonomy 与 coverage profile 显式编译为带 SHA-256 的 reference set，历史报告只对该快照计算。
- **外部分析高度封装**：BLAST/HMMER/BUSCO 等程序在 `config/tools.yaml` 中配置，`analyze` 自动选择文件或目录、执行、缓存并同步结果；支持目录输出、JSON parser、直接路径与 `conda run`。
- **判定可追溯**：profile 内容按 SHA-256 快照保存，decision 只追加不覆盖。
- **处理可重放**：状态机显式记录成功/失败状态，操作幂等，日志为 JSONL 与 SQLite 双份。
- **发布可验证**：release 是带 manifest、checksum、排除报告与 provenance 的目录快照。
