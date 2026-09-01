# 入门指南：从零开始使用 Operon

本文面向第一次使用 `operon` 的用户。按顺序阅读和操作即可建立一个最小但完整的基因组数据管理项目。

## 1. 环境准备

需要：

- Python 3.10 或更高版本
- Python 自带的 `venv` 与 `pip`
- 可用的 C 编译工具链；`operon` 默认构建并使用 Cython 内置 QC 扩展
- 可选：BUSCO、QUAST、FastQC、fastp 等外部工具（不在本指南中安装）

安装 `operon`：

```bash
# 在仓库根目录
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# 需要 SSH/SFTP 远程存储或计算时
python -m pip install -e '.[remote]'
```

验证安装：

```bash
operon --version
# 输出：operon 0.5.4

operon --help
```

## 2. 5 分钟演示项目

如果不想先创建真实项目，可以用合成数据体验完整流水线：

```bash
operon init-demo ./demo-project --project-id PRJ_DEMO_001
```

该命令会自动完成：

1. 初始化项目目录与配置；
2. 写入 2 个 organism、3 个 sample、1 个 run、3 个 assembly、2 个 annotation；
3. 生成合成 FASTA/GFF3/protein FASTA/双端 FASTQ；
4. 通过正常 ingest 流程归档 9 个文件；
5. 默认复制到 `standardized/`；
6. 运行内置 QC；
7. 分别用 `assembly_production_v1`、`annotation_release_v1`、`reads_qc_v1` 评估；
8. 创建 release `2026.08.demo`。

查看结果：

```bash
operon --project ./demo-project status
operon --project ./demo-project report decisions
operon --project ./demo-project report qc --entity-type assembly
```

演示预期：`ASM_000002` 因 `LOW_CONTIGUITY` 被 FAIL；`ANN_000003` 因 `CDS_NOT_MULTIPLE_OF_3` 与 `BROKEN_GFF3_PARENTS` 被 FAIL；其他实体 PASS。

验证 release：

```bash
cd ./demo-project/releases/2026.08.demo
sha256sum -c checksums.sha256
```

## 3. 建立第一个真实项目

### 3.1 初始化项目

```bash
operon init ./my-genome-project --project-id PRJ_MY_001 --name "My first genome project"
cd ./my-genome-project
```

`init` 会生成：

```text
project.yaml         项目配置
config/              schema、QC/coverage profiles 与外部工具配置 tools.yaml
metadata/            旧布局兼容说明（SQLite 是唯一可写 metadata 来源）
raw/ standardized/ qc/ analysis/ reports/ logs/ releases/ taxonomy/
```

`operon.sqlite` 不会在 init 时创建，第一次执行需要数据库的命令时自动创建。

> 提示：全局选项 `--project` 必须放在子命令之前。进入项目目录后可以省略它；
> 在项目外则使用 `operon --project /path/to/my-genome-project <子命令>`。

### 3.2 使用交互式导入向导

对于合作方交付、本地 pipeline 或其他非 NCBI 数据，直接启动纯英文向导：

```bash
operon import dataset
```

向导依次收集 source、organism、sample、sequencing、assembly、annotation 和文件。已有
organism 使用 scientific name 自动补全选择。source 会明确区分 INSDC 与非 INSDC，并
询问来源 database/repository、provider、记录 URL、引用文献与 License；非 INSDC 数据的
引用文献和 License 不可跳过。其他可选项可以跳过，但最终汇总会持续显示缺失警告。
汇总页可选择 `Edit source`、`Edit files` 等章节；修改完成后会直接回到汇总页，不会继续
原先的线性问题序列。只有选择 `Execute import` 并确认剩余警告后才会写 SQLite 和归档文件。

### 3.3 从 NCBI Datasets 一步导入（推荐用于公开组装）

如果已有 NCBI Datasets report 或 genome package，不需要逐字段执行 `add`：

```bash
# 先看计划，不修改项目
operon ncbi-datasets --input /data/ncbi_dataset.zip --dry-run

# 导入元数据，并自动归档 ZIP 中的 genome/GFF/CDS/protein/report
operon ncbi-datasets --input /data/ncbi_dataset.zip
```

也可以从 accession 在线下载：

```bash
export NCBI_EMAIL='you@example.org'
operon ncbi-datasets --accession GCF_000005845.2
```

大批量下载默认 3 个并行 worker，SSL/瞬时网络错误自动按指数退避重试：

```bash
operon ncbi-datasets --accession-file accessions.txt \
  --download-workers 3 --retries 4 --retry-backoff 1.0
```

