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
verify        校验本地对象，或实时复核远端驻留对象的清单与实际内容
standardize   生成 standardized 视图
qc            运行内置 QC
import-qc     导入外部 QC 指标
run-external  执行外部命令并记录 provenance
tools-check   检测外部程序与版本
analyze       执行配置文件中封装的 BLAST/HMMER/BUSCO 等分析
remotes       列出配置的远程端并测试连通性
push          上传 manifest 文件到远程镜像
pull          从远程镜像恢复 manifest 文件
evict         验证远端副本后删除本地大文件
locations     查看文件的本地/远程驻留位置
evaluate      运行规则引擎
curate        人工策展判定
release       创建 release
run-pipeline  单文件一站式流水线
taxonomy      导入 NCBI Taxonomy 快照并编译覆盖率分母
report        查看 QC、判定、分析结果或 taxonomy 覆盖率报告
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
- `--source` 除本地路径外，还支持 `sftp://[user@]host[:port]/path` 与
  `remote://<remote名>/<相对远端root路径>`（`remote://` 引用 `project.yaml` 的
  `remotes:` 配置）；会先经 SFTP 下载到临时文件再走原归档流程。未显式给出
  `--source-url` 时，自动把该 URL 记录为 `source_url`。SFTP 来源需要安装可选
  依赖 `operon[remote]`（paramiko）。
- `remote://` 路径必须是安全的 root 相对路径，并且已存在于该远端的
  `operon-manifest.json`；下载前后验证清单 SHA-256/size。裸 `sftp://` 没有镜像清单
  可对照，下载后由 ingest 计算并登记新的本地身份。
- 自动识别 `.gz` 等压缩；源文件有 `gzip` 后缀但不是 gzip magic 时报错。
- 同实体同角色不同 SHA-256 会拒绝归档。
- `--move` 移动而非复制源文件。
- 成功后实体状态为 `CHECKSUM_VERIFIED`，并回填相关实体的文件 ID 字段。

## verify

```bash
operon verify [--file-id FIL_...]...
```

逐个检查 manifest 路径与 SHA-256；不指定 `--file-id` 时检查全部。本地字节缺失时，
不会只信任 `file_locations` 的缓存状态，而会连接每个标记为 `AVAILABLE` 的远程端，
重新核对远端清单和实际 SHA-256/目录树身份。至少一个实时副本通过时显示
`REMOTE_ONLY`；远端对象确定缺失或损坏时显示 `MISSING`，并同步更新
`file_locations` 与 `files.status`。SSH 暂时不可达、无法作出数据丢失判断时显示
`REMOTE_UNVERIFIED`，保留原 `files.status`。后两种情况及本地校验失败均返回非零。

`verify` 引起的 `files.status` 变化写入 `changes` 审计；旧 metadata schema 1.0/1.1
项目首次确认 `REMOTE_ONLY` 时会保留自定义字段并升级到 1.2。

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
  [--cwd DIR] [--timeout SECONDS] [--backend {local,slurm,ssh}]
