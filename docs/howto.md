# How-to 操作手册

本手册以“任务 → 步骤”的方式组织，供日常使用者查阅。命令默认在项目根目录执行；如在项目外，请加全局参数 `--project /path/to/project`。

## 1. 如何批量录入元数据

小规模数据可用 `add`；成百上千条记录使用受控 CSV/XLSX 表格导入。先生成模板：

```bash
operon import table --table organisms --template organisms.xlsx
operon import table --table samples --template samples.csv
```

填写后先预览并确认：

```bash
operon import table --table organisms --file organisms.xlsx

# 自动化环境必须显式确认碰撞策略
operon import table --table organisms --file organisms.csv \
  --on-conflict update --yes
```

导入行为：

- 只允许人工管理的 entity/accession 表；`files`、QC、decision 等系统表不可覆盖。
- 先完成 schema、受控词汇和外键校验，再显示每行 `insert/update/unchanged`。
- `--on-conflict error` 拒绝修改已有行，`skip` 跳过，`update` 更新并记录逐字段审计。
- 不提供删除或“完整快照替换”语义；任一写入失败时该表的整个事务回滚。
- XLSX 的第一张 `data` 工作表用于导入；模板的第二张 `schema` 工作表仅供查看。

CSV 示例：

```text
assembly_id,sample_id,assembly_accession,assembly_version,assembly_level,assembly_method
ASM_000001,SMP_000001,GCA_000000001,1,chromosome,SPAdes v4.0.0
```

所有可用列请用 `operon schema --dump` 查看。

若要连同文件一起导入一个完整数据集，使用：

```bash
operon import dataset
```

向导界面暂时全部使用英文。source、taxonomy ID、sequencing、genome FASTA 或部分 annotation
文件都可以跳过；汇总审阅会保留醒目的 warning。选择 `Edit ...` 修改某一章节后会直接
回到汇总审阅，而不会接着运行原向导的后续章节。最终确认前不会修改 SQLite 或归档文件。

## 2. 如何扩展元数据字段

1. 打开 `config/schemas.yaml`。
2. 在对应表 `fields` 下添加字段，例如给 organism 加 `provenance_note`：

```yaml
tables:
  organisms:
    fields:
      provenance_note:
        type: string
        description: 项目自定义来源备注
```

3. 运行 `operon add ... --field provenance_note=...` 或 `operon import table ...`；系统会自动在 SQLite 表上增加该列（`ensure_metadata_columns`）。
4. 使用 `operon report metadata` 检查派生导出。

注意：CSV/XLSX 中的未知列会被拒绝；必须先改 schema，再导入数据。

## 3. 如何导入或下载 NCBI Datasets 组装

### 3.1 先预览已有元数据

适配器接受 NCBI Datasets 的 JSON/JSONL report、dataformat TSV/CSV、完整 ZIP
或解包目录。推荐第一次先运行 dry-run：

```bash
operon ncbi-datasets \
  --input /data/assembly_data_report.jsonl \
  --dry-run
```

输出中的 `new_ids` 是将要分配的 organism/sample/assembly/annotation 数量；
`metadata_rows` 包含将要 upsert 的行数。dry-run 不复制输入、不写数据库、不生成日志。
如果项目仍是 metadata schema 1.0，正式导入会保留自定义字段并补入 NCBI adapter
需要的 assembly 字段，升级到 1.1；dry-run 不修改 schema。

确认后去掉 `--dry-run`：

```bash
operon ncbi-datasets --input /data/assembly_data_report.jsonl
```

### 3.2 导入已有 ZIP 并自动归档文件

```bash
operon ncbi-datasets --input /data/ncbi_dataset.zip
```

程序会安全解包并识别：

| Datasets 文件 | `operon` 角色/实体 |
|---|---|
| `genomic.fna` | `genome_fasta` → assembly |
| `genomic.gff` / `.gff3` | `annotation_gff3` → annotation |
| `protein.faa` | `protein_fasta` → annotation |
| `cds_from_genomic.fna` | `cds_fasta` → annotation |
| `sequence_report.jsonl` / assembly report | `assembly_report` → assembly |

原始 ZIP 以 SHA-256 命名保存在 `raw/metadata/ncbi_datasets/`；生物文件通过
正常 `ingest` 进入 raw 和 `files.tsv`。重复导入相同包会复用相同内部 ID 与 file ID。

如果只需要元数据：

```bash
operon ncbi-datasets --input /data/ncbi_dataset.zip --no-archive-files
```

### 3.3 在线下载并自动归档

```bash
export NCBI_EMAIL='you@example.org'
# export NCBI_API_KEY='...'  # 可选，较高请求配额

operon ncbi-datasets \
  --accession GCF_000005845.2 \
  --accession GCA_000001405.29
```

大批量导入（默认 3 个并行下载 worker）：

```bash
operon ncbi-datasets --accession-file accessions.txt \
  --download-workers 3
```

默认批大小为 10（允许范围 1–100），aiohttp 最多同时下载 3 个批次（可设 1–10）。
每完成一个批次立即进入导入与归档，并删除该批次暂存 ZIP。Datasets ZIP 直接流式读取
report，生物文件一次只解出一个到项目文件系统，然后移入 `raw/`，不会在 `/tmp` 中
保留所有批次的 ZIP 和完整解包副本。

遇到 `[SSL] record layer failure`、连接中断、超时或 429/5xx 时无需手工重跑：

```bash
operon ncbi-datasets --accession-file accessions.txt \
  --retries 4 --retry-backoff 1.0
```

重试采用指数退避；默认即启用 4 次重试。如果某个 accession 无效、撤回或不可用，
NCBI 可能返回只有 README 的“空 package”；`operon` 会识别并报告具体 accession，
其他批次继续导入，最后汇总失败并返回非零退出码。需要进一步控制空间时，优先减少
`--batch-size` 或 `--download-workers`，并用重复的 `--include` 只请求必要文件类型。

默认下载所有适配器支持的类型。若只要组装和注释：

