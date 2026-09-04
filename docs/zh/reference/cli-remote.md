# 远程存储命令

## remotes

```bash
operon remotes
```

- 列出 `project.yaml` 的 `remotes:` 配置段中的远程端，并逐个测试连通性。
- 输出表格：`name` / `type` / `address` / `root` / `files`（远端清单条目数）/
  `status` / `error`。
- 任一远程端有 `error` 时返回退出码 1。
- 标准 `OperonDBS` 安装已包含 paramiko；SSH/SFTP 命令使用时才会惰性导入。
- 默认拒绝未知 SSH 主机密钥；通过 `known_hosts` 或 `host_key_sha256` 建立信任。

## push

```bash
operon push --remote NAME [--file-id FIL_...]...
```

- 把本地 manifest 文件上传到指定远程端（SFTP 镜像）；不指定 `--file-id` 时
  推送全部 manifest 文件。
- 文件和目录 artifact 均按 sha256 + size 校验；远端没有 `sha256sum` 时通过 SFTP
  流式哈希，绝不只比较大小。内容一致跳过；已有同路径不同内容会报
  `ConflictError`，绝不静默覆盖。
- 一次批量 push 只发布一次 `operon-manifest.json`。读—改—写期间通过远端原子目录
  `.operon-manifest.lock` 串行化写者，清单本身仍以唯一临时文件 + POSIX rename
  原子替换；写者异常退出会保留锁，错误会给出需人工核查的精确路径。
- 每次传输写入 `workflow_runs`（step 为 `push:<name>`）。单个文件失败不会中止其余
  文件；命令输出每项结果，任一项为 `error` 时整条命令最终返回退出码 1。
- 每个文件输出 `uploaded` / `indexed`（远端字节已存在并被纳入清单）/
  `skipped` / `error`。

## pull

```bash
operon pull --remote NAME [--file-id FIL_...]...
```

- 从指定远程镜像恢复文件；不指定 `--file-id` 时按远端清单遍历，但每条记录仍必须
  与本地 SQLite 中同一 `file_id + relative_path + sha256 + size_bytes` 完全一致；
  远端多出的未知对象不会被导入本地数据库。
- 同样按 sha256 + size 校验、幂等；本地已有不同字节时拒绝覆盖（`ConflictError`）。
- 恢复本地缺失文件后，其 `files.status` 恢复为 `CHECKSUM_VERIFIED`；传输记录
  写入 `workflow_runs`（step 为 `pull:<name>`），状态变化写入 `changes`。
- 单个条目失败后继续处理批内其他条目；只要存在 `error`，命令最终返回退出码 1。

## evict

```bash
operon evict --remote NAME [--file-id FIL_...]...
```

- 这是显式删除本地归档字节的操作；不指定 `--file-id` 时处理全部 manifest 文件。
- 删除前再次核对本地身份、远端清单身份和远端实际 SHA-256/目录树哈希；任一步不一致
  都拒绝删除。
- 成功后 `files.status` 为 `REMOTE_ONLY`，位置写入 `file_locations`，状态变化写入
  `changes`，并在 `.operon/placeholders/<file_id>.json` 写人类可读的小型指针。
- 单个条目校验或删除失败后继续处理批内其他条目；只要存在 `error`，命令最终返回
  退出码 1。
- `standardize` 和 `release` 前需先 `pull`；配置 `execution.ssh.storage_remote` 后，
  `analyze --backend ssh` 可直接使用远端输入。

## locations

```bash
operon locations [--file-id FIL_...]...
```

联合显示 `files` 与 `file_locations` 中的本地状态、远端名称、远端状态和最近校验时间。
该命令只读，不连接远端；需要实时复核时运行 `verify`（也会在 `push`、`pull`、
`evict` 或远端分析前置检查中按相应操作重新校验）。
