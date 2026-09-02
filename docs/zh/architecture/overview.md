# 架构总览

本文对应 `operon` 0.6.0、数据库内部 schema 2.7、metadata schema 1.4。

## 设计目标

`operon` 是一个小型、可验证、可追溯的基因组数据管理系统。它遵循以下原则：

1. 结构化元数据是唯一事实来源。
2. 原始数据不可修改，衍生数据可以重建。
3. 文件身份由校验和与稳定 ID 决定，而不是由路径决定。
4. QC 被写成明确规则，指标与判定分离。
5. 下载、标准化、质控、汇总和发布全部由确定性工作流执行。

这些原则与具体实现的对应关系：

| 设计原则 | 实现 |
|---|---|
| 实体分开建模，外部 accession 不作主键 | `organisms/samples/runs/assemblies/annotations/files/accessions` 表 |
| 外部来源、引用与许可可追溯 | `data_sources/source_links`，非 INSDC 来源强制 citation + License |
| 字段有类型、必填、允许值、含义 | YAML schema + 严格校验 |
| raw 不可变、standardized 派生 | 原子 ingest + `ConflictError` + 默认独立副本 |
| 文件名只含稳定 ID/角色/格式/压缩 | `canonical_filename()` |
| 路径不是文件身份 | `files.file_id + sha256 + size_bytes` |
| QC 分层 | `file_integrity/reads_basic/assembly_basic/annotation_basic` |
| 指标与判定分离 | `qc_results` 长表 + YAML profile 规则引擎 |
| taxonomy 覆盖率不随上游升级漂移 | NCBI taxonomy 快照 + 编译后的 reference-set TSV + SHA-256 |
| 自动化状态机、失败显式、幂等续跑 | `entity_state` + 严格迁移 + 原子操作 |
| provenance 机器可读 | `logs/workflow.jsonl` + `workflow_runs` 表 |
| 人工修改可审计 | `changes` 表 + `curate` 命令 |
| 误导入实体可逆退役 | append-only `RETIRE`/`RESTORE` 事件 + 层级有效状态视图；不删除归档 |
| 数据集版本化发布 | `release` + checksums + exclusions + provenance |
| 代码/配置/元数据/数据分离 | `operon/` 代码、`project.yaml`、`operon.sqlite`、`raw/` |

## 总体分层

```text
┌────────────────────────────────────────────────────────────┐
│ CLI 控制面：operon 命令（init/ingest/qc/evaluate/...）    │
├────────────────────────────────────────────────────────────┤
│ 业务层                                                       │
│  files.py       不可变文件归档、校验、标准化                    │
│  lifecycle.py   实体逻辑退役、恢复、影响预览与审计              │
│  adapters/      外部数据库来源解析、下载、字段映射与归档编排       │
│  qc/            流式 FASTA/FASTQ/GFF3/蛋白解析与指标计算        │
│  rules.py       YAML profile 规则引擎与判定                    │
│  taxonomy.py    NCBI taxonomy 快照与覆盖率分母编译               │
│  coverage.py    metadata/release 分类覆盖率报告                  │
│  tools.py       外部分析工具配置、版本探测、缓存执行、结果同步      │
│  release.py     release 快照生成                              │
│  workflow.py    状态机、JSONL 日志、外部命令执行器               │
│  execution.py   执行后端抽象（local/slurm/ssh）                  │
│  shutdown.py    SIGINT/SIGTERM 优雅停机与中断收尾                │
│  remotes.py     SFTP 远程镜像（push/pull、远端清单）              │
├────────────────────────────────────────────────────────────┤
│ 数据层                                                       │
│  schema.py      YAML 字段契约与 TSV 校验/规范化                │
│  database.py    SQLite DDL、迁移、事务、只读查询                │
│  reports.py     长表/宽表导出与人读报表                         │
├────────────────────────────────────────────────────────────┤
│ 配置层                                                       │
│  project.yaml   项目路径与默认参数                             │
│  config/schemas.yaml   元数据字段定义                         │
│  config/tools.yaml     外部分析程序与 recipe 配置              │
│  config/profiles/*.yaml   版本化 QC/coverage profile          │
├────────────────────────────────────────────────────────────┤
│ 文件系统层                                                   │
│  metadata/ raw/ standardized/ qc/ analysis/ reports/ logs/ releases/ │
└────────────────────────────────────────────────────────────┘
```

## 模块职责