```bash
operon ncbi-datasets \
  --accession GCF_000005845.2 \
  --include genome \
  --include gff3
```

下载使用 NCBI Datasets v2 API；Biopython Entrez 在 package 缺少 report 且设置了
email 时作为元数据回退。下载 ZIP 会在项目所在文件系统中流式写入、验证为合法 ZIP，
再进入导入流程；不会使用容量可能较小的 `/tmp` tmpfs。

### 3.4 去重与版本规则

- taxon ID 相同：复用 organism；
- BioSample accession 相同：复用 sample；
- paired GCF/GCA：映射到同一个 assembly；
- 完整 accession 相同（包括 `.版本`）：幂等更新；
- accession 版本变化：创建新的 `ASM_`，旧文件不被覆盖；
- BioProject 对 assembly 是一对多，因此保存在 `assemblies.bioproject_accession`，
  不放进唯一 accession 映射表；
- 缺少 BioSample 时：为 assembly 创建专属 sample，重复导入时通过 assembly 复用。

如果包中的同一实体/角色与已归档文件字节不同，命令会在元数据落库前拒绝，提示使用
新的 assembly/annotation 版本。

## 4. 如何归档双端测序数据

1. 先建立 sample 与 run：

```bash
operon add run --field sample_id=SMP_000001 --field library_layout=PAIRED
```

2. 分别归档 R1/R2：

```bash
operon ingest --source /data/SRR001_R1.fastq.gz \
  --entity-type run --entity-id RUN_000001 --role reads_r1

operon ingest --source /data/SRR001_R2.fastq.gz \
  --entity-type run --entity-id RUN_000001 --role reads_r2
```

3. 校验并 QC：

```bash
operon verify
operon qc --entity-type run --entity-id RUN_000001
```

R1 与 R2 的 `read_count` 会分别以各自的 `input_identity` 保存，同时系统会写入 `paired_read_count_match`。

## 5. 如何归档组装与注释

```bash
# assembly FASTA
operon ingest --source /data/ASM.fna.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta

# annotation 三件套
operon ingest --source /data/ANN.gff3.gz \
  --entity-type annotation --entity-id ANN_000001 --role annotation_gff3
operon ingest --source /data/ANN.cds.faa.gz \
  --entity-type annotation --entity-id ANN_000001 --role cds_fasta
operon ingest --source /data/ANN.protein.faa.gz \
  --entity-type annotation --entity-id ANN_000001 --role protein_fasta

# 全部归档后再统一 QC，避免只处理到部分注释
operon qc
```

`ingest` 会自动回填：

- `assemblies.fasta_file_id`
- `annotations.gff_file_id` / `cds_file_id` / `protein_file_id`

## 6. 如何配置封装式外部分析程序

外部程序统一在 `config/tools.yaml` 中配置，不再每次手工拼接命令。新项目由
`operon init` 自动生成；旧项目在第一次运行 `tools-check` 或 `analyze` 时若文件缺失
也会自动补建，不会覆盖已有配置。默认模板给出 `blastn_nt`、`blastp_nr`、
`hmmsearch_pfam`、`busco_autolineage` 和 `busco_lineage` recipe；需要按本机环境修改启动方式与数据库路径。
本文保留日常操作所需的速查；完整执行模型、全部字段、占位符、缓存身份、parser 专用
选项和接入新工具的检查清单见 [Recipe 配置参考](recipe-reference.md)。

直接使用 PATH 中的程序：

```yaml
tools:
  blastn:
    executable: blastn
    run_method: ""          # 直接执行 blastn
    version_args: ["-version"]
    version_pattern: 'blastn:\s*([^\s]+)'
```

使用 conda 环境：

```yaml
tools:
  blastn:
    executable: blastn
    run_method: "conda run --no-capture-output -n blast"
```

也可以写绝对路径的 conda、容器或其他前缀：

```yaml
run_method: "/opt/conda/bin/conda run --no-capture-output -n blast"
run_method: "singularity exec /data/images/blast.sif"
```

recipe 关键字段：

| 字段 | 含义 |
|---|---|
| `entity_type` / `file_role` / `format` | 从 manifest 中自动选择输入 artifact 的范围；目录使用 `format: directory` |
| `input_kind` | `file`（默认）或 `directory`；执行前严格检查并重新计算内容哈希 |
| `output_kind` | `file`（默认）或 `directory`；两者都支持非空校验、内容哈希与缓存复核 |
| `output_subdir` / `output_suffix` | 控制默认的 `analysis/<recipe>/<entity_id>/<file_id>.<role><suffix>` 名称 |
| `output_name` | 可选的单层名称模板，覆盖默认名称；BUSCO 应使用 `${file_id}.busco` 以避开 SEPP 的 `fasta` 路径替换缺陷 |
| `database` | 参考数据库或共享下载缓存路径；相对路径按项目根目录解析 |
| `database_version` | 数据库版本标签，参与缓存身份 |
| `database_checksum` | 可选；提供后作为严格数据库身份 |
| `database_mode` | `reference`（默认，按内容识别）或 `mutable_cache`（共享可增长下载区，要求 `database_version`） |
| `arguments` | 命令参数；占位符见下表 |
| `parameters` | 允许由 `analyze --param NAME=VALUE` 设置的受约束运行参数 |
| `result_parser` | `blast_tabular`、`hmmer_tblout`、`busco_json` 或 `none` |
| `result_glob` | 目录输出中 parser 要读取的结果文件 glob；BUSCO 通常为 `short_summary*.json` |
| `max_hits_per_query` | 每个 query 同步进 SQLite 的最大命中数 |

可用占位符：

| 占位符 | 内容 |
|---|---|
| `${input}` / `${output}` / `${database}` / `${threads}` | 完整输入 artifact、完整输出 artifact、数据库路径、线程数 |
| `${input_parent}` / `${input_name}` / `${input_stem}` | 输入父目录、文件名、stem |
| `${output_parent}` / `${output_name}` / `${output_stem}` | 输出父目录、artifact 名称、stem |
| `${file_id}` / `${file_role}` / `${entity_type}` / `${entity_id}` | 当前 manifest 和实体标识 |

