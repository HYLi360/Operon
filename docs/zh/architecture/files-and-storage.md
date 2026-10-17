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

## BGZF 归档压缩

新项目默认把未压缩的序列与注释归档（FASTA、FASTQ、GFF3、GTF）存为确定性 [BGZF](https://samtools.github.io/hts-specs/)：`ingest`（以及 `run-pipeline` 的 ingest 阶段）遇到未压缩的序列或注释文件时，先在归档目标旁改写为 BGZF，manifest 行记录的是**压缩后**字节（`compression: bgzip`、带 `.gz` 的 canonical 名称、压缩后的 SHA-256/size——身份以落盘字节为准，见 INV-3）。BGZF 块头不含 mtime，各块按固定大小独立 deflate，因此相同输入字节产生完全相同的输出字节。BGZF 是合法的多段 gzip 流，所有内置 QC 解析器和任何 gzip 透明的工具都能原样读取归档；块式结构同时支持随机读（`bgzip.BGZipReader` 可 seek）；按身份键控的 `index/fasta_lengths/` 长度缓存会在首次使用时按新校验和重建。

开关是 `project.yaml` 的 `storage.compress_archive`，每次 ingest 的解析顺序为：CLI 旗标 `--compress-archive`/`--no-compress-archive` > 项目键 > 键缺失。键缺失的旧项目保持明文存储——不会发生隐式迁移。源文件已是 gzip/BGZF 时原样存储；显式 `--compression` 参数固定存储形态；目录与其他二进制格式永不转换。

`operon recompress` 是面向既有项目的显式一次性迁移：把每个符合条件的明文 FASTA/FASTQ/GFF3/GTF 归档原地重写（先完整复算当前字节，再 BGZF 改写，再对解压字节复算哈希作为内容保持证明，然后逐字段带审计地更新 manifest 行），删除该文件已登记的远程位置（下一次 `push` 会取代过时的远端副本），并在文件已标准化时重建 `standardized/` 视图。release 成员一律拒绝——release 不可变；remote-only 或已退役实体的文件跳过并写明原因。按旧 SHA-256 键控的派生数据（`qc_results`、`analysis_jobs`、`fasta_lengths` 缓存）自然失效、按需惰性重建；以 `file_id` 键控的 `sequences` 表不受影响。见[文件与 QC 命令](../reference/cli-files-qc.md#recompress)。

## 派生索引缓存

`index/` 是项目共享的**可重建派生索引缓存**目录（EXT-8）。条目按 manifest 身份键控、不登记进
manifest，因此不进入 release 快照与备份——恢复后的项目按需惰性重建。目录布局约定为
`index/<kind>/…`：

- `index/fasta_lengths/{file_id}.{sha256}.v1.tsv` —— 内置 QC 使用的 assembly
  `seqid -> length` 映射（自 `index/` 引入前的 `qc/cache/fasta_lengths/` 迁来；旧条目在首次使用时收编）。
- `index/features/{file_id}.{sha256}.v1/` —— 扁平 GFF3/GTF feature 缓存（`data.parquet` +
  `meta.json` 身份标记），由 `operon features` 消费；首次查询时惰性构建，过期、损坏或缺少提交标记时透明重建。
  行组按源文件顺序写入（不全局排序），区域过滤靠行组统计修剪；查询经 polars `scan_parquet` 走
  投影与谓词下推。

只有 `operon features` 写入 feature 命名空间；那里的删除总与重建相伴，绝无悬空缓存。

## 远程镜像（SFTP）

`project.yaml` 的 `remotes:` 段可配置一个或多个 SFTP 远程镜像（`operon/remotes.py`），
把 manifest 文件同步到远端而不破坏本节的不变量：

- 普通文件与目录 artifact 全部按 `sha256 + size_bytes` 校验；服务器没有
  `sha256sum` 时通过 SFTP 流式计算 SHA-256，绝不退化为仅比较大小；目录使用与
  本地完全相同的确定性树哈希（含空目录和符号链接目标）；
- 远端维护 `operon-manifest.json` v2 清单（project_id + relative_path →
  file_id/sha256/size/kind/synced_at），清单更新以原子方式发布，并在多个控制端
  并发写入之间串行化；
- 所有相对路径在本地和远端均做根目录约束，拒绝绝对路径、`..` 与路径逃逸；远端
  清单的 `project_id` 和每条身份都必须与本地 SQLite 一致；
- 每次传输复用 `workflow_runs` 记录 provenance（step 为 `push:<name>` /
  `pull:<name>`）；成功位置同时缓存到 `file_locations`；
- `pull` 恢复本地缺失文件后把 `files.status` 恢复为 `CHECKSUM_VERIFIED`，该变化与
  `verify`/`evict` 的状态变化一样写入 `changes`。

清单发布与加锁、批次语义与逐条退出码，以及 `sftp://`/`remote://` ingest 属于命令级
细节，见[远程存储命令](../reference/cli-remote.md)与
[文件与 QC 命令](../reference/cli-files-qc.md#ingest)。

paramiko 是核心运行时依赖，但仍在代码中惰性导入，因此仅本地命令不会初始化 SSH
组件。

## 本地控制面与远程数据面

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
