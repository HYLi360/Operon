# Operon 基本架构

> 本文对应代码库当前状态：`operon` 0.3.0，数据库内部 schema 版本 `2.2`，
> `config/schemas.yaml` 中的元数据字段 schema 版本为 `1.2`。

## 1. 设计目标

`operon` 不是一个“整理文件夹的脚本”，而是一个小型、可验证、可追溯的基因组数据管理系统。它遵循以下原则：

1. 结构化元数据是唯一事实来源。
2. 原始数据不可修改，衍生数据可以重建。
3. 文件身份由校验和与稳定 ID 决定，而不是由路径决定。
4. QC 被写成明确规则，指标与判定分离。
5. 下载、标准化、质控、汇总和发布全部由确定性工作流执行。

这些原则与具体实现的对应关系：

| 设计原则 | 实现 |
|---|---|
| 实体分开建模，外部 accession 不作主键 | `organisms/samples/runs/assemblies/annotations/files/accessions` 表 |
| 字段有类型、必填、允许值、含义 | YAML schema + 严格校验 |
| raw 不可变、standardized 派生 | 原子 ingest + `ConflictError` + 默认独立副本 |
| 文件名只含稳定 ID/角色/格式/压缩 | `canonical_filename()` |
| 路径不是文件身份 | `files.file_id + sha256 + size_bytes` |
| QC 分层 | `file_integrity/reads_basic/assembly_basic/annotation_basic` |
| 指标与判定分离 | `qc_results` 长表 + YAML profile 规则引擎 |
| 自动化状态机、失败显式、幂等续跑 | `entity_state` + 严格迁移 + 原子操作 |
| provenance 机器可读 | `logs/workflow.jsonl` + `workflow_runs` 表 |
| 人工修改可审计 | `changes` 表 + `curate` 命令 |
| 数据集版本化发布 | `release` + checksums + exclusions + provenance |
| 代码/配置/元数据/数据分离 | `operon/` 代码、`project.yaml`、`metadata/`、`raw/` |

## 2. 总体分层

```text
┌────────────────────────────────────────────────────────────┐
│ CLI 控制面：operon 命令（init/ingest/qc/evaluate/...）    │
├────────────────────────────────────────────────────────────┤
│ 业务层                                                       │
│  files.py       不可变文件归档、校验、标准化                    │
│  adapters/      外部数据库来源解析、下载、字段映射与归档编排       │
│  qc/            流式 FASTA/FASTQ/GFF3/蛋白解析与指标计算        │
│  rules.py       YAML profile 规则引擎与判定                    │
│  tools.py       外部分析工具配置、版本探测、缓存执行、结果同步      │
│  release.py     release 快照生成                              │
│  workflow.py    状态机、JSONL 日志、外部命令执行器               │
│  execution.py   执行后端抽象（local/slurm/ssh）                  │
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
│  config/profiles/*.yaml   版本化 QC 判定规则                  │
├────────────────────────────────────────────────────────────┤
│ 文件系统层                                                   │
│  metadata/ raw/ standardized/ qc/ analysis/ reports/ logs/ releases/ │
└────────────────────────────────────────────────────────────┘
```

## 3. 模块职责

| 模块 | 主要职责 |
|---|---|
| `operon/cli.py` | argparse 命令解析、命令分发、人类可读输出 |
| `operon/config.py` | 读取 `project.yaml`，定位项目根目录，生成目录结构 |
| `operon/schema.py` | 内置元数据字段定义、TSV 读取/写出、类型校验与规范化 |
| `operon/database.py` | SQLite DDL、WAL/外键/索引、开发期兼容迁移与 schema 2.2 增量迁移、事务、只读查询 |
| `operon/files.py` | 文件格式/压缩识别、原子归档、幂等 ingest、checksum 验证、standardized 视图 |
| `operon/adapters/ncbi_datasets.py` | NCBI Datasets JSON/JSONL/TSV/ZIP 解析、REST 下载、Entrez 回退、稳定 ID 去重与自动归档 |
| `../operon/qc_module/parsers.py` | 纯 Python 流式解析 FASTA、FASTQ、GFF3、蛋白 FASTA |
| `../operon/qc_module/__init__.py` | 组装内置 QC stage，把指标写入 `qc_results` |
| `operon/rules.py` | 加载 profile，计算 PASS/FAIL 等判定，保存 profile 快照与 decision 历史 |
| `operon/tools.py` | 读取 `config/tools.yaml`，封装外部程序启动方式、版本探测、输入校验、缓存执行与结果回写 |
| `operon/workflow.py` | 合法状态迁移、`workflow.jsonl` 结构化日志、外部命令执行 |
| `operon/execution.py` | 执行后端抽象：`local`/`slurm`/`ssh`，sbatch 脚本生成与轮询、SSH/SFTP 传输、路径映射 |
| `operon/remotes.py` | SFTP 远程镜像：远端清单维护、按内容校验的幂等 push/pull、`sftp://`/`remote://` 下载 |
| `operon/release.py` | 生成不可变 release 目录与校验和 |
| `operon/reports.py` | QC 长表/宽表导出、状态与判定报表 |
| `operon/demo.py` | 生成确定性的合成演示项目 |