目录输入也可用 `ingest` 归档；系统复制整棵目录并按相对路径与文件内容计算稳定哈希：

```bash
operon ingest --source proteome_set/ --entity-type organism \
  --entity-id ORG_000001 --role other --format directory
```

检查配置与程序版本：

```bash
operon tools-check
```

## 7. 如何执行 BLAST、HMMSEARCH 与 BUSCO

修改 `database` 后执行：

```bash
# 全部 assembly 的 genome FASTA 对 nt 做 blastn
operon analyze --analysis blastn_nt

# 只处理 assembly 类目中的某一个实体
operon analyze --analysis blastn_nt --entity-id ASM_000001

# 全部 annotation 的 protein FASTA 对 nr 做 blastp
operon analyze --analysis blastp_nr

# 全部 annotation 的 protein FASTA 对 Pfam-A.hmm 做 hmmsearch
operon analyze --analysis hmmsearch_pfam
```

执行前预览计划与缓存命中情况：

```bash
operon analyze --analysis blastn_nt --dry-run
```

查看同步结果：

```bash
operon report analysis --analysis blastn_nt
operon report analysis --analysis blastn_nt --hits
```

结果去向：

- 完整输出 artifact（文件或目录）：`analysis/<recipe>/<entity_id>/<FIL_ID>.<role><output_suffix>`，例如
  `analysis/blastn_nt/ASM_000001/FIL_000001.genome_fasta.blastn.tsv`
- `analysis_jobs`：命令、工具版本、参数指纹、输入/数据库指纹、输出内容哈希、状态
- `analysis_results`：`query_count`、`hit_count`、`query_with_hit_count`、`best_evalue`
- `analysis_hits`：top hits 的 query、subject、指标值、rank
- `qc_results`：同名汇总指标以 `analysis:<recipe>` 为 stage 写入，可继续被 profile 使用

避免重复执行：缓存键由 `analysis_name + file_id + 输入 SHA-256 + 参数指纹 +
工具版本 + 数据库身份` 组成。所有条件都匹配时直接返回缓存；用 `--force` 才会重跑。
旧 completed 作业会被标记为 `superseded`，历史不丢失。精确指纹未命中时还有第二级
续跑：同一 `(analysis, file_id)` 的旧完成结果若输入未变且输出哈希验证一致，会被收养
（`adopted`）进当前指纹并在 `changes` 表留痕，不会重算——软件升级或 recipe 调整后
历史结果仍然可用。

每次运行前系统还会重新校验输入文件 SHA-256 或目录树哈希与 manifest 一致；被改动过
的 raw 输入会被直接拒绝，不会进入外部程序。

### 7.1 原生运行 BUSCO 并解析 JSON summary

默认 `busco_autolineage` recipe 使用普通 protein FASTA 输入和目录输出。BUSCO 的
`-o` 是短 run name，因此配置使用 `${output_name}`；`--out_path` 使用
`${output_parent}`，BUSCO 最终创建的目录恰好就是 `${output}`：

```yaml
tools:
  busco:
    executable: busco
    run_method:
      mode: conda
      bin: mamba
      env: busco_6.1.0
    version_args: ["--version"]
    version_pattern: 'BUSCO\s+([^\s]+)'
    recipes:
      busco_autolineage:
        description: BUSCO auto-lineage in protein mode
        entity_type: annotation
        file_role: protein_fasta
        format: fasta
        input_kind: file

        # 共享、可增长的 BUSCO lineage 下载区；运行前自动创建。
        database: resources/busco_downloads
        database_version: odb12
        database_mode: mutable_cache

        output_subdir: busco
        output_kind: directory
        # 不要让 BUSCO/SEPP 的输出父路径含有字符串 "fasta"；SEPP 会对完整路径
        # 执行 replace("fasta", "jplace")，从而生成一个不存在的父目录。
        output_name: ${file_id}.busco
        arguments:
          - -m
          - protein
          - -i
          - ${input}
          - -o
          - ${output_name}
          - --out_path
          - ${output_parent}
          - --download_path
          - ${database}
          - -c
          - ${threads}
          - --auto-lineage
          - --opt-out-run-stats
          - --tar
        result_parser: busco_json
        result_glob: short_summary*.json
```

如果希望完全可复现，先准备并冻结所需 lineage 数据，删除 `--auto-lineage`、指定
`--lineage_dataset`，加上 `--offline`，并把 `database_mode` 改为 `reference`；数据更新时
同步更新 `database_version` 或 `database_checksum`。`mutable_cache` 的身份由路径、显式
版本和可选 checksum 决定，不会因自动下载了另一个 lineage 而使旧作业缓存失效。

运行并查看结果：

```bash
operon tools-check
operon analyze --analysis busco_autolineage --entity-id ANN_000001 --threads 24 --dry-run
operon analyze --analysis busco_autolineage --entity-id ANN_000001 --threads 24
operon report analysis --analysis busco_autolineage --entity-id ANN_000001
```

完整目录类似：

```text
analysis/busco/ANN_000001/FIL_000003.busco/
```

`busco_json` 在 `result_glob` 命中的文件中优先选择唯一的
`short_summary.specific.*.json`；若仍有多个 specific summary，会拒绝猜测并要求收窄
glob。解析出的主要指标包括：

- `busco_complete_percent` / `busco_complete_count`
- `busco_single_copy_percent` / `busco_single_copy_count`
- `busco_duplicated_percent` / `busco_duplicated_count`
- `busco_fragmented_percent` / `busco_fragmented_count`
- `busco_missing_percent` / `busco_missing_count`
- `busco_n_markers`、`busco_domain`、`busco_lineage_dataset`
- 数据集日期、OrthoDB/数据集版本、物种数、NCBI taxid 和 BUSCO 报告版本

