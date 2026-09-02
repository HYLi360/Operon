# 项目与元数据命令

## 通用形式

```text
operon [--project PATH] [--version] <子命令> [参数]
```

- `--project PATH`：项目根目录或 `project.yaml` 路径；默认当前目录。该选项必须位于子命令之前。
- `--version`：显示 `operon` 版本。

## init

```bash
operon init [path] [--project-id PRJ_000001] [--name NAME]
```

创建 `project.yaml`、当前 schema 的空 `operon.sqlite`、`config/` 和生命周期目录，因此初始化后
可立即运行只读预览命令。`metadata/` 仅保留 0.4 迁移说明，不再生成可反向导入的空 TSV。
已存在 `project.yaml` 时报错。

## init-demo

```bash
operon init-demo [path] [--project-id PRJ_DEMO_001]
```

生成确定性合成数据，归档 9 个文件，运行 QC/评估，并创建 `releases/2026.08.demo`。

## status

```bash
operon status [--entity-type TYPE] [--entity-id ID] [--include-retired]
```

打印 `entity_state` 中的实体状态与说明。默认不显示有效退役实体；
`--include-retired` 用于历史审计。

## schema

```bash
operon schema            # 打印 schema 文件路径
operon schema --dump     # 打印 schema 全文
```

## migrate

```bash
operon --project /path/to/project migrate
```

应用当前版本的纯加法 database schema migration，然后输出目标版本、迁移账本、
`PRAGMA integrity_check` 和外键违规数量。该命令不执行 `ncbi-reconcile` 等业务修复；
迁移前应先运行只读的 `backup create`。

## import

```bash
operon import dataset

operon import table --table TABLE --template template.xlsx
operon import table --table TABLE --file data.csv \
  [--on-conflict {error,skip,update}] [--yes]
```

- `dataset`：启动纯英文 questionary 向导。已有 organism 按 scientific name 自动补全；source
  明确区分 INSDC/非 INSDC，并请求 database/repository、provider、record URL、citation 与
  License。非 INSDC 数据必须提供 citation/DOI 和 License。可跳过的其他字段或文件会在
  汇总页持续显示警告；进入任一章节修改后直接返回汇总页，不继续原始线性流程。最终确认前
  不修改项目。
- `table --template`：生成 `.csv` 或 `.xlsx` 空模板。XLSX 同时包含只读的 `schema` 工作表，列出类型、必填项、允许值与字段说明。
- `table --file`：读取 CSV 或 XLSX 第一张工作表，执行 schema/外键校验并打印逐行预览。
- 可导入表为 `organisms`、`samples`、`runs`、`assemblies`、`annotations`、`accessions`；系统管理的 `files` 不可由表格覆盖。表格更新或外键引用不能指向有效退役实体，应先显式 `restore`。
- 碰撞时 `error` 拒绝、`skip` 跳过已有行、`update` 逐字段更新并写入 `changes` 审计。非交互执行必须加 `--yes`；存在更新时还必须显式指定 `--on-conflict`。
- SQLite 是唯一可写 metadata 事实来源；旧的 `import-metadata`/`export-metadata` 已移除。

## add

```bash
operon add {organism|sample|run|assembly|annotation} \
  [--id INTERNAL_ID] [--field KEY=VALUE ...]
```

- `--field` 可重复。
- 不指定 `--id` 时自动分配下一个内部稳定 ID。
- 写入 SQLite 并记录审计；不会维护第二份可写 TSV 镜像。
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
  [--download-workers N] [--retries N] [--retry-backoff SECONDS] \
  [--resume-run WF_ID] [--plan-only]
```

至少提供一种来源：

- `--input PATH`：已有的 `assembly_data_report.json/jsonl`、`dataformat` TSV/CSV、
  NCBI Datasets ZIP，或解包目录；可重复。
- `--accession ACC`：调用 NCBI Datasets v2 API 下载 genome package；可重复。
- `--accession-file FILE`：每行一个 GCA/GCF accession，空行与 `#` 注释忽略。

默认行为：

- 在线下载请求 genome FASTA、GFF、protein FASTA、CDS FASTA 和 sequence report；
- 自动解析 organism、taxon、BioSample、BioProject、assembly 与 annotation 信息；
- 自动分配/复用内部 ID，paired GCA/GCF 指向同一 assembly；已有 canonical accession
  不因另一 alias 后到而改写，新 paired assembly 确定性优先 GCF；
- 完整版本化 accession 是 assembly 身份的一部分，新版本不会覆盖旧版本；
- ZIP/report 原件按 SHA-256 保存到 `raw/metadata/ncbi_datasets/`；
- 包内生物文件通过正常 `ingest` 路径进入 raw 和 files manifest；
- 更新 SQLite、实体状态、changes 审计与 workflow provenance。

常用变体：

