# 隐式行为、边界情形与已知问题

本页对应 `operon` {{ operon_version }}（数据库 schema {{ db_schema }}，元数据 schema {{ metadata_schema }}）。它记录代码中可观察到、但未在其他任务型或架构型页面中说明的行为：隐式语义、边界情形与已知问题。各项按当前实际行为如实描述，并标注实现模块以便核对。本页不是使用建议；受支持的工作流请参阅[指南](../guides/index.md)与[故障排查](../guides/troubleshooting.md)。

已修复的历史问题列在前面；其余条目描述当前行为与已接受的限制。

当前条目按三类标注：

- **有意为之但属隐式**：设计如此，但后果可能出乎意料（例如状态迁移对审计记录的影响）。
- **限制**：当前接受的能力边界或健壮性缺口。
- **已知问题**：缺陷或数据语义上的意外行为，此处如实记录而非隐而不报。尚未解决的问题会在条目中给出可行方案（如有）。

## 已修复问题

以下问题曾在本页记录，当前实现与回归测试已经覆盖：

| # | 领域 | 修复结果 |
|---|---|---|
| K1 | 判定 | 重评估追加自动判定时沿用当前 `curated_*` 字段；CLI 会在写入前一次性预览所有受影响的人工判定实体，非交互运行必须给出 `--yes`。 |
| K2 | QC 状态 | 每个文件都有聚合 QC 状态；实体取同层文件的最差值（`QC_FAILED` > `QC_RUNNING` > `QC_COMPLETE`），`operon qc` 列出每个文件状态。 |
| K3 | 标准化 | 批量标准化只要任一文件失败就返回退出码 1。 |
| K4 | release | release 在隐藏 staging 目录中构建，完成后原子发布；构建失败会清理 staging，数据库提交失败也会删除已发布目录。 |
| K5 | release | release 成员、带审计的 `RELEASED` 状态迁移和 release 数据库行在同一事务中提交，部分状态不会残留。 |
| K6 | export | export 在临时同级目录中构建，所有产物完成后才重命名；失败时清理临时树或恢复已有的空目标目录。 |
| K7 | 表格导入 | 更新既有元数据会保留生命周期状态和审计历史；发布预检会把评估后的元数据变化视为过期，要求重新 QC/evaluate。 |
| K8 | ingest（`move`） | `move` 先复制并校验归档、登记 manifest，最后才删除源文件；复制、校验或事务失败时源文件仍可恢复。 |
| K9 | 工具函数 | 空表只渲染一次表头和分隔线，不再生成重复的数据行。 |
| K10 | 导入向导 | annotation 提示默认值与草稿当前是否已有 annotation 一致（已有时才默认“是”）。 |

## 身份、归档与文件系统

