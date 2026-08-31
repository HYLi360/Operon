# 内置 QC 性能诊断

`operon qc` 会在 `logs/workflow.jsonl` 的每条 QC 运行记录中保存高精度分阶段耗时。
这些诊断字段只描述测量过程，不改变 QC 指标、判定阈值或文件身份。

## JSONL 计时结构

QC 记录保留原有的 `started_at`、`finished_at`、`tool`、`tool_version`、
`parameter_set` 和 `command`，并增加：

- `duration_seconds`：以 `time.perf_counter()` 测量的总耗时，单位为秒；
- `file_id`、`file_role`、`file_format`、`input_size_bytes` 和 `input_sha256`：
  当前命令直接处理的 manifest 文件身份；
- `parser_backend`：实际使用的解析器后端，当前默认是 `cython`；
- `stage_timings_seconds`：便于流式工具直接聚合的分阶段耗时；
- `qc_timing`：带 `schema_version`、时钟类型、主输入、关联输入和分阶段耗时的完整结构。

`qc_timing.integrity.verification_method` 区分 `cached_stat_fingerprint`、`full_sha256`、
`size_mismatch`、`changed_during_sha256`、`stat_error`、`sha256_error` 和 `missing`；
`rehash_requested` 记录本轮是否使用 `--rehash`。因此冷校验和正常重复 QC 不应混在
同一组性能统计中。

同一份 `qc_timing` 也序列化到 `workflow_runs.execution_details`，因此只有数据库、
没有 JSONL 文件时仍可诊断。旧 JSONL 记录没有这些可选字段，读取方必须继续兼容。

主要阶段如下：

| 阶段 | 含义 |
|---|---|
| `state_qc_running` | 写入 `QC_RUNNING` 状态及审计记录 |
| `file_integrity` | 检查文件；指纹命中时只读取 stat，否则完整计算 SHA-256 |
| `fasta_stats` | 当前 FASTA 的结构和序列统计 |
| `fastq_stats` | 当前 FASTQ 的结构、质量和采样去重统计 |
| `paired_read_check` | 查询配对文件；缓存未命中时还包括配对 FASTQ 计数 |
| `annotation_manifest_lookup` | 查询 annotation、assembly 和关联文件身份 |
| `assembly_fasta_integrity` | 校验关联 assembly FASTA 的 manifest 内容身份 |
| `assembly_fasta_length_cache_lookup` | 查找并加载按内容身份键控的 seqid-length 索引 |
| `assembly_fasta_lengths` | 缓存未命中时流式扫描 assembly FASTA 建立长度映射 |
| `assembly_fasta_length_cache_write` | 原子写入可重建的长度索引 |
| `assembly_fasta_length_map_prepare` | 为当前 parser backend 准备 seqid-length 查询映射；Cython 会将 str key 转为 bytes key |
| `gff3_scan` | 逐行解析 GFF3、属性、坐标和 ID/Parent 引用 |
| `gff3_finalize` | 汇总 missing Parent 和最终 GFF3 指标 |
| `protein_manifest_lookup` | 查询关联 protein FASTA |
| `protein_fasta_integrity` | 校验关联 protein FASTA 的 manifest 内容身份 |
| `protein_stats` | 扫描关联 protein FASTA |
| `qc_results_write` | 批量写入 `qc_results` |
| `state_qc_complete` / `state_qc_failed` | 写入最终状态及审计记录 |
| `unattributed` | 指标字典构造等未单独包裹的小段耗时，不与以上阶段重叠 |

计时值保留到微秒级是为了减少短任务的整秒量化误差，不代表操作系统调度和文件系统
噪声也具有微秒级稳定性。性能结论应基于同一环境中的多次配对运行和阶段耗时中位数。

## 532 个 annotation 的代表性复测集合

机器可读清单位于
[`benchmarks/qc_representative_entities.tsv`](../benchmarks/qc_representative_entities.tsv)。
它根据 2026-08-18 的旧实现与 2026-08-29 的 Cython 实测结果分层选择：

- `largest_*_regression` / `*_net_regression`：新版本整体耗时增加的对象；
- `largest_input*`：最大输入和最长任务，用于放大稳定热点；
- `annotation_speedup_control`：annotation 阶段曾明显变快的反例，避免只分析退化样本；
- `*_baseline_q*`：按三份 annotation 归档文件总大小选取的规模基线；
- `large_near_neutral_control`：较大但整体接近不变的控制对象。

`archived_annotation_bytes` 是 annotation 实体自身三份归档文件的合计，不包含 GFF3
运行时读取的关联 assembly FASTA。新 JSONL 会在 `qc_timing.related_inputs` 中记录该
assembly 的 `file_id + sha256 + size_bytes`，后续分析总读取规模时应把它纳入。