## 4. 项目目录结构

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
│       └── reads_qc_v1.yaml
├── metadata/                 # 人工可编辑 TSV 交换文件（导入/导出的源）
├── raw/                      # 不可变原始归档；metadata/ncbi_datasets 保存来源 report/ZIP
├── standardized/             # 稳定 ID 命名的处理视图（默认独立副本）
├── qc/                       # QC 输出与 aggregate/ 汇总表
├── analysis/                 # 分析工作区（外部工具输出、下游分析）
├── reports/                  # decisions、汇总导出
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

## 5. 数据模型

### 5.1 核心实体

```text
organisms (ORG_)
    └── samples (SMP_)
            ├── runs (RUN_)         测序 run，产生 reads
            └── assemblies (ASM_)   组装版本
                    └── annotations (ANN_)   注释版本
                            ├── GFF3
                            ├── CDS FASTA
                            └── protein FASTA
```

外部 accession 放在独立的 `accessions` 表中，不作为主键：

```text
internal_type   internal_id    namespace        accession         version
assembly        ASM_000001     NCBI_Assembly    GCA_000000001     1
sample          SMP_000001     NCBI_BioSample   SAMN0000001       1
```

### 5.2 files：文件清单

`files` 是归档文件的 manifest。关键字段：

```text
file_id, entity_type, entity_id, file_role, format, compression,
relative_path, source_url, size_bytes, sha256, downloaded_at, status
```

文件身份由 `file_id + sha256 + size_bytes` 定义。`relative_path` 只表示文件当前位于项目中的位置。

### 5.3 qc_results：QC 长表

内置 QC 和外部 QC 都写入同一张长表。当前版本每条结果额外绑定：

```text
file_id        该指标对应的 manifest 文件（可为空）
file_sha256    该输入文件的 SHA-256（可为空）
input_identity 唯一输入标识：
               file:{file_id}:{sha256} 或 entity:{entity_type}:{entity_id}
```

唯一约束为：

```text
(input_identity, qc_stage, metric_name, tool, tool_version, parameter_set)
```

这保证同一实体的 R1、R2、GFF3、蛋白 FASTA 等不同输入文件的同名指标不会互相覆盖。查询 `latest_metrics()` 时，对 `file_exists`、`sha256_match`、`parseable`、`paired_read_count_match` 这些“任一文件失败即失败”的指标取多个输入中的最小值（保守值）。

### 5.4 qc_profiles 与 decisions：可追溯判定

规则引擎每次 `evaluate` 都会：

1. 对 YAML profile 内容做规范化 JSON 序列化并计算 SHA-256；
2. 把 profile 快照写入 `qc_profiles`（同名同版本同内容去重）；
3. 把新的自动判定**追加**到 `decisions`，不覆盖旧判定；
4. `current_decisions` 视图返回每个 `(entity_type, entity_id, profile)` 的最新一条 decision。

因此修改 profile 阈值后重新 evaluate 会形成新的 decision 历史，release 和 `decisions` 命令默认读取 `current_decisions`，而 `export-metadata --include-generated` 会导出完整 history。

### 5.5 其他系统表

