# 文件与 QC 命令

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
  `--source-url` 时，自动把该 URL 记录为 `source_url`。SFTP 来源所需的 paramiko
  已包含在标准安装中。
- `remote://` 路径必须是安全的 root 相对路径，并且已存在于该远端的
  `operon-manifest.json`；下载前后验证清单 SHA-256/size。裸 `sftp://` 没有镜像清单
  可对照，下载后由 ingest 计算并登记新的本地身份。
- 自动识别 `.gz` 等压缩；源文件有 `gzip` 后缀但不是 gzip magic 时报错。
- 同实体同角色不同 SHA-256 会拒绝归档。
- 归档名称（实体 ID、角色和格式）必须是非空文件名组成部分，不得包含 `/`、`\`、控制字符或单独的 `.`/`..`。摄取和收养会在写入前拒绝解析后位于项目根目录之外的目标，包括符号链接越界和复用的 manifest 路径。
- 目标 canonical 路径已被不同字节占用时不会直接报错：占用者若被另一 manifest 行认领且
  字节一致（例如角色改名后遗留的文件），会先被搬到该行自己角色的 canonical 路径并更新
  `relative_path`；无人认领的中断残留会隔离为同目录的 `<文件名>.orphan-<sha前12位>`。
  两种情况都写入 `changes` 审计、保留原字节；占用字节与认领行 checksum 也不一致时才抛出
  `ConflictError`。
- `--move` 只有在归档副本完成 checksum 校验且 manifest/workflow 事务提交后才删除源文件；此前任一步失败都会保留源文件，便于重试。
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
- 新目标先写入同目录临时路径再原子发布；复制、checksum 校验或状态事务失败时会清理目标和临时链接，不留下半成品，可直接重试。

## qc

```bash
operon qc [--file-id FIL_...] [--entity-type TYPE] [--entity-id ID] \
            [--sample-size N] [--phred-offset {33,64,auto}] [--rehash]
```

- 默认处理所有 manifest 文件。
- `--sample-size` 控制 FASTQ 重复率/overrepresented 统计的前 N 条 reads 采样上限，必须为正整数，默认 1,000,000。
- `--phred-offset` 控制 FASTQ 质量分数解释，默认 `33`。只有明确的旧式数据才应指定 `64`；`auto` 在字符范围重叠、无法可靠区分时按现代 Phred+33 计算，并把 `quality_encoding` 记为 `ambiguous_assumed_phred33`。
- 默认复用 ingest/`verify` 最近一次完整 SHA-256 已通过且 stat 指纹完全不变的结果；
  指纹变化时自动重新计算 SHA-256。`--rehash` 无条件绕过该缓存，适合定期审计、迁移
  存储后的首轮检查或性能基线中的冷校验测试。对 annotation GFF3，它同时重新校验
  实际读取的 assembly 和 protein 关联输入，而不只是主 GFF3。
- assembly FASTA 的 `seqid -> length` 映射首次使用时写入
  `qc/cache/fasta_lengths/`；后续 QC 按完整内容身份复用。缓存缺失、格式损坏或身份不
  匹配时自动重建。`--rehash` 强制重新验证源文件 SHA-256，但内容身份未变时仍可复用
  长度索引，因为索引本身按已验证 SHA-256 键控。
- 结果按 `file_id + file_sha256 + input_identity` 写入 `qc_results`。
- 每个文件都有自己的 `QC_COMPLETE`、`QC_FAILED` 或 `QC_PENDING` 状态；实体状态取同层文件的最差值（`QC_FAILED` > `QC_RUNNING` > `QC_COMPLETE`），命令会列出每个文件状态。任一文件失败时命令返回非零。
- 每个文件的 `logs/workflow.jsonl` 记录包含 `duration_seconds`、实际 parser backend、
  主/关联输入身份及 `stage_timings_seconds`/`qc_timing` 分阶段高精度耗时；同一份详情
  也写入 `workflow_runs.execution_details`。字段定义与代表性复测集合见
  [内置 QC 性能诊断](../operations/qc-performance.md)。

## import-qc

```bash
operon import-qc --file TSV
```

必填列：`entity_type, entity_id, qc_stage, metric_name, metric_value, tool, tool_version, parameter_set`。
可选列：`file_id, file_sha256, metric_unit, evaluated_at`。
`file_id`/`file_sha256` 与 manifest 不一致时拒绝导入。