| 模块 | 主要职责 |
|---|---|
| `operon/cli.py` | argparse 命令解析、命令分发、人类可读输出 |
| `operon/config.py` | 读取 `project.yaml`，定位项目根目录，生成目录结构 |
| `operon/schema.py` | 内置元数据字段定义、类型校验与规范化、派生 TSV 写出 |
| `operon/database.py` | SQLite DDL、WAL/外键/索引、开发期兼容迁移与 schema 2.2–2.7 增量迁移、事务、只读查询 |
| `operon/files.py` | 文件格式/压缩识别、原子归档、幂等 ingest、checksum 验证、standardized 视图 |
| `operon/lifecycle.py` | 退役/恢复计划、append-only 生命周期事件、层级传播与当前退役清单 |
| `operon/import_wizard.py` | questionary 英文导入向导、Draft 汇总审阅、非线性章节修改、预检与提交 |
| `operon/table_import.py` | CSV/XLSX 模板、第一工作表读取、碰撞预览、受审计的 insert/patch |
| `operon/entity_view.py` | 内部 ID/accession 解析与 organism 根实体图展开 |
| `operon/backup.py` | SQLite 一致备份、control/results/full scope、checksum manifest 校验 |
| `operon/adapters/ncbi_datasets.py` | NCBI Datasets JSON/JSONL/TSV/ZIP 解析、REST 下载、Entrez 回退、稳定 ID 去重与自动归档 |
| `../operon/qc_module/parsers.py` | 纯 Python 行为参考实现，用于回归测试 Cython 解析器的指标与错误语义 |
| `../operon/qc_module/_parsers.pyx` | 内置 QC 必需的 Cython 生产解析器，指标输出与错误信息和纯 Python 参考实现逐位一致 |
| `../operon/qc_module/__init__.py` | 组装内置 QC stage，加载 Cython 解析器并把指标写入 `qc_results` |
| `operon/rules.py` | 加载 profile，计算 PASS/FAIL 等判定，保存 profile 快照与 decision 历史 |
| `operon/taxonomy.py` | 归档/导入不可变 NCBI Taxonomy，按 coverage profile 编译冻结分母及 provenance |
| `operon/coverage.py` | 校验 reference set，对 metadata 或 release 冻结范围计算 family/genus 覆盖率与缺失清单 |
| `operon/tools.py` | 读取 `config/tools.yaml`，封装外部程序启动方式、版本探测、输入校验、缓存执行与结果回写 |
| `operon/workflow.py` | 合法状态迁移、`workflow.jsonl` 结构化日志、外部命令执行 |
| `operon/execution.py` | 执行后端抽象：`local`/`slurm`/`ssh`，sbatch 脚本生成与轮询、SSH/SFTP 传输、路径映射 |
| `operon/shutdown.py` | 把 SIGINT/SIGTERM 转换为 `ShutdownRequested`，驱动各后端进程/作业清理与二次信号强制退出 |
| `operon/remotes.py` | SFTP 远程镜像：远端清单维护、按内容校验的幂等 push/pull、`sftp://`/`remote://` 下载 |
| `operon/release.py` | 生成不可变 release 目录与校验和 |
| `operon/reports.py` | QC 长表/宽表导出、metadata 派生快照、状态与判定报表 |
| `operon/demo.py` | 生成确定性的合成演示项目 |

## 项目目录结构

`operon init` 创建以下目录和文件。SQLite 数据库不在 init 时创建，而是在第一次执行需要数据库的命令时创建。

```text
project/
├── project.yaml              # 项目配置：路径、默认 QC profile、资源参数
├── operon.sqlite           # 基于文件的数据库（首次使用命令时创建）
├── config/
│   ├── schemas.yaml          # 元数据字段契约（类型/必填/允许值/正则）
│   ├── tools.yaml            # 外部分析程序配置（BLAST/HMMER/BUSCO、artifact 类型）
│   └── profiles/
│       ├── file_integrity_v1.yaml
│       ├── assembly_production_v1.yaml
│       ├── annotation_release_v1.yaml
│       ├── reads_qc_v1.yaml
│       └── coverage_viridiplantae_v1.yaml
├── metadata/                 # 旧项目布局兼容说明；不再作为读写数据源
├── raw/                      # 不可变原始归档；metadata/ 下保存 NCBI 来源 report/package
├── standardized/             # 稳定 ID 命名的处理视图（默认独立副本）
├── qc/                       # QC 输出与 aggregate/ 汇总表
├── analysis/                 # 分析工作区（外部工具输出、下游分析）
├── reports/                  # decisions、汇总导出及 coverage 报告
├── taxonomy/reference_sets/  # 编译后的不可变 family/genus 分母与 provenance
├── logs/workflow.jsonl       # 机器可读工作流日志
├── .operon/placeholders/     # REMOTE_ONLY 文件的小型、非权威指针
└── releases/                 # 不可变数据集发布快照
```

数据生命周期：

```text
外部来源
  └─> raw/           原样归档，写入 files manifest 与 SHA-256
       └─> standardized/   校验后派生统一命名副本/链接
            └─> qc/        只测指标，写入 qc_results
                 └─> evaluate   profile 规则产生 decision
                      └─> release  只有通过者进入发布快照
```