| 表 | 用途 |
|---|---|
| `entity_state` | 实体级状态机，含数据库 schema 标记行 |
| `workflow_runs` | 结构化运行记录（与 `logs/workflow.jsonl` 对应） |
| `file_locations` | `file_id` 在各远程镜像上的 URI、身份副本、可用状态与最近校验时间；可由远端清单重建 |
| `releases` / `release_members` | release 元数据与成员文件清单 |
| `analysis_jobs` | 外部分析作业：命令、版本、参数指纹、输入/数据库指纹、输出 checksum、缓存状态 |
| `analysis_results` / `analysis_hits` | 同步到数据库的分析汇总指标与 top hits 长表 |
| `changes` | 人工修改审计日志 |

## 6. 元数据流

```text
编辑 metadata/*.tsv
        │
        ▼
schema.validate_and_normalize()   类型、必填、允许值、正则、唯一性
        │
        ▼
交叉引用校验                       外键、file_id 引用
        │
        ▼
ensure_metadata_columns()          把 schema 中新增字段自动加到 SQLite 表
        │
        ▼
单事务写入 SQLite
```

- `import-metadata` 默认是**合并式 upsert**：已有记录按主键更新，新记录插入。
- `import-metadata --replace` 是**快照式替换**：在单一 SQLite 事务中，先删子表再删父表，再按依赖顺序重建；header-only 空表也会清空对应表。任一步失败整体回滚。
- `export-metadata` 将数据库内容写回 `metadata/*.tsv`，可用于版本控制或人工编辑。

### 6.1 NCBI Datasets adapter

`ncbi-datasets` 在通用 TSV 流程之前增加来源适配层，但不建立第二套数据模型：

```text
已有 JSON/JSONL/TSV/ZIP/目录 ─┐
                              ├─> report parser ─> 规范化映射 ─> schema 校验 ─> SQLite/TSV
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
- paired GCA/GCF 指向同一个 `ASM_`；
- `.1` → `.2` 被视为新的不可变 assembly 版本；
- BioProject 是一对多普通字段，不进入唯一 accession 映射表；
- 没有 BioSample 的记录使用 assembly 专属 sample；
- annotation 文件自动归属到对应 `ANN_`。

在写元数据前，适配器会计算待归档文件 SHA-256，检查同一实体/角色的包内冲突和
现有 manifest 冲突。原始 report/ZIP 按 SHA-256 保存到
`raw/metadata/ncbi_datasets/`；导入摘要写入 `changes` 和 workflow provenance。
旧项目在正式导入时会以合并方式补齐 adapter 自有字段并把 metadata schema 1.0
升级为 1.1；自定义字段保留，dry-run 只使用内存中的升级后 schema。

## 7. 文件归档与标准化

`ingest` 的保证：

1. 实体必须存在。
2. 自动识别格式与压缩；`.fna.gz`/`.fastq.gz` 可正确识别。
3. 同实体同角色不同 SHA-256：直接拒绝（`ConflictError`），防止 raw 被覆盖。
4. 相同内容重复 ingest：幂等返回同一个 `FIL_`。
5. 写入 raw 时使用“临时文件 + fsync + 原子 rename”。
6. 归档后再校验一次 checksum，成功才登记 manifest 并回填 `assemblies.fasta_file_id`、`annotations.*_file_id` 等关系。

`standardize` 默认**复制**到 `standardized/`，使 raw、standardized、release 三层互不共享可写 inode；`--link hardlink` 或 `--link symlink` 是显式兼容选项。

### 7.1 远程镜像（SFTP）

`project.yaml` 的 `remotes:` 段可配置一个或多个 SFTP 远程镜像（`operon/remotes.py`），
把 manifest 文件同步到远端而不破坏本节的不变量：

- 普通文件与目录 artifact 全部按 `sha256 + size_bytes` 校验；服务器没有
  `sha256sum` 时通过 SFTP 流式计算 SHA-256，绝不退化为仅比较大小；目录使用与
  本地完全相同的确定性树哈希（含空目录和符号链接目标）；
- 远端维护 `operon-manifest.json` v2 清单（project_id + relative_path →
  file_id/sha256/size/kind/synced_at），清单更新要求服务器支持 OpenSSH POSIX rename
  扩展，以“临时文件 + 原子替换”发布；
- 所有相对路径在本地和远端均做根目录约束，拒绝绝对路径、`..` 与路径逃逸；远端
  清单的 `project_id` 和每条身份都必须与本地 SQLite 一致；
- 每次传输复用 `workflow_runs` 记录 provenance（step 为 `push:<name>` /
  `pull:<name>`）；成功位置同时缓存到 `file_locations`；
- `pull` 恢复本地缺失文件后把 `files.status` 恢复为 `CHECKSUM_VERIFIED`；
- `ingest --source` 也可直接接受 `sftp://[user@]host[:port]/path` 与
  `remote://<name>/<path>`；后者必须存在于远端清单并先校验身份，前者下载后由
  ingest 计算新身份，再走与本地文件完全相同的归档流程。

