# 文件归档与内置 QC

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

`ingest` 和 `operon verify` 已经完整核对过 SHA-256；文件的 size/device/inode/mtime/
ctime 均未变化时，后续 `operon qc` 会复用这一结果，避免在大容量 HDD 上先完整读一遍
校验和、再读一遍做解析。需要强制重新读取全部字节审计时使用 `operon qc --rehash`；
`operon verify` 本身始终执行完整内容校验。

annotation GFF3 还会验证并读取关联的 assembly/protein。assembly FASTA 第一次参与
坐标检查时，`operon` 流式建立 `qc/cache/fasta_lengths/` 下的长度索引；后续运行无需
再次扫描数 GB 的序列内容。索引由 assembly 的 `file_id + sha256 + size_bytes` 标识，
损坏时自动重建，可安全删除。`--rehash` 会重新校验所有实际输入，但只要 assembly
内容 SHA-256 未变，已验证的长度索引仍可继续使用。

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

## 导入外部 QC 结果

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