```

- 命令用 shlex 解析，不经过 shell。
- 记录退出码、stdout/stderr 文件、起止时间到 `workflow_runs` 与 `logs/workflow.jsonl`。
- 仅当退出码为 0 且所有 `--expected-output` 非空时才判定成功。
- `--backend` 覆盖 `project.yaml` 的 `execution.backend`，可选 `local`（默认，
  本地子进程）、`slurm`（本地 Slurm 集群提交）或 `ssh`（在 SSH 远程主机上
  执行）。配置与前提见 [How-to 操作手册](howto.md)第 9 节。

## tools-check

```bash
operon tools-check
```

读取 `config/tools.yaml`，逐个执行 `version_args` 并用 `version_pattern` 提取版本。
程序缺失时显示 `ERROR` 与配置建议，不修改数据库；任一程序不可用时返回退出码 1。

## analyze

```bash
operon analyze --analysis NAME   [--entity-type TYPE] [--entity-id ID]   [--threads N] [--limit N] [--dry-run] [--force] [--keep-partial] [--backend {local,slurm,ssh}]
```

按 recipe 自动完成：

1. 从 files manifest 中选取匹配 `entity_type + file_role + format` 的文件或目录输入；
2. 按 `input_kind` 重新校验文件 SHA-256 或目录内容树哈希；
3. 探测并记录外部程序版本；
4. 渲染参数；除 `${input}`、`${output}`、`${database}`、`${threads}` 外，还支持
   `${input_parent}`、`${input_name}`、`${input_stem}`、`${output_parent}`、
   `${output_name}`、`${output_stem}`、`${file_id}`、`${file_role}`、`${entity_type}`、`${entity_id}`；
5. 命中 `analysis_jobs` 完成缓存时直接跳过，除非 `--force`；精确指纹未命中但存在
   输入相同、输出哈希验证一致的旧完成结果时，收养该结果（状态 `adopted`）而非重算；
6. 按 `output_kind: file|directory` 校验输出存在/非空并计算内容哈希；
7. 解析结果写入 `analysis_hits`/`analysis_results`，并同步汇总指标到 `qc_results`。

结果 parser 支持 `blast_tabular`、`hmmer_tblout`、`busco_json` 和 `none`。
`busco_json` 从目录的 `result_glob` 中选择唯一 specific JSON summary，写入 BUSCO
完整率、单拷贝/重复、碎片化、缺失、marker 数和 lineage 等指标。

`--backend` 覆盖 `project.yaml` 的 `execution.backend`，可选 `local`（默认）、
`slurm`（本地 Slurm 集群提交）或 `ssh`（在 SSH 远程主机上执行）；工具版本探测
也经同一后端执行。配置、前提与日志位置见 [How-to 操作手册](howto.md)第 9 节。
若 SSH 配置了 `storage_remote`，本地缺失但状态为 `REMOTE_ONLY` 的候选输入会先严格
验证远端清单和实际内容，再在远端原位使用。

中断与优雅停机：运行期间收到 Ctrl+C（SIGINT）或 SIGTERM 时，`analyze` 会优雅停机——

- 当前步骤的作业进程被完整终止：本地后端按进程组（含孙进程）先 SIGTERM 后
  SIGKILL；`slurm` 后端对排队/运行中的作业执行 `scancel`；`ssh` 后端终止远端
  `setsid` 进程组或对远端 Slurm 作业执行 `scancel`；
- 当前文件的 `analysis_jobs` 行被置为 `interrupted`（不会污染完成缓存），其半成品
  输出被删除（stdout/stderr 日志保留用于排查；加 `--keep-partial` 可保留半成品输出）；
- 批次不再处理后续文件，进程以退出码 130 退出；重跑同一命令即可从未完成的文件
  继续（`interrupted` 行不参与缓存命中）；
- 清理期间再次发送信号会立即强制退出（退出码 128+signum）。

若进程被 SIGKILL 等无法捕获的方式杀死，残留的 `RUNNING` 行会在下一次 `analyze`
启动时被清扫为 `interrupted`。

默认 recipe：`blastn_nt`、`blastp_nr`、`hmmsearch_pfam`、`busco_autolineage`（可自行增删）。
`config/tools.yaml` 的完整字段和执行语义见 [Recipe 配置参考](recipe-reference.md)。

## report analysis

```bash
operon report analysis [--analysis NAME] [--entity-type TYPE] [--entity-id ID] \
  [--hits] [--limit N]