paramiko 是可选依赖（`pip install 'operon[remote]'`），代码内惰性导入；核心依赖与
本地功能不受影响。cx_Freeze 的 `build` extra 和发布包包含 paramiko。

### 7.2 本地控制面与远程数据面

`operon` 0.3 的远程模型把“存、算、执行”拆为三个可组合角色：

```text
本地电脑：CLI + project.yaml + tools.yaml + SQLite + logs
                          │ SSH/SFTP
                          ▼
远程登录/调度节点：直接执行或提交 Slurm
                          │ 共享文件系统
                          ▼
远程数据面：raw/reference DB/临时分析输出
```

`push` 在远端建立经过身份校验的副本；`evict` 只有在再次验证远端实际内容后才删除
本地字节，把 `files.status` 置为 `REMOTE_ONLY`，在 `file_locations` 记录位置，并于
`.operon/placeholders/<file_id>.json` 写一个便于人查看的指针。指针不是事实来源，
`files` 与 `file_locations` 才是机器判定依据。`pull` 可随时把对象 hydrate 回逻辑
`relative_path`。

这里“raw 不可变”约束的是一个 `file_id` 的内容身份不能被另一组字节替换，并不要求
每台控制端永久保存一份物理副本。`evict` 是经校验的位置迁移：至少一个远端副本仍以
同一 SHA-256/size 存在，逻辑 raw 身份不变；远端副本不可信或缺失时绝不删除本地字节。

当 `execution.ssh.storage_remote` 指向同一远程文件系统时，`analyze` 遇到本地缺失的
输入不会先下载；它先核对远端清单和实际 SHA-256，再把本地逻辑路径映射到远端 root，
让远端命令直接读取该对象。当前要求计算节点通过 SSH 主机或其 Slurm 节点能看到该
remote root；“对象存储与完全不同的计算集群之间服务器端搬运”尚未实现。

## 8. QC 流水线

内置 QC 全部使用流式解析器，不把大文件整体读入内存：

| stage | 适用输入 | 代表指标 |
|---|---|---|
| `file_integrity` | 所有文件 | `file_exists`、`size_bytes`、`sha256_match`、`parseable` |
| `assembly_basic` | genome FASTA | `total_length`、`contig_n50/n90`、`contig_l50/l90`、`gc_percent`、`n_percent`、`gap_count`、`ambiguous_base_percent`、重复/空序列 |
| `reads_basic` | FASTQ | `read_count`、`total_bases`、`q20_percent`、`q30_percent`、`gc_percent`、`duplicate_percent`、`overrepresented_sequence_count`、read length N50、R1/R2 配对 |
| `annotation_basic` | GFF3 (+组装 FASTA/蛋白 FASTA) | gene/mRNA/CDS 数量、CDS 三联体比例、ID/Parent 完整性、坐标错误、seqid 匹配、蛋白重复 ID、X 比例、内部终止密码子 |

外部工具指标可通过 `import-qc` 进入同一长表，也可通过 `run-external` 以结构化方式执行并保存 provenance。

## 9. 规则引擎

阈值不在 QC 代码中，而在 `config/profiles/*.yaml`：

```yaml
version: 1
applies_to: [assembly]
required:
  - metric: sha256_match
    operator: "=="
    value: 1
    code: SHA256_MISMATCH
  - metric: contig_n50
    operator: ">="
    value: 1000
    code: LOW_CONTIGUITY
warnings:
  - metric: n_percent
    operator: ">"
    value: 1
    code: HIGH_GAP_CONTENT
```