```bash
# 只检查映射和将要创建的 ID，不写项目
operon ncbi-datasets --input ncbi_dataset.zip --dry-run

# 只导入元数据，不归档包内文件
operon ncbi-datasets --input assembly_data_report.jsonl --no-archive-files

# 在线只获取 genome FASTA 与 GFF
operon ncbi-datasets --accession GCF_000005845.2 \
  --include genome --include gff3

# 只计算缺失文件和下载分组，不下载、也不新增 workflow
operon ncbi-datasets --accession-file accessions.txt \
  --include genome --include sequence-report --plan-only

# 归档后继续生成 standardized 副本
operon ncbi-datasets --input ncbi_dataset.zip --standardize
```

`--email`/`--api-key` 也可通过 `NCBI_EMAIL`/`NCBI_API_KEY` 环境变量提供。
Biopython Entrez 只在少数 package 缺少 assembly report 时作为元数据回退；正常下载
使用 NCBI Datasets API，ZIP 采用流式写入和完整性检查。

`--batch-size` 默认 10、允许范围 1–100；`--download-workers` 默认 3、允许范围
1–10，使用 aiohttp 并发下载多个批次。每完成一个批次立即导入、归档并清理该批次暂存
ZIP。下载前会查询 manifest 和文件状态，逐 accession 计算真正缺少的 include；具有相同
缺失集合的 accession 才进入同一下载组。例如已有 GFF/CDS/protein、只缺 genome 与
sequence report 时，请求只包含 `genome,sequence-report`。annotation 的角色必须共同存在于
同一个未被 supersede 的 `ANN_`，不会从多个 annotation 拼出错误的“完整集合”。加
`--standardize` 时还要求对应 standardized 副本存在；全部满足的 accession 计入 summary
的 `skipped_existing`。`--plan-only` 仅支持 accession 下载来源，输出相同的
`download_plan`/`skipped_existing`，并以只读连接运行：不下载、不迁移 schema、不写数据库、
不新增 workflow；
`--no-archive-files` 模式下此 manifest 筛选不生效。
`--retries` 默认 4（允许范围 0–10），`--retry-backoff` 默认 1.0 秒并按指数退避；
SSL record layer failure、连接中断、超时、429/5xx 等瞬时错误会自动重试。
无效/撤回 accession 的 README-only package 会被识别并报告；其他批次继续导入，
最后汇总失败并返回非零。ZIP report 直接读取，文件成员逐个暂存到项目所在文件系统，
不会把所有批次或整包解压内容同时堆积在 `/tmp`。空间预检失败时会报告目标文件系统、
所需空间和可用空间。

中断与优雅停机：运行期间收到 Ctrl+C（SIGINT）或 SIGTERM 时，`ncbi-datasets` 会优雅
停机——并发下载被取消并停止接收新批次，当前批次的暂存 ZIP 被清理，本次运行会在
workflow provenance 中记录为 `interrupted`，每个 accession 的 `pending/downloading/
completed/failed/interrupted` 状态保存在 `adapter_run_items`。恢复时重跑相同请求并添加
`--resume-run WF_ID`；新 workflow 通过 `resumes_run_id` 链接旧失败运行，旧运行不会被改写。
请求指纹不同会被拒绝。已完成内容仍由 manifest 精确跳过。

## ncbi-reconcile

```bash
operon ncbi-reconcile
operon ncbi-reconcile --apply [--actor NAME]
```

默认只根据 SQLite 中的 metadata、文件 SHA-256、QC/analysis/release 引用生成修复计划，
不读取 raw 生物学内容，也不修改业务行。`--apply` 会以独立 repair workflow 执行计划：

- 相同 assembly/provider/version/date 且文件角色无不同 SHA 的重复 annotation 通过
  `entity_supersessions` 逻辑归并，原 `ANN_`、`FIL_` 和 raw 字节均保留；
- paired GCA/GCF 的 display canonical 恢复为已有文件证据指向的历史 canonical；没有历史
  证据时保留有效的当前 canonical，再无可用证据时才确定性优先 GCF；
- 非 canonical 来源的 assembly report/genome 使用
  `assembly_report_genbank/refseq`、`genome_fasta_genbank/refseq` 独立角色，归档文件同时
  物理移动到新角色的 canonical 路径（移动前统一校验所有目标路径，有字节冲突则整体拒绝；
  本地缺失的行不搬动，列入结果的 `skipped_path_moves`）；历史上已改名但未移动的行由
  `file_path_repairs` 通道补齐；
- 已有 QC 结果但被重导降级到早期状态的 annotation 恢复为 `QC_COMPLETE`；
- 每个字段的 before/after、原因、证据和 repair run 写入 `changes`。

出现同一目标来源角色不同 SHA 的冲突时，`--apply` 会拒绝执行，必须先人工审阅 dry-run。
应用后再次 dry-run 会排除已存在的 supersession；无新增异常时 summary 全部为 0。

## next-id

```bash
operon next-id {organism|sample|run|assembly|annotation|file}
```
