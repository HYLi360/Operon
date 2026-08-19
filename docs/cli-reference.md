# 命令参考

所有命令的全局选项：

```text
operon [--project PATH] [--version] <子命令> [参数]
```

- `--project PATH`：项目根目录或 `project.yaml` 路径；默认当前目录。必须放在子命令之前。
- `--version`：显示 `operon` 版本。

子命令总览：

```text
init          初始化项目
init-demo     生成合成演示项目并跑完流水线
status        查看实体状态
schema        查看/导出 schema
import-metadata   校验并导入 metadata TSV
export-metadata   导出 metadata TSV
add           新增 organism/sample/run/assembly/annotation
add-accession 添加外部 accession 映射
ncbi-datasets 离线导入或在线下载 NCBI Datasets genome package
next-id       查看下一个稳定 ID
ingest        归档文件到 raw 并登记 manifest
verify        校验 manifest 文件的存在性与 SHA-256
standardize   生成 standardized 视图
qc            运行内置 QC
import-qc     导入外部 QC 指标
run-external  执行外部命令并记录 provenance
tools-check   检测外部程序与版本
analyze       执行配置文件中封装的 BLAST/HMMER/BUSCO 等分析
analysis-results  查看同步到数据库的分析汇总/hits
evaluate      运行规则引擎
curate        人工策展判定
release       创建 release
run-pipeline  单文件一站式流水线
qc-table      查看/导出 QC 表
decisions     查看当前判定
query         只读 SQL 查询
set-state     人工设置状态（审计）
```

## init

```bash
operon init [path] [--project-id PRJ_000001] [--name NAME]
```

创建 `project.yaml`、`config/`、`metadata/` 和生命周期目录。已存在 `project.yaml` 时报错。

## init-demo

```bash
operon init-demo [path] [--project-id PRJ_DEMO_001]
```

生成确定性合成数据，归档 9 个文件，运行 QC/评估，并创建 `releases/2026.08.demo`。

## status

```bash
operon status [--entity-type TYPE] [--entity-id ID]
```

打印 `entity_state` 中的实体状态与说明。

## schema

```bash
operon schema            # 打印 schema 文件路径
operon schema --dump     # 打印 schema 全文
```

## import-metadata

```bash
operon import-metadata [--replace]
```

- 读取 `metadata/*.tsv`，按 `config/schemas.yaml` 校验规范化。
- 默认按主键 upsert 合并；`--replace` 在单事务中重建完整快照，空表会清空对应表。
- 校验交叉引用（sample→organism、run→sample、assembly→sample、annotation→assembly、accession→实体、file→实体、文件 ID 引用）。
- 自动为 schema 中新增字段扩展 SQLite 列。
- 成功后相关实体状态设为 `METADATA_VALIDATED`。

## export-metadata

```bash
operon export-metadata [--include-generated]
```

- 导出 7 张手动元数据表到 `metadata/`。
- `--include-generated` 额外导出 QC 长表/宽表到 `qc/aggregate/`、decision 完整历史到 `reports/decisions.tsv`。

## add

```bash
operon add {organism|sample|run|assembly|annotation} \
  [--id INTERNAL_ID] [--field KEY=VALUE ...]
```

- `--field` 可重复。
- 不指定 `--id` 时自动分配下一个内部稳定 ID。
- 写入 SQLite，同时追加到对应 `metadata/*.tsv`，并记录审计。
- 示例：`operon add organism --field scientific_name="Escherichia coli" --field taxonomy_source=NCBI`。

## add-accession

```bash
operon add-accession \
  --internal-type {organism|sample|run|assembly|annotation} \
  --internal-id ID --namespace NS --accession ACC \
  [--version VERSION] [--primary]
```

例如 `--namespace NCBI_Assembly --accession GCA_000000001`。

## ncbi-datasets

```bash
operon ncbi-datasets \
  [--input PATH ...] \
  [--accession GCF_OR_GCA ...] \
  [--accession-file FILE] \
  [--include {genome,gff3,protein,cds,sequence-report} ...] \
  [--no-archive-files] [--standardize] [--dry-run] \
  [--no-preserve-source] [--email EMAIL] [--api-key API_KEY] \
  [--timeout SECONDS] [--batch-size N] \
  [--download-workers N] [--retries N] [--retry-backoff SECONDS]
```

至少提供一种来源：

- `--input PATH`：已有的 `assembly_data_report.json/jsonl`、`dataformat` TSV/CSV、
  NCBI Datasets ZIP，或解包目录；可重复。
- `--accession ACC`：调用 NCBI Datasets v2 API 下载 genome package；可重复。
- `--accession-file FILE`：每行一个 GCA/GCF accession，空行与 `#` 注释忽略。

默认行为：

