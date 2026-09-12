# Remote-First 运行模式

Remote-first 模式适用于数据体量和计算都在远端 HPC 上、本地工作站只负责记录事件与
整理结果的场景：

- 本地项目是控制面：`operon.sqlite` 是唯一可写事实源，每次传输、驱逐、运行和 QC
  导入都会落入 `workflow_runs`、`changes` 与 `logs/workflow.jsonl`；
- 远端镜像保存字节：SFTP 镜像为每个被驱逐的文件保留经过 checksum 校验的副本；
- 远端主机执行计算：`run-external` 与 `analyze` 通过 `ssh` 执行后端（可选远端
  Slurm）提交命令，内置 QC 解析器则通过 `qc-measure` 在远端运行。

```text
工作站（控制面）                            HPC（字节 + 计算）
  operon.sqlite, config/, logs/    SFTP     远端镜像 root
  决策、QC、provenance           <------->  （operon-manifest.json + 对象）
  operon CLI / TUI                 SSH      ssh/slurm 后端 + qc-measure
```

本页把 [SFTP 远程存储](remote-storage.md) 与 [Slurm 与 SSH 远程执行](remote-execution.md)
串成一条完整工作流；字段级细节仍以那两个页面为准。

## 配置

一个 SFTP 镜像，加一个指向同一镜像的 SSH 执行配置：

```yaml
# project.yaml
remotes:                            # 字段细节见《SFTP 远程存储》
  mycluster:
    type: sftp
    host: hpc.example.org
    root: /data/operon-mirror

execution:                          # 字段细节见《Slurm 与 SSH 远程执行》
  backend: ssh
  ssh:
    storage_remote: mycluster       # 同一镜像；自动继承 host 与 root
    scheduler: slurm                # 或 none，直接在 SSH 主机上执行
```

这里只列出本流程需要的键；其余 `remotes:` 与 `execution:` 字段见
[SFTP 远程存储](remote-storage.md)与 [Slurm 与 SSH 远程执行](remote-execution.md)。

配置 `storage_remote` 后，本地缺失（`REMOTE_ONLY`）的输入会先对照本地 manifest、
远端清单和远端实际字节完成校验，再直接在远端 root 下原位消费——不会仅为跑一个作业
就把数据拉回工作站。

## 端到端工作流

### a. 本地归档，建立可信身份

```bash
operon ingest --source ASM.fna.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta
```

### b. 把字节镜像到远端

```bash
operon push --remote mycluster --file-id FIL_000001
```