- **raw 不可变性是绝对的，本地文件丢失也不例外。** 同实体同角色重新 ingest 不同字节会抛出 `ConflictError`，即使先前归档的文件在本地已缺失或损坏（`files.py`）。不存在就地替换路径；请归档为新实体版本，或走带审计的修复流程。
- **不受清单管理的遗留文件会被隔离，而非删除。** 当 `raw/` 内规范目标路径被清单不认识的字节占用时，占用者会被移动为同目录下的 `<name>.orphan-<sha12-prefix>`（`files.py`）。`.orphan-*` 文件可能出现在本应不可变的归档目录中；它们不是 manifest 成员。
- **压缩检测是不对称的。** 名为 `.gz` 但 magic bytes 不是 gzip 的文件会被拒绝；反之，普通文件名但内容为 gzip 的文件会被静默记录为 `compression=gzip`（`files.py`，`detect_compression`）。
- **格式检测看扩展名，不看内容。** 至多剥掉一个 `.gz` 后缀；`.fasta.bz2` 等未知扩展名得到格式 `other`（`files.py`，`detect_format`）。
- **`standardize --link-kind hardlink` 对目录产物回退为完整复制**——只有单文件能建立硬链接（`files.py`，`standardize_file`）。新标准化目标采用原子发布，任何失败都会清理。
- **`verify` 恒做全量 SHA-256；`qc` 使用 stat 指纹缓存。** `touch` 或复制会使缓存失效并强制 QC 重新哈希，但 `operon verify` 从不读缓存（`files.py`，`verify_local_file_identity` 与 `verify_files`）。
- **`REMOTE_UNVERIFIED` 只是输出状态。** 远端不可达时由 `verify` 打印，从不持久化，也不写审计记录——但会使命令以退出码 1 结束（`files.py`）。网络抖动不会被误判为数据丢失，但会让命令失败。
- **`verify` 只对本地字节缺失的文件实时核查远端。** 本地字节存在时，远端漂移不会被检测到（`files.py`）。
- **`operon` 命令作用于最近的上层项目。** `Project.find` 向上遍历父目录寻找 `project.yaml`，因此在子目录中执行的命令会静默作用于外层项目（`config.py`）。
- **目录身份不含时间戳与所有权。** `sha256_directory` 覆盖相对路径、空目录、文件字节、大小和符号链接目标；目录内出现 FIFO/socket 会让该目录树的 ingest/verify 以 `OSError` 失败（`utils.py`）。
- **中断的复制会留下隐藏的临时目录。** `atomic_copytree` 在目标旁的 `.target.XXXX` 临时目录中工作；中途崩溃会将其遗留，需手动清理（`utils.py`）。
- **manifest 相对路径是 POSIX 路径。** Windows 上计算出的相对路径会含反斜杠；远程与导出代码会把 `\` 归一化为 `/`，但存储契约仅支持 POSIX（`config.py`、`remotes.py`）。

## 数据库、事务与并发

- **并发写只在 30 秒内被串行化。** 数据库运行于 WAL 模式，`busy_timeout=30000` 且使用即时写事务；写入者在读取前等待锁，超过超时仍会收到 `database is locked`（退出码 1）。稳定 ID 在同一把锁下预留，因此并发分配唯一；失败插入可能留下编号间隙（`database.py`）。
- **迁移在每次可写打开时执行，而非仅在 `operon migrate` 时。** 打开旧 schema 的项目时，任何命令都会顺带应用未执行的加法迁移（`database.py`）。迁移全部为加法（新列/新表）；没有破坏性迁移，也没有数据回填，唯 pre-1.0 重建除外（见[数据库兼容性](../operations/database-compatibility.md)）。
- **只读访问要求 WAL 为空。** 只读挂载只有在 `-wal` 文件为空时才能打开，否则报错并提示到可写主机上做 checkpoint（`database.py`）。
- **`operon query` 拒绝的不只是写入。** SQL authorizer 拒绝 DML/DDL/ATTACH/SAVEPOINT，且仅放行 PRAGMA 白名单，因此 `PRAGMA journal_mode` 或 `VACUUM` 会以 *not authorized* 失败，尽管它们并非对表的写入（`database.py`）。
- **把状态设置为当前值是静默 no-op。** `set_state` 只在状态真正变化时写入状态和审计记录；相同状态不产生审计记录（`workflow.py`）。
- **批量状态写入绕过迁移表。** QC/evaluate 循环使用 `set_state_bulk`，强制接受每一次迁移；审计原因（reason）是唯一痕迹（`workflow.py`）。

## 时间戳、日志与排序

- **时间戳是带偏移的本地时间，不是 UTC。** `now_iso()` 输出形如 `2026-09-05T14:30:00+08:00`。多数按时间排序的逻辑直接做字符串比较，这只在所有写方共享同一时钟/偏移时安全；只有 `workflow list` 通过 `julianday()` 补偿（`utils.py`、`workflow.py`）。跨时区协作者可能看到误导性的顺序。
- **`logs/workflow.jsonl` 与数据库可能不一致。** JSONL run 记录的追加没有进程间锁，且在数据库提交之后才落盘；事务回滚会丢弃其缓冲的 JSONL 记录，两个并发进程还可能交错写入半行（`utils.py`、`workflow.py`）。
- **run 的输入身份嵌入绝对路径。** `workflow_runs.input_sha256` 是 `path:sha256` 行列表的哈希；相同内容位于不同路径（项目搬移）会得到不同的输入身份（`workflow.py`）。
- **输出条数限制是隐式的。** `workflow list` 默认 50 行（`--limit 0` 为不限，`--to` 为开区间）；`report analysis` 默认 20 行且无截断提示；空结果会打印 `(no QC results)` 之类的字面哨兵（`cli.py`）。
- **执行环境探测失败是静默的。** 环境采集失败时 run 正常完成，只是没有 `environment_id`，也没有警告（`workflow.py`）。

## 指标与判定

- **缺失的指标永远不会导致门槛失败。** 必需规则对应的指标没有值时判定为 `NOT_EVALUATED`，实体落入 `QC_COMPLETE`（而非 `QC_FAILED`）。没有解析器的格式（如 BAM、目录）会停留在 `QC_COMPLETE`，仅因永远到不了 `PASS` 而被 release 排除（`rules.py`）。
- **`evaluate` 只覆盖已有 QC 结果的实体。** 从未被 `qc` 处理过的实体不会有 decision；发布预检会在创建任何产物前拒绝这类活动范围内实体（`rules.py`、`release.py`）。
- **多文件实体会混合来自不同文件的指标。** `latest_metrics` 按输入身份分区：保守布尔指标（`file_exists`、`sha256_match`、`parseable`、`paired_read_count_match`）取所有输入的最小值，而其他指标取最近评估输入的值——一个判定可能把一个文件的计数与另一个文件的布尔值组合起来（`database.py`）。
- **更改 QC 采样参数会累积行。** QC 结果按参数集 upsert；用不同 `--sample-size`/`--phred-offset` 重跑 `qc` 会新增一组平行行而非替换，`latest_metrics` 中最新的静默胜出（`database.py`）。
- **阈值语义是闭区间，集合比较按字符串。** `>=`/`<=` 为闭，`between` 两端皆闭，`in`/`not_in` 把指标值当字符串比较，缺少 `operator` 的规则恒通过（`rules.py`）。profile 结构缺口（缺 `min`/`max`/`values`）的行为见 [QC profile 指南](../guides/qc-profiles.md)。
- **`curate` 接受任意判定字符串。** 值会被转大写但不校验是否属于判定枚举；无法识别的值原样存储，并把实体映射到 `QC_COMPLETE`（`rules.py`）。
- **`config/profiles/` 下的每个 `.yaml` 都必须能解析。** 该目录中的草稿或改到一半的 YAML 会让所有加载 profile 的 `evaluate`/`analyze` 命令失败（`profiles.py`）。
- **人工覆盖会在重新评估后保留。** 最新自动判定沿用当前 `curated_*` 字段；显式 `curate` 才会改变生命周期（`rules.py`、`cli.py`）。

## 内置 QC 解析

- **FASTA 序列行内部的空白会计入无效碱基。** 行首尾会被去除，但内部空格保留并推高 `invalid_base_count`；序列数据必须为 ASCII（`qc_module/parsers.py`）。
- **seqid 是 header 的第一个空白分隔 token。** 重复检测分别统计完整 header 与 seqid（`qc_module/parsers.py`）。
- **protein 内部终止子计数会原谅末尾的 `*`。** 末尾终止子会被减去（下限 0）；`missing_start` 要求第一个残基为 `M`（`qc_module/parsers.py`）。
- **FASTQ 重复率只基于前 N 条 reads。** `duplicate_sampling_strategy=first_n`，默认采样 1,000,000 条；在远大于采样量的文件中，集中在后段的重复序列不可见（`qc_module/parsers.py`）。
- **Phred `auto` 在区间重叠时假定 33。** Sanger/Illumina 质量区间重叠时静默取 33；不确定性只通过 `quality_encoding=ambiguous_assumed_phred33` 体现。ASCII 33–126 之外的质量字符会中止 QC（`qc_module/parsers.py`）。
- **GFF3 容忍多种不规则性。** 制表符字段数 ≠9 的行计入 `coordinate_error_count` 并跳过；`##FASTA` 之后的内容被忽略；CDS 三联检查只看坐标、忽略 phase 列；Parent 完整性对照整个文件检查，因此前向引用可以通过（`qc_module/parsers.py`）。
- **`parseable` 只存在于有解析器的格式。** FASTA/FASTQ/GFF3 会记录；其他格式使 `parseable == 1` 门槛永远处于 `NOT_EVALUATED`（`qc_module/__init__.py`）。
- **配对 reads 匹配会被静默跳过**——当同批 FASTQ 没有 manifest 行或不在磁盘上时，既无指标也无警告（`qc_module/__init__.py`）。
- **损坏的 FASTA 长度缓存会被静默重建。** 摘要/计数不匹配时删除并重建缓存（`qc_module/__init__.py`）。
- **Cython 与纯 Python 解析器零容忍差异。** 指标与错误消息字符串必须逐字节一致；由 `tests/regression/test_cython_parser_parity.py` 强制。
- **实体 QC 状态取同层文件的最差结果。** 每个文件状态都会报告；任一文件失败即为 `QC_FAILED`，有文件待处理时为 `QC_RUNNING`，否则为 `QC_COMPLETE`（`qc_module/__init__.py`）。

