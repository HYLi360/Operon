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
