# NCBI Datasets 导入与下载

## 导入或下载 NCBI Datasets 组装

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
如果项目仍是旧 metadata schema，正式导入会保留自定义字段、补入 NCBI adapter
需要的 assembly 字段和 paired-source 文件角色，并升级到 1.4；dry-run 不修改 schema。

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

下载计划会先查询 manifest 和文件状态，为每条 accession 计算真正缺少的 include，再按
缺失集合分组。例如已有 GFF/CDS/protein、只需补 genome/sequence-report 时只请求后两类。
summary 的 `download_plan` 给出每个下载组，`skipped_existing` 给出无需下载的 accession。
正式下载前可以只生成同一计划，不下载也不写 workflow：

```bash
operon ncbi-datasets --accession-file accessions.txt \
  --include genome --include sequence-report \
  --plan-only > ncbi-download-plan.json
```

`--plan-only` 只接受 `--accession`/`--accession-file` 来源；它会检查 manifest 状态和本地路径，
因此应在实际项目（或包含 raw 的完整快照）上运行，不能用不含 raw 的 results/control 备份
推断生产环境的缺失文件。

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

中断后保留原失败运行，并以新运行恢复：

```bash
operon ncbi-datasets --accession-file accessions.txt \
  --include genome --include sequence-report \
  --resume-run WF_20260830_001233+0800_4f212100
```

恢复命令的 accession、include 和归档选项必须与原运行一致。新 workflow 的
`resumes_run_id` 指向旧运行；旧运行保持 `failed`/`interrupted`，每条 accession 的尝试
结果保存在 `adapter_run_items`，已完成内容由 manifest 精确跳过。

### 3.4 去重与版本规则

- taxon ID 相同：复用 organism；
- BioSample accession 相同：复用 sample；
- paired GCF/GCA：映射到同一个 assembly；新实体确定性优先 GCF 作为 display canonical，
  已有合法 canonical 不因另一 alias 后到而改写；
- 完整 accession 相同（包括 `.版本`）：幂等更新；
- accession 版本变化：创建新的 `ASM_`，旧文件不被覆盖；
- BioProject 对 assembly 是一对多，因此保存在 `assemblies.bioproject_accession`，
  不放进唯一 accession 映射表；
- 缺少 BioSample 时：为 assembly 创建专属 sample，重复导入时通过 assembly 复用。

如果包中的同一实体/角色与已归档文件字节不同，命令会在元数据落库前拒绝，提示使用
新的 assembly/annotation 版本。

paired GCA/GCF 的来源 report 或 genome 可以具有不同字节。canonical 来源继续使用
`assembly_report`/`genome_fasta`，另一来源使用带 `_genbank` 或 `_refseq` 后缀的受控角色，
因此不会把来源差异误判为同一角色覆盖。annotation 身份包含来源 accession、provider、
version 和 release date；旧库第一次重导时通过严格相同元数据兼容匹配接续原 `ANN_`。

### 3.5 修复旧 adapter 遗留

先只预览：

```bash
operon --project /path/to/project ncbi-reconcile > ncbi-reconcile-plan.json
```

审阅计划和 `warnings` 后应用：

```bash
operon --project /path/to/project ncbi-reconcile --apply --actor "$USER"
```

修复采用逻辑 supersession 和补偿 change，不删除旧 annotation、file、workflow 或 raw 字节。
如果计划发现目标来源角色已有不同 SHA，应用会终止；不要强制改 SQL，应先确认两个来源
记录是否确实属于不同 assembly/annotation 版本。

需要在旧库上完整执行备份、schema 迁移、预演、修复、验收和恢复下载时，请按
[NCBI adapter 恢复与迁移手册](../operations/ncbi-recovery-migration.md)逐项操作。