## 外部分析

- **参考数据库身份是（路径，大小，mtime），不是内容。** 未显式给出 `database_checksum` 时，touch 或复制 BLAST 数据库都会改变身份（导致缓存未命中），而保持大小+mtime 的就地修改会复用陈旧缓存（`tools.py`）。
- **工具版本探测有一个粗糙的回退。** 版本正则未命中时，第一个像版本的 token——或整个首行截断到 200 字符——会成为 `tool_version`；探测原始输出在 provenance 中截断到 4000 字符（`tools.py`）。
- **BLAST 表格解析静默丢弃。** 字段数与 `result_columns` 不符的行被无错跳过，`max_hits_per_query`（默认 5）截断每个 query 存储的 hits——`analysis_results` 汇总可能偏少（`tools.py`）。
- **结果解析不是原子的。** hits 与 results 分别在两个事务中写入；解析中途崩溃会给随后标记为 failed 的作业留下部分 hits（`tools.py`）。
- **空输出即失败。** 只有退出码为 0 且每个期望输出都存在且非空，run 才是 `completed`；合法的 0 字节 TSV 会让 run 失败（`workflow.py`）。
- **BUSCO auto-lineage 拒绝路径中含 `fasta` 的输出。** 这是对 SEPP 路径改写缺陷的刻意防御；见 [recipe 示例](recipe-parsers-examples.md)（`tools.py`）。
- **陈旧缓存条目自愈。** 输出被删除或修改的缓存作业会标记为 `superseded` 并重跑；先清理遗留的陈旧输出（`tools.py`）。
- **非本地后端的 dry-run 无法探测版本。** 它使用占位工具版本，因此打印的缓存判定可能与真实运行不同（`tools.py`）。
- **遗留的 `RUNNING` 作业在启动时清扫。** 每次非 dry 的 `analyze` 先把被杀进程留下的 `RUNNING` 作业改标为 `interrupted`（`tools.py`）。
- **`analyze --limit N` 取按 `file_id` 排序的前 N 个文件**——这是批量控制，不是公平性保证（`tools.py`）。
- **被中断的外部命令不会留下 run 行。** `run-external`/`analyze` 在结束时写入 `workflow_runs` 行；运行中途的 `KeyboardInterrupt`/`ShutdownRequested` 只留下 stdout/stderr 日志和（分析场景）一行 `interrupted` 的 `analysis_jobs`（`workflow.py`）。

