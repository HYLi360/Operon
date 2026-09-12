# 外部分析

## 配置外部分析程序

外部程序统一在 `config/tools.yaml` 中配置，避免在每次运行时手工拼接命令。新项目由
`operon init` 自动生成；旧项目在第一次运行 `tools-check` 或 `analyze` 时若文件缺失
也会自动补建，不会覆盖已有配置。默认模板给出 `blastn_nt`、`blastp_nr`、
`hmmsearch_pfam`、`busco_autolineage`、`busco_lineage` 与命令链形式的 `rpsblast_cdd`
recipe；需要按本机环境修改启动方式与数据库路径。
本文保留日常操作所需的速查；完整执行模型、全部字段、占位符、缓存身份、parser 专用
选项和接入新工具的检查清单见 [Recipe 配置参考](../reference/recipe-overview.md)。

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
| `commands` | 有序的多步命令链（与 `arguments` 互斥）；各步共享确定性的 `${work_dir}` 暂存目录 |
| `parameters` | 允许由 `analyze --param NAME=VALUE` 设置的受约束运行参数 |
| `result_parser` | `blast_tabular`、`hmmer_tblout`、`hmmer_domtblout`、`rpsbproc_tabular`、`busco_json` 或 `none` |
| `result_glob` | 目录输出中 parser 要读取的结果文件 glob；BUSCO 通常为 `short_summary*.json` |
| `max_hits_per_query` | 每个 query 同步进 SQLite 的最大命中数 |
| `version` | 可选正整数（缺省 1，非法值报错）；与配置内容一起进入 `analyze` 记录的 recipe 快照，可用 `operon recipes history/show` 查看 |

可用占位符：

| 占位符 | 内容 |
|---|---|
| `${input}` / `${output}` / `${database}` / `${threads}` | 完整输入 artifact、完整输出 artifact、数据库路径、线程数 |
| `${input_parent}` / `${input_name}` / `${input_stem}` | 输入父目录、文件名、stem |
| `${output_parent}` / `${output_name}` / `${output_stem}` | 输出父目录、artifact 名称、stem |
| `${file_id}` / `${file_role}` / `${entity_type}` / `${entity_id}` | 当前 manifest 和实体标识 |

使用 `commands` 命令链的 recipe 还可使用 `${work_dir}`：每次运行确定性的中间产物暂存
目录，运行前重建、结束后清理。

所有 command block 都使用顶层 tool 的 `run_method`。调用其他程序的 block 可以增加
`version_args` 与 `version_pattern`；该程序的版本会与 argv、退出码一起记录在
`workflow_runs.execution_details.steps` 中。

目录输入也可用 `ingest` 归档；系统复制整棵目录并按相对路径与文件内容计算稳定哈希：

```bash
operon ingest --source proteome_set/ --entity-type organism \
  --entity-id ORG_000001 --role other --format directory
```

检查配置与程序版本：

```bash
operon tools-check
```

## 运行 BLAST、HMMER 与 BUSCO

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

批量控制：`--limit N` 只处理按 `file_id` 排序的前 N 个匹配文件；`--threads` 覆盖 recipe
默认线程数。

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
- `analysis_alignments`：带坐标的 parser 解析出的每条命中各一行（query/subject ID、rank、区间、e-value、bitscore、identity 百分比），不受 `max_hits_per_query` 截断
- `qc_results`：同名汇总指标以 `analysis:<recipe>` 为 stage 写入，可继续被 profile 使用

避免重复执行：缓存键由 `analysis_name + file_id + 输入 SHA-256 + 参数指纹 +
工具版本 + 数据库身份` 组成。所有条件都匹配时直接返回缓存；用 `--force` 才会重跑。
旧 completed 作业会被标记为 `superseded`，历史不丢失。精确指纹未命中时还有第二级
续跑：同一 `(analysis, file_id)` 的旧完成结果若输入未变且输出哈希验证一致，会被收养
（`adopted`）进当前指纹并在 `changes` 表留痕，不会重算——软件升级或 recipe 调整后
历史结果仍然可用。