- 在线下载请求 genome FASTA、GFF、protein FASTA、CDS FASTA 和 sequence report；
- 自动解析 organism、taxon、BioSample、BioProject、assembly 与 annotation 信息；
- 自动分配/复用内部 ID，paired GCA/GCF 指向同一 assembly；
- 完整版本化 accession 是 assembly 身份的一部分，新版本不会覆盖旧版本；
- ZIP/report 原件按 SHA-256 保存到 `raw/metadata/ncbi_datasets/`；
- 包内生物文件通过正常 `ingest` 路径进入 raw 和 files manifest；
- 更新 `metadata/*.tsv`、实体状态、changes 审计与 workflow provenance。

常用变体：

```bash
# 只检查映射和将要创建的 ID，不写项目
operon ncbi-datasets --input ncbi_dataset.zip --dry-run

# 只导入元数据，不归档包内文件
operon ncbi-datasets --input assembly_data_report.jsonl --no-archive-files

# 在线只获取 genome FASTA 与 GFF
operon ncbi-datasets --accession GCF_000005845.2 \
  --include genome --include gff3

# 归档后继续生成 standardized 副本
operon ncbi-datasets --input ncbi_dataset.zip --standardize
```

`--email`/`--api-key` 也可通过 `NCBI_EMAIL`/`NCBI_API_KEY` 环境变量提供。
Biopython Entrez 只在少数 package 缺少 assembly report 时作为元数据回退；正常下载
使用 NCBI Datasets API，ZIP 采用流式写入和完整性检查。

`--batch-size` 默认 10、允许范围 1–100；`--download-workers` 默认 3、允许范围
1–10，使用 aiohttp 并发下载多个批次。每完成一个批次立即导入、归档并清理该批次暂存
ZIP。`--retries` 默认 4（允许范围 0–10），`--retry-backoff` 默认 1.0 秒并按指数退避；
SSL record layer failure、连接中断、超时、429/5xx 等瞬时错误会自动重试。
无效/撤回 accession 的 README-only package 会被识别并报告；其他批次继续导入，
最后汇总失败并返回非零。ZIP report 直接读取，文件成员逐个暂存到项目所在文件系统，
不会把所有批次或整包解压内容同时堆积在 `/tmp`。空间预检失败时会报告目标文件系统、
所需空间和可用空间。

## next-id

```bash
operon next-id {organism|sample|run|assembly|annotation|file}
```

## ingest

```bash
operon ingest \
  --source FILE --entity-type TYPE --entity-id ID --role ROLE \
  [--format FMT] [--compression COMPRESSION] \
  [--source-url URL] [--move]
```

- `--role` 常用值：`genome_fasta`、`annotation_gff3`、`cds_fasta`、`protein_fasta`、`reads_r1`、`reads_r2`、`reads_single`。
- 自动识别 `.gz` 等压缩；源文件有 `gzip` 后缀但不是 gzip magic 时报错。
- 同实体同角色不同 SHA-256 会拒绝归档。
- `--move` 移动而非复制源文件。
- 成功后实体状态为 `CHECKSUM_VERIFIED`，并回填相关实体的文件 ID 字段。

## verify

```bash
operon verify [--file-id FIL_...]...
```

逐个检查 manifest 路径与 SHA-256；不指定 `--file-id` 时检查全部。失败返回非零。

## standardize

```bash
operon standardize [--file-id FIL_...]... [--link {copy|hardlink|symlink}]
```

- 默认 `copy`：raw/standardized 不共享 inode。
- `hardlink`/`symlink` 为显式兼容与节省空间选项。
- 目标已存在且 checksum 一致时跳过；不一致则拒绝覆盖。

## qc

```bash
operon qc [--file-id FIL_...] [--entity-type TYPE] [--entity-id ID] \
            [--sample-size N]
```

- 默认处理所有 manifest 文件。
- `--sample-size` 控制 FASTQ 重复率/overrepresented 统计的采样上限，默认 1,000,000。
- 结果按 `file_id + file_sha256 + input_identity` 写入 `qc_results`。
- 成功后实体状态为 `QC_COMPLETE`；失败为 `QC_FAILED` 并返回非零。

## import-qc

```bash
operon import-qc --file TSV
```

必填列：`entity_type, entity_id, qc_stage, metric_name, metric_value, tool, tool_version, parameter_set`。
可选列：`file_id, file_sha256, metric_unit, evaluated_at`。
`file_id`/`file_sha256` 与 manifest 不一致时拒绝导入。

## run-external

```bash
operon run-external \
  --step STEP --command 'CMD ARGS' \
  [--entity-type TYPE] [--entity-id ID] \
  [--parameter-set PS] [--expected-output PATH ...] \
  [--cwd DIR] [--timeout SECONDS]
```

- 命令用 shlex 解析，不经过 shell。
- 记录退出码、stdout/stderr 文件、起止时间到 `workflow_runs` 与 `logs/workflow.jsonl`。
- 仅当退出码为 0 且所有 `--expected-output` 非空时才判定成功。

## tools-check

```bash
operon tools-check
```

读取 `config/tools.yaml`，逐个执行 `version_args` 并用 `version_pattern` 提取版本。
程序缺失时显示 `ERROR` 与配置建议，不修改数据库；任一程序不可用时返回退出码 1。

## analyze