## 执行后端

- **本地 CPU 时间可能多计。** `cpu_seconds` 是 `getrusage(RUSAGE_CHILDREN)` 的差值，包含同进程并发回收的其他子进程；Windows 没有 `resource` 模块，因此该值为 `None`（`execution.py`）。
- **Slurm 载荷只在 `cd` 成功时执行。** 生成的批处理脚本用 `cd` 的退出码守卫载荷，并总是写退出码文件（`execution.py`）。
- **`sacct` 内存解析把裸数字当字节。** Slurm 对较小的精确值不加后缀，`MaxRSS=123` 会被解析成一个极小的 MB 值（`execution.py`）。
- **SSH 超时可能留下仍在运行的远端进程。** 载荷在 `setsid --wait` 与远端 `/tmp` pidfile 下运行；超时时进程组先收 SIGTERM 再 SIGKILL，但 pidfile 缺失时错误只能提示"远端进程可能仍在运行"（`execution.py`）。
- **输出回拉是不对称的。** 远端输出不存在时被跳过（由期望输出检查报告），但本地已有内容不同的输出会抛 `ConflictError`（`execution.py`）。
- **路径改写是词法层面的。** 远程执行只改写项目根内绝对路径的词法形式；符号链接参数会被先解析，词法在内但实际链接到外部的路径会报错（`execution.py`）。