这些值同时写入 `analysis_results` 和 `qc_results`，可直接在 QC profile 中引用。例如：

```yaml
required:
  - metric: busco_complete_percent
    operator: ">="
    value: 95
    code: LOW_BUSCO_COMPLETENESS
warnings:
  - metric: busco_duplicated_percent
    operator: ">"
    value: 20
    code: HIGH_BUSCO_DUPLICATION
```

#### 指定 lineage、保留多个 BUSCO 结果

绿色植物跨度很大，不适合把整个项目强制到同一个 lineage。默认仍建议全库运行
`busco_autolineage`；需要对某个类群按固定标尺复核时使用 `busco_lineage`：

```bash
operon analyze --analysis busco_lineage \
  --entity-id ANN_000001 \
  --threads 24 \
  --param lineage_dataset=fabales_odb12.2
```

`lineage_dataset` 必须由 recipe 的 `parameters` 声明并通过 pattern 校验，不能借此注入
任意额外命令参数。lineage 同时进入输出名和缓存指纹，所以同一 annotation 的多个固定
lineage 结果并存，不以最新输出覆盖：

```text
analysis/busco_lineage/ANN_000001/FIL_000003.fabales_odb12.2.busco/
analysis/busco_lineage/ANN_000001/FIL_000003.eudicotyledons_odb12.2.busco/
```

QC 长表也使用带 lineage 的 stage 保存。宽表因同名指标只能有一列，会折叠为最近值，
所以正式判定应在 profile 中写 `source.qc_stage`；默认 BUSCO QC profile明确绑定
`analysis:busco_autolineage`，不会被后来运行的固定 lineage 结果静默替换。

## 8. 如何运行外部工具并保留 provenance

```bash
operon run-external \
  --step quast \
  --parameter-set quast_v1 \
  --entity-type assembly \
  --entity-id ASM_000001 \
  --expected-output qc/assemblies/ASM_000001/quast/report.tsv \
  --command 'quast -o qc/assemblies/ASM_000001/quast raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta'
```

- `--command` 使用 shell 风格引号解析，但不经过 shell，可避免注入。
- stdout/stderr 保存到 `logs/<WF_ID>.stdout.log` 和 `.stderr.log`。
- 运行记录同时写入 `logs/workflow.jsonl` 与 `workflow_runs` 表。
- 只有退出码为 0 且所有 `--expected-output` 存在且非空，才记录 `completed`；否则记录 `failed` 并返回非零。

## 9. 如何在 Slurm 集群或 SSH 远程主机上运行外部分析

`run-external` 与 `analyze` 默认在本地以子进程执行外部命令（`local` 后端）。通过
`project.yaml` 的 `execution:` 段可把执行后端切换为本地 Slurm 集群（`slurm`）或
SSH 远程主机（`ssh`，HPC 头节点与云虚拟机均适用）。所有后端共用同一份 provenance
契约：退出码、起止时间、日志路径照常写入 `workflow_runs` 与 `logs/workflow.jsonl`，
成功判定（退出码 0 且 `--expected-output` 非空）与输入/输出 SHA-256 校验不变。

配置示例（全部字段可选，旧项目无需修改）：

```yaml
# project.yaml
execution:
  backend: local            # local | slurm | ssh
  slurm:
    partition: ""
    time: "24:00:00"
    mem_gb: 0               # 0 = 不写 --mem
    extra_sbatch: []        # 追加的 #SBATCH 行，如 ["--gres=gpu:1"]
    setup_commands: []      # 如 ["module load blast/2.15"]
    poll_interval: 15       # squeue 轮询间隔（秒）
  ssh:
    host: ""
    user: ""
    port: 22
    key_file: ""            # 空 = SSH agent / 默认密钥；不支持密码
    remote_root: ""         # 项目在远端的绝对 POSIX 路径；空 = 共享文件系统
    storage_remote: ""      # REMOTE_ONLY 输入所在的 remotes: 名称
    scheduler: none         # none | slurm
    connect_timeout: 30
    known_hosts: ""         # 可选额外 known_hosts 文件
    host_key_sha256: ""     # 可选 SHA256:... 主机密钥指纹固定
    insecure_accept_unknown_host: false
```

命令行 `--backend {local,slurm,ssh}` 可单次覆盖 `execution.backend`：

```bash
operon analyze --analysis blastn_nt --backend slurm
operon run-external --step quast --backend ssh \
  --command 'quast -o qc/quast_out raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta' \
  --expected-output qc/quast_out/report.tsv
```

Slurm 后端的前提与行为：

- 项目目录必须位于与计算节点共享的文件系统上；`sbatch`/`squeue` 需在 PATH 中，
  缺失时报配置错误。
- 每个 run 在 `logs/` 下生成 `<run_id>.sbatch` 批处理脚本（`--cpus-per-task` 取
  线程数，可选 `--time`/`--partition`/`--mem` 与 `extra_sbatch`；`setup_commands`
  插入在命令前），以 `sbatch --parsable` 提交并按 `poll_interval` 轮询 `squeue`；
  作业消失后读取脚本末尾写入的 `<run_id>.exitcode` 退出码文件（失败时回退
  `sacct`）。stdout/stderr 指向 `logs/` 下的 `<run_id>.stdout.log` /
  `<run_id>.stderr.log`。本地和远端 Slurm 都遵守所配置的完整轮询间隔；exitcode
  在共享文件系统上短暂不可见时会先重试，提交输出前有警告行也能解析最终 job ID。
- 超时按 `--timeout`（秒）控制，超时尝试 `scancel`。

SSH 后端的前提与行为：

- 需要可选依赖 paramiko：`pip install 'operon[remote]'`（或
  `pip install paramiko`）；未安装时只在使用 SSH/SFTP 功能时报配置错误。
- `execution.ssh.scheduler: slurm` 时改为在远端主机走 sbatch/squeue 提交与轮询；
  否则直接在远端执行，并把 stdout/stderr 流式回传到本地日志文件。
