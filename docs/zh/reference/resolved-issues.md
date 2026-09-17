# 已修复问题

曾在[隐式行为、边界情形与已知问题](behaviors-and-limitations.md)中记录、且当前实现与回归测试已经覆盖的历史问题。

目前并行使用两套编号：

- **K 系列**——该列表位于 behaviors-and-limitations 页内时的原始编号。为保持历史连续性原样保留；不再发放新的 K 编号。
- **ODR 系列**——来自缺陷注册表（仓库根目录的 `defects.yml`）的记录。每条 ODR 记录都带有报告日期、受影响版本、行为复现、处置与当前状态，且每个已修复记录都有以其编号标记的回归测试支撑。流程见[缺陷跟踪](../contributor/defect-tracking.md)。

新修复的问题追加到 ODR 表。

## ODR 系列

| # | 领域 | 修复结果 |
|---|---|---|
| ODR-0001 | 导入向导 | schema 字段名不再被直接拼入 INSERT 列：向导提交边界会校验并引用每个标识符（`operon.sql.quote_identifier()`），恶意 schema 会抛出 `ValidationError` 且整个提交回滚。 |
| ODR-0002 | 数据库 API | `insert_row()`/`upsert_rows()` 及读取辅助 `table_columns()`/`export_rows()`/`export_active_rows()` 会校验并引用每个动态标识符；值仍走参数绑定，关键字列名照常可用。 |
| ODR-0003 | 表格导入 | `apply_table_import()` 在执行边界重新校验表名是否属于 `IMPORTABLE_TABLES`，并引用所有标识符；被篡改的 preview 会抛出 `ValidationError` 并回滚。 |
| ODR-0004 | 远程镜像 | 上传校验现在在 `put` 内部的暂存名上、最终重命名之前完成；被截断或损坏的上传随暂存字节一并删除，绝不会占据最终远端路径，后续 push 会重新上传而不是把该位置标为 `CORRUPT`。 |
| ODR-0005 | Release | 发布预检不再在 `config/profiles/<profile>.yaml` 缺失时提前返回：`release` 与基于 decision 的 `export` 会预先加载并校验指定的 QC profile，拼写错误会以校验错误失败，而不是发布零成员 release 或空导出包。 |
| ODR-0006 | Release | 最终重命名与 `releases` 插入之间的崩溃不再卡死该版本：重试会通过孤儿目录的 `provenance.json` 识别并移除它，然后重新构建 release；占据该路径的其他内容仍抛 `FileExistsError`。 |
| ODR-0007 | Export | export 的 `workflow_runs` 行现在只在 workspace 重命名为最终目标之后提交，命令记录的也是最终目标路径；记录失败会移除已发布的目录，因此不会再报告目标不存在的“已完成导出”。 |
| ODR-0008 | Export | export 不再把已存在的空目标目录当作工作区：所有导出都在隐藏的兄弟目录中 staging，通过 `rmdir` + 原子重命名发布（目标混入内容时安全失败），失败路径只删除 staging 目录树——调用方目录的子项不再会被删除。 |
| ODR-0009 | Ingest | 幂等重 ingest 不再无条件改写 `files.status`：`STANDARDIZED` 文件保持其状态，真实的状态迁移改走 `set_file_status` 并写入 `changes` 审计行。 |
| ODR-0010 | Standardize | `standardize` 不再直接写入 `STANDARDIZED`：文件状态走 `set_file_status`、实体迁移走 `set_state`，二者都记入 `changes`；非法迁移（例如对已 `RELEASED` 的实体重新标准化）会被拒绝，且 `CHECKSUM_FAILED` 新增了指向 `STANDARDIZED` 的合法恢复边，供重新校验通过的字节使用。 |
| ODR-0011 | QC / decisions | 重跑 `qc` 或 `evaluate` 不再把 `ACCEPTED`/`RELEASED`（或 `REVIEW`/`REJECTED`）实体降级：批量状态写入改走 `set_state_guarded`，新鲜的 QC 证据与 decision 行照常记录，但生命周期状态只能由显式的 `curate` 或强制 `set-state` 改变。 |
| ODR-0012 | Database | 可写打开时的派生视图重建现在运行在单个 immediate 事务内（使用普通 `execute`，因为 `executescript` 会隐式提交），两个进程不会再在另一个连接的 DROP 与 CREATE 之间相撞而报 `view ... already exists`。 |
| ODR-0013 | Files / QC | 目录产物现在用与入库时相同的确定性树哈希校验：`verify_local_file_identity` 按 manifest 中的 format 分派，文件↔目录类型翻转按 missing 报告，stat 指纹缓存对目录旁路（目录自身的 mtime 在任何成员变动时都会改变）。内置 QC 的 checksum 阶段与 `fanout` 的来源校验现在能通过完好的目录树。 |
| ODR-0014 | TimeTree | 快照加载与标定输入现在以 `ValidationError` 失败，不再抛裸异常：`snapshot.json` 缺失/畸形/结构不全、原始响应不可读、taxa/constraints 表中的非整数 taxon ID、以及不可读或畸形的进化树文件，都会给出有上下文的 `error:` 消息（退出码 2），而不是 traceback。 |
| ODR-0015 | Shutdown | 各中断清理点现在会在记录收尾完成后调用 `shutdown.cleanup_completed()`，重新启用优雅处理：清理完成之后才到达的信号（批量回退、exit-130 报告阶段）会抛出新的 `ShutdownRequested`，而不是强制退出一个已无清理事项的进程。`os._exit(128+signum)` 逃生舱仍保留给清理确实仍在进行时到达的第二次信号。 |
| ODR-0016 | Analysis tools | 工具版本与数据库身份缓存现在带 300 秒 TTL，不再是进程生命周期缓存：一个批次仍只付一次探测开销，但长期运行的进程（TUI）会在过期后重新探测，因此就地升级的工具或在原路径替换的参考数据库会被察觉，而不是继续按陈旧身份规划。 |

