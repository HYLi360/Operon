# 快速开始

## 端到端演示

演示项目使用确定性合成数据，可在不准备真实数据的情况下验证安装和完整流水线。

如果不想先创建真实项目，可以用合成数据体验完整流水线：

```bash
operon init-demo ./demo-project --project-id PRJ_DEMO_001
```

该命令会自动完成：

1. 初始化项目目录与配置；
2. 写入 2 个 organism、3 个 sample、1 个 run、3 个 assembly、2 个 annotation；
3. 生成合成 FASTA/GFF3/protein FASTA/双端 FASTQ；
4. 通过正常 ingest 流程归档 9 个文件；
5. 默认复制到 `standardized/`；
6. 运行内置 QC；
7. 分别用 `assembly_production_v1`、`annotation_release_v1`、`reads_qc_v1` 评估；
8. 创建 release `2026.08.demo`。

查看结果：

```bash
operon --project ./demo-project status
operon --project ./demo-project report decisions
operon --project ./demo-project report qc --entity-type assembly
```

演示预期：`ASM_000002` 因 `LOW_CONTIGUITY` 被 FAIL；`ANN_000003` 因 `CDS_NOT_MULTIPLE_OF_3` 与 `BROKEN_GFF3_PARENTS` 被 FAIL；其他实体 PASS。

验证 release：

```bash
cd ./demo-project/releases/2026.08.demo
# Linux
sha256sum -c checksums.sha256
# macOS
shasum -a 256 -c checksums.sha256
```
