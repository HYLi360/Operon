# 文件归档与内置 QC

新项目默认把未压缩的 FASTA/FASTQ 归档存为确定性 BGZF（`project.yaml` 的
`storage.compress_archive: true`）：未压缩的源在 ingest 时被改写为 BGZF，manifest 记录压缩后
字节。已带 `.gz` 的源（如下面的例子）原样存储，所有内置 QC 解析器对两种形态都透明。单次运行
可用 `operon ingest --no-compress-archive` 关闭，或在项目级把该键设为 `false`；既有项目的明文
归档可随后用 `operon recompress` 迁移。见 [BGZF 归档压缩](../architecture/files-and-storage.md#bgzf-归档压缩)。

## 归档双端测序数据

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

现代 FASTQ 默认按 Phred+33 计算 Q20/Q30。只有已确认是旧式 Phred+64 的数据才使用
`--phred-offset 64`；不能确定时可用 `--phred-offset auto`，其模糊结果会明确记录为
`ambiguous_assumed_phred33`。R1 与 R2 的 `read_count` 会分别以各自的
`input_identity` 保存，同时系统会写入 `paired_read_count_match`。

`ingest` 和 `operon verify` 已经完整核对过 SHA-256；文件的 stat 指纹未变化时，后续
`operon qc` 会复用这一结果；assembly 的 `seqid -> length` 索引按相同内容身份缓存在
`index/fasta_lengths/` 下，annotation QC 对实际读取的 assembly/protein 也做同样的
身份校验。需要强制重新读取全部字节审计时使用 `operon qc --rehash`（`operon verify`
本身始终执行完整内容校验）。两种缓存都是可删除、可重建的派生数据，具体保证见
[QC 流水线](../architecture/qc-and-rules.md)。

## 归档组装与注释

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

同一 `annotation_gff3` 角色下的 `.gtf` 来源按同样方式存储（`format: gtf`、同样的 BGZF 默认）；内置 QC 仍只评估 GFF3。每个注释归档在首次查询时还会获得 `index/features/` 下的扁平 feature 缓存——见 [Feature 索引命令](../reference/cli-features.md)。

## 归档比对 BAM/CRAM

```bash
operon ingest --source /data/sample.bam \
  --entity-type assembly --entity-id ASM_000001 --role alignment_bam

operon ingest --source /data/sample.cram \
  --entity-type assembly --entity-id ASM_000001 --role alignment_cram
```

BAM 与 CRAM 以 `format: bam`/`cram`、`compression: none` 归档：它们的压缩属于格式本身，因此 canonical 名称为 `<entity>.<role>.bam`/`.cram`（不带 `.gz`）。显式 `.gz` 源后缀或 `--compression gzip` 则记录为外层压缩。内置 `qc` 用 pysam 测量这两种格式（flagstat 式计数与比例、insert size 统计、MAPQ>=30 比例、NM tag 错误率、平均深度与覆盖广度，即 `alignment_basic` stage）；`qc-measure --format bam|cram` 在无项目环境下产出同样的指标。

## 导入外部 QC 结果

把 BUSCO/QUAST/FastQC/fastp 等输出整理为 TSV：

```text
entity_type	entity_id	file_id	qc_stage	metric_name	metric_value	metric_unit	tool	tool_version	parameter_set
assembly	ASM_000001	FIL_000001	busco	complete_percent	96.4	percent	busco	5.8.2	embryophyta_odb12
assembly	ASM_000001	FIL_000001	quast	contig_n50	2845913	bp	quast	5.2.0	default
```

必填列、可选列以及 `file_id`/`file_sha256` 的 manifest 校验见 [import-qc](../reference/cli-files-qc.md#import-qc)；`metric_unit` 省略时单位为空，`evaluated_at` 省略时使用导入时刻。

导入：

```bash
operon import-qc --file external_qc.tsv
```