程序自动建立 organism → sample → assembly/annotation 关系、分配稳定 ID、保存
GCA/GCF/BioSample/Taxonomy 映射，并把 ZIP 原件保存到
`raw/metadata/ncbi_datasets/`。若使用此方式，可直接跳到“校验归档”。

### 3.4 手工录入元数据

`operon` 至少需要建立如下关系链：

```text
organism (ORG_) -> sample (SMP_) -> assembly (ASM_) / run (RUN_)
                                  -> annotation (ANN_)
```

使用 `add` 命令逐条录入：

```bash
# 1) organism
operon add organism \
  --field scientific_name="Arabidopsis thaliana" \
  --field taxon_id=3702 \
  --field taxonomic_rank=species \
  --field taxonomy_source=NCBI

# 2) sample
operon add sample \
  --field organism_id=ORG_000001 \
  --field strain=Col-0 \
  --field tissue=leaf \
  --field tissue_normalized="young leaf" \
  --field country=China \
  --field country_iso=CN \
  --field collection_date=2026-04-15

# 3) assembly
operon add assembly \
  --field sample_id=SMP_000001 \
  --field assembly_accession=GCA_999999999 \
  --field assembly_version=1 \
  --field assembly_level=chromosome \
  --field reference_status=representative
```

命令输出类似 `added assembly ASM_000001`。不指定 `--id` 时系统自动分配下一个稳定 ID；也可以先用 `operon next-id assembly` 预留编号。

如果有测序数据，再添加 run：

```bash
operon add run \
  --field sample_id=SMP_000001 \
  --field run_accession=SRR999999999 \
  --field library_strategy=WGS \
  --field library_source=GENOMIC \
  --field library_layout=PAIRED \
  --field platform=ILLUMINA
```

如果只有组装没有 reads，跳过 run 即可。

### 3.5 查看和导出元数据

```bash
operon query "SELECT * FROM assemblies"
operon report metadata
```

`report metadata` 会从 SQLite 生成 `reports/metadata/*.tsv` 和带 SHA-256 的
`manifest.json`。这些文件是只读派生快照，适合浏览、交换或版本控制；修改它们不会
改变数据库。快照还包含 `data_sources.tsv` 与 `source_links.tsv`，用于审阅来源、引用、
License 及其关联对象。批量写入应使用 `operon import table` 的 CSV/XLSX 模板与预览流程。

### 3.6 手工归档文件到 raw

以组装 FASTA 为例：

```bash
operon ingest \
  --source /data/GCA_999999999.fna.gz \
  --entity-type assembly \
  --entity-id ASM_000001 \
  --role genome_fasta \
  --source-url https://ftp.ncbi.nlm.nih.gov/...
```

输出示例：

```text
registered FIL_000001 -> raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta.gz (sha256 7b5a0aa0...)
```

系统会：

- 识别 `.fna.gz` 为 gzip 压缩的 FASTA；
- 计算 SHA-256；
- 原子复制到 `raw/assemblies/ASM_000001/`；
- 归档后再次校验；
- 写入 `files` manifest 并回填 `assemblies.fasta_file_id`。

双端 reads 需要两条命令：

```bash
operon ingest --source /data/SRR999999999_1.fastq.gz \
  --entity-type run --entity-id RUN_000001 --role reads_r1

operon ingest --source /data/SRR999999999_2.fastq.gz \
  --entity-type run --entity-id RUN_000001 --role reads_r2
```

### 3.7 校验归档

```bash
operon verify
```

正常时每个文件状态为 `CHECKSUM_VERIFIED`。如果文件被移动、删除或篡改，会显示 `MISSING` 或 `CHECKSUM_FAILED`，且命令返回非零退出码。

### 3.8 标准化

```bash
operon standardize
```

默认将已验证文件**复制**到 `standardized/`，因此 raw、standardized 与后续 release 互不共享可写 inode。空间紧张且明确理解风险时，可以显式使用：

```bash
operon standardize --link hardlink
```

### 3.9 运行内置 QC

```bash
# 全部已归档文件
operon qc

# 只处理某个 assembly
operon qc --entity-type assembly --entity-id ASM_000001
```

FASTQ 默认按现代 Phred+33 解释质量字符；确认输入是旧式 Phred+64 时，使用
`operon qc --phred-offset 64`。重复率和 overrepresented 指标默认取前
1,000,000 条 reads，可通过正整数 `--sample-size` 调整。

查看 QC 长表：