- 常见的“先 SSH 登录节点，再进入计算节点”不需要第二次交互式 SSH：把登录节点配置
  为 `host`，设置 `scheduler: slurm`，`operon` 在登录节点运行 `sbatch`，Slurm 再把
  作业派发到计算节点。前提是登录节点与计算节点都能看到同一 `remote_root`。如果集群
  没有调度器、计算节点只能经 SSH 跳板访问，当前后端尚未提供第二跳命令配置。
- 配置绝对 POSIX `remote_root` 后，argv/cwd 中经过根目录包含性校验的项目路径前缀会
  改写为该远端路径；`..`/符号链接造成的路径逃逸会被拒绝。留空表示远端与本地共享
  文件系统。配置 `storage_remote` 时默认继承其 root；若又显式设置不同的
  `remote_root`，初始化执行器时即报配置错误，避免“存储验证通过但计算路径不存在”。
- 默认拒绝 known_hosts 中没有的主机。首次使用前应由管理员核对主机公钥后写入
  `~/.ssh/known_hosts`，或配置 `known_hosts` / `host_key_sha256`；
  `insecure_accept_unknown_host: true` 只适合明确接受风险的临时测试环境。
- `analyze` 自动把尚在本地的输入经 SFTP 上传到远端；远端没有 `sha256sum` 时会
  通过 SFTP 流式计算 SHA-256，目录则计算完整树哈希，不会退化为 size 校验。
  已有不同内容时拒绝覆盖。
- 配置 `storage_remote` 后，本地状态为 `REMOTE_ONLY` 的输入会先对照本地 SQLite、
  远端清单和远端实际内容，再直接在远端 root 原位读取，不会先下载到个人电脑。
- 同一分析批次复用一个惰性 SSH 连接完成工具版本探测、远端输入验证、数据库预检和
  各文件命令，批次结束后关闭；不会为每个文件的每一步重新握手。
- 运行前只删除严格限定在 `remote_root` 内的精确 expected-output 路径，避免旧结果
  冒充本次输出；拉回后再次比较本地/远端内容。已有本地输出不同则报冲突。
- SSH 直连命令超时时，`operon` 使用权限收紧的远端 PID 文件向该命令的进程组发送
  TERM，必要时再发送 KILL；若 PID 文件或终止命令不可用，错误会明确说明远端进程
  可能仍在运行。远端 Slurm 则使用 `scancel`。
- SSH 直连模式要求远端主机提供 util-linux 的 `setsid`（用于以独立进程组运行命令
  并可靠回传退出码）。Linux 发行版默认包含；macOS/BSD 远端没有该命令，直连命令会
  以 127 失败，此类远端应使用支持 Slurm 的 Linux 主机或本地后端。
- 远端 `reference` 数据库必须预先放在 recipe 的 `database` 路径并声明
  `database_checksum`；身份同时包含远端执行位置。`mutable_cache` 仍要求
  `database_version`，不存在时在远端自动建目录。

工具版本探测（`version_args + version_pattern`）在非 `local` 后端时也通过同一
后端执行，无需在远端手工准备。

单个 recipe 可用 `slurm:` mapping 覆盖 `execution.slurm` 的同名字段，例如给
BUSCO 单独调整内存与时间（完整字段见 [Recipe 配置参考](recipe-reference.md)）：

```yaml
recipes:
  busco_autolineage:
    slurm:
      mem_gb: 64
      time: "72:00:00"
```

> **测试说明**：Slurm 与 SSH 后端的自动化测试基于模拟环境（fake sbatch/squeue
> 与内存态 SSH/SFTP 实现），尚未在真实 HPC 集群上实测。首次在生产集群使用前，
> 建议先用一个短时小任务（如 `run-external --backend slurm/ssh` 跑一条
> `--command 'echo ok'`）验证提交、轮询与输出拉回链路。

## 10. 如何用远程存储备份与恢复归档

除第 15 节的本地备份外，`project.yaml` 的 `remotes:` 段可以配置一个或多个 SFTP
远程镜像，用于把 manifest 文件按内容校验地同步到远端：

```yaml
# project.yaml
remotes:
  mycluster:
    type: sftp
    host: hpc.example.org
    user: hyli360
    port: 22
    key_file: ~/.ssh/id_rsa
    root: /data/operon-mirror
    known_hosts: ~/.ssh/known_hosts
    # 也可固定管理员提供的指纹：host_key_sha256: SHA256:base64...
    insecure_accept_unknown_host: false
```

SFTP 功能需要可选依赖 paramiko：`pip install 'operon[remote]'`。

先列出配置并测试连通性（任一远程端报错时退出码为 1）：

```bash
operon remotes
```

推送与恢复：

```bash
# 全部 manifest 文件上传到远端镜像
operon push --remote mycluster

# 只推送指定文件
operon push --remote mycluster --file-id FIL_000001 --file-id FIL_000002

# 从远端镜像恢复（缺省恢复远端清单全部条目）
operon pull --remote mycluster

# 查看每个 file_id 的本地/远程驻留状态
operon locations
```

语义与本地的 raw 不变量一致：

- 普通文件和目录 artifact 全部按 sha256 + size 校验且幂等；目录哈希包含相对路径、
  空目录、文件内容和符号链接目标。远端已有不同字节时报 `ConflictError`；
- 远端维护带 `project_id` 的 `operon-manifest.json` v2 清单。清单原子替换要求
  SFTP 服务器支持 OpenSSH `posix-rename@openssh.com` 扩展；不支持时失败关闭，不会
  退化成先删除旧清单再写新清单。一次 push 批次只重写一次清单，并以远端原子目录
  `.operon-manifest.lock` 保护读—改—写；若进程崩溃留下锁，报错会提示精确路径，
  只能在确认没有活跃 push 后人工移除；
- 远端相对路径必须安全地位于 root 下；默认 pull 对每条记录重新核对本地 SQLite
  的 `file_id + relative_path + sha256 + size_bytes`，远端清单不能改写本地身份；
