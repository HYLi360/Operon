# 文件归档、标准化与远程存储

## 文件归档与标准化

`ingest` 的保证：

1. 实体必须存在。
2. 自动识别格式与压缩；`.fna.gz`/`.fastq.gz` 可正确识别。
3. 同实体同角色不同 SHA-256：直接拒绝（`ConflictError`），防止 raw 被覆盖。
4. 相同内容重复 ingest：幂等返回同一个 `FIL_`。
5. 写入 raw 时使用“临时文件 + fsync + 原子 rename”。
6. 归档后再校验一次 checksum，成功才登记 manifest 并回填 `assemblies.fasta_file_id`、`annotations.*_file_id` 等关系。

`standardize` 默认**复制**到 `standardized/`，使 raw、standardized、release 三层互不共享可写 inode；`--link hardlink` 或 `--link symlink` 是显式兼容选项。

### 7.1 远程镜像（SFTP）

`project.yaml` 的 `remotes:` 段可配置一个或多个 SFTP 远程镜像（`operon/remotes.py`），
把 manifest 文件同步到远端而不破坏本节的不变量：

- 普通文件与目录 artifact 全部按 `sha256 + size_bytes` 校验；服务器没有
  `sha256sum` 时通过 SFTP 流式计算 SHA-256，绝不退化为仅比较大小；目录使用与
  本地完全相同的确定性树哈希（含空目录和符号链接目标）；
- 远端维护 `operon-manifest.json` v2 清单（project_id + relative_path →
  file_id/sha256/size/kind/synced_at），清单更新要求服务器支持 OpenSSH POSIX rename
  扩展，以“唯一临时文件 + 原子替换”发布；一次 push 批次只发布一次清单，读—改—写
  由远端原子目录 `.operon-manifest.lock` 串行化，避免多控制端并发 push 丢失条目；
- 所有相对路径在本地和远端均做根目录约束，拒绝绝对路径、`..` 与路径逃逸；远端
  清单的 `project_id` 和每条身份都必须与本地 SQLite 一致；
- 每次传输复用 `workflow_runs` 记录 provenance（step 为 `push:<name>` /
  `pull:<name>`）；成功位置同时缓存到 `file_locations`；
- push/pull/evict 采用逐条结果语义：单项失败写入 `error` 后继续批内其余对象，CLI
  在存在任一错误时返回非零；
- `pull` 恢复本地缺失文件后把 `files.status` 恢复为 `CHECKSUM_VERIFIED`，该变化与
  `verify`/`evict` 的状态变化一样写入 `changes`；
- `ingest --source` 也可直接接受 `sftp://[user@]host[:port]/path` 与
  `remote://<name>/<path>`；后者必须存在于远端清单并先校验身份，前者下载后由
  ingest 计算新身份，再走与本地文件完全相同的归档流程。

paramiko 是可选依赖（`pip install 'OperonDBS[remote]'`），代码内惰性导入；核心依赖与
本地功能不受影响。cx_Freeze 的 `build` extra 和发布包包含 paramiko。

### 7.2 本地控制面与远程数据面

`operon` 0.3 的远程模型把“存、算、执行”拆为三个可组合角色：

```text
本地电脑：CLI + project.yaml + tools.yaml + SQLite + logs
                          │ SSH/SFTP
                          ▼
远程登录/调度节点：直接执行或提交 Slurm
                          │ 共享文件系统
                          ▼
远程数据面：raw/reference DB/临时分析输出
```

`push` 在远端建立经过身份校验的副本；`evict` 只有在再次验证远端实际内容后才删除
本地字节，把 `files.status` 置为 `REMOTE_ONLY`，在 `file_locations` 记录位置，并于
`.operon/placeholders/<file_id>.json` 写一个便于人查看的指针。指针不是事实来源，
`files` 与 `file_locations` 才是机器判定依据。`pull` 可随时把对象 hydrate 回逻辑
`relative_path`。

`file_locations.status=AVAILABLE` 只是本地驻留缓存，不是永久可用性的证明。`verify`
在本地字节缺失时必须实时读取远端清单并核对实际内容；确认丢失/损坏后把文件置为
`MISSING`，暂时无法连接时只返回失败的 `REMOTE_UNVERIFIED` 检查结果而不武断改写
持久状态。确认仍可用时刷新远端位置并维持 `REMOTE_ONLY`。

这里“raw 不可变”约束的是一个 `file_id` 的内容身份不能被另一组字节替换，并不要求
每台控制端永久保存一份物理副本。`evict` 是经校验的位置迁移：至少一个远端副本仍以
同一 SHA-256/size 存在，逻辑 raw 身份不变；远端副本不可信或缺失时绝不删除本地字节。

当 `execution.ssh.storage_remote` 指向同一远程文件系统时，`analyze` 遇到本地缺失的
输入不会先下载；它先核对远端清单和实际 SHA-256，再把本地逻辑路径映射到远端 root，
让远端命令直接读取该对象。当前要求计算节点通过 SSH 主机或其 Slurm 节点能看到该
remote root；“对象存储与完全不同的计算集群之间服务器端搬运”尚未实现。