核心集合包含 10 个实体，历史新版本耗时合计约 256 秒；完整集合包含 18 个实体，
历史新版本耗时合计约 315 秒。核心集合适合快速迭代，完整集合用于确认热点在不同规模
和控制样本上是否稳定。

在项目根目录运行核心集合：

```bash
awk -F '\t' 'NR > 1 && $1 == "core" {print $3}' \
  benchmarks/qc_representative_entities.tsv |
while IFS= read -r entity_id; do
  operon qc --entity-type annotation --entity-id "$entity_id"
done
```

运行核心加扩展集合：

```bash
awk -F '\t' 'NR > 1 {print $3}' benchmarks/qc_representative_entities.tsv |
while IFS= read -r entity_id; do
  operon qc --entity-type annotation --entity-id "$entity_id"
done
```

正式比较建议至少重复三轮，并交替运行待比较版本，避免把页缓存、后台 I/O 或机器负载
误判为代码变化。对每个实体的三个文件应保持完整运行，因为 GFF3 的 annotation QC 会
读取关联 assembly/protein，而另外两个 FASTA 任务可作为已经确认加速路径的内部对照。

2026-08-29 的 18 个实体、三轮完整实测中，SSD 与 HDD 分别约为 238.1 秒和 295.3 秒；
配对中位数总耗时相差约 56.4 秒。差值几乎全部来自 `file_integrity`（SSD 约 12.0 秒，
HDD 约 68.8 秒），而 `gff3_scan` 分别约 155.8 秒和 153.8 秒，说明当时 HDD 首次完整
SHA-256 是主要的介质相关损失，GFF3 解析则是两种介质共同的 CPU 热点。该批下载没有
`assembly_fasta`，只有 GFF3、CDS 与 protein，因此 `assembly_fasta_lengths` 未被覆盖；
后续补齐 assembly FASTA 后应在 HDD 上单独建立新的基线，不应把它与旧 SSD 数据直接
作总耗时对比。

据此，当前实现增加了两项直接针对热点的优化：不变的 immutable raw 文件复用已完成
SHA-256 的 stat 指纹；Cython GFF3 对常见 ASCII 行直接按 bytes 分割并只提取 QC 所需的
`ID`/`Parent`，遇到非 ASCII 或百分号转义时回退到完整 UTF-8/属性解析。两条路径都由
Python/Cython parity 回归覆盖，指标字典和错误信息契约不变。

2026-08-31 补齐 assembly FASTA 后，同一 18 个实体在 HDD 上每轮需要读取约 66.58 GB
assembly，`assembly_fasta_lengths` 三轮分别为 417.9、403.7、403.4 秒，约占总耗时
60%。因此长度映射现按 assembly 的 `file_id + sha256 + size_bytes + cache format` 保存
到 `qc/cache/fasta_lengths/`：首次仍执行完整扫描，后续进程记录
`related_inputs[].length_cache.status=hit` 并只承担索引加载成本。缓存损坏会自动删除并
重建；缓存头中的 SHA-256 摘要还会检测格式合法但内容已变化的索引行。JSONL 中
`built`、`hit` 或 `write_failed` 明确记录本轮行为。

0.5.3 在同一 18 个实体、54 个文件、HDD 环境的三轮复测中，首轮 18 个缓存均为
`built`，后两轮 36 次均为 `hit`。0.5.2 后两轮平均总耗时为 670.13 秒，0.5.3
热缓存平均为 269.05 秒，耗时下降 59.85%，整体约 2.49 倍；annotation GFF3 文件
合计由 588.51 秒降至 186.32 秒，约 3.16 倍。每轮约 403.6 秒的 assembly 扫描被
约 4.43 秒的缓存加载取代。18 个索引合计约 114 MiB，相对于每轮 66.58 GB 的原始
assembly 读取量很小。

## 判定优化热点

汇总时优先比较各实体、各阶段的中位数，而不是只比较总墙钟时间：

1. `file_integrity` 高且 `verification_method=full_sha256`：完整 SHA-256 或存储吞吐占主导；
   指纹命中后该阶段应接近常数时间；
2. `assembly_fasta_lengths` 高：本轮首次建立关联 assembly 长度索引；若重复运行仍出现
   该阶段而不是 `assembly_fasta_length_cache_lookup`，应检查缓存路径或缓存损坏；
3. `gff3_scan` 高：行分割、UTF-8 解码、字段/属性拆分、集合和 Counter 操作占主导；
4. `gff3_finalize` 高：ID/Parent 集合汇总占主导；
5. `protein_stats` 高：protein FASTA 扫描或记录拼接占主导；
6. `qc_results_write` / 状态阶段高：SQLite 写入和 fsync 占主导；
7. `unattributed` 异常高：需要增加更细的计时边界。

只有当热点在退化样本、规模基线和至少一个控制样本中可重复出现时，才应进入针对性
优化；优化后仍需运行 Python/Cython parity 测试，保证指标与错误文本不变。