`push` 会校验远端实际内容并登记 `file_locations`；见 [push](../reference/cli-remote.md#push)。

### c. 驱逐本地字节

```bash
operon evict --remote mycluster --file-id FIL_000001
operon locations --file-id FIL_000001
```

`evict` 只有在三重验证全部通过后才删除本地副本：远端清单身份一致、对远端实际字节
实时核验 SHA-256、以及本地字节的最终核验。之后文件状态变为 `REMOTE_ONLY`，
`.operon/placeholders/<file_id>.json` 下保留一个小型指针文件，状态变化写入
`changes` 审计。见 [evict](../reference/cli-remote.md#evict)。

### d. 远程分析

任意命令走 `run-external`，配置好的 recipe 走 `analyze`：

```bash
operon run-external --step quast --backend ssh \
  --command 'quast -o qc/quast_out raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta' \
  --expected-output qc/quast_out/report.tsv

operon analyze --analysis blastn_nt --backend ssh \
  --entity-type assembly --entity-id ASM_000001
```

`REMOTE_ONLY` 输入经 `storage_remote` 在远端原位消费；运行成功后期望输出会被拉回
项目内。见 [Slurm 与 SSH 远程执行](remote-execution.md)。

带 `commands` 命令链的 recipe（如 `rpsblast_cdd`）与远端后端兼容：每一步按顺序通过
同一个远端 executor 执行，确定性的 `${work_dir}` 暂存目录——与项目根下的其他路径一样——
会映射到远端 root，在运行前于远端创建、结束后清理。

### e. 远程运行内置 QC

先在 HPC 上安装 `operon`（版本要求见下文“限制”），在远端原位度量文件，把 JSON
payload 作为运行输出拉回，再导入 `qc_results`：

```bash
operon run-external --step qc_builtin --backend ssh \
  --entity-type assembly --entity-id ASM_000001 \
  --command 'operon qc-measure \
    --file raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta \
    --format fasta --role genome_fasta \
    --sha256 <sha256> --size-bytes <size> --file-id FIL_000001 \
    --out qc/metrics/FIL_000001.json' \
  --expected-output qc/metrics/FIL_000001.json

operon import-qc --file qc/metrics/FIL_000001.json
operon evaluate
```

`qc-measure` 解析前先校验字节身份，产出的 stage/指标名与本地 `qc` 完全一致；
`import-qc` 会重算受影响实体的 QC 状态，并在 `workflow_runs` 中记录一条
`import-qc` 步骤；随后 `evaluate` 照常套用版本化 profile。见
[qc-measure](../reference/cli-files-qc.md#qc-measure) 与
[import-qc](../reference/cli-files-qc.md#import-qc)。

`qc-measure` 默认把每行的 `parameter_set` 标为 `builtin_v2`。FASTQ 使用复合标签
——`builtin_v2:sample_<取样量>:phred_<偏移>`——因此读段级指标背后的取样量与 Phred
设置会保留在标签中。`--parameter-set NAME` 可替换基础标签。`qc_results` 的 upsert
键包含 `parameter_set`，所以自定义参数集写入的行会与默认参数集在同一 file、stage、
metric 下的行并存，而不是互相覆盖。

该命令需要文件的 manifest 身份信息，可用以下任一方式查询：

```bash
operon locations --file-id FIL_000001     # 每个文件的驻留状态
operon verify --file-id FIL_000001        # 实时校验；输出 current_sha256
operon report metadata                    # reports/metadata/files.tsv：完整 manifest
operon query "SELECT file_id, file_role, format, sha256, size_bytes FROM files WHERE file_id='FIL_000001'"
```

### f. 发布前拉回字节

`standardize`、`release` 与 `export` 需要本地字节，先把所需文件取回：

```bash
operon pull --remote mycluster --file-id FIL_000001
```

### g. 两个面都要备份

含 `REMOTE_ONLY` 文件时，本地数据库与远端镜像 root 是两个独立的备份对象；
见 [备份、迁移与续跑](backup-migration.md)。

## 限制

- `run-external` 与 `analyze` 是同步阻塞的：本地 CLI 进程全程持有远端作业。进程
  被中断或触发 `--timeout` 都会取消远端作业（远端 Slurm 用 `scancel`，SSH 直连向
  进程组发送 TERM/KILL）。目前不存在“提交后断开、稍后回收”的异步模式。
- `qc-measure` 要求 HPC 上安装 `operon`（本文档对应 {{ operon_version }}）：

  ```bash
  pip install OperonDBS==<version>
  ```

  payload 的 `tool_version` 与本地安装不一致时，`import-qc` 只打印警告仍会导入，
  但不同版本之间指标语义可能发生漂移——请保持两端安装同一版本。
- `operon qc` 是本地专属命令（没有 `--backend`）。状态为 `REMOTE_ONLY` 的文件会被
  **跳过**而不是判为失败：不写 `qc_results`、不改变实体状态，仅向 stderr 打印
  `SKIPPED` 警告；显式 `--file-id` 指定且所选文件全部被跳过时，命令以退出码 1
  结束。对这类文件，要么先 `pull` 回本地再跑 `qc`，要么走上面的
  `qc-measure` + `import-qc` 远程路径。
- `operon tools-check` 的版本探测始终在本地执行，与配置的执行后端无关。
  （`analyze`/`run-external` 运行期间的版本探测则走所配置的后端。）