- 每次传输都写入 `workflow_runs` provenance（step 为 `push:<name>` /
  `pull:<name>`），成功位置写入 `file_locations`；
- push/pull/evict 的单个条目失败不会中止整个批次；每项都会输出结果并写 provenance，
  其余条目继续，任一项为 `error` 时命令最终返回退出码 1；
- `pull` 恢复本地缺失文件后，其 `files.status` 恢复为 `CHECKSUM_VERIFIED`，变化写入
  `changes` 审计。

### 10.1 本地只保留控制面，远端保存并计算大文件

这是个人电脑控制 HPC 最常见的推荐流程：

```bash
# 1. 首次归档仍在本地建立可信身份
operon ingest --source ASM.fna.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta

# 2. 推到远端；push 会校验实际远端内容并登记 file_locations
operon push --remote mycluster --file-id FIL_000001

# 3. 再次验证远端后删除本地大文件，留下 SQLite + 小型指针
operon evict --remote mycluster --file-id FIL_000001
operon locations --file-id FIL_000001

# locations 是缓存视图；verify 会重新连接远端并核对清单与实际内容
operon verify --file-id FIL_000001

# 4. 本地发命令；输入在远端原位消费，结果和 provenance 回到本地
operon analyze --analysis blastn_nt --backend ssh \
  --entity-type assembly --entity-id ASM_000001

# 5. 本地流程确实需要字节时再 hydrate
operon pull --remote mycluster --file-id FIL_000001
```

配置时令执行端引用同一个远端镜像：

```yaml
execution:
  backend: ssh
  ssh:
    storage_remote: mycluster   # 自动继承 host/user/port/key/root/host-key 策略
    scheduler: slurm            # 或 none，直接在 SSH 主机执行
```

`evict` 是显式删除本地字节的命令；不指定 `--file-id` 会处理全部 manifest 对象。
它只在远端清单身份和远端实际内容均通过严格校验后执行，并在 `changes` 中审计状态
变化。`standardize` 与 `release` 仍需要本地字节，应先 `pull`；外部 `analyze` 则可
直接消费 REMOTE_ONLY 输入。

本地缺失对象运行 `verify` 时也会实时检查远端，而不是把 `file_locations` 的
`AVAILABLE` 当作永久证明。远端对象已被带外删除或损坏时返回 `MISSING` 并更新缓存；
SSH 暂时不可达时返回检查结果 `REMOTE_UNVERIFIED` 和退出码 1，但保留最近一次持久
状态，避免把网络故障误判为数据丢失。

也可以不经镜像配置，直接从 URL 归档远程文件（未显式给 `--source-url` 时自动
记录该 URL）：

```bash
operon ingest --source sftp://hyli360@hpc.example.org:22/data/ASM.fna.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta

operon ingest --source remote://mycluster/raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta
```

分工：本节是“按内容校验的远端镜像”；第 15 节仍是针对 `operon.sqlite`、`config/`
等本地目录的整体备份与迁移。

## 11. 如何导入外部 QC 结果

把 BUSCO/QUAST/FastQC/fastp 等输出整理为 TSV：

```text
entity_type	entity_id	file_id	qc_stage	metric_name	metric_value	metric_unit	tool	tool_version	parameter_set
assembly	ASM_000001	FIL_000001	busco	complete_percent	96.4	percent	busco	5.8.2	embryophyta_odb12
assembly	ASM_000001	FIL_000001	quast	contig_n50	2845913	bp	quast	5.2.0	default
```

必填列：`entity_type, entity_id, qc_stage, metric_name, metric_value, tool, tool_version, parameter_set`。

可选列：

- `file_id`：必须是 manifest 中存在的文件，且其 entity 与行中的 entity 一致。
- `file_sha256`：如果给出，必须与 manifest 中该文件的 SHA-256 一致。

导入：

```bash
operon import-qc --file external_qc.tsv
```

## 12. 如何创建新的 QC profile

在 `config/profiles/` 下添加 YAML 文件，例如 `phylogenomics_v1.yaml`：

```yaml
kind: qc
version: 1
description: 系统发育基因组学准入规则
applies_to: [assembly]
required:
  - metric: sha256_match
    operator: "=="
    value: 1
    code: SHA256_MISMATCH
  - metric: parseable
    operator: "=="
    value: 1
    code: FORMAT_INVALID
  - metric: busco_complete_percent
    operator: ">="
    value: 90
    code: LOW_BUSCO_COMPLETENESS
  - metric: contamination_percent
    operator: "<="
    value: 3
    code: HIGH_CONTAMINATION
warnings:
  - metric: busco_duplicated_percent
    operator: ">"
    value: 20
    code: HIGH_BUSCO_DUPLICATION
```

支持的运算符：`>=`、`<=`、`>`、`<`、`==`、`!=`、`between`（需 `min`/`max`）、`in`/`not_in`（需 `values`）、`exists`。

运行：

```bash
operon evaluate --profile phylogenomics_v1
operon report decisions --profile phylogenomics_v1
```

每次 evaluate 都会保存 profile 内容快照，并追加 decision 历史。

### 12.1 按分类器指标选择门限：`value_by`

当一个数值指标的合理门限取决于另一个指标时，可用 `value_by`。绿色植物 BUSCO
auto-lineage 是典型场景：整个 Viridiplantae 不适合使用同一个 lineage，也不应要求用户
逐物种查询 taxonomy 后手工选择；先让 BUSCO 自动选择 lineage，再让 profile 根据实际
`busco_lineage_dataset` 选择完整率门限：

