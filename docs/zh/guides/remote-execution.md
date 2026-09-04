# Slurm 与 SSH 远程执行

## 远程执行配置

`run-external` 与 `analyze` 默认在本地以子进程执行外部命令（`local` 后端）。通过
`project.yaml` 的 `execution:` 段可把执行后端切换为本地 Slurm 集群（`slurm`）或
SSH 远程主机（`ssh`，HPC 头节点与云虚拟机均适用）。所有后端共用同一份 provenance
契约：退出码、起止时间、日志路径照常写入 `workflow_runs` 与 `logs/workflow.jsonl`，
成功判定（退出码 0 且 `--expected-output` 非空）与输入/输出 SHA-256 校验不变。

配置示例（全部字段可选，旧项目无需修改）：

```yaml
# project.yaml
execution:
  backend: local            # local | slurm | ssh
  slurm:
    partition: ""
    time: "24:00:00"
    mem_gb: 0               # 0 = 不写 --mem
    extra_sbatch: []        # 追加的 #SBATCH 行，如 ["--gres=gpu:1"]
    setup_commands: []      # 如 ["module load blast/2.15"]
    poll_interval: 15       # squeue 轮询间隔（秒）
  ssh:
    host: ""
    user: ""
    port: 22
    key_file: ""            # 空 = SSH agent / 默认密钥；不支持密码
    remote_root: ""         # 项目在远端的绝对 POSIX 路径；空 = 共享文件系统
    storage_remote: ""      # REMOTE_ONLY 输入所在的 remotes: 名称
    scheduler: none         # none | slurm
    connect_timeout: 30
    known_hosts: ""         # 可选额外 known_hosts 文件
    host_key_sha256: ""     # 可选 SHA256:... 主机密钥指纹固定
    insecure_accept_unknown_host: false
```

命令行 `--backend {local,slurm,ssh}` 可单次覆盖 `execution.backend`：

```bash
operon analyze --analysis blastn_nt --backend slurm
operon run-external --step quast --backend ssh \
  --command 'quast -o qc/quast_out raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta' \
  --expected-output qc/quast_out/report.tsv
```

Slurm 后端的前提与行为：

- 项目目录必须位于与计算节点共享的文件系统上；`sbatch`/`squeue` 需在 PATH 中，
  缺失时报配置错误。
- 每个 run 在 `logs/` 下生成 `<run_id>.sbatch` 批处理脚本（`--cpus-per-task` 取
  线程数，可选 `--time`/`--partition`/`--mem` 与 `extra_sbatch`；`setup_commands`
  插入在命令前），以 `sbatch --parsable` 提交并按 `poll_interval` 轮询 `squeue`；
  作业消失后读取脚本末尾写入的 `<run_id>.exitcode` 退出码文件（失败时回退
  `sacct`）。stdout/stderr 指向 `logs/` 下的 `<run_id>.stdout.log` /
  `<run_id>.stderr.log`。本地和远端 Slurm 都遵守所配置的完整轮询间隔；exitcode
  在共享文件系统上短暂不可见时会先重试，提交输出前有警告行也能解析最终 job ID。
- 超时按 `--timeout`（秒）控制，超时尝试 `scancel`。

SSH 后端的前提与行为：

- 需要可选依赖 paramiko：`pip install 'operon[remote]'`（或
  `pip install paramiko`）；未安装时只在使用 SSH/SFTP 功能时报配置错误。
- `execution.ssh.scheduler: slurm` 时改为在远端主机走 sbatch/squeue 提交与轮询；
  否则直接在远端执行，并把 stdout/stderr 流式回传到本地日志文件。
- 远端 Slurm 在作业内捕获执行环境，因此 provenance 记录计算节点而不是 SSH 登录节点；
  探针失败不影响作业结果。
- 常见的“先 SSH 登录节点，再进入计算节点”不需要第二次交互式 SSH：把登录节点配置
  为 `host`，设置 `scheduler: slurm`，`operon` 在登录节点运行 `sbatch`，Slurm 再把
  作业派发到计算节点。前提是登录节点与计算节点都能看到同一 `remote_root`。如果集群
  没有调度器、计算节点只能经 SSH 跳板访问，当前后端尚未提供第二跳命令配置。
