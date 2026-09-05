# SFTP 远程存储

## 远程镜像与数据驻留

除第 15 节的本地备份外，`project.yaml` 的 `remotes:` 段可以配置一个或多个 SFTP
远程镜像，用于把 manifest 文件按内容校验地同步到远端：

```yaml
# project.yaml
remotes:
  mycluster:
    type: sftp
    host: hpc.example.org
    user: hyli360
    port: 22
    key_file: ~/.ssh/id_rsa
    root: /data/operon-mirror
    known_hosts: ~/.ssh/known_hosts
    # 也可固定管理员提供的指纹：host_key_sha256: SHA256:base64...
    insecure_accept_unknown_host: false
    connect_timeout: 30            # 秒；同时限定远端 manifest 锁的等待时长
```

标准 `OperonDBS` 安装已包含 SFTP 功能所需的 paramiko。

先列出配置并测试连通性（任一远程端报错时退出码为 1）：

```bash
operon remotes
```

推送与恢复：

```bash
# 全部 manifest 文件上传到远端镜像
operon push --remote mycluster

# 只推送指定文件
operon push --remote mycluster --file-id FIL_000001 --file-id FIL_000002

# 从远端镜像恢复（缺省恢复远端清单全部条目）
operon pull --remote mycluster

# 查看每个 file_id 的本地/远程驻留状态
operon locations
```

语义与本地的 raw 不变量一致：

- 普通文件和目录 artifact 全部按 sha256 + size 校验且幂等；目录哈希包含相对路径、
  空目录、文件内容和符号链接目标。远端已有不同字节时报 `ConflictError`；
- 远端维护带 `project_id` 的 `operon-manifest.json` v2 清单。清单原子替换要求
  SFTP 服务器支持 OpenSSH `posix-rename@openssh.com` 扩展；不支持时失败关闭，不会
  退化成先删除旧清单再写新清单。一次 push 批次只重写一次清单，并以远端原子目录
  `.operon-manifest.lock` 保护读—改—写；若进程崩溃留下锁，报错会提示精确路径，
  只能在确认没有活跃 push 后人工移除；
- 远端相对路径必须安全地位于 root 下；默认 pull 对每条记录重新核对本地 SQLite
  的 `file_id + relative_path + sha256 + size_bytes`，远端清单不能改写本地身份；
- 每次传输都写入 `workflow_runs` provenance（step 为 `push:<name>` /
  `pull:<name>`），成功位置写入 `file_locations`；
- push/pull/evict 的单个条目失败不会中止整个批次；每项都会输出结果并写 provenance，
  其余条目继续，任一项为 `error` 时命令最终返回退出码 1；
- `pull` 恢复本地缺失文件后，其 `files.status` 恢复为 `CHECKSUM_VERIFIED`，变化写入
  `changes` 审计。被驱逐前已经是 `STANDARDIZED` 的文件在恢复后保持 `STANDARDIZED` 状态。

### 10.1 本地只保留控制面，远端保存并计算大文件

这是个人电脑控制 HPC 最常见的推荐流程：

```bash
# 1. 首次归档仍在本地建立可信身份
operon ingest --source ASM.fna.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta

# 2. 推到远端；push 会校验实际远端内容并登记 file_locations
operon push --remote mycluster --file-id FIL_000001

# 3. 再次验证远端后删除本地大文件，留下 SQLite + 小型指针
operon evict --remote mycluster --file-id FIL_000001
operon locations --file-id FIL_000001

# locations 是缓存视图；verify 会重新连接远端并核对清单与实际内容
operon verify --file-id FIL_000001

# 4. 本地发命令；输入在远端原位消费，结果和 provenance 回到本地
operon analyze --analysis blastn_nt --backend ssh \
  --entity-type assembly --entity-id ASM_000001

# 5. 本地流程确实需要字节时再 hydrate
operon pull --remote mycluster --file-id FIL_000001
```

配置时令执行端引用同一个远端镜像：

```yaml
execution:
  backend: ssh
  ssh:
    storage_remote: mycluster   # 自动继承 host/user/port/key/root/host-key 策略
    scheduler: slurm            # 或 none，直接在 SSH 主机执行
```

`evict` 是显式删除本地字节的命令；不指定 `--file-id` 会处理全部 manifest 对象。
它只在远端清单身份和远端实际内容均通过严格校验后执行，并在 `changes` 中审计状态
变化。`standardize` 与 `release` 仍需要本地字节，应先 `pull`；外部 `analyze` 则可
直接消费 REMOTE_ONLY 输入。

驱逐时会在 `.operon/placeholders/<file_id>.json` 写入小型指针文件（`pull` 恢复字节
时删除）。首次出现远程独占状态时，还会自动扩展 `config/schemas.yaml`、加入
`REMOTE_ONLY` 文件状态并把 `schema_version` 提升到 1.2——该文件会被规范化格式重写，
其中的手写注释会丢失。

本地缺失对象运行 `verify` 时也会实时检查远端，而不是把 `file_locations` 的
`AVAILABLE` 当作永久证明。远端对象已被带外删除或损坏时返回 `MISSING` 并更新缓存；
SSH 暂时不可达时返回检查结果 `REMOTE_UNVERIFIED` 和退出码 1，但保留最近一次持久
状态，避免把网络故障误判为数据丢失。

也可以不经镜像配置，直接从 URL 归档远程文件（未显式给 `--source-url` 时自动
记录该 URL）：

```bash
operon ingest --source sftp://hyli360@hpc.example.org:22/data/ASM.fna.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta

operon ingest --source remote://mycluster/raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta
```

分工：本节是“按内容校验的远端镜像”；第 15 节仍是针对 `operon.sqlite`、`config/`
等本地目录的整体备份与迁移。