每次运行前系统还会重新校验输入文件 SHA-256 或目录树哈希与 manifest 一致；被改动过
的 raw 输入会被直接拒绝，不会进入外部程序。

### 原生运行 BUSCO 并解析 JSON summary

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

## 运行其他外部工具

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
- `--tool` 记录从 `config/tools.yaml` 探测到的工具版本；`--input` 声明参与 provenance
  哈希的输入文件/目录（可重复）；`--threads`、`--cwd`、`--timeout` 与 `--backend`
  （默认 `local`，亦可选 `slurm` 或 `ssh`）控制执行方式。
- stdout/stderr 保存到 `logs/<WF_ID>.stdout.log` 和 `.stderr.log`。
- 运行记录同时写入 `logs/workflow.jsonl` 与 `workflow_runs` 表。
- 只有退出码为 0 且所有 `--expected-output` 存在且非空，才记录 `completed`；否则记录 `failed` 并返回非零。

## 序列筛选与域提取

parser 会把结构化命中行写入 `analysis_alignments` 的分析（`blast_tabular`、
`hmmer_domtblout`、`rpsbproc_tabular`）可以直接驱动序列级子集划分，无需再解析工具输出。
两个命令负责物化 FASTA 子集并记录 provenance；产物经 `adopt` 重新登记进 manifest。

**完整性必须用 e-value 阈值控制，而不是 max hits 上限。** `max_target_seqs` 式的截断会
让工具静默丢掉真实命中，此后任何"无命中序列"的判定都是错的。应保持工具输出的命中列表
完整（在 recipe 中调高或去掉 max hits 限制），把严格性放在筛选时的 `--evalue-max` 等
阈值上：`analysis_alignments` 保存全量解析命中，阈值可以随时调整而不必重跑分析。

`select-sequences` 输出一个 manifest FASTA 中序列有（或没有）匹配命中的子集：

```bash
# 在 e-value <= 1e-5 下有 Specific 类 CDD 命中的蛋白：
operon select-sequences --file-id FIL_000003 \
  --analysis rpsblast_cdd --hit-type Specific --evalue-max 1e-5 \
  --out analysis/external/cdd_positives.faa \
  --manifest analysis/external/cdd_positives.tsv

# 补集——没有任何匹配命中的序列：
operon select-sequences --file-id FIL_000003 \
  --analysis rpsblast_cdd --hit-type Specific --evalue-max 1e-5 \
  --require-no-hit \
  --out analysis/external/cdd_negatives.faa \
  --manifest analysis/external/cdd_negatives.tsv
```

筛选语义：重复的 `--analysis` 之间是 OR（任一 analysis 命中即入选，如 blast OR
hmmsearch）；`--subject-like`、`--evalue-max`、`--min-span`、`--hit-type` 之间是 AND。
跨 analysis 的交集（blast AND hmmsearch）分两步：先按第一个 analysis 筛选并 `adopt`
子集，再从 adopt 后的文件按第二个 analysis 筛选。需要按分类群差异化阈值时，按批次跑
分析，并用 `--entity-type`/`--entity-id` 限制计入的比对范围。

`extract-domains` 把命中区间本身从 FASTA 中切出，支持侧翼扩展与边界截断：

```bash
operon extract-domains --file-id FIL_000003 \
  --analysis rpsblast_cdd --subject-like 'bhlh%' --evalue-max 1e-5 \
  --flank 5 --min-length 30 --best-only \
  --out analysis/external/bhlh_domains.faa \
  --manifest analysis/external/bhlh_domains.tsv
```

`--best-only`（默认）每个 query 只保留 e-value 最优的区间；`--all-regions` 输出全部
区间，header 为 `<seqid>|region:<start>-<end>`。manifest TSV 记录全部候选区间，包括
被排除者及其原因。区间也可以来自外部坐标表（`--regions-tsv`，列为 `seqid,start,end`）
而非某个 analysis。

两个命令都只向 `workflow_runs` 写入含完整命令行的 provenance 步骤，不会自行登记产物。
显式 adopt 产物，后续 recipe 才能把它选作输入：

```bash
operon adopt --file analysis/external/bhlh_domains.faa \
  --entity-type annotation --entity-id ANN_000001 \
  --role bhlh_domain_fasta --format fasta --derived-from FIL_000003
```