```

- 默认显示 `analysis_results` 汇总指标。
- `--hits` 显示 `analysis_hits` 中的 top hits。
- `--limit` 默认 20。

## remotes

```bash
operon remotes
```

- 列出 `project.yaml` 的 `remotes:` 配置段中的远程端，并逐个测试连通性。
- 输出表格：`name` / `type` / `address` / `root` / `files`（远端清单条目数）/
  `status` / `error`。
- 任一远程端有 `error` 时返回退出码 1。
- 需要安装可选依赖 `operon[remote]`（paramiko）；未安装时只在使用 SSH/SFTP
  功能时报配置错误。
- 默认拒绝未知 SSH 主机密钥；通过 `known_hosts` 或 `host_key_sha256` 建立信任。

## push

```bash
operon push --remote NAME [--file-id FIL_...]...
```

- 把本地 manifest 文件上传到指定远程端（SFTP 镜像）；不指定 `--file-id` 时
  推送全部 manifest 文件。
- 文件和目录 artifact 均按 sha256 + size 校验；远端没有 `sha256sum` 时通过 SFTP
  流式哈希，绝不只比较大小。内容一致跳过；已有同路径不同内容会报
  `ConflictError`，绝不静默覆盖。
- 一次批量 push 只发布一次 `operon-manifest.json`。读—改—写期间通过远端原子目录
  `.operon-manifest.lock` 串行化写者，清单本身仍以唯一临时文件 + POSIX rename
  原子替换；写者异常退出会保留锁，错误会给出需人工核查的精确路径。
- 每次传输写入 `workflow_runs`（step 为 `push:<name>`）。单个文件失败不会中止其余
  文件；命令输出每项结果，任一项为 `error` 时整条命令最终返回退出码 1。
- 每个文件输出 `uploaded` / `indexed`（远端字节已存在并被纳入清单）/
  `skipped` / `error`。

## pull

```bash
operon pull --remote NAME [--file-id FIL_...]...
```

- 从指定远程镜像恢复文件；不指定 `--file-id` 时按远端清单遍历，但每条记录仍必须
  与本地 SQLite 中同一 `file_id + relative_path + sha256 + size_bytes` 完全一致；
  远端多出的未知对象不会被导入本地数据库。
- 同样按 sha256 + size 校验、幂等；本地已有不同字节时拒绝覆盖（`ConflictError`）。
- 恢复本地缺失文件后，其 `files.status` 恢复为 `CHECKSUM_VERIFIED`；传输记录
  写入 `workflow_runs`（step 为 `pull:<name>`），状态变化写入 `changes`。
- 单个条目失败后继续处理批内其他条目；只要存在 `error`，命令最终返回退出码 1。

## evict

```bash
operon evict --remote NAME [--file-id FIL_...]...
```

- 这是显式删除本地归档字节的操作；不指定 `--file-id` 时处理全部 manifest 文件。
- 删除前再次核对本地身份、远端清单身份和远端实际 SHA-256/目录树哈希；任一步不一致
  都拒绝删除。
- 成功后 `files.status` 为 `REMOTE_ONLY`，位置写入 `file_locations`，状态变化写入
  `changes`，并在 `.operon/placeholders/<file_id>.json` 写人类可读的小型指针。
- 单个条目校验或删除失败后继续处理批内其他条目；只要存在 `error`，命令最终返回
  退出码 1。
- `standardize` 和 `release` 前需先 `pull`；配置 `execution.ssh.storage_remote` 后，
  `analyze --backend ssh` 可直接使用远端输入。

## locations

```bash
operon locations [--file-id FIL_...]...
```

联合显示 `files` 与 `file_locations` 中的本地状态、远端名称、远端状态和最近校验时间。
该命令只读，不连接远端；需要实时复核时运行 `verify`（也会在 `push`、`pull`、
`evict` 或远端分析前置检查中按相应操作重新校验）。

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

## taxonomy

```bash
operon taxonomy import --input PATH --version VERSION
operon taxonomy list
operon taxonomy compile --profile NAME --taxonomy-version VERSION
operon taxonomy reference-sets
```

- `import`：归档并导入 NCBI Datasets `taxonomy_report.jsonl`/package，或至少含
  `nodes.dmp`、`names.dmp` 的官方 NCBI taxdump ZIP/tar；可选的
  `merged.dmp`/`delnodes.dmp` 会转成 TaxID alias；`--version` 是显式、不可变的
  taxonomy 版本标签。
- `list`：列出来源文件身份、版本、节点数和导入状态。
- `compile`：读取 `config/profiles/<NAME>.yaml` 中 `kind: taxonomy_coverage` 的作用域、
  rank、排除规则与阈值，生成
  `taxonomy/reference_sets/<NAME>@<VERSION>.tsv` 及 provenance sidecar。
- `reference-sets`：列出已冻结分母的 family/genus 行数、SHA-256 和编译时间。
- 同一 taxonomy 版本不同字节、同一 reference-set 身份不同 profile/结果都作为冲突
  拒绝；相同输入重复执行则幂等复用。

完整 profile 格式与不变量见 [NCBI Taxonomy 覆盖率](taxonomy-coverage.md)。

## report

```bash
operon report qc [--entity-type TYPE] [--entity-id ID] [--export]
operon report decisions [--profile NAME]
operon report analysis [--analysis NAME] [--entity-type TYPE] [--entity-id ID] \
  [--hits] [--limit N]
operon report coverage --reference-set NAME@TAXONOMY_VERSION [--scope metadata]
operon report coverage --reference-set NAME@TAXONOMY_VERSION --release VERSION
```

- `qc`：打印 QC 长表；`--export` 额外写出 `qc/aggregate/qc_results.tsv` 与
  `qc_results.wide.tsv`。
- `decisions`：显示 `current_decisions`（每个 entity/profile 的最新判定）。
- `analysis`：显示同步到数据库的分析汇总；`--hits` 改为显示 top hits，`--limit`
  默认 20。
- `coverage`：只对指定的冻结 taxonomy reference set 计算 family/genus 覆盖率。
  默认 `--scope metadata` 审计当前 `organisms`；`--release VERSION` 改为沿
  `release_members` 和 release 内冻结元数据统计已发布数据集，并复核创建时保存的
  metadata SHA-256。二者互斥。
- coverage 报告写入 `reports/coverage/COV_<input-hash>/`，包括分子/分母、完整目标、
  缺失清单、纳入/排除观察和 provenance。完全相同输入会校验并复用既有报告。

coverage 计算成功且达到 profile 中全部阈值时返回 0；报告成功生成但至少一个 rank
未达标时返回 1。阈值不写死在命令或代码中。

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
| `1` | 命令完成但检查未通过，或运行期失败（如 coverage 未达 YAML 阈值、verify/QC/外部命令失败） |
| `2` | `operon` 领域错误（配置错误、校验失败、实体不存在、冲突等） |