## 远程镜像

- **遗留的 manifest 锁会阻塞所有加锁操作，直到人工移除。** 锁是远端原子 `mkdir`，没有过期机制；崩溃的 push 会刻意留下 `.operon-manifest.lock`，错误消息给出确切路径（`remotes.py`）。
- **manifest 发布失败会留下未索引的远端文件。** 逐文件上传成功但最终 manifest 写入失败时，远端对象留在服务器上未被索引，本地批次报告错误；下一次 push 会发现相同字节并记为 `indexed`（`remotes.py`）。
- **没有 `project_id` 的远端 manifest 会被静默认领。** 把 remote 指向空目录或无主目录会在无确认的情况下接管它（`remotes.py`）。
- **不带 `--file-id` 的 `pull` 要求条目在本地存在。** 它遍历远端 manifest，对本地数据库中不存在的任何条目抛 `ConflictError`；远端是既有 manifest 的镜像，不是向空项目独立恢复的备份（`remotes.py`）。
- **evict 只核查一个指定远端。** `evict` 依据单一 remote 置 `REMOTE_ONLY`；其他已配置 remote 可能没有该文件，而 `verify` 接受任意一个已验证远端即视为足够（`remotes.py`、`files.py`）。
- **`sftp://` ingest 没有完整性锚点。** 既无期望哈希也无主机密钥固定选项；正确性依赖 ingest 时对接收字节的哈希（`remotes.py`）。
- **目录产物每次检查都流式遍历整棵树。** 对目录的 `matches()` 会通过 SFTP 走遍每个文件，因此 push/pull/evict 目录产物的开销为 O(树大小)（`remotes.py`）。
- **manifest 锁等待时长等于 `connect_timeout`。** 每个 remote 的 `connect_timeout`（默认 30 秒）同时限定 push/pull 等锁的时长（`remotes.py`）。

## release 与 export

- **`checksums.sha256` 只覆盖数据文件。** 元数据 TSV 与 `manifest.tsv` 的哈希记入 `provenance.json` 和数据库汇总，但不进 `checksums.sha256`，因此 `sha256sum -c` 只校验 release 的一个子集（`release.py`）。
- **发布预检要求所有活动范围内实体都有 decision。** 缺少 decision 或评估后元数据发生变化时，在发布任何产物前拒绝 release；失败和退役判定仍会写入 `exclusions.tsv`，未评估实体不会静默消失（`release.py`）。
- **`--link hardlink` 会与 `raw/` 共享 inode。** 默认是复制，文档中关于不可变的论证也以复制为前提；选择硬链接会让 release 与 raw 归档重新共享 inode（`release.py`）。
- **选出零个文件的 export 仍算成功。** 退出码 0，得到空的 `manifest.tsv` 与空的 `checksums.sha256`（`export.py`）。
- **export 符号链接存储完全解析后的目标。** `--link symlink` 指向 `source.resolve()`；移动项目会断链，不过经链接的校验仍然通过（`export.py`）。
- **release/export 失败可恢复。** 两个命令都先 staging 并在失败时清理，不会主动发布不完整目标；操作系统级崩溃仍可能留下隐藏 staging 目录，需要后续清理。

## 生命周期与身份解析

- **retire 幂等；restore 只能从根做起。** 对已 retire 的实体再次 retire 会报告 `changed: False`。从祖先继承退役的实体不能单独恢复——必须恢复退役根；子实体的直接退役在恢复父实体后仍然保留（`lifecycle.py`）。
- **retire 纯属逻辑删除。** `physical_changes` 恒为零：不删元数据行、不动文件字节、不改历史 release（`lifecycle.py`）。
- **裸 accession 匹配区分大小写。** 内部 ID（`ASM_000001`）大小写不敏感并统一大写，但裸 accession 必须与存储时的大小写一致；一个 accession 对应多个实体时需要 `NAMESPACE:ACCESSION`（`entity_view.py`）。
- **`show` 默认拒绝已退役实体。** 命中已退役实体时会报错，除非给出 `--include-retired`；否则也会静默隐藏退役/被取代的后代（`entity_view.py`）。