```yaml
kind: qc
version: 1
description: BUSCO 6.1.0 / odb12.2 auto-lineage gates for Viridiplantae
applies_to: [annotation]

required:
  - metric: busco_complete_percent
    operator: ">="
    value_by:
      metric: busco_lineage_dataset
      values:
        eudicotyledons_odb12.2: 70
        poales_odb12.2: 80
        fabales_odb12.2: 75
        lamiales_odb12.2: 70
        embryophyta_odb12.2: 70
        liliopsida_odb12.2: 75
        brassicales_odb12.2: 80
        solanales_odb12.2: 75
        malpighiales_odb12.2: 75
        rosaceae_odb12.2: 85
        chlorophyceae_odb12.2: 60
        viridiplantae_odb12.2: 65
        rosales_odb12.2: 90
        trebouxiophyceae_odb12.2: 80
        chlorophyta_odb12.2: 85
      unknown: warning
    source:
      qc_stage: analysis:busco_autolineage
    code: BUSCO_COMPLETENESS_FAIL
    unknown_code: BUSCO_LINEAGE_UNCONFIGURED
```

`value_by.metric` 和被判定的 `metric` 从同一个来源读取。selector 的字符串值命中
`values` 后，所选数值临时成为普通 `value`，再执行原有 operator。

未知 selector 的策略：

| `unknown` | required rule 的行为 |
|---|---|
| `warning` | 不判 required 失败，但产生 warning；适合 BUSCO 新增 lineage |
| `fail` | required 失败 |
| `not_evaluated` | 视为缺少可用门限，最终 `NOT_EVALUATED` |
| `ignore` | 跳过该规则 |

warning rule 主要使用 `warning` 或 `ignore`；其他策略不会把 warning 提升为 required
失败。缺省策略为 `not_evaluated`，避免遇到未配置类别时静默放行。

### 12.2 用 `source.qc_stage` 固定指标来源

同一实体可以同时拥有 auto-lineage 和多个固定-lineage BUSCO 结果。正式判定不能依赖
“同名指标里最后写入哪一条”，因此规则可显式限定来源：

```yaml
source:
  qc_stage: analysis:busco_autolineage
```

如果该 stage 没有 required metric，结果为缺少指标/`NOT_EVALUATED`；不会回退到其他
stage 的同名结果。固定 lineage 也可以作为 profile 来源，例如：

```yaml
source:
  qc_stage: analysis:busco_lineage:lineage_dataset=fabales_odb12.2
```

### 12.3 内置绿色植物 BUSCO profile

新项目会生成：

```text
config/profiles/annotation_busco_viridiplantae_odb12_v1.yaml
```

它明确绑定 `analysis:busco_autolineage`，包含四类判定：

1. lineage-specific complete 下限：低于下限 `FAIL`；
2. complete 未达到建议 PASS 线：`PASS_WITH_WARNINGS`；
3. fragmented 超过 lineage 经验高位：`BUSCO_FRAGMENTED_HIGH`；
4. duplicated 超过 lineage 经验高位：`BUSCO_DUPLICATION_REVIEW`，只复核、不直接 FAIL。

门限来自 2026-08-27 对 532 个绿色植物 annotation 的 BUSCO 6.1.0/odb12.2 分布分析，
是当前研究集合的经验 profile，不是 BUSCO 官方通用标准。升级 BUSCO/OrthoDB、改变物种
范围或研究用途时，应复制为新的版本化 profile 并重新估计，不能静默修改旧 profile。

运行：

```bash
operon evaluate \
  --profile annotation_busco_viridiplantae_odb12_v1 \
  --entity-type annotation
operon report decisions \
  --profile annotation_busco_viridiplantae_odb12_v1
```

旧项目的 `operon init` 配置不会被自动覆盖；需要从新项目模板复制该 profile，或按本文
示例在原项目 `config/profiles/` 中创建同名版本化 YAML。

## 13. 如何审计科与属的 taxonomy 覆盖率

新项目带有 `config/profiles/coverage_viridiplantae_v1.yaml` 示例。先根据研究范围修改
clade 根 TaxID、family/genus 层级、extinct/environmental/unclassified 等排除规则与
覆盖率阈值，并以新的版本化文件名保存。coverage profile 必须声明
`kind: taxonomy_coverage`；阈值不会从代码默认值补入。

导入一个显式版本的 NCBI Taxonomy（Datasets `taxonomy_report.jsonl`/package 或官方
taxdump archive），并把 profile 编译成不可变分母：

```bash
operon taxonomy import \
  --input /data/taxonomy/2026-08-01/taxonomy_report.jsonl \
  --version 2026-08-01
operon taxonomy compile \
  --profile coverage_viridiplantae_v1 \
  --taxonomy-version 2026-08-01
```

传统 taxdump 没有 extinct 布尔字段；若 profile 使用 `exclude_extinct: true`，compile
会明确拒绝该组合。应显式改用化石 TaxID 子树/名称规则，或导入带 extinct 标注的
Datasets taxonomy report，不能让排除规则静默失效。

默认从当前 `organisms` 元数据审计“库里采了什么”：

```bash
operon report coverage \
  --reference-set coverage_viridiplantae_v1@2026-08-01
```

审计一个已发布数据集时，改用 release 口径：

```bash
operon report coverage \
  --reference-set coverage_viridiplantae_v1@2026-08-01 \
  --release 2026.08
```

报告给出 family/genus 的分子、分母、覆盖率、阈值与缺失清单。release 口径读取
release 内冻结的元数据并以 `release_members` 和创建时保存的 metadata SHA-256 校验，
不受活动库后续 TaxID 修改影响；release 内快照被改动则明确拒绝。
完整 profile 字段、快照身份、幂等/冲突规则、输出文件和 GTDB 局限见
[NCBI Taxonomy 覆盖率](taxonomy-coverage.md)。

## 14. 如何人工覆盖判定而不丢失审计

```bash
operon curate \
  --entity-type assembly --entity-id ASM_000003 \
  --profile assembly_production_v1 \
  --decision PASS \
  --reviewer "zhang.san" \
  --reason "N 含量偏高来自已知着丝粒，不影响本研究" \
  --evidence "见 cytology report 2026-08-01"
```

规则：