```bash
operon analyze --analysis NAME   [--entity-type TYPE] [--entity-id ID]   [--threads N] [--limit N] [--dry-run] [--force]
```

按 recipe 自动完成：

1. 从 files manifest 中选取匹配 `entity_type + file_role + format` 的文件或目录输入；
2. 按 `input_kind` 重新校验文件 SHA-256 或目录内容树哈希；
3. 探测并记录外部程序版本；
4. 渲染参数；除 `${input}`、`${output}`、`${database}`、`${threads}` 外，还支持
   `${input_parent}`、`${input_name}`、`${input_stem}`、`${output_parent}`、
   `${output_name}`、`${output_stem}`、`${file_id}`、`${file_role}`、`${entity_type}`、`${entity_id}`；
5. 命中 `analysis_jobs` 完成缓存时直接跳过，除非 `--force`；
6. 按 `output_kind: file|directory` 校验输出存在/非空并计算内容哈希；
7. 解析结果写入 `analysis_hits`/`analysis_results`，并同步汇总指标到 `qc_results`。

结果 parser 支持 `blast_tabular`、`hmmer_tblout`、`busco_json` 和 `none`。
`busco_json` 从目录的 `result_glob` 中选择唯一 specific JSON summary，写入 BUSCO
完整率、单拷贝/重复、碎片化、缺失、marker 数和 lineage 等指标。

默认 recipe：`blastn_nt`、`blastp_nr`、`hmmsearch_pfam`、`busco_autolineage`（可自行增删）。
`config/tools.yaml` 的完整字段和执行语义见 [Recipe 配置参考](recipe-reference.md)。

## analysis-results

```bash
operon analysis-results [--analysis NAME] [--entity-type TYPE] [--entity-id ID]   [--hits] [--limit N]
```

- 默认显示 `analysis_results` 汇总指标。
- `--hits` 显示 `analysis_hits` 中的 top hits。
- `--limit` 默认 20。

## evaluate

```bash
operon evaluate [--profile NAME] [--entity-type TYPE] [--entity-id ID]
```

- 默认 profile 来自 `project.yaml` 的 `qc.default_profile`。
- 指定 `--entity-id` 时必须同时指定 `--entity-type`。
- 保存 profile SHA-256 快照，追加 decision；状态机按判定更新。

## curate

```bash
operon curate \
  --entity-type TYPE --entity-id ID --profile NAME \
  --decision DECISION --reviewer REVIEWER --reason REASON [--evidence TEXT]
```

修改该 entity/profile 最新 decision 的 `curated_*` 字段并写入 `changes` 审计表。

## release

```bash
operon release --version VERSION --profile NAME \
  [--link {copy|hardlink}] [--copy-files]
```

- 默认 `copy`，生成与 raw/standardized 不共享 inode 的 release。
- `--copy-files` 是 `--link copy` 的兼容别名。
- 已存在的 version 目录会拒绝重复创建。
- 仅纳入 `current_decisions` 中 PASS、PASS_WITH_WARNINGS、ACCEPT_WITH_WARNING 的文件；其余实体写入 `exclusions.tsv`。

## run-pipeline

```bash
operon run-pipeline \
  --source FILE --entity-type {run|assembly|annotation} --entity-id ID \
  --role ROLE [--format FMT] [--compression C] [--source-url URL] \
  [--profile NAME]
```

依次执行 `ingest -> standardize -> qc -> evaluate`。任一阶段失败返回非零。

## qc-table

```bash
operon qc-table [--entity-type TYPE] [--entity-id ID] [--export]
```

- 打印长表；`--export` 额外写出 `qc/aggregate/qc_results.tsv` 与 `qc_results.wide.tsv`。

## decisions

```bash
operon decisions [--profile NAME]
```

显示 `current_decisions`（每个 entity/profile 的最新判定）。

## query

```bash
operon query "SQL"
```

只读 SQL。允许 SELECT 与只读 PRAGMA（如 `table_info`、`foreign_key_list`）；拒绝 DML/DDL/写 PRAGMA/ATTACH/VACUUM 等。

## set-state

```bash
operon set-state --entity-type TYPE --entity-id ID --state STATE \
  [--message TEXT] [--force]
```

- 校验合法迁移；非法迁移需 `--force`，且会写入 `changes` 审计表。
- 合法状态包括：`DISCOVERED`、`METADATA_FETCHED`、`METADATA_VALIDATED`、`DOWNLOAD_PENDING`、`DOWNLOADED`、`CHECKSUM_VERIFIED`、`STANDARDIZED`、`QC_RUNNING`、`QC_COMPLETE`、`ACCEPTED`、`REVIEW`、`REJECTED`、`RELEASED` 及 `DOWNLOAD_FAILED`、`CHECKSUM_FAILED`、`FORMAT_INVALID`、`METADATA_INVALID`、`STANDARDIZATION_FAILED`、`QC_FAILED`。

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 运行期/数据库/命令执行失败（如 verify 失败、QC 失败、外部命令失败） |
| `2` | `operon` 领域错误（配置错误、校验失败、实体不存在、冲突等） |