- 配置绝对 POSIX `remote_root` 后，argv/cwd 中经过根目录包含性校验的项目路径前缀会
  改写为该远端路径；`..`/符号链接造成的路径逃逸会被拒绝。留空表示远端与本地共享
  文件系统。配置 `storage_remote` 时默认继承其 root；若又显式设置不同的
  `remote_root`，初始化执行器时即报配置错误，避免“存储验证通过但计算路径不存在”。
- 默认拒绝 known_hosts 中没有的主机。首次使用前应由管理员核对主机公钥后写入
  `~/.ssh/known_hosts`，或配置 `known_hosts` / `host_key_sha256`；
  `insecure_accept_unknown_host: true` 只适合明确接受风险的临时测试环境。
- `analyze` 自动把尚在本地的输入经 SFTP 上传到远端；当 `remote_root` 非空时，
  `run-external` 也会上传每个 `--input` 声明的输入。暂存路径在解析符号链接后必须
  仍位于本地项目根目录内。远端没有 `sha256sum` 时会通过 SFTP 流式计算 SHA-256，
  目录则计算完整树哈希，不会退化为 size 校验；已有不同内容时拒绝覆盖。
- 配置 `storage_remote` 后，本地缺失的输入会先对照本地 SQLite、远端清单和远端实际
  内容，再直接在远端 root 原位读取，不会先下载到个人电脑。实时校验成功后，先前
  陈旧的 `MISSING` 状态会通过审计记录恢复为 `REMOTE_ONLY`。
- 同一分析批次复用一个惰性 SSH 连接完成工具版本探测、远端输入验证、数据库预检和
  各文件命令，批次结束后关闭；不会为每个文件的每一步重新握手。
- 运行前只删除严格限定在 `remote_root` 内的精确 expected-output 路径，避免旧结果
  冒充本次输出；拉回后再次比较本地/远端内容。已有本地输出不同则报冲突。
- SSH 直连命令超时时，`operon` 使用权限收紧的远端 PID 文件向该命令的进程组发送
  TERM，必要时再发送 KILL；若 PID 文件或终止命令不可用，错误会明确说明远端进程
  可能仍在运行。远端 Slurm 则使用 `scancel`，记录取消请求是否被接受，并取回当时
  可用的部分日志与作业内环境探针。
- SSH 直连模式要求远端主机提供 util-linux 的 `setsid`（用于以独立进程组运行命令
  并可靠回传退出码）。Linux 发行版默认包含；macOS/BSD 远端没有该命令，直连命令会
  以 127 失败，此类远端应使用支持 Slurm 的 Linux 主机或本地后端。
- 远端 `reference` 数据库必须预先放在 recipe 的 `database` 路径并声明
  `database_checksum`；身份同时包含远端执行位置。`mutable_cache` 仍要求
  `database_version`，不存在时在远端自动建目录。

工具版本探测（`version_args + version_pattern`）在非 `local` 后端时也通过同一
后端执行，无需在远端手工准备。

单个 recipe 可用 `slurm:` mapping 覆盖 `execution.slurm` 的同名字段，例如给
BUSCO 单独调整内存与时间（完整字段见 [Recipe 配置参考](../reference/recipe-overview.md)）：

```yaml
recipes:
  busco_autolineage:
    slurm:
      mem_gb: 64
      time: "72:00:00"
```

> **测试说明**：Slurm 与 SSH 后端的自动化测试基于模拟环境（fake sbatch/squeue
> 与内存态 SSH/SFTP 实现）。SSH/SFTP、远端原位分析和远端 Slurm 链路还于
> 2026-09-04 在 Linux OpenSSH 登录节点、共享 GPFS 文件系统和 Slurm 计算节点上完成
> 真实冒烟。每套部署仍应运行自己的短任务，核对主机密钥、文件系统可见性、分区、
> 提交、取消、轮询与输出拉回。