## K 系列（历史）

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
| K11 | 分类 | 缺失字段在任何形态下都不满足条件——包括 `not:` 取反形态和作为 `any:` 组的析取项；`between` 遇到非数值操作数时抛出带字段与值上下文的校验错误，而不是裸 `ValueError`。 |
| K12 | 分类 | `classify-sequences` 不再隐瞒被忽略的内容：被取代（非最新）的 completed 作业会计入 `ignored_completed_jobs` 并给出警告，因不在 `sequences` 注册表而被跳过的文件也会显式计数。 |
| K13 | fan-out | `fanout --dry-run` 现在执行完整预检——源校验和与注册表新鲜度、单元 identity 计算、冲突/占用检查——并与真实运行一样抛 `ConflictError`，同时仍不写任何文件、不开 run 行；每个计划单元标注 `would_create`/`would_reuse`。单元 seqid 也会在生成 FASTA 前规范化为排序序，因此重排指派 TSV 不再改变单元字节。 |
| K14 | fan-out | 中断（Ctrl+C）现在会把 fan-out 的 workflow run 落为 `interrupted`（退出码 130），不再留下 `running` 行；本次运行创建的目标仍会被删除。 |
| K15 | recipe | `file_role_prefix` 按 `:` 边界匹配：`sub` 选中精确的 `sub` 与所有 `sub:*`，不再误捕 `sub2:*`。 |
| K16 | 执行后端 | 被信号杀死的 Slurm 作业不再被记为成功：`sacct` 回退会把 `exit:signal` 中非零的信号分量合成 shell 风格退出码（OOM 终止的 `0:9` 变为 137），并在 run 的 details 中以 `slurm_exit_signal` 记录该信号；退出码/记账重试也从 5 次 × 1 秒提高到 10 次 × 2 秒（`_SLURM_EXIT_CODE_RETRIES`/`_SLURM_EXIT_CODE_RETRY_SECONDS`）。 |
| K17 | 执行后端 | SSH 后端不再在运行前删除远端输出：既有远端输出先重命名为 `<path>.operon-prev-<uuid>` 备份，运行成功且新输出校验通过后删除备份，失败或中断时尽力把备份恢复原位——失败运行不会再毁掉此前的远端输出。 |
| K18 | 外部分析 | 陈旧 `RUNNING` 清扫现在按当前 analysis 限定范围：非 dry 的 `analyze` 只把本 analysis 的 `RUNNING` 行标为 `interrupted`，并发运行的其他 analysis 的活作业不再被波及（`tools.py`，`_sweep_stale_running_jobs`）。 |
