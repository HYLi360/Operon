# SFTP 远程存储

## 远程镜像与数据驻留

除[本地备份、迁移与续跑](backup-migration.md)之外，`project.yaml` 的 `remotes:` 段可以配置一个或多个 SFTP
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

## 推送、恢复与查看驻留位置

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

远程镜像沿用本地 raw 不变量：普通文件与目录 artifact 按 SHA-256 + size 校验且幂等，
远端同路径不同字节报 `ConflictError`；目录哈希覆盖相对路径、空目录、文件内容与符号
链接目标。清单原子替换要求 SFTP 服务器支持 OpenSSH `posix-rename@openssh.com`
扩展，不支持时失败关闭；若进程崩溃留下 `.operon-manifest.lock`，只能在确认没有活跃
push 后人工移除。被驱逐前已经是 `STANDARDIZED` 的文件在 `pull` 恢复后保持该状态。
批次发布、清单 v2 身份与逐条退出码语义见[远程存储命令](../reference/cli-remote.md)。

## 本地只保留控制面，远端保存并计算大文件

完整的 archive → push → evict → analyze → pull 流程见 [Remote-First 运行模式](remote-first.md)。
要让 `analyze` 原位消费本地缺失（`REMOTE_ONLY`）的输入，可让执行端指向同一个远端镜像：

```yaml
execution:
  backend: ssh
  ssh:
    storage_remote: mycluster   # 自动继承该远程端的 host 与 root
    scheduler: slurm            # 或 none，直接在 SSH 主机执行
```

驱逐时会在 `.operon/placeholders/<file_id>.json` 写入小型指针文件（`pull` 恢复字节
时删除）。首次出现远程独占状态时，还会自动扩展 `config/schemas.yaml`、加入
`REMOTE_ONLY` 文件状态；只有当该文件仍使用旧值 `""`、`1.0` 或 `1.1` 时，
`schema_version` 才会提升到 1.2，当前版本的项目保持原版本号、只增加新的枚举值。
文件一旦被改写，就会采用规范化格式，其中的手写注释会丢失。

`standardize` 与 `release` 需要本地字节，应先 `pull`；`evict` 的校验语义，以及
`locations` 是缓存视图而 `verify` 才是实时复核，见[远程存储命令](../reference/cli-remote.md)。

也可以不经镜像配置，直接从 `sftp://` 与 `remote://` URL 归档远程文件；见
[ingest](../reference/cli-files-qc.md#ingest)。

分工：本节是“按内容校验的远端镜像”；数据与计算都在 HPC、本地只做事件记录的
完整流程见 [Remote-First 运行模式](remote-first.md)；`operon.sqlite`、`config/` 等
本地目录的整体备份与迁移见[备份、迁移与续跑](backup-migration.md)。