```bash
operon report qc --entity-type assembly
```

导出长表和宽表：

```bash
operon report qc --export
# 生成 qc/aggregate/qc_results.tsv 与 qc_results.wide.tsv
```

### 3.10 外部 QC 指标（可选）

例如 BUSCO 结果整理为 TSV 后：

```bash
operon import-qc --file busco_results.tsv
```

外部 TSV 必填列：

```text
entity_type, entity_id, qc_stage, metric_name, metric_value,
tool, tool_version, parameter_set
```

可选列 `file_id`、`file_sha256`；提供时会与 manifest 交叉校验。具体格式见 How-to 手册。

### 3.11 运行封装式 BLAST / HMMER / BUSCO 分析

外部分析程序在 `config/tools.yaml` 中配置。默认模板提供 `blastn_nt`、
`blastp_nr`、`hmmsearch_pfam` 与 `busco_autolineage` recipe，需先按本机环境修改
启动方式与数据库路径：

第一次修改或编写 recipe 时，建议同时查看 [Recipe 配置参考](recipe-reference.md)；其中
集中说明输入选择、文件/目录 artifact、输出命名、占位符、数据库身份、缓存与 parser。

```yaml
tools:
  blastn:
    executable: blastn
    run_method: "conda run --no-capture-output -n blast"
    version_args: ["-version"]
    version_pattern: 'blastn:\s*([^\s]+)'
    recipes:
      blastn_nt:
        entity_type: assembly
        file_role: genome_fasta
        database: /data/db/nt        # 改成真实路径
```

检查程序是否可用并解析版本：

```bash
operon tools-check
```

对数据库中全部 assembly 执行 blastn_nt（自动挑选所有 `genome_fasta` 文件）：

```bash
operon analyze --analysis blastn_nt
```

按类目或实体筛选、先看计划：

```bash
operon analyze --analysis blastn_nt --entity-type assembly
operon analyze --analysis blastn_nt --entity-id ASM_000001
operon analyze --analysis blastn_nt --dry-run
```

查看同步到数据库的汇总和 top hits：

```bash
operon report analysis --analysis blastn_nt
operon report analysis --analysis blastn_nt --hits
```

对 annotation 蛋白文件执行 HMMER：

```bash
operon analyze --analysis hmmsearch_pfam
operon report analysis --analysis hmmsearch_pfam --hits
```

BUSCO 使用目录输出，并直接读取 `short_summary*.json`：

```bash
# config/tools.yaml 中确认 mamba 环境名和共享下载区：
# database: resources/busco_downloads
# database_version: odb12
# database_mode: mutable_cache
operon analyze --analysis busco_autolineage --entity-id ANN_000001 --threads 24 --dry-run
operon analyze --analysis busco_autolineage --entity-id ANN_000001 --threads 24
operon report analysis --analysis busco_autolineage --entity-id ANN_000001

# 对一个分类子集用显式 lineage 复核；不同 lineage 结果会共存
operon analyze --analysis busco_lineage --entity-id ANN_000001 --threads 24 \
  --param lineage_dataset=fabales_odb12.2
```

BUSCO 的 recipe 将 `output_name` 设为 `${file_id}.busco`；`-o` 使用
`${output_name}`，`--out_path` 使用 `${output_parent}`。这样还能避开 SEPP 把父路径中的
`fasta` 错换为 `jplace` 的缺陷。完整结果位于
`analysis/busco/<ANN_ID>/<FIL_ID>.busco/`。完整配置、JSON 指标名和
offline 数据集冻结方案见 How-to 第 7 节。

覆盖整个绿色植物时，建议继续以 auto-lineage 作为统一 QC 来源，再用内置的经验门限
profile 按实际 lineage 选择阈值：

```bash
operon evaluate --profile annotation_busco_viridiplantae_odb12_v1 \
  --entity-type annotation
```

该 profile 明确读取 `analysis:busco_autolineage`；后续固定-lineage复核不会静默改变判定。
门限来源、`value_by` 和结果共存语义见 How-to 第 12 节与 Recipe 配置参考。

每次成功执行都会记录工具、版本、完整命令、输入内容哈希、数据库身份与输出
内容哈希。文件和目录都受相同缓存校验；相同输入、参数、工具版本和数据库身份会自动命中缓存而跳过执行；
`--force` 可强制重跑。

### 3.12 运行规则引擎

```bash
operon evaluate --profile assembly_production_v1
operon report decisions
```

判定与原因示例：