- 只修改该 entity/profile 的**最新** decision 的 `curated_*` 字段。
- 自动判定与更早的历史 decision 保持原样。
- 修改同时写入 `changes` 审计表。
- 状态机按策展后的 decision 更新为 ACCEPTED/REJECTED/REVIEW。

## 15. 如何用 SQL 查询

`query` 是只读的。常用查询示例：

```bash
# 所有 PASS/FAIL 状态
operon query "SELECT entity_type, entity_id, decision, reason_codes FROM current_decisions"

# 一个 assembly 及其文件
operon query "
SELECT a.assembly_id, f.file_id, f.file_role, f.relative_path, f.sha256
FROM assemblies a JOIN files f ON f.entity_id=a.assembly_id
WHERE a.assembly_id='ASM_000001'
"

# QC 指标宽表式查看
operon query "
SELECT entity_id, metric_name, metric_numeric, metric_unit, evaluated_at
FROM qc_results
WHERE entity_type='assembly' AND qc_stage='assembly_basic'
ORDER BY entity_id, metric_name
"

# 查看数据库内 schema 版本标记
operon query "SELECT entity_id, state, message FROM entity_state WHERE entity_type='database'"
```

`SELECT`、`PRAGMA table_info` 等只读操作可用；`UPDATE`、`INSERT`、`DROP`、`PRAGMA user_version=...`、`ATTACH` 等会被拒绝。

如果目标是从一个 organism 根 accession 查看完整数据树，不必手写 JOIN：

```bash
operon show NCBI_Taxonomy:3702
operon show ORG_000001
operon show GCF_000001405.40 --json
```

`show` 会把匹配到的任意实体向上解析到 organism，再列出其全部 sample、run、assembly、
annotation、accession 和 file。裸 accession 有歧义时使用 `namespace:accession`。

## 16. 如何备份和迁移项目

推荐由 `backup` 命令创建 SQLite 一致快照，而不是在数据库运行期间直接复制文件：

```bash
# 配置、SQLite、审计与 workflow 日志
operon backup create --output /backups/my-project-control --scope control

# 另加 QC、analysis、reports、taxonomy、releases
operon backup create --output /backups/my-project-results --scope results

# 再加 raw、standardized 和本地占位符等全部项目管理数据
operon backup create --output /backups/my-project-full --scope full

operon backup verify --input /backups/my-project-full
```

若使用 `REMOTE_ONLY`，本地备份还必须覆盖含 `file_locations` 的 SQLite；远端应独立
备份镜像 root（包括 `operon-manifest.json`）和实际对象。占位符本身不是恢复依据，
只有本地 `files` 身份与远端清单/字节同时保留，才能在新电脑上安全 hydrate。

`report metadata` 不是备份：它只导出便于浏览和交换的 metadata/manifest TSV，不包含
完整 QC、decision、changes、workflow、remote location 和数据库迁移状态。

更稳妥的做法是定期创建 release，并在 release 目录执行 `sha256sum -c checksums.sha256`。

备份策略可按重建成本分级：

| 类型 | 示例 | 策略 |
|---|---|---|
| 不可替代 | 原始 FASTQ、外部原始下载、人工整理的元数据 | 多副本备份、checksum、不可变 |
| 重建昂贵 | assembly、注释、全基因组比对 | 保存并备份 |
| 易于重建 | 临时索引、中间排序文件、缓存 | 可清理，但保留生成规则 |

该原则成立的前提是工具环境与数据库版本都能重建，否则“可重建”只是理论上的。

旧版 v1 数据库**无需手工迁移**：当前程序打开数据库时会自动迁移 `qc_results` 与 `decisions` 到 v2 结构，旧 QC 数据以 `legacy:` 输入身份保留，旧 decision 可继续通过 `current_decisions` 读取。

## 17. 如何续跑失败任务

所有核心步骤都幂等：

- 同一文件重复 `ingest`：相同 SHA-256 返回同一 `FIL_`，不重复复制。
- `standardize`：目标已存在且 checksum 相同则跳过。
- `qc`：同一 `input_identity + stage + metric + tool/version/parameter_set` upsert，不产生重复行。
- `evaluate`：追加新 decision，不覆盖历史。
- `release`：版本目录已存在时拒绝重复创建，不会悄悄覆盖。
- `taxonomy compile`：相同 profile/taxonomy/TSV 复用；身份相同而内容不同则拒绝覆盖。
- `report coverage`：输入成员、profile 和 reference-set 身份相同则校验并复用旧报告。
- `analyze`：Ctrl+C/SIGTERM 优雅停机后，当前作业记为 `interrupted`、半成品输出被清理；
  重跑时已完成文件走缓存，输入未变且输出验证一致的旧结果会被收养（`adopted`），只有
  真正未完成的文件才重新计算。

因此从中断处直接重跑相同命令即可。可通过 `status` 查看每个实体当前处于哪个状态。

## 18. 如何排查 checksum 与格式问题

```bash
operon verify          # 找 MISSING / CHECKSUM_FAILED
operon status          # 看实体级状态
operon query "SELECT file_id, entity_type, entity_id, file_role, status, relative_path FROM files WHERE status != 'CHECKSUM_VERIFIED'"
```

典型处理：

| 状态 | 建议 |
|---|---|
| `REMOTE_ONLY` | 预期状态；用 `operon locations` 看缓存位置、用 `operon verify` 实时复核，需本地字节时执行 `pull` |
| `REMOTE_UNVERIFIED`（仅 verify 输出） | 远端暂时不可达，未确认副本是否仍在；检查 SSH/网络后重试 `verify` |
| `MISSING` | 恢复文件到 `relative_path`，或从源头重新归档为新实体版本 |
| `CHECKSUM_FAILED` | 不要继续 QC；确认文件是否被误改，从原始来源恢复 |
| `QC_FAILED` | 查看 `operon report qc` 中 `parseable=0` 的文件，以及 `logs/workflow.jsonl` 中的错误 |
| 格式解析失败 | 用外部工具（如 `seqkit stats`、GFF3 validator）检查；修复后作为新版本归档，不要覆盖 raw |