判定输出为数据：

```text
PASS / PASS_WITH_WARNINGS / REVIEW / FAIL / EXCLUDED / NOT_EVALUATED
```

每条 decision 记录 `reason_codes`、`observed`、`thresholds`、profile 版本与 SHA-256 快照。

## 10. 状态机

```text
DISCOVERED -> METADATA_FETCHED -> METADATA_VALIDATED -> DOWNLOAD_PENDING
-> DOWNLOADED -> CHECKSUM_VERIFIED -> STANDARDIZED -> QC_RUNNING
-> QC_COMPLETE -> ACCEPTED / REVIEW / REJECTED -> RELEASED
```

失败状态也显式存在：`DOWNLOAD_FAILED`、`CHECKSUM_FAILED`、`FORMAT_INVALID`、`METADATA_INVALID`、`STANDARDIZATION_FAILED`、`QC_FAILED`。

`set_state` 校验合法迁移；批量流程内部使用强制但留痕的迁移，人工强制迁移必须写 reason 并进入 `changes` 表。

## 11. Release

`release --version <版本> --profile <profile>` 使用 `current_decisions` 挑选 PASS/PASS_WITH_WARNINGS/人工 ACCEPT_WITH_WARNING 的文件，并生成：

```text
manifest.tsv / decisions.tsv / exclusions.tsv / profile_history.tsv
qc_summary.tsv / provenance.json / checksums.sha256
software_versions.tsv / README.md / 元数据表快照 / data/ 成员文件
```

release 默认 `copy`，保证与 raw/standardized 不共享 inode；`--link hardlink` 是显式空间优化选项。

## 12. 关键正确性保证

- **只读查询**：`query` 使用独立只读 SQLite 连接 + authorizer，拒绝 DML、DDL、写 PRAGMA、ATTACH/VACUUM 等副作用操作。
- **原子导入**：metadata import 在单事务内完成，失败整体回滚。
- **幂等**：相同输入重复执行不会产生重复文件或覆盖正确结果；不同输入被明确拒绝。
- **可追溯**：provenance 同时写入 `logs/workflow.jsonl` 和 `workflow_runs`。
- **自动迁移**：打开旧版 v1 数据库时，`qc_results` 和 `decisions` 会自动迁移到 v2 结构，旧数据不丢失（旧 QC 以 `legacy:` 身份保留）。

## 13. 封装式外部分析

外部 BLAST/HMMER/BUSCO 等程序不再需要手工拼接命令。`config/tools.yaml` 中的
recipe 声明输入类目、artifact 类型、启动方式、参数和结果解析器；`analyze` 命令自动：

1. 从 `files` manifest 中选出匹配 `entity_type + file_role + format` 的全部输入；
2. 按 `input_kind` 校验输入文件或目录仍存在且内容哈希与 manifest 一致；
3. 探测程序版本（`version_args + version_pattern`）并记录到 `analysis_jobs`；
4. 计算参考数据库身份（单文件 SHA-256 / 目录指纹 / 显式 checksum）；
5. 按 `analysis_name + file_id + 输入 SHA + 参数指纹 + 工具版本 + 数据库身份` 查找已完成缓存，命中则跳过；
6. 未命中时以 `conda run`、容器前缀或直接路径启动程序；文件与目录输出都必须存在且非空，stdout/stderr 落盘；
7. 计算文件或目录内容哈希，解析 top hits 或 BUSCO JSON summary 写入
   `analysis_hits`/`analysis_results`，并同步同名指标到 `qc_results`。

目录使用由相对路径、空目录、文件大小/内容和符号链接目标组成的确定性树哈希。
`database_mode: mutable_cache` 用于 BUSCO 等会逐步下载 lineage 的共享缓存，以显式
`database_version` 标识其逻辑版本；不可变参考库仍使用默认的 `reference` 内容身份。

外部命令的实际执行由 `execution.py` 的后端抽象接管，`run_external_command` 通过
`get_executor(project, backend)` 选择后端：