## 元数据、表格导入与 taxonomy

- **字面量 "na"、"n/a"、"null"、"none" 会变成 NULL。** 对任何字符串字段（不区分大小写）生效，除非该字段的 `allowed` 列表恰好包含该 token——名为 "None" 的 strain 或 "NA" 的 isolate 会被静默置空（`schema.py`）。
- **`id` 类型字段统一大写；`allowed` 值做大小写归一。** 输入不区分大小写匹配，按 schema 中的拼写存储（`schema.py`）。
- **TSV 注释是结构性的。** 首个非空白字符为 `#` 的行一律跳过，包括数据行；表头必须是第一行非注释行；除表头以空名结尾时允许缺少最后一个空列外，列数不齐即为错误。带 BOM 的文件可以处理（`schema.py`）。
- **重复键检测可能不完整。** 同批导入中一旦有更早的行出现字段错误，后续行的重复主键/唯一约束检查会被跳过，失败清单可能少于实际（`schema.py`）。
- **日期校验依赖 Python 版本。** Python 3.11+ 的 `datetime.fromisoformat` 接受 `20240115` 这类宽松格式；3.10 上同样的值会报错（`schema.py`）。
- **XLSX 只读第一个工作表**（按工作簿顺序，与名称无关），其余工作表被忽略。带小数部分的 Excel 序列日期成为 datetime（`table_import.py`）。
- **更新时空白单元格会清空现有值。** 预览 diff 会显示，但省略列保留现值；若有任何行会发生变化，`--on-conflict error` 拒绝执行。整个 apply 在一个事务中（`table_import.py`）。
- **元数据修补保留生命周期状态但会使评估新鲜度失效。** 更新既有行会记录字段级审计变化，后续 release 预检要求在该时间点之后重新 QC/evaluate（`table_import.py`、`release.py`）。
- **同文件内的前向引用无法解析。** 引用校验不能解析同一文件中较晚出现的 ID；请按 organism → sample → run/assembly 的顺序分多次导入（`table_import.py`）。
- **taxdump 快照没有灭绝数据。** taxdump 导入的 `is_extinct` 存为 NULL，因此带 `exclude_extinct: true` 的 coverage profile 面对它直接报错；只有 NCBI Datasets JSONL 来源携带该字段（`taxonomy.py`）。
- **失败的 taxonomy 导入会在磁盘上留下来源副本。** 来源先复制到 `raw/metadata/ncbi_taxonomy/`，再进入导入事务；失败时事务回滚，但已复制的文件留在磁盘上且未登记（`taxonomy.py`）。
- **taxonomy 版本不可变。** 同版本不同字节再导入会抛 `ConflictError`；parent/alias 引用完整性在导入时强制，因此不完整的 taxdump 会被拒绝（`taxonomy.py`）。
- **覆盖率百分比先舍入再比较。** 数值先按四舍五入（half-up）保留 4 位小数再做闭区间 `>=` 阈值比较，因此 79.99996 % 舍入为 80.0000 %，通过 80 % 阈值（`coverage.py`）。
- **覆盖率只支持 NCBI taxonomy。** taxonomy 来源不是 `NCBI` 的观测以 `UNSUPPORTED_TAXONOMY_SOURCE` 排除；复用的缓存报告若判定为 FAIL，仍以退出码 1 呈现（`coverage.py`）。

## NCBI Datasets 适配器