完整参数契约见参考页：[extract-domains 与 select-sequences](../reference/cli-analysis.md)。

## 回注册外部工作流产出（adopt）

`operon export` 把选定实体物化为输入侧 manifest；snakemake/nextflow 等工作流消费后，
用 `operon adopt` 把派生 artifact 注册回数据库。被 adopt 的文件进入 `files`
manifest，可继续 QC、evaluate、export、release，也能被后续 recipe 按
`entity_type + file_role + format` 选为输入，从而串起级联分析。

单个产物：

```bash
operon adopt \
  --file analysis/external/ASM_000001/megahit/final.contigs.fa \
  --entity-type assembly --entity-id ASM_000002 \
  --role megahit_contigs --format fasta \
  --derived-from FIL_000001
```

批量模式供工作流在 rule 末尾一次回注册整批产出。manifest 可以是 JSON（list of
dict）：

```json
[
  {
    "path": "analysis/external/ASM_000002/megahit/final.contigs.fa",
    "entity_type": "assembly",
    "entity_id": "ASM_000002",
    "role": "megahit_contigs",
    "format": "fasta",
    "derived_from": ["FIL_000001"]
  }
]
```

也可以是带表头的 TSV（`format`、`compression`、`workflow_run_id` 列可选；
`derived_from` 列用逗号分隔多个 file_id）：

```text
path	entity_type	entity_id	role	format	derived_from
analysis/external/ASM_000002/megahit/final.contigs.fa	assembly	ASM_000002	megahit_contigs	fasta	FIL_000001,FIL_000004
```

```bash
operon adopt --from-manifest adopt_manifest.json
```

- 每条必须含 `path`、`entity_type`、`entity_id`、`role`、`derived_from`（至少一个已
  注册的 file_id）；相对路径按项目根解析。
- 产物物化到 `analysis/adopted/<entity_id>/`；同实体同 role 相同字节幂等复用，不同
  字节报 `ConflictError`。整批预检查后在同一事务中注册；提交前失败会回滚元数据、谱系、
  状态和工作流记录，并删除新建制品，保留既有文件。目标被冲突内容占用时，应先显式处理
  冲突再重试。完成状态的 JSONL 记录仅在提交后写出。
- role 由工作流自由命名；谱系边写入 `file_lineage` 表，可用 `operon query` 审计。

## 重建已捕获的 Conda 环境

通过显式的 `conda run`、`mamba run` 或 `micromamba run` 启动器执行分析。
在 `operon workflow show RUN_ID --format json` 中找到 `environment_id`，
或列出已存储快照，然后检查捕获状态和限制：

```bash
operon environments list
operon environments show ENVIRONMENT_ID
operon environments export ENVIRONMENT_ID > explicit.txt
micromamba create -p ./restored-env --file explicit.txt
# 也可使用：conda create -p ./restored-env --file explicit.txt
```

在兼容的相同 OS/架构上使用新的 prefix。已有安装包缓存可支持离线重建
（`micromamba create --offline ...`）；长期重建需要保留安装包或保持 channel 可访问，
导出不打包安装包文件。私有 URL 中的凭据会被移除。

环境文档在入库前已完成脱敏：hostname 只保留截断的 SHA-256 令牌，路径值中的 home 目录
前缀被折叠为 `~`。因此发布或导出的项目，其环境记录中不含可读的 hostname 和用户 home
路径，这些文档可以随 release 一起分发而无需额外清理。引入脱敏之前捕获的记录不会被改写，
可能仍保留原始值。

通过 `operon run-external` 在恢复环境中运行相同的小型输入和参数，比较存储的
`conda.package_fingerprint`、`system_fingerprint`、`hardware_fingerprint`，
以及实际输出校验和（或明确选择的数值容差）。独立的包指纹排除安装 prefix；包清单一致
并不能证明手动修改的已安装文件或 pip 包也已恢复。

环境捕获不改变既有缓存复用或自动沿用策略。测试实际重算时使用 `analyze --force`；
`run-external` 也会直接执行命令。旧运行缺少指纹并不意味着环境相同。