- `local`（默认）：原有本地子进程行为，完全不变；
- `slurm`：本地 Slurm 集群。在 `logs/` 下生成 `<run_id>.sbatch` 批处理脚本
  （`--cpus-per-task` 取线程数，可选 `--time`/`--partition`/`--mem`、
  `extra_sbatch` 与 `setup_commands`），用 `sbatch --parsable` 提交并按
  `poll_interval` 轮询 `squeue`，作业消失后读取脚本写入的 `<run_id>.exitcode`
  退出码文件（失败时回退 `sacct`）；前提是项目目录位于与计算节点共享的
  文件系统上；
- `ssh`：通过 paramiko（可选依赖 `operon[remote]`，惰性导入）在 SSH 远程主机
  （HPC 头节点/云虚拟机）上执行；`execution.ssh.scheduler: slurm` 时改为在远端
  走 sbatch/squeue。支持 `remote_root` 路径映射（空表示共享文件系统）；输入
  文件经 SFTP 上传（内容一致跳过，严格 SHA-256/目录树哈希；不同内容拒绝覆盖）；
  若配置 `storage_remote`，REMOTE_ONLY 输入在远端原位消费。运行前清除精确计算出的
  远端旧输出，expected outputs 经临时文件拉回并与远端内容再次比对；已有本地输出
  只有内容完全相同时才接受。

三个后端共用同一份 provenance 与正确性契约：退出码、起止时间、日志照常写入
`workflow_runs` 与 `logs/workflow.jsonl`；SQLite 额外保存 executor、scheduler job ID
与资源/脚本详情，成功判定与输入/输出 SHA-256 校验不变；
工具版本探测在非 `local` 后端时也经同一后端执行。单个 recipe 可用 `slurm:`
mapping 覆盖 `execution.slurm` 的同名字段（如给 BUSCO 单独调内存/时间）。

日常使用见 [How-to 操作手册](howto.md)；字段、占位符、artifact、数据库身份、缓存和
parser 的完整契约见 [Recipe 配置参考](recipe-reference.md)。

## 14. 扩展边界

当前内置来源适配器先覆盖 NCBI Datasets；ENA 等来源仍属于后续扩展边界。内置 QC
覆盖文件级、reads 基础、assembly 结构与 annotation 结构。BUSCO 已通过目录输出和
JSON summary parser 原生接入；QUAST、Merqury、Kraken2、CheckM2 等尚未提供 parser
的工具仍可通过 `run-external` + `import-qc` 接入。下游比较基因组分析在 `analysis/`
中由外部工作流完成，`operon` 负责数据准入、provenance 与发布。

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

## 15. 应用发布文件结构

项目使用 cx_Freeze 从 `pyproject.toml` 构建独立应用目录。执行
`python -m cx_Freeze build` 后，发布内容固定落在：

Linux 构建机还需要系统命令 `patchelf`；它是 cx_Freeze 处理 ELF 依赖的构建期工具，
不属于 `operon` 的 Python 运行时依赖。缺少时 cx_Freeze 会在 `build_exe` 阶段直接停止。

```text
build/release/
├── operon                  # 命令行可执行文件；Windows 为 operon.exe
├── lib/                    # Python 运行时、operon 包与第三方依赖
└── share/doc/operon/       # README 和 docs/
```

应用发布目录与 `operon release` 生成的数据集快照是两个不同概念：前者交付程序，
后者交付经过筛选并可校验的数据。

## 16. 开发与测试

```bash
python -m pip install -e '.[dev]'
python -m pytest

# 也可按类目执行
python -m pytest tests/unit
python -m pytest tests/integration
python -m pytest tests/regression tests/compatibility
```

pytest 测试按 `unit`、`integration`、`regression`、`compatibility` 四类组织，覆盖：
Python 3.10 语法与运行时门禁、schema 校验与受控词汇、metadata round-trip 与事务
回滚、稳定 ID、默认副本隔离、query 只读约束、file-aware QC 身份、profile/decision
历史、gzip FASTA 识别、assembly/annotation QC、规则判定、幂等 ingest 与冲突保护、
checksum 篡改检测、demo 端到端流水线与 release 校验、NCBI Datasets adapter、
BLAST/HMMER/BUSCO 封装执行、目录 artifact、JSON summary、conda run 前缀解析、
缓存命中/强制重跑、结果回写与输入篡改拒绝。
