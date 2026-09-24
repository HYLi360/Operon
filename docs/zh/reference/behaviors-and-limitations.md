# 隐式行为、边界情形与已知问题

本页对应 `operon` {{ operon_version }}（数据库 schema {{ db_schema }}，元数据 schema {{ metadata_schema }}）。它记录代码中可观察到、但未在其他任务型或架构型页面中说明的行为：隐式语义、边界情形与已知问题。各项按当前实际行为如实描述，并标注实现模块以便核对。本页不是使用建议；受支持的工作流请参阅[指南](../guides/index.md)与[故障排查](../guides/troubleshooting.md)。

已修复的历史问题移至[已修复问题](resolved-issues.md)；其余条目描述当前行为与已接受的限制。

当前条目按三类标注：

- **有意为之但属隐式**：设计如此，但后果可能出乎意料（例如状态迁移对审计记录的影响）。
- **限制**：当前接受的能力边界或健壮性缺口。
- **已知问题**：缺陷或数据语义上的意外行为，此处如实记录而非隐而不报。尚未解决的问题会在条目中给出可行方案（如有）。

以 **已知问题** 开头的条目属于第三类；其余条目属于有意为之但属隐式的语义或已接受的限制。

## 身份、归档与文件系统

- **raw 不可变性是绝对的，本地文件丢失也不例外。** 同实体同角色重新 ingest 不同字节会抛出 `ConflictError`，即使先前归档的文件在本地已缺失或损坏（`files.py`）。不存在就地替换路径；请归档为新实体版本，或走带审计的修复流程。
- **不受清单管理的遗留文件会被隔离，而非删除。** 当 `raw/` 内规范目标路径被清单不认识的字节占用时，占用者会被移动为同目录下的 `<name>.orphan-<sha12-prefix>`（`files.py`）。这类文件可能出现在本应不可变的归档目录中，且不是 manifest 成员；占用行归属与审计细节见 [ingest 页面](cli-files-qc.md#ingest)。
- **压缩检测是不对称的。** 名为 `.gz` 但 magic bytes 不是 gzip 的文件会被拒绝；反之，普通文件名但内容为 gzip 的文件会被静默记录为 `compression=gzip`（`files.py`，`detect_compression`）。
- **格式检测先看扩展名，再看调用方给出的 file role，从不看内容。** 至多剥掉一个 `.gz` 类后缀，role 回退只在扩展名查找之后、返回 `other` 之前生效，因此以 `genome_fasta` 身份 ingest 的 `x.fasta.bz2` 会记为 `fasta` 而非 `other`（`files.py`，`detect_format`）。
- **manifest 相对路径是 POSIX 路径。** Windows 上 `Project.project_rel` 原样返回 `os.path.relpath`，因此存储的 manifest 路径可能含反斜杠；`remotes.py` 是唯一把 `\` 归一化为 `/` 的模块，而 release 与 export 原样复制 `relative_path`，所以存储契约仅支持 POSIX（`config.py`、`remotes.py`、`export.py`）。
- **`standardize --link hardlink` 对目录产物回退为完整复制**——只有单文件能建立硬链接（`files.py`，`standardize_file`）。新标准化目标采用原子发布，任何失败都会清理。
- **`verify` 恒做全量 SHA-256；`qc` 使用 stat 指纹缓存。** `touch` 或复制会使缓存失效并强制 QC 重新哈希，但 `operon verify` 从不读缓存（`files.py`，`verify_local_file_identity` 与 `verify_files`）。
- **`REMOTE_UNVERIFIED` 只是输出状态。** 它从不持久化，不写审计记录，也不改动既有 `files.status`——但单凭它就会让命令以退出码 1 结束，因此网络抖动会让命令失败，却不会被误判为数据丢失（见 [verify](cli-files-qc.md#verify)）（`files.py`）。
- **`verify` 只对本地字节缺失的文件实时核查远端。** 本地字节存在时，远端漂移不会被检测到（`files.py`）。
- **`operon` 命令作用于最近的上层项目。** `Project.find` 向上遍历父目录寻找 `project.yaml`，因此在子目录中执行的命令会静默作用于外层项目（`config.py`）。
- **目录身份不含时间戳与所有权。** `sha256_directory` 覆盖相对路径、空目录、文件字节、大小和符号链接目标；目录内出现 FIFO/socket 会让该目录树的 ingest/verify 以 `OSError` 失败（`utils.py`）。
- **中断的复制会留下隐藏的临时条目。** `atomic_copytree` 在目标旁的 `.target.XXXX` 临时目录中工作，`atomic_copy`/`atomic_write_text` 会留下同级的 `.<name>.XXXX` 临时文件；只有进程内异常才会清理它们，因此 SIGKILL 或断电会将其遗留，需手动清理（`utils.py`）。
- **幂等重 ingest 不会降级已标准化的文件。** 重新 ingest 相同字节会重新校验归档：`STANDARDIZED` 文件保持原状态不变，其他状态迁移走带审计的 `set_file_status` 路径（状态未变时不写任何内容），只有 `downloaded_at`/`source_url` 会被补充（`files.py`）。
- **`standardize` 走带审计的状态机。** 文件状态（`set_file_status`）与实体迁移（`set_state`）都会记录进 `changes`；为 `RELEASED`/`ACCEPTED` 实体 staging 新的标准化目标会被作为非法迁移拒绝（先用 `set-state --force` 做带审计的人工纠正），而幂等路径会修复 `CHECKSUM_VERIFIED` 的崩溃窗口、且不惊动已经继续推进的实体。重新校验通过的字节允许从 `CHECKSUM_FAILED` 直接前进到 `STANDARDIZED`（`files.py`、`workflow.py`）。
- **`standardize` 每次运行都重新哈希。** 源与目标的哈希每次重算，stat 指纹缓存只写不读，因此重复执行该命令要付出一次全量字节扫描，与 `qc` 不同（`files.py`）。

## 数据库、事务与并发

- **并发写只在 30 秒内被串行化。** 数据库运行于 WAL 模式，`busy_timeout=30000` 且使用即时写事务；写入者在读取前等待锁，超过超时仍会收到 `database is locked`（退出码 1）。稳定 ID 在同一把锁下预留，因此并发分配唯一；失败插入可能留下编号间隙（`database.py`）。
- **迁移在每次可写打开时执行，而非仅在 `operon migrate` 时。** 打开旧 schema 的项目时，任何命令都会顺带应用未执行的加法迁移（`database.py`）。迁移全部为加法（新列/新表）；没有破坏性迁移，也没有数据回填，唯 pre-1.0 重建除外（见[数据库兼容性](../operations/database-compatibility.md)）。
- **只读访问要求 WAL 为空。** 只读挂载只有在 `-wal` 文件为空时才能打开，否则报错并提示到可写主机上做 checkpoint（`database.py`）。
- **`operon query` 拒绝的不只是写入。** SQL authorizer 拒绝 DML/DDL/ATTACH/SAVEPOINT，且仅放行只读 PRAGMA 白名单，因此并非表写入的副作用语句同样失败：`PRAGMA journal_mode` 报 *not authorized*，`VACUUM` 报 *authorization denied*（见 [query](cli-decisions-reports.md#query)）（`database.py`）。
- **把状态设置为当前值只是“半个”no-op。** 审计行只在状态真正变化时写入，但 `entity_state` 行会被无条件改写：等值调用仍会覆盖 `message`（`None` 会清空原文本）与 `updated_at`，只是不写 `changes` 行。`set_state_bulk` 强制接受每一次迁移（绕过迁移表），但审计行为同样只在状态变化时写入（`workflow.py`、`database.py`）。
- **重跑 `qc` 或 `evaluate` 不会降级已有决定的实体。** 批量 QC 与自动重评估都走受守卫的状态写入：处于 `ACCEPTED`/`REVIEW`/`REJECTED`/`RELEASED` 的实体保持其生命周期状态，同时新鲜的 QC 证据与 decision 行照常记录；只有显式的 `curate` 或 `set-state --force` 才能移动它（`workflow.py`、`qc/__init__.py`、`rules.py`）。
- **可写打开在单个 immediate 事务内重建派生视图。** `current_decisions`、`effective_retired_entities` 与 `current_entity_lifecycle` 在写锁保护下删除并重建，因此并发的 `operon` 进程（或 CLI 与 TUI）会等待而不是在重建中途相撞（`database.py`）。
- **每次可写打开都会重建 schema 对象。** 视图重建会改变 `PRAGMA schema_version`，即使没有任何数据变化也会在每次可写打开时写入 WAL；只读命令在此之前就返回（`database.py`）。
- **来自更新 schema 的数据库会被静默接受。** 没有任何代码读取或比较存储的 `database/SCHEMA` 标记，`SCHEMA_VERSION` 只被写入和报告：旧二进制会打开该数据库，用自己的版本字符串重写标记并重建自己的视图（`database.py`、`cli.py`）。
- **`project.yaml` 不做校验，`init` 也不是原子的。** 缺失或改名的键会抛出裸 `KeyError`（traceback，退出码 1）而非配置错误；`Project.init` 先写 `project.yaml`，因此后续失败会留下半初始化项目——`Project.find` 会接受它，而重试会拒绝重新初始化（`config.py`、`cli.py`）。
- **三个默认配置键没有任何读取方。** `resources.max_memory_gb`、`qc.sample_reads_for_duplicates` 与 `project.description` 由 `operon init` 写入，但没有任何代码路径读取（只有 `resources.default_threads` 与 `qc.default_profile` 会被消费），设置它们既无效果也无警告（`config.py`、`tools.py`）。

## 时间戳、日志与排序

- **时间戳是带偏移的本地时间，不是 UTC——而且同一个数据库混用两者。** `now_iso()` 输出形如 `2026-09-05T14:30:00+08:00`，多数按时间排序的逻辑直接做字符串比较，这只在所有写方共享同一时钟/偏移时安全；`julianday()` 补偿出现在 `workflow list`、release 过期检查和 TUI 中，而 `database/SCHEMA` 标记与 `schema_migrations.applied_at` 用 SQL `datetime('now')`（UTC，空格分隔）写入同一数据库（`utils.py`、`workflow.py`、`release.py`、`database.py`）。
- **`logs/workflow.jsonl` 与数据库可能不一致。** JSONL run 记录的追加没有进程间锁，且在数据库提交之后才落盘，事务回滚会丢弃其缓冲记录；每条记录以一次追加加 `fsync` 写入，因此现实风险是缺失、重复或乱序的行，而不是半行（`utils.py`、`workflow.py`）。
- **run 的输入身份嵌入绝对路径。** `workflow_runs.input_sha256` 是 `path:sha256` 行列表的哈希；相同内容位于不同路径（项目搬移）会得到不同的输入身份（`workflow.py`）。
- **run 的输入身份在不同步骤之间不可比。** 外部命令哈希的是 `path:sha256` 列表，而内置 QC 存的是文件的纯 SHA-256，因此同一文件在不同步骤得到不同的 `input_sha256`；`log_run` 还把 `tool_version` 默认成 `operon` 版本，于是 ingest 与 QC 行会把 Operon 本身记为工具（`workflow.py`、`qc/__init__.py`）。
- **输出条数限制是隐式的。** `workflow list` 默认 50 行（`--limit 0` 为不限，`--to` 为开区间；见 [workflow list](cli-workflow.md)），`report analysis` 默认 20 行且无截断提示，而 `report qc` 与 `report decisions` 不设上限；空结果会打印 `(no QC results)`、`(no decisions)`、`(no analysis results)` 之类的字面哨兵（`cli.py`、`reports.py`）。
- **失败记账是尽力而为的。** 在失败或中断路径于 `try`/`except Exception: pass` 中写入自己的 `workflow_runs` 行、随后重新抛出原始异常的地方，记账写入一旦失败就会被吞掉，因此失败命令可能让 run 行停留在 `running`，且没有任何记账失败的痕迹（`import_wizard.py`、`taxonomy.py`、`adapters/ncbi_datasets.py`）。
- **执行环境探测失败是静默的。** 环境采集失败时 run 正常完成，只是没有 `environment_id`，也没有警告（`workflow.py`）。

## 指标与判定

- **没有解析器的格式会停留在 `QC_COMPLETE`。** 必需规则对应的指标没有值时判定为 `NOT_EVALUATED` 而不是失败（见 [QC profile 指南](../guides/qc-profiles.md)），因此没有解析器的格式（如 BAM）同样停留在 `QC_COMPLETE`，仅因永远到不了 `PASS` 而被 release 排除（`rules.py`）。
- **`evaluate` 只覆盖已有 QC 结果的实体。** 从未被 `qc` 处理过的实体不会有 decision；发布预检会在创建任何产物前拒绝这类活动范围内实体（`rules.py`、`release.py`）。
- **多文件实体会混合来自不同文件的指标。** `latest_metrics` 按输入身份分区：保守布尔指标（`file_exists`、`sha256_match`、`parseable`、`paired_read_count_match`）取所有输入的最小值，而其他指标取最近评估输入的值——一个判定可能把一个文件的计数与另一个文件的布尔值组合起来（`database.py`）。
- **更改 QC 采样参数会累积行。** QC 结果按参数集 upsert；用不同 `--sample-size`/`--phred-offset` 重跑 `qc` 会新增一组平行行而非替换，`latest_metrics` 中最新的静默胜出（`database.py`）。
- **阈值语义是闭区间，集合比较按字符串。** `>=`/`<=` 为闭，`between` 两端皆闭，`in`/`not_in` 把指标值当字符串比较，缺少 `operator` 的规则恒通过（`rules.py`）。profile 结构缺口（缺 `min`/`max`/`values`）的行为见 [QC profile 指南](../guides/qc-profiles.md)。
- **`curate` 接受任意判定字符串。** 值会被转大写但不校验是否属于判定枚举；无法识别的值原样存储，并把实体映射到 `QC_COMPLETE`（`rules.py`）。
- **损坏的 profile 只影响它自己指向的那一个。** `evaluate` 只加载指定的单个 profile 文件，`analyze` 根本不读 profile，因此两者都不会枚举 `config/profiles/`；该目录中的草稿或改到一半的 YAML 只有在遮蔽了指定 profile 时才有影响，整目录列举只属于 TUI 的 profile 界面（`rules.py`、`profiles.py`）。当被指定的 profile 存在 YAML 语法错误时，命令以 traceback 和退出码 1 失败，而不是 `error:` 消息，因为 `yaml.YAMLError` 不是 `OperonError`（`cli.py`）。
- **人工覆盖会在重新评估后保留。** 最新自动判定沿用当前 `curated_*` 字段；显式 `curate` 才会改变生命周期（`rules.py`、`cli.py`）。
- **数值规则作用在文本指标上会晚失败。** `latest_metrics` 可能返回文本值，数值算子随后抛出裸 `ValueError`，CLI 只打印无上下文的 `error: ...` 并以退出码 1 结束，而不是给出指名规则或指标的错误（`database.py`、`rules.py`、`cli.py`）。

## 内置 QC 解析

- **FASTA 序列行内部的空白会计入无效碱基。** 行首尾会被去除，但内部空格保留并推高 `invalid_base_count`；序列数据必须为 ASCII（`qc/parsers.py`）。
- **seqid 是 header 的第一个空白分隔 token。** 重复检测分别统计完整 header 与 seqid（`qc/parsers.py`）。
- **protein 内部终止子计数会原谅末尾的 `*`。** 末尾终止子会被减去（下限 0）；`missing_start` 要求第一个残基为 `M`（`qc/parsers.py`）。
- **FASTQ 重复率只基于前 N 条 reads。** `duplicate_sampling_strategy=first_n`，默认采样 1,000,000 条；在远大于采样量的文件中，集中在后段的重复序列不可见（`qc/parsers.py`）。
- **Phred `auto` 在区间重叠时假定 33。** Sanger/Illumina 质量区间重叠时静默取 33；不确定性只通过 `quality_encoding=ambiguous_assumed_phred33` 体现。ASCII 33–126 之外的质量字符会中止 QC（`qc/parsers.py`）。
- **GFF3 容忍多种不规则性。** 制表符字段数 ≠9 的行计入 `coordinate_error_count` 并跳过；`##FASTA` 之后的内容被忽略；CDS 三联检查只看坐标、忽略 phase 列；Parent 完整性对照整个文件检查，因此前向引用可以通过（`qc/parsers.py`）。
- **`parseable` 只存在于有解析器的格式。** FASTA/FASTQ/GFF3 会记录；其他格式使 `parseable == 1` 门槛永远处于 `NOT_EVALUATED`（`qc/__init__.py`）。
- **配对 reads 匹配会被静默跳过**——当同批 FASTQ 没有 manifest 行或不在磁盘上时，既无指标也无警告（`qc/__init__.py`）。
- **损坏的 FASTA 长度缓存会被静默重建。** 摘要/计数不匹配时删除并重建缓存（`qc/__init__.py`）。
- **Cython 与纯 Python 解析器零容忍差异。** 指标与错误消息字符串必须逐字节一致；由 `tests/regression/test_cython_parser_parity.py` 强制。
- **`qc --file-id` 仍会继承同层文件的失败。** 实体状态聚合所有同层文件，因此只针对一个健康文件的运行会在同层文件 `QC_FAILED` 时以 1 退出；对 `REMOTE_ONLY` 文件执行 `--file-id` 时，唯一结果被跳过，同样以 1 退出（`qc/__init__.py`、`cli.py`）。
- **R1/R2 配对信任未经校验的同层字节。** 同批 FASTQ 通过直接重读计数，没有 manifest 校验和检查，因此保持记录数不变的字节级损坏既不会改变配对指标，也不会影响报告出的完整性（`qc/__init__.py`、`qc/parsers.py`）。
- **annotation 会继承同层文件的失败。** assembly 与 protein 输入在 GFF3 解析之前校验，因此缺失或变化的同层文件会在 annotation 上记录 `parseable=0` 并使其失败（`qc/__init__.py`）。
- **重复 seqid 会放大长度并削弱 GFF3 坐标检查。** 长度映射把同名 seqid 的每条记录相加，因此含重复 seqid 的 assembly 会报告偏大的总量，超出真实记录长度的 feature 末端仍可能通过坐标检查（`qc/parsers.py`、`qc/__init__.py`）。
- **read 过表示使用硬编码阈值。** 某序列超过前 N 条采样 reads 的 1 % 即计为过表示——该阈值写在 QC 代码里而非版本化 profile 中，并继承了采样盲区（`qc/parsers.py`、`qc/_parsers.pyx`）。
- **`protein_stats` 只惩罚 `X` 与 `*`。** 其他残基一律接受并计数，因此 `-`、`J`、`O`、`U` 与数字既不受惩罚也不被报告，已定义的蛋白字母表常量从未被引用（`qc/parsers.py`）。
- **alignment QC 用自己的中位数并合并 header。** 偶数个取值取上中位数，共识并列取最先出现的残基（与 `utils.median` 不同）；`safe_id` 是 header 的第一个空白分隔 token，因此首个 token 之后不同的 header 会塌缩为同一行且不做重复检测（`qc/alignment.py`、`qc/parsers.py`）。
- **`qc-measure` 混用参数集。** `file_exists` 写在默认参数集下，而其他阶段使用调用方给出的集合；GFF3 测量至少需要 `--assembly-fasta`/`--protein-fasta` 之一；因此自定义参数集的远程测量会导入一行落在默认集合中的数据，随后本地 QC 可以覆盖它（`qc/measure.py`、`qc/__init__.py`）。
- **不可测量的值会变成真实的 0。** `pct(x, 0)` 与 `median([])` 返回 `0.0`，因此空 FASTA 会存入 `gc_percent=0` 与 `median_sequence_length=0`，规则会把它当测量值比较，而不是 `NOT_EVALUATED`（`utils.py`、`qc/parsers.py`）。
- **`qc_all` 会静默跳过被取代与已退役实体。** 批量运行可能报告成功，而整批实体从未被处理，显式 `--entity-id` 指向被取代实体时也一样（`qc/__init__.py`）。

## 外部分析

- **参考数据库身份取决于数据库模式。** 未显式给出 `database_checksum` 时，单文件数据库按完整内容哈希，目录数据库只按每个文件的相对路径、大小与 mtime 指纹化，而 `database_mode: mutable_cache` 完全忽略内容；因此目录型数据库（BLAST 索引目录）在 touch 或复制后会产生虚假缓存未命中，在保持大小与 mtime 的就地修改后又可能复用陈旧结果，其单个索引文件也永远不会获得独立身份（`tools.py`）。
- **工具版本探测有一个粗糙的回退。** 版本正则未命中时，第一个像版本的 token——或整个首行截断到 200 字符——会成为 `tool_version`；探测原始输出在 provenance 中截断到 4000 字符（`tools.py`）。
- **BLAST 表格解析静默丢弃。** 字段数与 `result_columns` 不符的行被无错跳过，`max_hits_per_query`（默认 5）截断每个 query 存储的 hits——`analysis_results` 汇总可能偏少（`tools.py`）。
- **结果解析不是原子的。** hits 与 results 分别在两个事务中写入；解析中途崩溃会给随后标记为 failed 的作业留下部分 hits（`tools.py`）。
- **空输出即失败。** 只有退出码为 0 且每个期望输出都存在且非空，run 才是 `completed`；合法的 0 字节 TSV 会让 run 失败（`workflow.py`）。
- **BUSCO auto-lineage 拒绝路径中含 `fasta` 的输出。** 这是对 SEPP 路径改写缺陷的刻意防御；见 [recipe 示例](recipe-parsers-examples.md)（`tools.py`）。
- **陈旧缓存条目自愈。** 输出被删除或修改的缓存作业会标记为 `superseded` 并重跑；先清理遗留的陈旧输出（`tools.py`）。
- **非本地后端的 dry-run 无法探测版本。** 它使用占位工具版本，因此打印的缓存判定可能与真实运行不同（`tools.py`）。
- **`analyze --limit N` 取按 `file_id` 排序的前 N 个文件**——这是批量控制，不是公平性保证（`tools.py`）。
- **被中断的外部命令不会留下 run 行。** `run-external`/`analyze` 在结束时写入 `workflow_runs` 行；运行中途的 `KeyboardInterrupt`/`ShutdownRequested` 只留下 stdout/stderr 日志和（分析场景）一行 `interrupted` 的 `analysis_jobs`（`workflow.py`）。
- **`--threads` 参与缓存指纹。** 解析后的线程数（默认 4）会被哈希进参数指纹，因此即使工具输出完全相同，改动线程数也会让每个文件重跑（`tools.py`）。
- **是否参与 array 对缓存不可见。** 一个批次以单个 Slurm job array 还是逐文件作业提交，从不进入参数指纹或缓存身份，因此两种提交形式共享同一份完成缓存（`tools.py`）。
- **`analyze` 不向任何后端传超时。** 因此挂起的工具在本地和 SSH 上都会无限运行，Slurm 后端只受默认 `--time=24:00:00` 限制（`tools.py`、`execution.py`）。
- **已知问题：失败的运行会保留部分输出。** 只有中断才会删除已算出的产物；其他任何失败（包括结果解析错误）都会把截断的 TSV 或写了一半的输出目录留在磁盘上，看起来就像结果（`tools.py`）。
- **`analyze` 可能改写 manifest。** 配置了 SSH `storage_remote` 时，本地字节缺失而远端副本校验通过的文件会被静默改标为 `REMOTE_ONLY` 并留下审计行，因此一次分析会改动文件状态（`tools.py`）。
- **陈旧 `RUNNING` 清扫按当前 analysis 限定范围。** 非 dry 的 `analyze` 只把本 analysis 遗留的 `RUNNING` 行标为 `interrupted`；并发跑同一 recipe 的两个进程仍可能互相清扫对方的活作业（`tools.py`）。
- **结果汇总基于被截断的 hit 集。** `hit_count`、`best_evalue` 与由此得出的“top hit”只用 `max_hits_per_query` 之内保留的 hits，`hit_rank` 是该行在结果文件中的位置而非得分排序，因此汇总可能偏少并给出错误顺序（`tools.py`）。
- **recipe 笔误要么静默降级，要么抛出裸错误。** 不在 `result_columns` 中的指标列会被无提示丢弃，而 query/subject 列不在 `result_columns` 中会抛出普通 `ValueError`（退出码 1，而非校验错误）（`tools.py`）。
- **HMMER 解析的默认行为不对称。** 没有 `# <program> ::` 头时按 hmmscan 列映射处理，且 tblout 解析器会静默丢弃不足 6 个字段的行，而 rpsbproc 解析器会报错（`tools.py`）。
- **版本与数据库身份缓存带 300 秒 TTL。** 一个批次只付一次探测开销，但长期运行的进程（例如 TUI）会在 TTL 过期后重新探测，因此就地升级的工具或在原路径替换的参考数据库会被察觉；TTL 本身不可配置（`tools.py`）。
- **版本探测有硬上限，并在 `logs/` 下暂存。** 它使用硬编码的 120 秒超时，并把临时输出暂存在项目的 `logs/` 目录下；两者都不可配置（`tools.py`）。
- **命令链只校验最后一步的输出。** 中间产物在 run 被记为 `completed` 之前从不检查，因此链可以在中间产物缺失的情况下“完成”（`workflow.py`）。
- **被采纳的 lineage 边只插不改。** `(derived_file_id, input_file_id)` 上的 `INSERT OR IGNORE` 意味着重复采纳永远不会更新 `workflow_run_id`，而且根本不存在更新路径（不同字节抛 `ConflictError`）；首个绝对源路径作为 `source_url` 胜出，相对 manifest 路径按项目根而非工作目录解析，也没有自引用或环检测（`lineage.py`）。
- **`extract-domains`/`select-sequences` 会读取所有已完成作业。** 比对来自该文件的所有已完成作业，没有“最新作业”选择，源 FASTA 也从不做校验和验证（`sequence_tools.py`）。
- **序列抽取按字节原样复制。** 子序列保留 gap 与简并码，`--min-length` 在加侧翼之前过滤，顺序与命名按字典序（`seq10` 排在 `seq2` 之前），`--all-regions` 的 header 使用加侧翼后的坐标；`--regions-tsv` 中非正的起点会被拒绝，而来自数据库的坐标会被截到 1（`sequence_tools.py`）。
- **空结果与重复抽取都是静默的。** 空结果是 0 字节 FASTA 且退出码 0，原因只写在可选 manifest 中；既有输出被静默覆盖且从不登记；非空 `sequences` 注册表会替换由 FASTA 推导的 seqid 全集（但 FASTA 仍会被解析以取序列体）；重复 seqid 保留第一条记录（`sequence_tools.py`）。
- **分类标签按 profile 名称索引，并在任何指标变化时重新审计。** `sequence_labels` 以 `(file_id, seqid, profile_name)` 唯一，因此 profile 版本升级会就地覆盖标签；`details_json` 嵌入观测到的 hit，因此指标变化会让每个标签重新审计，即使标签本身未变；删除只针对当前范围内的文件，因此范围外的陈旧标签会残留（`classify.py`、`database.py`）。
- **分类中缺失字段在任何形态下都不满足条件。** 无论用什么 operator（包括 `!=`、`not_in` 与 `exists`）判定都为 False；对缺失字段条件的 `not:` 取反仍然是 False，`any:` 组中缺失字段的析取项视为不满足；`between` 要求数值操作数，否则抛出带字段与值上下文的校验错误，而 `in`/`not_in` 比较 `str(value)`（`classify.py`）。
- **分类只读取每个 analysis+file 最新的 completed 作业，但会明说。** 同一组合下更早的 completed 作业按 supersede 纪律被忽略，但计数非零时会在输出与 run details 中以 `ignored_completed_jobs` 呈现；不在 `sequences` 注册表中的文件被跳过，且跳过计数会显式打印（`classify.py`）。
- **`fanout --dry-run` 执行真正的预检，且 unit 字节与 TSV 行序无关。** dry-run 会校验每个源的 SHA-256 与注册表新鲜度、计算单元 identity、执行冲突/占用检查——冲突与真实运行一样抛 `ConflictError`——但仍不写文件、不开 run 行；每个计划单元标注 `would_create`/`would_reuse`。单元内的 seqid 在生成 FASTA 前规范化为排序序（词法序，`seq10` 排在 `seq2` 之前——与 `sequence_tools` 一致的确定性约定），因此重排指派 TSV 行不再改变单元字节。unit 角色 `<prefix>:<unit>` 会把 `:` 带进归档文件名；下游 `file_role_prefix` 选择按 `:` 边界匹配，`sub` 选中精确的 `sub` 与所有 `sub:*`，不捕 `sub2:*`（`fanout.py`、`tools.py`）。
- **`fanout` 会把中断记为 `interrupted` 的 run。** Ctrl+C 会删除已创建的目标并把 `workflow_runs` 行落为 `interrupted`（退出码 130），不再停留在 `running`。零 unit 与无法解析或有歧义的 seqid 仍是硬错误。`--source-file` FASTA 的 SHA-256 与注册表新鲜度会被验证（dry-run 同样执行），但指派表本身不做校验和验证（`fanout.py`）。
- **TUI 分析对话框通过协作式取消事件取消，而非信号式的精确落点。** 后端选择器与 `--backend` 对应（项目默认 / local / slurm / ssh），并在 worker 启动前预检：`execution.ssh` 配置不全、缺少 `sbatch`/`squeue`、或 recipe 的 `slurm:` 覆盖非法都会变成表单内联错误，而不是逐文件失败。Cancel/Escape 置位核心暴露的同一个 `cancel_event`（`run_analysis(..., cancel_event=...)`）并取消 worker；批处理在下一个文件/规划/收集边界停止——当前正在处理的文件仍可能跑完，进度回调异常路径保留为逐文件循环的第二道停止点。已提交的 Slurm 作业或 job array 以一次 `scancel` 取消，直连 SSH 载荷在远端主机上终止；所有取消路径都走与信号相同的中断收尾（`interrupted` 作业行、部分产物删除、下次运行时清扫陈旧 RUNNING 行）（`tui/actions.py`、`tui/screens/analyze.py`、`tools.py`、`execution.py`）。
- **TUI 日志跟随只读本地文件，且从不取消。** run 详情屏的 *Follow logs* 每秒用与 `workflow show --follow` 相同的增量读取器轮询 `logs/<run_id>.stdout.log`/`.stderr.log`（被截断或轮转的日志从头重读，开启开关会重放文件当前已有的内容），在 run 离开 `running` 时自行停止，且只观察——取消 run 仍是 CLI 动作。该开关仅在 run 运行中提供；SSH 后端的 stdout/stderr 在结束时才拉回，因此远程 run 在结束前不会显示任何内容（`tui/screens/runs.py`、`workflow.py`）。TUI 的 *Run external* 对话框所运行的命令同样无法从 TUI 中断（`tui/screens/run_external.py`）。
- **命中浏览器的导出与 CLI 等价，而序列 label 没有 CLI 读取命令。** Tasks 屏的 *Analysis hits* 使用与 `report analysis --hits` 相同的只读查询，其 *Export* 走 CLI 自己的渲染器，因此文件（`text`/`tsv`/`json`）与 `--out` 逐字节一致——但与 CLI 报表一样，导出不会写入任何 `changes` 或 `workflow_runs` 行。`sequence_labels`（`classify-sequences` 的产物，显示在 Files 详情与 `l` 浏览器中）完全没有 CLI 读取命令：TUI 的 label 视图就是该表的读取侧。
- **按钮在按压动画期间会忽略点击。** Textual 的 `Button` 在仍带有 `-active` 按压效果（约 0.2 秒）时会吞掉点击，因此对同一按钮的快速双击会丢失一次；TUI 各对话框按"一次操作一次 Confirm"设计，测试也会等该效果结束后再点击。
- **Files 屏的派生产物对话框把注册交给 adopt。** *Extract domains* 与 *Select sequences* 通过核心写入 FASTA，随后把路径交给 adopt 对话框（`derived_from` = 源文件），因为这两条命令都不注册输出——与 CLI 相同的两步流程。adopt 的 manifest 模式必须先 *Preview* 才能 Confirm（界面显示解析出的条目数）；`fanout` 的 dry run 是强制第一步，它执行真正的预检且完全不写入（也不写 run 行）；输入一旦改动，预览失效且 Confirm 再次被禁用。
- **分类 profile 表单只到一层 `any:`/`not:`。** Config 屏的 `sequence_classification` 编辑器建模平铺条件、一个 `any:` 组与一个 `not:` 取反——与 qc 编辑器为规则提供的形式一致。嵌套更深的 profile（或 `sources`/`rules` 不是映射）以只读打开：界面给出原因、禁用 *Save profile*、且绝不改写文件，因此手写的结构在每次访问编辑器后都完好如初。未建模的键在每一层原样保留，组合出的文档保持原有的键顺序。
- **profile 的 kind 属于文件，而非编辑器。** TUI 按磁盘上读到的 `kind` 把文档分派到 qc、分类或 coverage 表单；保存拒绝跨 kind（用分类文档编辑 `kind: qc` 文件，或反之，都会报错而不是写入）。`taxonomy_coverage` 编辑器建模扁平 coverage 语法（NCBI source、root TaxIDs、family/genus rank、排除过滤器、逐 rank 阈值）；结构超出表单的 profile 以只读打开，未建模的键在文档层与分节层逐字保留。

### TimeTree

- **TimeTree 查询缓存永久有效且不透明。** 缓存以 URL 为键且没有 TTL，`--refresh` 覆盖唯一一条记录，损坏的记录会成为永久硬失败且没有网络回退；`--retries`（1–10，默认 3）计的是总尝试次数，不处理 `Retry-After`，错误信息会把响应体截断到 500 字符，每次成功的真实请求之后都会固定休眠（`adapters/timetree.py`）。
- **`timetree fetch` 使用固定的请求间隔。** 1.0 秒延迟是硬编码的，没有对应开关；快照发布拒绝覆盖，单个非法 pair 会中止整次运行；`load_snapshot` 会重新校验每个原始响应，却从不复查记录下来的 `pairs_sha256`；`calibrations --out` 非原子地覆盖目标文件；查询子命令需要项目，并且即使在缓存命中时也会写 `workflow_runs` 行（`timetree.py`）。
- **校准汇总是启发式的。** 嵌套对象会被展平，第一条携带约六种 age 键别名的记录胜出；`timeline` 把任何响应体当 CSV 解析而不做格式检查；校准行会嵌入本地缓存的绝对路径与原始检索时间（`adapters/timetree.py`）。

## 执行后端

- **本地 CPU 时间可能多计。** `cpu_seconds` 是 `getrusage(RUSAGE_CHILDREN)` 的差值，包含同进程并发回收的其他子进程；Windows 没有 `resource` 模块，因此该值为 `None`（`execution.py`）。
- **Slurm 载荷只在 `cd` 成功时执行。** 生成的批处理脚本用 `cd` 的退出码守卫载荷，并总是写退出码文件（`execution.py`）。
- **`sacct` 内存解析把裸数字当字节。** Slurm 对较小的精确值不加后缀，`MaxRSS=123` 会被解析成一个极小的 MB 值（`execution.py`）。
- **SSH 超时可能留下仍在运行的远端进程。** 载荷在 `setsid --wait` 与远端 `/tmp` pidfile 下运行；超时时进程组先收 SIGTERM 再 SIGKILL，但 pidfile 缺失时错误只能提示“远端进程可能仍在运行”（`execution.py`）。
- **输出回拉是不对称的。** 远端输出不存在时被跳过（由期望输出检查报告），但本地已有内容不同的输出会抛 `ConflictError`（`execution.py`）。
- **路径改写跟随解析后的路径。** 远程执行会改写解析目标位于项目根内的参数，即使字面参数并不在根内；而字面上在根内、解析后却指向根外的路径会抛 `ValidationError`（`execution.py`）。
- **本地环境探测在进程内记忆化 300 秒。** `LocalExecutor.run` 仍在载荷之前探测，但 `capture_local` 会对共享同一启动器和工作目录的重复命令复用脱敏后的文档，因此一个批次只付一次探测开销；不同的启动器（例如另一个 Conda 环境）或不同的 `cwd` 分别缓存。条目在 `_LOCAL_CAPTURE_TTL_SECONDS`（300 秒）后过期并重新探测——与上文版本/数据库身份缓存相同的 TTL 策略，长期运行的 TUI 会话不会因此陈旧——且失败的捕获从不缓存，一次抖动不会污染整个批次（`execution.py`、`environment_capture.py`）。
- **资源采样只观察直接子进程。** 使用默认的 `conda run`/`mamba run` 启动器时，`max_rss_mb`/`avg_rss_mb` 描述的是启动器进程，而不是它启动的工具（`execution.py`）。
- **失败的环境采集仍会作为该 run 的环境入库。** 占位文档 `{"capture_schema": 1, "capture_status": "failed"}` 会成为作业的环境，随后的 `strict` 比较看到 `unavailable` 并降级为 `warn`（`execution.py`、`workflow.py`）。
- **本地 Slurm 的取消失败是静默的。** 本地 Slurm 后端在超时和中断时忽略 `scancel` 失败，而远端 Slurm 路径会记录 `cancellation_error` 并给出警告；轮询中途的 `squeue`/SSH 控制失败会直接中止而不调用 `scancel`，因此正在运行的作业可能被遗留（`execution.py`）。
- **Slurm 默认值很宽松。** `--time=24:00:00`，未配置时没有 partition 也没有内存限制，`poll_interval` 为 15 秒（有效下限 0.1 秒）（`execution.py`）。
- **`setup_commands` 在作业切换目录之前执行。** 它们先于载荷的 `cd` 运行，因此其中的相对路径行为与载荷不同（`execution.py`）。
- **SSH 后端用重命名备份保护既有远端输出。** 运行前，`remote_root` 下已存在的远端输出被重命名为 `<path>.operon-prev-<uuid>`；运行成功且新输出拉回校验通过后删除备份，失败或中断时尽力把备份恢复原位（先移除任何半成品新远端输出）。回拉仍只在成功时发生，因此 `--keep-partial` 依旧只决定失败或中断后哪些本地产物被保留——远端备份的恢复与它无关（`execution.py`）。
- **Slurm array 提交是 opt-in 且仅限 Slurm。** recipe 的 `slurm.array: true` 只在 executor 支持时才把未命中缓存的文件作为单个 job array 提交——本地 Slurm 后端始终支持，SSH 后端仅在 `scheduler: slurm` 时支持（SSH 直连的 `run_array` 为 `None`）——且至少有两个文件需要计算；其余情形一律回落为逐文件提交。记账按 task 进行（`sacct -j <array_id>_<index>`，因此 `0:9`→137 的信号折叠逐 task 生效），中断时整个 array 以一次 `scancel` 取消：已写出各自 `<run_id>.exitcode` 文件的 task 视为已完成，其余标记为 `interrupted`，下次运行重跑（`execution.py`、`tools.py`）。

## 远程镜像

- **遗留的 manifest 锁会阻塞所有加锁操作，直到人工移除。** 锁是远端原子 `mkdir`，没有过期机制；崩溃的 push 会刻意留下 `.operon-manifest.lock`，错误消息给出确切路径（`remotes.py`）。
- **manifest 发布失败会留下未索引的远端文件。** 逐文件上传成功但最终 manifest 写入失败时，远端对象留在服务器上未被索引，本地批次报告错误；下一次 push 会发现相同字节并记为 `indexed`（`remotes.py`）。
- **没有 `project_id` 的远端 manifest 会被静默认领。** 把 remote 指向空目录或无主目录会在无确认的情况下接管它（`remotes.py`）。
- **不带 `--file-id` 的 `pull` 要求条目在本地存在。** 它遍历远端 manifest，对本地数据库中不存在的任何条目抛 `ConflictError`；远端是既有 manifest 的镜像，不是向空项目独立恢复的备份（`remotes.py`）。
- **传输故障绝不等于内容判定。** 只有服务端明确返回“文件不存在”才算缺失；连接中断、socket 关闭或会话失效会抛出 `RemoteUnavailableError`，而不是报告“与清单不一致”，因此不会有任何 `file_locations` 行被翻成 `MISSING`，也不会删除任何字节。`evict` 遇到此类错误立即中止本次运行，并在下次调用时续跑（已处理的文件被跳过）；`verify` 保持原状态并报 `REMOTE_UNVERIFIED`（`remotes.py`、`files.py`）。
- **`evict` 先提交记录、后删除字节。** 先写入 `REMOTE_ONLY`，因此中断只会留下“字节仍可恢复、记录已是 remote-only”的状态；若删除失败，状态会回退并撤销指针（`remotes.py`）。
- **evict 只核查一个指定远端。** `evict` 依据单一 remote 置 `REMOTE_ONLY`；其他已配置 remote 可能没有该文件，而 `verify` 接受任意一个已验证远端即视为足够（`remotes.py`、`files.py`）。
- **`sftp://` ingest 没有完整性锚点。** 既无期望哈希也无主机密钥固定选项；正确性依赖 ingest 时对接收字节的哈希（`remotes.py`）。
- **目录产物每次检查都流式遍历整棵树。** 对目录的 `matches()` 会通过 SFTP 走遍每个文件，因此 push/pull/evict 目录产物的开销为 O(树大小)（`remotes.py`）。
- **只有 `push` 会取 manifest 锁。** `push` 是 `manifest_lock()` 的唯一调用方；`pull`、`evict`、`check_remote` 从不等待它，而对 `push` 来说等待时长受每个 remote 的 `connect_timeout`（默认 30 秒）限制（`remotes.py`）。
- **发布成功后的解锁失败会报告失败。** 新 manifest 已在锁内发布完成，因此 push 可能以退出码 2 结束，而远端已经持有新对象（`remotes.py`）。
- **`connect_timeout` 只限定 SSH 握手。** SFTP 的 put/get/流式操作以及锁自身的 SFTP 调用都没有超时，因此挂起的传输永远不会中止（`remotes.py`）。
- **远端哈希先有上限，随后回退为流式。** `sha256sum` 有硬编码的 600 秒超时，超时后改为按 1 MiB 分块通过 SFTP 流式读取（`remotes.py`）。
- **替换远端 manifest 需要服务端支持 POSIX rename。** 发布更新后的 manifest 使用 SFTP 的 `posix_rename` 扩展；没有它，第二次 push 永远无法发布（`remotes.py`）。
- **no-op 的 push 不会认领远端。** 只有在条目变化时才写远端 manifest，因此全部跳过的 push 会让它没有 `project_id`，而 `pull` 从不写远端 manifest，所以对无主远端的“认领”并不持久（`remotes.py`）。
- **上传在发布前于暂存名上完成校验。** `put` 先写入 `<目标>.operon-tmp-<uuid>` 暂存路径，当调用方给出期望身份时在暂存路径上校验 sha256 与大小，然后才重命名到位；校验失败会删除暂存字节，因此被截断的上传绝不会占据最终路径，下一次 push 只会重新上传（`remotes.py`）。

## release 与 export

- **哈希覆盖范围比文件名暗示的要小。** `checksums.sha256` 只覆盖数据文件；`provenance.json` 对元数据 TSV 只携带一个 `metadata_sha256`，`manifest.tsv` 只被哈希进数据库汇总与 `releases` 行，而 `qc_summary.tsv`、`decisions.tsv`、`profile_history.tsv`、`exclusions.tsv`、`software_versions.tsv` 与 `README.md` 的哈希不在任何地方（`release.py`）。
- **发布预检要求所有活动范围内实体都有 decision。** 缺少 decision 或评估后元数据发生变化时，在发布任何产物前拒绝 release，因此这些实体不会静默消失；排除表由该 profile 的当前 decision 构建，因此对该 profile 没有 decision 的已退役实体既不出现在 `manifest.tsv`，也不出现在 `exclusions.tsv`（`release.py`）。
- **未知的 release profile 会被拒绝。** `release` 与 `export --decision ... --profile ...` 都会在任何输出落地之前加载并校验指定的 QC profile，因此拼写错误会抛出校验错误，而不是发布零成员 release 或空包（`release.py`、`export.py`）。
- **预检与 QC 快照都只覆盖一部分。** 预检只覆盖有 manifest 文件的实体类型，且只覆盖 `_ENTITY_TABLES` 中的五种，其他实体类型跳过过期评估检查；`qc_summary.tsv` 既不按 profile 过滤也不被哈希（`release.py`）。
- **已知问题：hardlink 回退在 provenance 中不可见。** `os.link` 被拒绝后会静默回退为普通复制，而 `provenance.json` 仍记录 `hardlink`（release）或 `link_kind: hardlink`（export），目录则始终复制（`release.py`、`export.py`）。
- **中断的 release 发布会在重试时自动恢复。** 原子重命名与数据库提交之间的崩溃会留下一个没有 `releases` 行的已发布目录；下一次对同一版本的 `release` 会移除这个孤儿目录——仅当其 `provenance.json` 标明该版本——并重新构建，而占据该路径的其他任何内容仍抛 `FileExistsError`（`release.py`）。
- **release 与 export 的输出继承 staging 权限。** staging 目录以 0700 创建（原子单文件复制为 0600）后被重命名到位，因此已发布目录可能对其他用户不可读，而附属 TSV 仍是 umask 默认值（`release.py`、`export.py`、`utils.py`）。
- **选择错误的行为不对称。** 未知 `--file-id` 在 `export` 中静默选出零个文件，而 `push`/`pull`/`evict` 对同样输入会报错；同时给出 `--profile` 而不给 `--decision` 没有任何效果；export 的 `manifest.tsv` 缺少 release manifest 携带的 `compression` 列（`export.py`、`remotes.py`）。
- **export 的 run 行在发布之后才记录。** `workflow_runs` 行只在 workspace 重命名为最终目标之后写入，命令中记录的也是最终路径；若记录失败，已发布的目录会被移除，因此数据库既不会声称一个不存在的导出，也不会发布一个未记录的导出（`export.py`）。
- **已存在的空 export 目标会被整体替换。** export 始终在隐藏的兄弟 workspace 中 staging——调用方的目录永远不会充当工作区——发布时先用 `rmdir` 移除已存在的空目标（若期间混入内容则安全失败）再重命名；失败路径只会删除 staging 目录树（`export.py`）。
- **选出零个文件的 export 仍算成功。** 退出码 0，得到只有表头的 `manifest.tsv`（11 列）与 0 字节的 `checksums.sha256`，后者会被 `sha256sum -c` 以“没有可解析的校验行”拒绝（`export.py`、`schema.py`）。
- **export 符号链接存储完全解析后的目标。** `--link symlink` 指向 `source.resolve()`；移动项目会断链，不过经链接的校验仍然通过（`export.py`）。
- **release/export 失败可恢复。** 两个命令都先 staging 并在失败时清理，不会主动发布不完整目标；操作系统级崩溃仍可能留下隐藏 staging 目录，需要后续清理。
- **`--link hardlink` 会与 `raw/` 共享 inode。** 选择硬链接会让 release 与 raw 归档重新共享 inode，而不可变性的论证正是以不存在共享为前提；默认值及其理由见 [release 架构页面](../architecture/release-lifecycle.md)（`release.py`）。

## 生命周期与身份解析

- **retire 只对“直接退役”幂等。** 对已直接退役的实体再次 retire 会报告 `changed: False`，但仅通过祖先退役的实体会新增一条直接 `RETIRE` 并报告 `changed: True`——它随后会在祖先恢复后仍保持退役。restore 只能从根做起：继承退役的实体不能单独恢复，子实体的直接退役在恢复父实体后仍然保留（`lifecycle.py`）。
- **retire 纯属逻辑删除。** `physical_changes` 恒为零：不删元数据行、不动文件字节；引用图与无 `purge` 策略见 [release 架构页面](../architecture/release-lifecycle.md)（`lifecycle.py`）。
- **`reason_code` 在幂等短路之前校验。** 对已退役实体用未知代码再次 retire 会报错，而不是报告 `changed: false`（CLI 已把该参数限制为同样七个代码，因此这影响的是直接调用 API 的场景）；`restore` 会静默把调用方给出的 reason code 换成 `manual_restore`（`lifecycle.py`）。
- **裸 accession 匹配区分大小写。** 内部 ID（`ASM_000001`）大小写不敏感并统一大写，但裸 accession 必须与存储时的大小写一致；一个 accession 对应多个实体时需要 `NAMESPACE:ACCESSION`（`entity_view.py`）。
- **`show` 默认拒绝已退役实体。** 命中已退役实体时会报错，除非给出 `--include-retired`；否则也会静默隐藏退役/被取代的后代（`entity_view.py`）。

## 元数据、表格导入与 taxonomy

- **字面量 "na"、"n/a"、"null"、"none" 会变成 NULL。** 对任何字符串字段（不区分大小写）生效，除非该字段的 `allowed` 列表恰好包含该 token——名为 "None" 的 strain 或 "NA" 的 isolate 会被静默置空（`schema.py`）。
- **schema 强制转换是静默的，且不区分字段类型。** 缺失值规则对所有字段类型生效，因此数值或日期字段里字面写 `NA` 也会变成 NULL；boolean 字段中无法识别的非空值会变成 `1`；TSV 表头重名会在解析后的行里塌缩，最后一列胜出（`schema.py`）。
- **`id` 类型字段统一大写；`allowed` 值做大小写归一。** 输入不区分大小写匹配，按 schema 中的拼写存储（`schema.py`）。
- **TSV 注释是结构性的。** 首个非空白字符为 `#` 的行一律跳过，包括数据行；表头必须是第一行非注释行；除表头以空名结尾时允许缺少最后一个空列外，列数不齐即为错误。带 BOM 的文件可以处理（`schema.py`）。
- **重复键检测可能不完整。** 同批导入中一旦有更早的行出现字段错误，后续行的重复主键/唯一约束检查会被跳过，失败清单可能少于实际（`schema.py`）。
- **日期校验依赖 Python 版本。** Python 3.11+ 的 `datetime.fromisoformat` 接受 `20240115` 这类宽松格式；3.10 上同样的值会报错（`schema.py`）。
- **XLSX 只读第一个工作表**（按工作簿顺序，与名称无关），其余工作表被忽略。带小数部分的 Excel 序列日期成为 datetime（`table_import.py`）。
- **更新时空白单元格会清空现有值。** 预览 diff 会显示，但省略列保留现值；若有任何行会发生变化，`--on-conflict error` 拒绝执行。整个 apply 在一个事务中（`table_import.py`）。
- **元数据修补保留生命周期状态但会使评估新鲜度失效。** 更新既有行会记录字段级审计变化，后续 release 预检要求在该时间点之后重新 QC/evaluate（`table_import.py`、`release.py`）。
- **同文件内的前向引用无法解析。** 引用校验不能解析同一文件中较晚出现的 ID；请按 organism → sample → run/assembly 的顺序分多次导入（`table_import.py`）。
- **taxdump 快照没有灭绝数据。** taxdump 导入的 `is_extinct` 存为 NULL，因此带 `exclude_extinct: true` 的 coverage profile 面对它会直接报错，而不是悄悄削弱规则；只有 NCBI Datasets JSONL 来源携带该字段（见 [taxonomy coverage 指南](../guides/taxonomy-coverage.md)）（`taxonomy.py`）。
- **失败的 taxonomy 导入会在磁盘上留下来源副本。** 来源先复制到 `raw/metadata/ncbi_taxonomy/`，再进入导入事务；失败时事务回滚，但已复制的文件留在磁盘上且未登记（`taxonomy.py`）。
- **taxonomy 版本不可变。** 同版本不同字节再导入会抛 `ConflictError`；parent/alias 引用完整性在导入时强制，因此不完整的 taxdump 会被拒绝（`taxonomy.py`）。
- **覆盖率百分比先舍入再比较。** 数值先按四舍五入（half-up）保留 4 位小数再做闭区间 `>=` 阈值比较，因此 79.99996 % 舍入为 80.0000 %，通过 80 % 阈值（`coverage.py`）。
- **覆盖率只支持 NCBI taxonomy。** taxonomy 来源不是 `NCBI` 的观测以 `UNSUPPORTED_TAXONOMY_SOURCE` 排除；复用的缓存报告若判定为 FAIL，仍以退出码 1 呈现（见 [taxonomy coverage 指南](../guides/taxonomy-coverage.md)）（`coverage.py`）。
- **参考集不可修补。** 其身份是 `{profile_name}@{taxonomy_version}`，不含 profile 内容版本，因此修改 profile 后重新编译会抛 `ConflictError`，而不是生成新的参考集（`taxonomy.py`）。
- **覆盖率报告内嵌冻结的 profile，且无法重新生成。** 报告存储编译时的 profile 文档，从不重新读取 `config/profiles/*.yaml`；也没有 `--force`，因此删除或被篡改的 `COV_*` 报告目录会让 CLI 无法重建它（`coverage.py`）。
- **覆盖率统计的是分类广度，不是采样深度。** 无论多少不同生物命中同一参考目标，分子只计一次；metadata 范围写入空白的 file/member 证据；已退役生物被丢弃且不给排除原因码；report ID 为 `COV_{input_sha[:16]}`（`coverage.py`）。
- **排除原因先命中先得并去重。** `excluded_observation_count` 统计的是每个 (organism, reason) 一行，而不是每条观测；谱系回溯在深度 100 处停止（`coverage.py`）。
- **每次覆盖率运行都会重新校验参考集。** 参考 TSV 的 SHA-256 与大小会对照数据库行、sidecar provenance、冻结 profile 与行数校验，因此重新同步或修改参考集文件会让其所有既有报告不可用（文件本身并不会被删除）（`coverage.py`）。
- **覆盖率范围与 rank 都很窄。** release 范围覆盖率对早于冻结 `metadata_sha256` 契约的 release、以及成员类型不在硬编码接受集合内的 release 直接中止；rank 固定为 family/genus；每 rank 阈值必填、必须落在 [0,100] 且不做默认或截断；某 rank 行数为零即致命（`coverage.py`、`taxonomy.py`）。
- **`operon taxonomy import` 在事务之前修改配置。** `config/schemas.yaml`（实体类型与 file role 取值、ID 模式、版本 1.3）在导入事务开始之前就被改写，因此导入失败后配置修改仍然保留。导入来源按内容寻址（`{sha256}{suffix}`），普通非归档文件一律分类为 JSONL，重复 TaxID 会以裸 `sqlite3.IntegrityError`（退出码 1）呈现而非校验错误（`taxonomy.py`）。
- **相同字节换个版本标签再导入会复制身份。** 归档副本被复用，但仍会登记第二条 `files` 行，因此驻留与驱逐记账会为同一产物看到两个文件身份（`taxonomy.py`）。
- **名称模式与诊断转储的行为不对称。** `exclude_name_patterns` 是无锚定、区分大小写的 `re.search` 模式：编译时只对目标 rank 候选名测试，报告时则对每个谱系节点名测试，因此一个模式可以静默把某个支系同时从分子与分母中移除。`merged.dmp`/`delnodes.dmp` 是可选的，因此同一退役 TaxID 会随导入格式不同解析为 UNKNOWN、MAPPED_ALIAS 或 DELETED_TAXID；taxonomy 导入只接受本地文件——没有 URL、超时或重试选项（`taxonomy.py`、`coverage.py`）。
- **表格导入与元数据导入对 CSV 的处理不一致。** `operon table import` 用 `csv.DictReader` 读取 `.csv`，因此 `#` 行是数据，列数不齐的行会以 `unknown field(s) [None]` 呈现，而不是 TSV 那套结构性注释与字段数规则（`table_import.py`、`schema.py`）。
- **TUI Config 界面在文件消失后会重启 profile/recipe 版本号。** 删除或重命名 `config/profiles/<name>.yaml`（或从 `config/tools.yaml` 中移除某个 recipe）会让编辑器读到“版本不存在”，于是下次保存重新写入 **version 1**；内容未变时 `INSERT OR IGNORE` 会静默复用旧快照，但内容变化时会插入第二行且 `profile_version = 1`，同一 name 的版本号不再单调。该界面也无法移除 `max_hits_per_query`（清空输入会保留文件中的值并提示“unchanged”）（`tui/screens/config.py`、`tui/actions.py`）。

## NCBI Datasets 适配器

- **导入最终还是可能清空字段。** 适配器的合并检查把来源中的 `na`/`n/a`/`null`/`none` 视为非空，随后它们被归一化为 NULL，而 upsert 会写入每一列——因此既有值确实会被清成 NULL，且清空会写审计，这与“只增改不清空”的读法相矛盾（`adapters/ncbi_datasets.py`、`schema.py`）。
- **部分归一化是静默的。** 未知 sex 值变为 `unknown`；无法解析的日期和越界的经纬度变为 NULL——没有错误或原因码（`adapters/ncbi_datasets.py`）。
- **只接受 `GCA_`/`GCF_` accession**（可带版本号，统一大写）；SRA run 与其他标识体系是硬性校验错误（`adapters/ncbi_datasets.py`）。
- **部分下载失败时保留已提交批次。** 部分批次失败而其余导入成功时，run 在成功批次已提交之后抛校验错误；重跑会幂等跳过已完成批次（`adapters/ncbi_datasets.py`）。
- **pre-2.6 注释桥接是保守的。** 仅当 report 属于 assembly 的规范 accession、且该注释行未被其他 accession 认领时才复用既有注释行——这是对“GCA/GCF 成对包注释元数据相同但 GFF 字节不同”的规避（`adapters/ncbi_datasets.py`）。
- **磁盘预检保留固定 64 MiB**，且下载暂存在项目根内，从不使用 `/tmp`（`adapters/ncbi_datasets.py`）。
- **适配器自动升级旧元数据 schema。** 打开 pre-{{ metadata_schema }} 的 `config/schemas.yaml` 会就地升级，归一化格式并丢弃手写注释（与首次 REMOTE_ONLY 驱逐触发的归一化相同；见[远程存储指南](../guides/remote-storage.md)）（`adapters/ncbi_datasets.py`）。
- **不带版本号的 accession 会重复下载已归档的包。** `_canonical_accession` 保留版本号，因此当归档 assembly 为 `GCF_000001405.40` 而请求写 `GCF_000001405` 时，`_assembly_asset_role` 会推导出备用来源角色 `genome_fasta_refseq`；manifest 中没有任何行带该角色，于是该 include 被判定为缺失，每次运行都会重新下载整个包。ingest 仍是幂等的，所以这只是重复传输而非重复归档——需要复用归档时请写带版本号的 accession（`adapters/ncbi_datasets.py`）。
- **`ncbi-reconcile` 可能应用了改动却不留痕迹。** accession 行已消失的 accession-primary 更新会被静默跳过，重放的 supersession 条目也不写审计记录；两者对幂等性都是正确的，但在 `changes` 中不可见（`ncbi_reconcile.py`）。

## 备份与导入向导

- **打开数据库失败会关闭连接。** `Database.__init__` 中 DDL 或迁移抛错时（例如 `operon.sqlite` 损坏），连接会在异常离开构造函数之前关闭，只读打开路径同理——失败的打开既不会泄漏句柄，也不会残留文件锁（`database.py`）。
- **`backup create` 通过 SQLite backup API 快照。** 默认 `--scope` 为 `control`，而每个范围——包括 `control`——都会内嵌一份完整的 SQLite 快照，快照用 backup API 取得，因此即使其他连接正在写入也保持一致，且不复制 WAL 文件（`backup.py`、`cli.py`）。
- **`results` 范围不能恢复数据。** 它在 control 之上增加 QC/analysis/reports/taxonomy/releases，但排除 `raw/` 与 `standardized/`；只有 `full` 包含数据字节，且目标目录必须在项目根之外且不存在（见[备份与迁移指南](../guides/backup-migration.md)）（`backup.py`）。
- **备份对跳过内容保持沉默。** 缺失的范围目录被无提示跳过，因此备份可以在遗漏 `releases/` 的情况下成功；只有文件与符号链接会被登记和校验（空目录不可见），`verify` 从不打开 SQLite 快照，而缺少 `size_bytes`/`sha256` 的 manifest 条目会以裸 `KeyError` traceback 逃逸（`backup.py`）。
- **只有部分符号链接会被重定位。** `standardized/` 下指向项目内部的绝对符号链接会在创建备份时改写为相对目标；相对链接、树中其他位置的链接以及指向项目外部的绝对目标保留原文，而且没有 restore 命令（`backup.py`）。
- **`backup verify` 拒绝多余的意外文件**，而不只是缺失或被改动的文件（`backup.py`）。
- **向导在哈希与复制期间持有写锁。** ingest 发生在一个大事务中；失败时数据库回滚、缓冲的 JSONL 记录被丢弃、仅删除新创建的 `raw/` 目标——已存在的目标保留（`import_wizard.py`）。
- **向导会重复哈希、烧掉 ID，且依赖用户名。** 每个输入被哈希两到三次，第一遍还在写锁之外；`db.next_id()` 在提示仍打开时就预留并提交 ID，因此取消向导会永久烧掉 ID；actor 取自 `os.environ.get("USER")`，因此容器或 cron 运行会记录 NULL actor；新实体被强制为 `METADATA_VALIDATED`（`import_wizard.py`、`database.py`）。
- **向导需要 TTY 且只接受常规文件**（不支持目录产物）。若展示过的 ID 被其他进程占用，preflight 会报错，向导必须重启（`import_wizard.py`）。
- **已知问题：草稿生命周期长于数据库状态时向导会中断。** 重新填充页面时会把草稿记住的 ID 赋给选择控件；若实体在向导打开期间被退役或删除，该 `Select` 值已不在新加载的选项中，于是在 worker 回调中的 UI 线程上抛出 `InvalidSelectValueError`——页面永远不会渲染，也不会显示内联错误。同理，启动失败后导航仍然可用而 reserved-ID 映射为空，此时输入 organism 名称并按 Next 会抛出未捕获的 `KeyError`。离开 sequencing/annotation 页面时只清除启用复选框而不清除其输入，重新启用该段时旧文本会再次出现（`import_wizard.py`，TUI）。
- **demo 项目是字节确定的，且刻意包含损坏数据。** 该合成项目使用固定种子（20260816）、固定 contig 长度和 400×100 bp reads，ID 硬编码（`ANN_000002` 处留有空缺），release 为 `2026.08.demo`，项目为 `PRJ_DEMO_001`；它刻意带有损坏的注释（601 nt CDS、悬空的 mRNA parent）、恒为 `'I'` 的 read 质量和与 R1 无关的随机 DNA（而非反向互补）的 R2；其来源位于 `examples/synthetic_source`，`full` 备份会复制它（`demo.py`、`backup.py`）。
- **demo 先登记 protein 再登记 GFF3**，让注释 QC 能看到完整的注释组合；对多文件实体，文件插入顺序会影响 QC 完整性（`demo.py`）。

## 环境与关机

- **本地与远端环境文档天然不同。** 本地采集包含 Python 与 `operon` 版本，而远端探测无法报告它们，因此同一机器在 local 与 SSH 执行下可能得到不同的 `environment_id`；同一个本地后端也可能产出两份文档，因为进程内探测带有这些版本而启动器探测没有（`environment.py`、`environment_capture.py`、`execution.py`）。
- **`environment_id` 覆盖整份文档，因此主机身份仍会区分环境。** 脱敏把 hostname 换成可比较的 `sha256:` 摘要但仍保留在文档中，而指纹哈希整份文档，因此不同主机上的相同配置永远不会去重；只有 `relevance_fingerprint` 忽略 hostname。脱敏在捕获时完成（远端探针文件本身以 `umask 077` 写入），脱敏之前的文档不迁移；探针的 `home` 键是瞬态的（见[执行模型](../architecture/external-analysis.md)）（`environment.py`、`execution.py`）。
- **脱敏是浅层的。** 它只遍历顶层字符串，并在文档富化之前运行，因此 `file://` 形式的 Conda 包 URL 会把绝对 home 路径留在入库文档和导出的 `@EXPLICIT` 规格中；home 前缀模式只在 token 起始处匹配，因此 `/mnt/data/home/u/x` 会保留；`HOME=/` 会完全禁用 home 脱敏（`environment.py`、`environment_capture.py`）。
- **采集状态与标志会进入指纹。** `capture_status`、`capture_errors`、`capture_scope` 与硬件采集标志都是被哈希文档的一部分，因此一条无法解码的记录就会让原本不变的环境得到不同的 `environment_id`；http/https/file 之外的 Conda 包 URL 会被清空，从而强制 `conda.status="partial"` 并使 `export_conda` 拒绝导出（`environment_capture.py`）。
- **不支持的启动器与包元数据被原样存储。** wrapper 与容器启动器、以及未知的 conda/mamba 选项被记录为 `unsupported_launcher` 与 `capture_scope: executor_only`；每个包的完整 `depends` 列表和一行 base64 的 `conda-meta` 记录会被存储，且没有大小上限（`environment_capture.py`）。
- **环境降级只被记录，不被强制执行。** Slurm 与远端 Slurm 后端没有作业前探针，因此 `strict` 策略被降级，run details 记录 ASCII 字面量 `environment_policy_degraded: strict->warn`；当任一侧缺少子指纹时，比对记录为 `environment_compare: unavailable`，此时 `warn` 照常复用，`strict` 也降级为 `warn` 而不会使缓存失效（见[执行模型](../architecture/external-analysis.md)）（`tools.py`、`environment.py`）。
- **第二次信号跳过清理。** 第一次 SIGINT/SIGTERM 触发优雅关机（退出码 130）；清理期间的第二次信号直接 `os._exit(128+signum)`。`graceful_shutdown` 在主线程之外是 no-op（`shutdown.py`）。

## CLI 约定

- **已知问题：字段数超标的 coverage 报告会让 TUI 查看器崩溃。** `data.read_coverage_report` 原样返回行数据，因此手工编辑过的 `reports/coverage/COV_*/coverage_*.tsv` 若某行字段数多于表头，`CoveragePanel` 会在应用线程抛出 `ValueError: More values provided than there are columns.`，而不是显示内联错误（`tui/screens/coverage.py`）。
- **退出码：** 0 成功；1 为运行时/SQLite/OSError（包括 release/export 的 `FileExistsError`）以及 `qc`/`verify`/`analyze`/`push`/`pull`/`evict`/`backup verify`/`report coverage` 中任何逐条目失败；2 为所有 `OperonError`（校验、冲突、校验和、远程、配置）；130 为首次中断（伴随“进度已保存、可重跑同一命令”的提示——仅对可续跑的 NCBI 适配器与分析路径成立）；第二次信号为 `128+signum`（`cli.py`、`shutdown.py`）。映射按异常类别进行，因此处理函数中未捕获的 `sqlite3.Error`、`RuntimeError`、`OSError` 或 `ValueError` 也会以 1 返回并把消息前缀为 `error:`，即使该失败属于编程错误而非运行时状况；该链条之外的异常类型（`KeyError`、`yaml.YAMLError`）会以 traceback 逃逸并以退出码 1 结束。退出码 130 背后的可续跑中断语义见 [analyze](cli-analysis.md#analyze)。

## 推迟到 1.0 版本

若干开发期兼容垫片只为 1.0 之前创建的数据库而存在，计划在 1.0 移除（代码中标记 `TODO(1.0)`）：

- `database.py` 中 pre-1.0 迁移调用与 `_migrate_pre_1_0_schema()` 重建（遗留行保留 `legacy:` 输入身份，永不与新 QC 行去重）。
- `adapters/ncbi_datasets.py` 中针对旧 schema 的防御性字段投影与自动元数据 schema 升级。

其他开发期垫片没有 `TODO(1.0)` 标记，也不在该清单中：`adapters/ncbi_datasets.py` 中的 pre-2.6 注释桥接与遗留 `_table_exists` 守卫、首次 REMOTE_ONLY 驱逐时对 `config/schemas.yaml` 的改写（`remotes.py`），以及为本地运行保留执行后端出现之前摘要的 `location_identity` 摘要垫片（`tools.py`）。

完整清单与移除策略见[数据库兼容性](../operations/database-compatibility.md)。