- **导入只能新增或更新，永远不清空字段。** 来源中的空值不会覆盖既有非空值（`adapters/ncbi_datasets.py`）。
- **部分归一化是静默的。** 未知 sex 值变为 `unknown`；无法解析的日期和越界的经纬度变为 NULL——没有错误或原因码（`adapters/ncbi_datasets.py`）。
- **只接受 `GCA_`/`GCF_` accession**（可带版本号，统一大写）；SRA run 与其他标识体系是硬性校验错误（`adapters/ncbi_datasets.py`）。
- **部分下载失败时保留已提交批次。** 部分批次失败而其余导入成功时，run 在成功批次已提交之后抛校验错误；重跑会幂等跳过已完成批次（`adapters/ncbi_datasets.py`）。
- **pre-2.6 注释桥接是保守的。** 仅当 report 属于 assembly 的规范 accession、且该注释行未被其他 accession 认领时才复用既有注释行——这是对"GCA/GCF 成对包注释元数据相同但 GFF 字节不同"的规避（`adapters/ncbi_datasets.py`）。
- **磁盘预检保留固定 64 MiB**，且下载暂存在项目根内，从不使用 `/tmp`（`adapters/ncbi_datasets.py`）。
- **适配器自动升级旧元数据 schema。** 打开 pre-{{ metadata_schema }} 的 `config/schemas.yaml` 会就地升级，归一化格式并丢弃手写注释（与首次 REMOTE_ONLY 驱逐触发的归一化相同；见[远程存储指南](../guides/remote-storage.md)）（`adapters/ncbi_datasets.py`）。

## 备份与导入向导

- **`backup create` 通过 SQLite backup API 快照。** 即使其他连接正在写入也能得到一致快照，且不复制 WAL 文件（`backup.py`）。
- **`results` 范围不能恢复数据。** 它在 control 之上增加 QC/analysis/reports/taxonomy/releases，但排除 `raw/` 与 `standardized/`；只有 `full` 包含数据字节。目标目录必须在项目根之外且不存在（`backup.py`）。
- **`backup verify` 拒绝多余的意外文件**，而不只是缺失或被改动的文件（`backup.py`）。
- **向导在哈希与复制期间持有写锁。** ingest 发生在一个大事务中；失败时数据库回滚、缓冲的 JSONL 记录被丢弃、仅删除新创建的 `raw/` 目标——已存在的目标保留（`import_wizard.py`）。
- **向导需要 TTY 且只接受常规文件**（不支持目录产物）。若展示过的 ID 被其他进程占用，preflight 会报错，向导必须重启（`import_wizard.py`）。
- **demo 先登记 protein 再登记 GFF3**，让注释 QC 能看到完整的注释组合；对多文件实体，文件插入顺序会影响 QC 完整性（`demo.py`）。

## 环境与关机

- **本地与远端环境文档天然不同。** 本地采集包含 Python 与 `operon` 版本；远端探测无法报告它们，因此同一机器在 local 与 SSH 执行下可能得到不同的 `environment_id`（`environment.py`）。
- **第二次信号跳过清理。** 第一次 SIGINT/SIGTERM 触发优雅关机（退出码 130）；清理期间的第二次信号直接 `os._exit(128+signum)`。`graceful_shutdown` 在主线程之外是 no-op（`shutdown.py`）。

## CLI 约定

- **退出码：** 0 成功；1 为运行时/SQLite/OSError（包括 release/export 的 `FileExistsError`）以及 `qc`/`verify`/`analyze`/`push`/`pull`/`evict`/`backup verify`/`report coverage` 中任何逐条目失败；2 为所有 `OperonError`（校验、冲突、校验和、远程、配置）；130 为首次中断（伴随"进度已保存、可重跑同一命令"的提示——仅对可续跑的 NCBI 适配器与分析路径成立）；第二次信号为 `128+signum`（`cli.py`、`shutdown.py`）。
- **只要存在非 `CHECKSUM_VERIFIED` 且非 `REMOTE_ONLY` 的文件，`operon verify` 就以 1 退出**，包括瞬态的 `REMOTE_UNVERIFIED`（`cli.py`）。

## 推迟到 1.0 版本

若干开发期兼容垫片只为 1.0 之前创建的数据库而存在，计划在 1.0 移除（代码中标记 `TODO(1.0)`）：

- `database.py` 中 pre-1.0 迁移调用与 `_migrate_pre_1_0_schema()` 重建（遗留行保留 `legacy:` 输入身份，永不与新 QC 行去重）。
- `adapters/ncbi_datasets.py` 中针对旧 schema 的防御性字段投影与自动元数据 schema 升级。

完整清单与移除策略见[数据库兼容性](../operations/database-compatibility.md)。
