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
| ODR-0017 | Tests | 版本单一来源测试不再与并发的 sdist 构建竞争：它在持有机器级 sdist 构建锁的同时通过 `importlib.reload()` 重读 `operon.__version__`，因此在兄弟 worker 就地重写 `OperonDBS.egg-info` 期间导入 `operon` 的 xdist worker 不会把陈旧或缺失的版本冻结进断言。 |
| ODR-0018 | Analysis tools | 在 Slurm array 路径上由分析 `progress_callback` 抛出的取消（TUI 的 `AnalysisCancelled`）不再被吞成逐文件失败：回调异常现在会中止整个批次——已写出 exit-code 文件的 task 按执行器中断路径完全相同的方式落为 completed/failed，其余计划标记为 `interrupted`，原始异常继续向调用方传播。KeyboardInterrupt 子类仍走既有的外层中断路径。 |
| ODR-0019 | TUI | 过滤行不再把控件挤出容器：每个面板与模态的过滤行（`jobs-filters`、`hits-filters`、`hits-filters-2`、`hits-export`）都补了宽度规则——弹性搜索框占 `1fr`，窄下拉与 limit 字段给固定宽度。回归测试挂载受影响的界面与模态，断言每个控件完全落在自己那一行内且宽度至少 8 列。 |
| ODR-0020 | TUI | 分析作业对话框不再把表格压成一行、也不再让两个窗格重叠：改用固定高度的盒子（`#modal-box.jobs`，80%）与双窗格主体——表格占 `1fr` 并在内部滚动，详情移到右列。回归测试断言两个窗格都在盒子内、互不重叠且保持可用尺寸。 |
| ODR-0021 | Tests | 复制出的项目不再继承源数据库的只读状态：`tests/helpers.copy_project_tree()` 会丢弃所有 `*.sqlite-shm`（SQLite 下次打开时据日志重建共享内存索引），并恢复复制出的数据库、日志与 journal 文件的写权限。`test_data_layer_never_writes()` 改为在 demo 项目的私有副本上运行，而不是共享的模块级 fixture；回归测试断言只读会话之后复制出的项目能以可写方式打开。 |
| ODR-0022 | TUI | worker 结果不再可能在模态关闭之后落地：`operon/tui/screens/common.py` 新增 `WorkerResults`，其 `post_to_ui()` 把结果交给 UI 线程，`apply_from_worker()` 在控件已销毁时丢弃结果。`WriteModal` 与 `Panel` 继承它，`AnalysisJobsModal`、`RunDetailScreen`、`SequenceLabelsModal`、`EnvironmentsModal`、`AnalysisHitsModal`、`ImportWizardScreen` 亦然，且 `operon/tui/screens` 中所有 worker 调用点都改走 `post_to_ui()`——同时替换了手写的 `is_running`/`RuntimeError` 块。回归测试固定该守卫及其前提（无守卫的调用仍会抛错）；竞态本身依赖时序，测试不固定交错顺序。 |
| ODR-0023 | TUI | 配置编辑器不再读取尚未挂载完延迟行的表单：`MountTracked`（`operon/tui/screens/common.py`）是一个稍后自行填充并报告 `mounts_settled` 的容器，每个编辑器行（`RuleRow`、`ConditionRow`、`ConditionEditor`、`BestByRow`、`SourceRow`、`ClassificationRuleRow`）都是 `ComposedRows`，在自己的 `on_mount` 里置位 `form_ready`，`ConfigPanel._form_mounting()` 依据这两路信号作答——在表单仍在构建时落地的保存会以 `FORM_MOUNTING_MESSAGE` 拒绝，而不是去遍历半成品树。`FittingSelect` 通过逐轮重试 `_setup_options_renderables`/`_init_selected_option` 屏蔽 Textual 自身的挂载期查询。测试在直接读表单前先等 `_await_form_ready`/`_await_rows`，`_click` 容忍 `OutOfBounds` 并等待残留的 `-active` 按压效果消退（ODR-0024），两个回归测试固定了保存拒绝与行就绪闩。顺带修掉了 `SourceRow.on_mount` 中两个 lambda 共享一个 `view` 的晚绑定缺陷。 |
| ODR-0024 | Tests | 按钮上残留的按压效果不再吞掉第二次点击：`tests/unit/test_tui_config.py::_click` 会在再次点击前等待 `-active` 效果消退——与 `test_tui_derived_ops.py::_click` 自派生产物对话框以来一直使用的步骤一致——recipe 测试也改为等待它预期的结果（先出现内联冲突错误，再出现模态），而不是单次 `pilot.pause()`。该测试带 `@pytest.mark.bug("ODR-0024")` 标记。 |
| ODR-0025 | CI | 发布工作流不再构建拆分之前的文档目录：它与 CI 的 docs job 一样构建 `docs/en` 与 `docs/zh`，且 `tests/unit/test_docs_projects.py` 新增守卫，会读取工作流文件（内联 `run:` 与块形式都覆盖）并在某条 `sphinx-build` 未同时指名两个语言树时失败。 |
| ODR-0026 | TUI | `Select` 未完成挂载时，条件运算符不再被组合成空白：`FittingSelect` 在延迟之前先采用构建它时的值（`_adopt_value_before_overlay`）并发布 `options_ready`，`ConfigPanel._form_mounting` 现在要求表单中每个 `Select` 都给出该信号，因此读取方会等待，而不是组合出空白运算符。`BestByRow` 通过 `actions.coerce_scalar` 解析 rank 值与默认值，整数保持为整数。 |
| ODR-0027 | Tests | 两个配置界面测试不再早一个消息循环轮次读取 UI：等待都不再是固定次数的 `pilot.pause()`——滚动测试先等 `_await_form_ready`，再等编辑器自身增高；通知断言改走新增的 `_await_notification` 辅助。该文件中所有同类位置（六处通知断言）一并改掉，因为碰巧通过的那几处正是下一次慢速运行会挑中的。 |
| ODR-0028 | 分析 | `--dry-run` 不再预览一个无法执行的作业：本地参考库存在性检查在 dry-run 下同样执行，远端 `reference` 库则经后端做一次只读的 provisioning stat（`mutable_cache` 由运行自身创建，此时无物可校验），因此 `analyze --dry-run` 会给出与真实运行相同的 `reference database not found: <path>; edit config/tools.yaml` 或 `remote reference database is not provisioned at <path>` 错误，且命令以非零退出。后端版本探测被有意保持为未探测——它需要执行命令——`reference/cli-analysis.md` 中的承诺已相应改写；`guides/remote-execution.md` 与 `reference/recipe-fields.md` 的坑位描述同步更新。 |
| ODR-0029 | Tests | run 详情测试不再把空的 worker 集合当作界面加载完成：`#run-detail` 的每次读取都改为等待它要断言的内容。`test_tui_writes.py` 新增 `_detail_text()`/`_await_detail_text()`，其两处 run-external 位置只在其断言的标记出现后才读取详情文本，`test_tui.py` 的 runs 表格位置也用了同一对辅助的副本。该窗口不仅被修复也被守卫：`test_no_tui_test_reads_run_detail_straight_after_settling` 扫描测试模块中对 `#run-detail` 的裸 `_static_text(...query_one(...))` 赋值并在此类出现时失败；`test_run_detail_read_waits_for_the_loaded_content` 则压住界面加载，让旧写法在占位内容仍在时“通过”，正是那次抖动落入的形态。 |
| ODR-0030 | TUI | 表单延迟替换期间，旧编辑器行不再虚假满足就绪条件：`MountTracked` 在挂载下一代之前先退掉正在被替换的那一代，并给每次替换打上世代标记，因此在其移除尚未完成时被取代的替换会丢弃自己的挂载，而不是多加一代。`mounts_settled` 在等待的替换落地前一直为 false（`_EMPTIED` 哨兵让“有内容→空”的替换也维持等待状态），`_replacing()` 在上一代仍在树中时保持未就绪。回归测试在一个轮次内连续触发两次重建后保存，断言只有一代行、且文档与文件一致。 |
| ODR-0031 | TUI | 详情面板不再应用与当前选择已经不匹配的 worker 结果：`WorkerResults` 给每次请求打上 key，面板把该 key 交给 worker（线程侧 `post_to_ui(..., key=...)`，面板侧 `apply_from_worker(..., key=...)`），因此在更新的请求打上 key 之后才到达的载荷会被丢弃。没有 key 的结果与从未打过 key 的控件都按当前结果处理，因此对未接入的 worker 完全惰性：`Panel.begin_request()` 与 `is_current_result()` 是它的两半，`FilesPanel`/`EntitiesPanel` 打 `file_id` 或实体 key，`CoveragePanel` 打报告 id，`PublishPanel` 打 release profile 与导出过滤元组。 |
| ODR-0032 | TUI | 首次面板渲染被丢弃不再让应用停在启动画面：`Panel._apply` 仍会丢弃控件已销毁的结果，但被丢弃的结果不再在“已告知应用本面板首次加载结束”之前返回——首次加载状态两条路径都会置位，因此渲染失败会被上报，而不是把启动画面卡住。`OperonApp._finish_startup` 也不再等待从不回报的面板：超过 `startup_deadline` 秒后，它展示已加载内容、记录未回报的面板、收起启动画面并提示用户按 `r` 重试。该截止时间通过推进应用时钟来覆盖（冻结的时钟无法体现已流逝时间），面板级测试则参数化了两种丢弃形态：`MountError` 与 `NoMatches`。 |
| ODR-0033 | Tests | 环境采集测试不再断言采集在 `$HOME` 下会脱敏的原始路径：三处断言改为与采集自身的路径形态比较，通过新增的 `_captured()` 辅助用 `environment._redact_home` 与 `environment._local_home()` 处理期望值，因此无论 scratch 目录在哪都成立；`TMPDIR` 位于 `$HOME` 之内的运行现在是矩阵会主动覆盖的情形，而不是会踩中的陷阱。这三个测试带 `@pytest.mark.bug("ODR-0033")` 标记。 |
| ODR-0034 | TUI | 分类编辑器的嵌套限制不再由调用方无法到达的分支来保证：depth 参数被移除，`_condition_within_depth(condition)` 对组中组、组的 `not:`、非列表的 `any:` 组以及非字典条件一律返回 `False`——正是 `classification_form_supported` 会拒绝的那组形态。`_rebuild` 的叶子回退保留（作为编辑器自身最后一道防线）并在注释中说明。由 `test_classification_form_refuses_what_it_cannot_represent` 固定，其用例逐一点名每种被拒形态与只读提示出现的原因。 |
| ODR-0035 | TUI | 模式切换替换主体期间，分类编辑器不再抛异常：`editor_document()` 先询问主体自身的 `MountTracked.mounts_settled`（ODR-0023 引入的就绪答案），在行尚未就位时组合由 `_rebuild` 在替换主体前记录的 `ConditionEditor._seeded_document`。两种模式都以种子条件作答，而不再抛异常（leaf/`not:`）或报告空组（`any:`），因此落在这个窗口里的保存会保留用户正在编辑的条件。由测试固定：该测试钩住主体自身的挂载，使读取恰好发生在窗口之内，而不是碰巧被某次轮询撞上。 |
| ODR-0036 | TUI | 在删除嵌套条件行过程中落地的组合不再读取没有输入控件的行：`editor_document()` 只组合 `.condition-field` 仍在树中的行（`ConditionEditor._composable_rows`），当 leaf/`not:` 主体没有这样的行时回退到 `_seeded_document`，因此 `any:` 分支报告剩余的行，叶子分支则保留条件而不是抛异常。由测试固定：该测试从按下 ✕ 起逐轮读取，直到该行消失。 |
| ODR-0037 | Tests | 关闭流程测试不再在 shell 写入 pid 之前读取孙进程 pid 文件：新增的 `_grandchild_pid()` 在两处读取点都按同样的五秒预算轮询到内容可解析为止——中断前的等待保持宽容（没有 pid 也可继续），运行后的读取则是严格的。该测试带 `@pytest.mark.bug("ODR-0037")` 标记。 |
| ODR-0038 | TUI | `Select` 自身的 `on_mount` 不再与受保护的覆盖方法并行裸跑：`FittingSelect` 覆盖了 Textual 处理器经实例调用的两个方法（`_setup_options_renderables` 与 `_init_selected_option`），并在其中吸收 overlay 的 `NoMatches`，因此基类处理器不会抛错。就绪状态不再能从该异常推断，`_on_mount` 与重试改为先询问树（`_overlay_present`）再置位 `options_ready`——对读取方而言 ODR-0023/ODR-0026 的契约不变。 |
| ODR-0039 | TUI | `Select` 挂载重试耗尽不再让 `options_ready` 永久为 `False`、使表单闸门无限等待：`FittingSelect` 新增 `options_gave_up`（与 `options_ready` 并列的第二信号），并在重试预算耗尽时打一条点名控件的警告。`ConfigPanel` 通过 `_blocked_save_message` 回应该被阻断的保存：一旦有控件放弃，它会点名卡住的控件并提示重载 profile，否则仍沿用“仍在加载”的措辞。 |
| ODR-0040 | QC / 报告 | 会被电子表格当作公式执行的文本单元格现在加前导撇号转义——即以 `=`、`+`、`-`、`@`、TAB 或 CR 开头的值。覆盖两个对齐后端产出的 `sequence_qc.tsv`（逐字节 parity 不变）以及所有 `write_tsv` 消费者（release 与 export 的 manifest、report TSV、TimeTree 候选表）。非字符串单元格保持原有字节，已转义的值不会二次转义，provenance 哈希按转义后的字节计算，因此再读回 release 仍然一致。 |
| ODR-0041 | TUI | 配置界面的 History 按钮不再以仅 QC 使用的 profile 字段为闸门：`_open_profile_history()` 通过 `_active_profile_name()` 解析 profile 名——分类编辑器在前台时给出分类编辑器自己的名字，否则给出 `current_profile`——因此 History 打开的是用户当前正在编辑的那个 profile 的快照历史。由测试固定：从全新界面（从未选择过 qc profile）点击 `#classification-history`，断言模态标题点名分类 profile，且陈旧的 qc 选择不会渗入其中。 |
| ODR-0043 | TUI | 运行中真实点击 Cancel 不再把 `WriteModal` 子类连同其 worker 一并关掉：Textual 的 `MessagePump` 会沿整条 MRO 调用每个 `on_button_pressed`，因此 `QcModal`、`AnalyzeModal`、`RunExternalModal`、`TaxonomyImportModal`、`CompileReferenceSetModal` 的“运行中取消”分支现在先调用 `event.prevent_default()`（沿用 `NcbiDatasetsModal` 的做法），在 `WriteModal.on_button_pressed` 之前终止这次分发。回归测试在桩动作阻塞时通过 `Button.press()` 派发真实 `Button.Pressed`，断言模态保持打开且运行仍能完成。 |
| ODR-0044 | Export / 报告 | `write_tsv` 改为自己决定单元格引号，不再委托 `csv`：含 TAB、CR、LF 或 `"` 的单元格加引号并双写内部引号，其余原样写出。CPython 3.11 改变了 csv 对 CR/LF 单元格的引号规则，导致 3.10 上同一行写出不同字节——按 provenance 哈希的产物（release manifest、export 身份）因此随解释器漂移。现在 3.10–3.15 字节一致，且与此前 3.11+ 的输出完全相同。 |
| ODR-0045 | TUI | 面板加载现在带世代戳：`reload()` 把在 UI 线程打上的世代交给 worker，被更新加载取代的 payload 会丢弃而不是渲染。此前已经在读的线程可能晚于新加载落地，把刚输入的过滤条件移除的行又恢复出来——即 macOS/Python 3.15 CI 上实体过滤始终未生效的形态。 |

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
