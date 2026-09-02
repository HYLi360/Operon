# 日常操作顺序

## 单文件流水线

对于已经建好实体记录的单文件，可以直接运行：

```bash
operon run-pipeline \
  --source /data/GCA_999999999.fna.gz \
  --entity-type assembly \
  --entity-id ASM_000001 \
  --role genome_fasta \
  --profile assembly_production_v1
```

它依次执行：

```text
ingest -> standardize（含 checksum 复核） -> QC -> evaluate
```

## 日常操作顺序

```bash
# 1. 录入/更新元数据并校验
operon import dataset               # 交互式完整数据集
operon add ...                       # 精确新增一个实体
operon import table --table ...     # CSV/XLSX 批量表格

# 2. 归档新数据
operon ingest ...
operon verify

# 3. 标准化与 QC
operon standardize
operon qc

# 4. 外部 QC / 封装分析
operon import-qc --file ...
operon tools-check
operon analyze --analysis blastn_nt
operon report analysis --analysis blastn_nt

# 5. 判定与发布
operon evaluate --profile ...
operon report decisions
operon release --version ... --profile ...
```

## 常见问题

| 症状 | 原因与处理 |
|---|---|
| `no project.yaml found` | 当前目录不在项目内；用 `--project /path` 或先 `cd` 到项目根目录 |
| `already has FIL_... for role ... with sha256 ...` | 同实体同角色已有不同字节文件；raw 不可变，应为新数据建新 assembly/run 版本，而不是覆盖 |
| `CHECKSUM_FAILED` | 文件被改动；恢复原始文件或重新从源头归档（新实体版本） |
| table 导入报字段错误 | 阅读错误中的行号/字段；修改 CSV/XLSX，或先在 `config/schemas.yaml` 中扩展字段 |
| `query` 拒绝 UPDATE/PRAGMA | 这是设计行为；修改数据请使用受控命令（`add`、`import table`、`curate` 等） |
| `tools-check` 报 `cannot launch ...` | 修改 `config/tools.yaml` 的 `executable`/`run_method`；conda 环境写法见 How-to 手册 |
| `analyze` 报数据库不存在 | 把 recipe 的 `database` 改为真实 BLAST/HMM 数据库路径 |

下一步建议阅读 [How-to 操作手册](../guides/index.md) 和 [架构说明](../architecture/index.md)。