```text
entity_type  entity_id   profile                 decision  reasons
assembly     ASM_000001  assembly_production_v1  PASS      -
assembly     ASM_000002  assembly_production_v1  FAIL      LOW_CONTIGUITY
```

修改 `config/profiles/*.yaml` 后重新 evaluate 不会覆盖旧判定，而会追加新的 decision；
`report decisions` 默认展示最新一条。profile 必须用 `kind: qc` 与同目录中的
`kind: taxonomy_coverage` 覆盖率画像区分。

### 3.13 人工策展（可选，但必须留痕）

```bash
operon curate \
  --entity-type assembly \
  --entity-id ASM_000002 \
  --profile assembly_production_v1 \
  --decision ACCEPT_WITH_WARNING \
  --reviewer "$USER" \
  --reason "该样本是已知低连续性参考，仅用于分类，不用于共线性分析" \
  --evidence "查看 2026-08-16 评审记录"
```

自动判定不会被覆盖，策展写入 `curated_*` 字段和 `changes` 审计表。

### 3.14 只读 SQL 查询

```bash
# 一个 protein 文件属于哪个 assembly/sample/organism
operon query "
SELECT f.file_id, f.entity_id AS annotation_id,
       a.assembly_id, s.sample_id, o.organism_id, o.scientific_name
FROM files f
JOIN annotations ann ON ann.annotation_id=f.entity_id
JOIN assemblies a ON a.assembly_id=ann.assembly_id
JOIN samples s ON s.sample_id=a.sample_id
JOIN organisms o ON o.organism_id=s.organism_id
WHERE f.file_role='protein_fasta'
"
```

`query` 使用只读连接和 authorizer：`SELECT` 与只读 schema PRAGMA 可用，DML、DDL、写 PRAGMA、ATTACH/VACUUM 会被拒绝。

### 3.15 创建 release

```bash
operon release --version 2026.08 --profile assembly_production_v1
```

默认 `copy` 模式。release 中包含 `manifest.tsv`、`exclusions.tsv`、`qc_summary.tsv`、
`profile_history.tsv`、`data_sources.tsv`、`source_links.tsv`、`provenance.json`、
`checksums.sha256` 等。

验证 release：

```bash
cd releases/2026.08
sha256sum -c checksums.sha256
```

## 4. 一条命令的单文件流水线

对于已经建好实体记录的单文件，可以直接运行：

```bash
operon run-pipeline \
  --source /data/GCA_999999999.fna.gz \
  --entity-type assembly \
  --entity-id ASM_000001 \
  --role genome_fasta \
  --profile assembly_production_v1
```

它依次执行：

```text
ingest -> standardize（含 checksum 复核） -> QC -> evaluate
```

## 5. 推荐的日常使用顺序

```bash
# 1. 录入/更新元数据并校验
operon import dataset               # 交互式完整数据集
operon add ...                       # 精确新增一个实体
operon import table --table ...     # CSV/XLSX 批量表格

# 2. 归档新数据
operon ingest ...
operon verify

# 3. 标准化与 QC
operon standardize
operon qc

# 4. 外部 QC / 封装分析
operon import-qc --file ...
operon tools-check
operon analyze --analysis blastn_nt
operon report analysis --analysis blastn_nt

# 5. 判定与发布
operon evaluate --profile ...
operon report decisions
operon release --version ... --profile ...
```

## 6. 常见问题速查

| 症状 | 原因与处理 |
|---|---|
| `no project.yaml found` | 当前目录不在项目内；用 `--project /path` 或先 `cd` 到项目根目录 |
| `already has FIL_... for role ... with sha256 ...` | 同实体同角色已有不同字节文件；raw 不可变，应为新数据建新 assembly/run 版本，而不是覆盖 |
| `CHECKSUM_FAILED` | 文件被改动；恢复原始文件或重新从源头归档（新实体版本） |
| table 导入报字段错误 | 阅读错误中的行号/字段；修改 CSV/XLSX，或先在 `config/schemas.yaml` 中扩展字段 |
| `query` 拒绝 UPDATE/PRAGMA | 这是设计行为；修改数据请使用受控命令（`add`、`import table`、`curate` 等） |
| `tools-check` 报 `cannot launch ...` | 修改 `config/tools.yaml` 的 `executable`/`run_method`；conda 环境写法见 How-to 手册 |
| `analyze` 报数据库不存在 | 把 recipe 的 `database` 改为真实 BLAST/HMM 数据库路径 |

下一步建议阅读 [How-to 操作手册](howto.md) 和 [架构说明](architecture.md)。
