# QC、规则引擎与状态机

## QC 流水线

内置 QC 默认且必须加载 Cython 流式解析器；纯 Python 版本只作为逐指标、逐错误文本的
行为对照。FASTA、FASTQ、GFF3 与 protein FASTA 都统一识别 LF、CRLF、lone-CR；
序列与质量字段要求 ASCII，header/GFF3 文本按 UTF-8 校验。FASTQ 结构必须是完整的
四行记录，截断、空 header、缺失 `+` 行或非法质量字符都会使 `parseable=0`。
`parseable` 只反映格式解析器的真实执行结果；没有内置解析器的格式（`other`、目录等）
不记录该指标，视为未评估。

| stage | 适用输入 | 代表指标 |
|---|---|---|
| `file_integrity` | 所有文件 | `file_exists`、`size_bytes`、`sha256_match`、`parseable` |
| `assembly_basic` | genome FASTA | `total_length`、`contig_n50/n90`、`contig_l50/l90`、`gc_percent`、`n_percent`（严格只统计 N）、`gap_count`/`gap_percent`（比对缺口字符 `-` 的连续段与占比）、`ambiguous_base_percent`、重复 seqid/完整 header、circular/空序列 |
| `reads_basic` | FASTQ | `read_count`、`total_bases`、`q20_percent`、`q30_percent`、`gc_percent`、`duplicate_percent`、采样数量/策略、`overrepresented_sequence_count`、read length N50、R1/R2 配对 |
| `annotation_basic` | GFF3 (+组装 FASTA/蛋白 FASTA) | gene/mRNA/CDS 数量、CDS 三联体比例、ID/Parent 完整性、坐标错误、seqid 匹配、蛋白重复 ID、X 比例、内部终止密码子 |

归档或显式 `verify` 完整计算 SHA-256 成功后，系统把 manifest SHA-256 与本地文件的
`size + device + inode + mtime_ns + ctime_ns` 绑定到可重建缓存。后续内置 QC 只在这组
指纹完全不变时复用校验结果；大小或任一 stat 字段变化都会自动回退到完整 SHA-256。
`operon qc --rehash` 可无条件绕过缓存。文件身份仍始终是
`file_id + sha256 + size_bytes`，指纹既不是新的身份，也不能替代周期性的显式 `verify`。

annotation QC 对运行时读取的 assembly/protein 关联输入执行同一身份校验；`--rehash`
同时覆盖主输入和这些关联输入。assembly 的 `seqid -> length` 映射按
`file_id + sha256 + size_bytes + cache format` 原子写入
`qc/cache/fasta_lengths/`。该索引属于可删除、可重建的派生数据：首次缺失或损坏时
流式扫描 FASTA 重建，内容身份不变时可跨进程复用，不进入 metadata 事实来源。

外部工具指标可通过 `import-qc` 进入同一长表，也可通过 `run-external` 以结构化方式执行并保存 provenance。

FASTQ 在单次解析中累计 256 以内的质量字符直方图，再按显式 Phred offset 计算
Q20/Q30，不二次读取或重复解压文件。默认 offset 为现代 Phred+33；`auto` 在可观察
字符范围重叠时仍按 33 计算，但通过 `quality_encoding=ambiguous_assumed_phred33`
保留不确定性。重复率使用确定性的前 N 条 reads 精确计数，记录
`duplicate_sampled_read_count`、`duplicate_is_sampled` 和
`duplicate_sampling_strategy=first_n`；配对 read count 在同一批 QC 中缓存复用。

每个内置 QC 文件任务还用单调高精度时钟记录完整耗时和不重叠的阶段耗时。JSONL
记录通过 `qc_timing.schema_version` 版本化，并保存当前文件以及 annotation 运行时读取的
assembly/protein 关联文件身份；同一结构序列化到 `workflow_runs.execution_details`。
这使完整 SHA-256/指纹缓存、FASTA/FASTQ、assembly length、GFF3 scan/finalize、protein scan、SQLite
写入与状态转换的成本可以分别聚合，而不改变 QC 指标语义。完整字段和复测方法见
[内置 QC 性能诊断](../operations/qc-performance.md)。

## 规则引擎

阈值不在 QC 代码中，而在 `config/profiles/*.yaml`：

```yaml
kind: qc
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

## 状态机

```text
DISCOVERED -> METADATA_FETCHED -> METADATA_VALIDATED -> DOWNLOAD_PENDING
-> DOWNLOADED -> CHECKSUM_VERIFIED -> STANDARDIZED -> QC_RUNNING
-> QC_COMPLETE -> ACCEPTED / REVIEW / REJECTED -> RELEASED
```

失败状态也显式存在：`DOWNLOAD_FAILED`、`CHECKSUM_FAILED`、`FORMAT_INVALID`、`METADATA_INVALID`、`STANDARDIZATION_FAILED`、`QC_FAILED`。

`set_state` 校验合法迁移；批量流程内部使用强制但留痕的迁移，人工强制迁移必须写 reason 并进入 `changes` 表。
