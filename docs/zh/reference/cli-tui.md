# 终端界面（`operon tui`）

`operon tui` 打开项目的交互式终端用户界面（TUI）。读取操作使用短生命周期的只读
数据库连接，因此在 CLI 命令对同一项目执行写操作时，保持 TUI 打开也是安全的。
写操作调用与 CLI **完全相同的核心函数**，因此审计行（`changes`）、
workflow 溯源记录（`workflow_runs`）及语义与等价命令完全一致。每个写操作遵循
相同的流程：表单或计划预览 → 对话框中显示等价的 CLI 命令 → 显式确认（Confirm）
→ 在后台 worker 中执行变更 → 成功通知并刷新面板，或显示内联错误信息（对话框
保持打开）。

学名以斜体显示，其中独立的分类等级缩写 `subsp.`、`ssp.`、`var.`、
`subvar.`、`f.` 和 `subf.` 使用正体。原始文本和空白保持不变，其他部分保留斜体。

## 安装

TUI 基于 [Textual](https://textual.textualize.io/)，包含在标准 `OperonDBS` 安装中。

## 用法

```bash
operon [--project PATH] tui
```

项目通过全局 `--project` 选项选择，与其他命令完全一致。

## 启动画面

启动时，`operon` 显示夜湖插画，同时在后台加载八个面板。画面从首次绘制起至少
显示 2 秒，且会等待所有面板的首次加载完成或返回错误。失败的加载会在相应
面板显示错误，可在该面板按 `r` 重试。启动期间暂停导航，仍可按 `ctrl+q` 退出。

左下角的加载状态和右下角的已安装程序版本使用实时英文文本，黑底白字。
原图为 1024 × 768 像素。启动画面自动选择显示方式：

- **纯文本：** Linux TTY、未知或 16 色终端，以及设置了 `NO_COLOR` 的环境，
  显示居中的圆角方形块状 OPERON 字标与加宽字距的副标题。使用基础 ANSI 青色
  与白色保留原有配色，不依赖 RGB 转义序列；`NO_COLOR` 使用单色。非 UTF-8
  输出使用 ASCII `#` 组字；窗口太小时改为普通文本。
- **彩色块：** 终端声明支持 256 色或真彩色时，保留夜湖半块字符画，副标题使用
  可读的终端文本。
- **Kitty：** `TERM=xterm-kitty` 且未经过 tmux/screen 时，通过
  [Kitty 图片协议](https://sw.kovidgoyal.net/kitty/graphics-protocol/)直接显示 PNG。
  图片以 base64 分块通过终端连接传输，因此 SSH 无需共享文件系统或远程安装
  `kitten`。图片适配底栏上方区域，缩放窗口时重新定位；关闭启动画面、退出或被
  其他弹窗覆盖时清理图片，且只删除本启动画面的图片。

SSH 本身不决定颜色或图片能力，远程终端环境变量应正确描述本地模拟器。
Kitty 自动选择基于 `TERM`，不会主动探测协议。tmux/screen 内自动使用字符显示。
终端忽略图片请求时，仍可看到下层的字符画；图片资源或输出出错时回退为纯文本。
不新增运行时依赖。图片适配假定字符格高度是宽度的两倍。

可通过 `OPERON_SPLASH=auto|text|blocks|kitty` 手动选择，例如：

```bash
OPERON_SPLASH=text operon --project PATH tui
OPERON_SPLASH=kitty operon --project PATH tui
```

仅在终端及中间转发层支持协议时强制选择 `kitty`；此显式选择也会覆盖
`NO_COLOR`。未知取值按自动模式处理。最短显示时间与加载完成条件保持不变。

[用户配置](cli-config.md)中的 `ui.splash` 可在不设置环境变量的情况下给出同样的
选择；两者同时存在时 `OPERON_SPLASH` 仍然优先。

## 界面

左侧边栏（或数字键）在九个界面之间切换：

| 界面 | 按键 | 内容 |
|------|------|------|
| Home | `1` | 项目标识、各类实体计数、文件数量与总大小、判定分布、最新 release、最近 10 条 workflow 运行记录，以及"Attention needed"（需要关注）小节（failed/interrupted 运行、当前判定为 REVIEW/FAIL 的实体、状态不健康的文件）。*NCBI Datasets import*、*Import dataset* 与 *Import table* 按钮分别打开 NCBI Datasets 对话框（见下文）、导入向导与受控 metadata 表格对话框（见下文）；向导也可用 `i` 键打开。*Create backup* 与 *Verify backup* 按钮分别打开备份对话框（见下文）。 |
| Entities | `2` | 层级树（organisms → samples → runs 与 assemblies → annotations），并显示每个实体的当前状态。选中节点时显示其元数据字段、accession、状态、关联文件，以及最新的内置 QC 与外部分析（如 BUSCO/QUAST）指标。已逻辑退休的实体默认显示（暗淡加删除线）；按 `t` 可隐藏它们。按 `x` 打开生命周期对话框（见下文）。按 `a` 添加一条元数据记录（见下文）。 |
| Files | `3` | 可过滤的文件清单表格（子串过滤加状态选择器）。移动光标即可查看完整文件记录、其 `file_locations` 驻留列表，以及 *Sequence labels* 小节（`classify-sequences` 的结果按 label 与 profile 聚合）。状态带有颜色标记：已验证为绿色，`REMOTE_ONLY` 为蓝色，`MISSING`/`CHECKSUM_FAILED` 为红色。按 `i`/`v`/`q`/`l` 分别进行归档、校验、QC 与 label 浏览器，按 `e`/`s`/`a`/`f` 分别进行 extract-domains、select-sequences、adopt 与 fanout（见下文）。 |
| Tasks | `4` | Workflow 运行监控（指处理任务，而非测序 run），数据源与 `operon workflow list` 使用相同的只读查询。一行紧凑过滤条放 状态/step/entity/数量上限；CLI 其余过滤——`--from`/`--to`（ISO-8601；非法值或 `--from` ≥ `--to` 在对话框内内联报错）、`--run-id`、`--parent-run-id`、`--tool`、`--executor`、`--offset` 与 `--oldest-first`——收在 *More…* 按钮后的进阶过滤对话框里，按钮上显示当前生效的过滤项数（*Clear* 清空；`--resumes-run-id` 与机器格式仍只在 CLI）。表格在进入时加载、按 `r` 手动刷新（无后台轮询），光标与滚动位置在刷新间保持不变。在某一行按 `enter` 查看完整运行记录（与 `operon workflow show` 相同的小节）；按 `esc` 返回。对*运行中*的 run，详情屏的 *Follow logs* 每秒追加一次本地 `logs/<run_id>.stdout.log`/`.stderr.log` 的增量，直到 run 离开 `running` 并报告最终状态——它只观察、从不取消（SSH 后端的日志在结束时才拉回，因此在此之前没有内容）。*Analysis jobs* 按钮打开只读的 `analysis_jobs` 浏览器（analysis/status/limit 过滤，并显示选中行的完整错误与产物路径），其中也包括在 job array 中被中断的任务——这类行永远不会有 `workflow_runs` 行。*Environments* 按钮浏览已捕获的执行环境（与 `operon environments list` 相同的列表），并以只读方式渲染 *View JSON*、*Export explicit* 与 *Export yaml*——把 conda spec 落盘仍是 CLI 重定向（缺少包清单等错误内联显示）。*New analysis* 与 *Run external* 按钮分别打开分析对话框与外部命令对话框；*Analysis hits* 按钮打开比对命中浏览器（`report analysis --hits` 的列与过滤，*Export* 写出的文件与 CLI `--out` 逐字节一致）（见下文）。 |
| Decisions | `5` | 来自 `current_decisions` 视图的当前判定（有效判定 = 存在人工裁定时的裁定值，标记 `✎curated`），支持 profile/判定/文本过滤。按 `e` 评估，按 `c` 裁定选中行（见下文）。 |
| Config | `6` | 项目配置文件的结构化、基于控件的编辑器（不提供自由文本 YAML 编辑）：**QC Profiles**（含 `kind: qc` 与 `kind: sequence_classification` 两类 profile，各自独立的表单）与 **Tools & Recipes**。详见下文。 |
| Publish | `7` | 不可变 release 构建器与选择性导出构建器（两个标签页），写入前均提供只读预览。详见下文。 |
| Coverage | `8` | 已导入的 NCBI Taxonomy 快照、已编译的 reference set、覆盖度报告生成（`operon report coverage`），以及已有 `reports/coverage/COV_*` 报告的浏览器。详见下文。 |
| Remotes | `9` | 列出 `project.yaml` 中 `remotes:` 配置的镜像及其解析后的端点（名称、类型、地址、根路径）——加载时**不建立任何连接**——并提供按需的 *Check connectivity* 按钮：逐镜像调用与 CLI 相同的核心 `check_remote`（`files`/`status`/`error` 列；失败的镜像以警告通知呈现，对应 CLI 的退出码 1）。进入界面、刷新或写入后的重载都不会触发连通性检查。*File locations* 小节是 `operon locations` 背后的项目级驻留列表（每个 file/remote 组合一行，列与排序同 CLI；最多显示前 2000 行并给出提示，过滤映射重复的 `--file-id`）。两个小节都回显各自的等价命令；*Push…* / *Pull…* 按钮打开传输对话框（见下文）。 |

全局按键：`1`–`9` 切换界面，`r` 刷新当前界面，`i` 打开数据集导入向导
（焦点位于 Files 界面时除外，此时 `i` 为归档 ingest），`?` 显示按键帮助，
`ctrl+q` 退出。

## 写操作

每个对话框都会显示等价的 CLI 命令，并随表单输入实时同步；记录的审计与溯源
信息与该命令完全一致。

| 按键 | 界面 | 操作 | 等价命令 |
|------|------|------|----------|
| `e` | Decisions | 在选定的 profile 下评估全部实体或选中行所属实体；完成后提示"N decisions evaluated"。 | `operon evaluate --profile … [--entity-type … --entity-id …]` |
| `c` | Decisions | 裁定选中的判定：选择新判定、reviewer（预填 `$USER`）、必填的 reason、可选的 evidence。校验错误（实体已退休、无自动判定）以内联方式显示，对话框不关闭。 | `operon curate --entity-type … --entity-id … --profile … --decision … --reviewer … --reason …` |
| `l` | Files | 浏览 `sequence_labels`（`classify-sequences` 的产物）：项目级 label × 序列数/文件数摘要，加上选中文件的 label 行（500 行窗口）。label 没有对应的 CLI 读取命令——本视图即 `classify-sequences` 的读取侧，`report analysis --hits` 展示其背后的比对命中。 | —（无 CLI 对应命令） |
| `e` | Files | 从选中的 FASTA 提取比对 query 区域（*Extract domains*）：区域来源是 Select（`--analysis` 命中或 `--regions-tsv` 路径，同一时刻只有一个可用），另有区域模式（best hit / all regions）、flank、最小长度、`--subject-like`/`--evalue-max`（仅 analysis 模式）、输出路径与可选 manifest。输出的 FASTA **不会**被注册：运行结束后 adopt 对话框会预填输出路径、源文件（作为 `derived_from`）与源实体。 | `operon extract-domains --file-id … (--analysis … \| --regions-tsv …) [--flank …] [--min-length …] [--best-only\|--all-regions] [--subject-like …] [--evalue-max …] --out … [--manifest …]` |
| `s` | Files | 按分析命中筛选选中 FASTA 的子集（*Select sequences*）：两个 analysis 名（OR）、`--subject-like`、`--evalue-max`、`--min-span`、`--hit-type`、require-hit / require-no-hit Select、可选实体过滤、输出路径与可选 manifest。至少要有一个命中条件（与核心同一条规则）；输出未注册，成功后与 `e` 一样链入 adopt。 | `operon select-sequences --file-id … [--analysis … …] [--subject-like …] [--evalue-max …] [--min-span …] [--hit-type …] [--require-hit\|--require-no-hit] [--entity-type … --entity-id …] --out … [--manifest …]` |
| `a` | Files | 注册派生产物（*Adopt*）：*single item* 表单（path、entity、role、format/compression 留空自动检测、逗号分隔的 `derived_from` file id、可选 workflow run id 与 actor）或 *manifest* 表单——其 JSON/TSV 先由 *Preview* 按钮解析，解析出的条目数显示前 Confirm 保持禁用。同一 entity+role 且字节相同会复用已有注册（通知里给出 reused 数量）；字节不同则抛冲突、内联显示且不写入任何内容。 | `operon adopt (--file … --entity-type … --entity-id … --role … --derived-from … … \| --from-manifest …) [--format …] [--compression …] [--workflow-run-id …] [--actor …]` |
| `f` | Files | 把已注册 FASTA 拆分为按 unit 的文件（*Fan out units*）：assignments file id、逗号分隔的 source file id、所属实体、role 前缀、unit/seqid 列名与可选 parent run id。*Dry run (preflight)* 按钮执行真正的预检——源校验和与注册表新鲜度、unit 身份、冲突/占用检查——并把每个计划中的 unit 标为 `would_create`/`would_reuse`；只有干净的 dry run 之后 Confirm 才可用（输入一旦改动又会被禁用），真跑完成后报告 created/reused 数量。 | `operon fanout --assignments-file … --source-file … … --entity-type … --entity-id … --role-prefix … [--unit-column …] [--seqid-column …] [--parent-run-id …] [--actor …] [--dry-run]` |
| — | Tasks | 浏览比对命中（即 `report analysis --hits` 视图）：analysis/entity/query/subject/evalue-max/limit 过滤，使用相同的只读查询与列顺序。*Export* 用 CLI 自己的渲染器写出当前查询结果，因此 text/tsv/json 文件与 CLI 逐字节一致；不带 `--hits` 的作业汇总视图仍只在 CLI。 | `operon report analysis --hits [--analysis … --entity-type … --entity-id … --query-id … --subject-id … --evalue-max … --limit … --include-retired --format {text,tsv,json} --out PATH]` |
| `x` | Entities | 退休（对已退休实体则为恢复）选中实体。对话框先加载只读影响计划（受影响实体/文件/引用，物理变更——逻辑退休恒为零），计划显示无变化时阻止 Confirm；RETIRE 必须提供 reason code。 | `operon retire\|restore <id> --reason … [--reason-code …] --apply --yes` |
| `a` | Entities | 添加一条元数据记录：选择实体类型，可指定内部 ID（留空则自动分配下一个），并填写可重复的 `KEY=VALUE` 字段行（*Add field* 追加一行，✕ 删除）。schema 与外键违规以内联错误显示，对话框不关闭。 | `operon add <type> [--id …] --field KEY=VALUE …` |
| `A` | Entities | 为选中实体登记外部 accession（internal type/id 已预填）：namespace、accession、可选 version 与 *primary* 复选框；目标实体必须存在且未退休。必填缺失时内联显示。 | `operon add-accession --internal-type … --internal-id … --namespace … --accession … [--version …] [--primary]` |
| `n` | Entities | 预留下一个稳定内部 ID（全部六类，含 `file`）。预留即消耗——对话框保持打开显示结果，Confirm 被禁用，*Close* 后 gap 保留，与 CLI 一致。 | `operon next-id <type>` |
| `i` | Files | 将文件（本地路径或 `sftp://`/`remote://` URL）归档到 `raw/`，表单根据选中行预填。format/compression 留空时自动检测。校验和冲突（同一实体+角色的字节不同）以红色内联显示，绝不覆盖。 | `operon ingest --source … --entity-type … --entity-id … --role …` |
| `v` | Files | 校验选中文件，或在"verify all N files?"确认后校验全部文件。失败项（`MISSING`、`CHECKSUM_FAILED` 等）会在错误对话框中列出。 | `operon verify [--file-id …]` |
| `q` | Files | 对选中文件或全部文件运行内置 QC，带实时进度条（"k/n · 当前 file_id"）。完成通知与 CLI 文本一致（"QC complete: ok/total file(s) passed built-in stages"）；失败项在错误对话框中列出。Cancel 在文件之间协作式地停止批处理——已完成文件的结果保留。 | `operon qc [--file-id …] [--sample-size …] [--phred-offset …] [--rehash]` |
| `I` | Files | 从 `qc-measure` JSON 载荷或外部 TSV 表（按内容判别）导入外部 QC 指标。路径在对话框内输入；**Preview import** 是强制预检——解析并对照 manifest 校验，不写入任何内容，报告格式、指标条数与受影响实体——通过后 Confirm 才解锁。完成通知沿用 CLI 措辞；导入是追加指标，绝不覆盖已有结果。 | `operon import-qc --file …` |
| `S` | Files | 将选中文件（未选中时为全部文件）staging 到 `standardized/`；选择 link 类型（默认 `copy`，或 `hardlink`/`symlink`）后 Confirm。先校验源文件校验和，`raw/` 保持不变；已有已验证目标的文件报告为 "already staged"；状态迁移被拒绝时（例如 `RELEASED` 实体）内联报错，且不写入任何内容。 | `operon standardize [--file-id …] [--link {copy,hardlink,symlink}]` |
| `P` | Files | 为**新**文件跑单源流水线——`ingest` → `standardize` → `QC` → `evaluate`——实体字段根据选中行预填。**Preview pipeline** 是强制预检：解析 QC profile、检查实体，并报告 evaluate 是否会复用 curated 决策，全程不写入；任何表单改动都会重新锁定 Confirm。当会覆盖 curated 决策时，Confirm 还需显式勾选"重新评估"复选框（即本对话框对 `--yes` 的替代）；QC 失败会在 evaluate 之前停止并弹出错误对话框。 | `operon run-pipeline --source … --entity-type … --entity-id … --role … [--profile …] [--format …] [--compression …] [--source-url …] [--yes]` |
| `i` | 全局 | 打开数据集导入向导（也可通过 Home 按钮；在 Files 界面 `i` 仍为归档 ingest）。 | `operon import dataset` |
| — | Home | 从 NCBI Datasets 导入（*NCBI Datasets import* 按钮）：离线输入（report JSON/JSONL、Datasets ZIP、已解包目录）和/或按 accession 在线下载，覆盖 CLI 的全部参数。*Dry run (preflight)* 按钮是 Confirm 前的强制预检——离线输入以 `--dry-run` 解析（不写入任何内容），纯 accession 请求以 `--plan-only` 运行（只给出下载计划，不下载、不写 run 行）——任何表单改动都会重新锁定 Confirm。下载过程中 Cancel 为协作式取消：运行在下一个批次边界停止，记录为 `interrupted`，并可用 `--resume-run` 续跑。 | `operon ncbi-datasets [--input … …] [--accession … …] [--accession-file …] [--include … …] [--no-archive-files] [--standardize] [--dry-run] [--no-preserve-source] [--email …] [--api-key …] [--timeout …] [--batch-size …] [--download-workers …] [--retries …] [--retry-backoff …] [--resume-run …] [--plan-only]` |
| — | Home | 导入受控 metadata 表格（*Import table* 按钮）：选择六张可导入表之一，然后生成空 CSV/XLSX 模板，或预览并导入已有文件。*Preview import* 是 Confirm 前的强制预检——不写入任何内容，并填充 key/action/changed-fields 预览表；对表、模式或文件路径的任何修改都会重新锁定 Confirm。Confirm 应用与 CLI 相同的计划，写出相同的 `changes` 审计行。会修改已有行的导入必须显式选择 on-conflict 策略（留空则复现 CLI 的 "existing rows would change" 错误）。运行中的导入不可中断，并拒绝关闭。 | `operon import table --table … (--template … \| --file … [--on-conflict …])` |
| — | Publish | 在成员/排除预览之后创建不可变 release；版本重复时内联报错。 | `operon release --version … --profile … [--copy-files\|--link hardlink]` |
| — | Publish | 在数量/字节预览之后执行选择性导出；输出目录非空时内联报错。 | `operon export --output … [--entity-type … --entity-id … --file-id … --file-role … --format … --state … --decision … --profile …] [--link …] [--no-qc]` |
| — | Coverage | 生成分类覆盖度报告；低于 profile 阈值的结果是警告通知（FAIL），而不是崩溃。 | `operon report coverage --reference-set … [--release …]` |
| — | Coverage | 导入 NCBI Taxonomy 包（*Import taxonomy…* 按钮）：归档并导入 `taxonomy_report.jsonl` / Datasets 包 / taxdump 压缩包，并指定不可变的版本标签；版本与字节都相同时复用已有快照。运行中的导入无法从 TUI 中断。 | `operon taxonomy import --input … --version …` |
| — | Coverage | 将 taxonomy_coverage profile 对照 READY 快照编译为不可变 reference set（*Compile reference set…* 按钮）：profile 与 taxonomy 版本通过下拉选择（仅列出 READY 快照）；profile/快照/字节都相同时复用已有 reference set。运行中的编译无法从 TUI 中断。 | `operon taxonomy compile --profile … --taxonomy-version …` |
| — | Config / Tasks | 对匹配的清单文件运行分析 recipe（Config 屏选中 recipe 后的 *Run analysis*，或 Tasks 屏 *New analysis* 内选择 recipe）。运行时参数按 recipe 声明的 spec 渲染并与 CLI 完全相同的校验；支持 entity-type/entity-id/limit/threads 过滤、执行后端（项目默认 / local / slurm / ssh；worker 启动前预检，缺少 `sbatch` 或 `execution.ssh` 配置不全都会内联报错）、dry-run（在对话框内显示只读计划）、force 与 keep-partial，带实时进度条与协作取消。Cancel 在下一个文件/规划/收集边界停止批处理，并把已提交的工作整体取消（Slurm 作业或 job array 一次 `scancel`；直连 SSH 载荷在远端主机上终止）；已完成文件的结果保留。逐文件失败在错误对话框中列出；运行结束后跳转到 Tasks 屏。 | `operon analyze --analysis … [--param NAME=VALUE …] [--entity-type …] [--entity-id …] [--limit …] [--threads …] [--backend {local,slurm,ssh}] [--dry-run] [--force] [--keep-partial]` |
| — | Tasks | 运行一条带结构化溯源的外部命令（*Run external*）：step、按 shlex 解析的命令行（shell 引号语义；不支持管道与重定向）、可选 entity/tool/parameter-set、逗号分隔的声明 inputs（与 CLI 一样做哈希与暂存）与 expected outputs、threads、工作目录、超时与执行后端（与分析对话框相同的预检）。预览显示等效 CLI 命令，并说明提交时会分配新的 run id；"跑失败"也是已记录的结果，因此无论成败都会在 Tasks 屏打开完整 run 记录（命令、退出码、错误、日志路径）。运行中的命令无法从 TUI 中断——CLI 的 Ctrl+C 可以。 | `operon run-external --step … --command … [--entity-type … --entity-id … --parameter-set … --tool … --input … --threads … --expected-output … --cwd … --timeout … --backend {local,slurm,ssh}]` |
| — | Home | 创建带校验清单的备份（*Create backup* 按钮）：选择范围（`control`、`results` 或 `full`）与目标目录，Confirm 后在与 CLI 相同的只读会话上调用 `operon.backup.create_backup`。目标必须不存在且位于项目根目录之外（两者均为内联错误）；每个被复制的文件都会带 sha256 记录进 `backup-manifest.json`。运行中的备份无法从 TUI 中断。 | `operon backup create --output … [--scope {control,results,full}]` |
| — | Home | 校验已有备份（*Verify backup* 按钮）：逐条重新计算清单条目的哈希、比对符号链接目标，并把清单之外的文件报告为 unexpected。该对话框是只读的——不写任何内容——结果保留在屏幕上：`OK` 加已校验数量，或 `FAILED` 加失败明细列表；失败同时以错误通知呈现（对应 CLI 的退出码 1）。 | `operon backup verify --input …` |
| — | Remotes | 把清单文件上传到已配置的镜像（*Push…* 按钮，默认预选表格中选中的镜像）：remote 选择、重复的 `--file-id` 过滤（留空 = 全部清单文件）、本地选择预览（数量与字节数；远端是否已有副本要等传输逐文件判定并记为 `skipped`），随后 Confirm。传输期间显示活动指示——核心不上报逐文件进度，且运行中的 push 无法从 TUI 中断。结束后渲染并保留 CLI 的结果表（`file_id`、`relative_path`、`status`、`error`，并以 `push <remote>: <状态>: <数量>, …` 收尾）；有失败的批次会保留失败明细，同时发出错误通知（对应 CLI 的退出码 1）。 | `operon push --remote … [--file-id …]` |
| — | Remotes | 从已配置的镜像恢复文件（*Pull…* 按钮）：remote 选择、重复的 `--file-id` 过滤（留空 = *远端*清单中的每一条，清单在传输开始时从镜像读取）。逐字节校验且幂等：`skipped` 表示本地字节已一致，本地存在但字节不同的文件绝不覆盖；下载完成的文件被记录为 `CHECKSUM_VERIFIED`（不会把 `STANDARDIZED` 降级）并写入驻留行。结果表与错误通知同上，运行中的 pull 无法中断。 | `operon pull --remote … [--file-id …]` |

以上所有操作都会追加与 CLI 相同的 `changes` 审计行和 `workflow_runs` 溯源
记录，因此在报告与导出中，通过 TUI 执行的操作与命令行操作无法区分。

### QC 参数

Files 界面的 QC 对话框支持正整数 FASTQ 采样数（默认 1,000,000 条 reads）、
Phred offset `33`（默认）、`64` 或 `auto`，以及 **Recompute input SHA-256**
（`--rehash`）。自动识别存在歧义时按 33 处理，与 CLI 一致。非法采样数会在
worker 启动前被拒绝。等价命令随参数更新；Confirm 固定本次参数，并在运行
期间锁定控件。执行失败后控件恢复可编辑，可修正参数并重试。

## 数据集导入向导

导入向导（按全局 `i` 键打开）是 `operon import dataset`
的 Textual 移植版。它按小节逐页引导——**Source → Organism → Sample →
Sequencing → Assembly → Annotation → Files**——带 Next/Back 导航和与
questionary 流程一致的逐字段校验（source database/provider 必填；非 INSDC
来源额外要求引用文献和许可证；文件路径必须存在）。organism/sample/assembly/
annotation 选择器列出现有的未退休实体以供复用，选择 "Create a new …" 则分配
新的内部 ID（`db.next_id`）。Sequencing 与 Annotation 为可选小节（复选框）；
注释文件角色（GFF3/CDS/protein）和 reads 角色（R1/R2/single）仅在启用相应
小节时显示。

初始化和页面加载期间导航暂不可用。初始化失败时显示内联错误，可点击
**Retry initialization** 重试。进入实体页面时会刷新选择列表；若草稿引用的
ID 已不在列表中，选择器保持未选择并显示错误，必须明确改选实体或选择新建。
禁用 Sequencing 或 Annotation 并点击 Next 会丢弃该小节的草稿；再次进入时，
输入和选择均被清空，重新启用不会恢复旧内容。

最后的 **Summary** 页面渲染由 questionary 向导自身的 `_summary`/`_warnings`
辅助函数产生的计划与警告，提供非线性的 "Edit <section>" 跳转，并且仅在显式
点击 **Execute import** 后执行。提交走与 CLI 向导相同的单事务
`import_wizard._commit`——数据源注册、实体行、`entity_state`、`changes` 审计、
文件归档（失败时回滚已暂存文件）以及运行日志完全一致。成功后通知列出创建的
实体 ID 与文件数量；错误内联显示，不会留下写入一半的数据。

## NCBI Datasets 导入

该对话框（通过 Home 界面的 *NCBI Datasets import* 按钮打开）是
`operon ncbi-datasets` 的 TUI 表单。它接受离线输入（逗号分隔的
`assembly_data_report` JSON/JSONL 路径、Datasets ZIP 或已解包目录）、
逗号分隔的 accession 和/或 accession 文件、include 类型列表，以及 CLI 的
全部调优参数（`--no-archive-files`、`--standardize`、`--dry-run`、
`--no-preserve-source`、`--email`/`--api-key`、`--timeout`、`--batch-size`、
`--download-workers`、`--retries`、`--retry-backoff`、`--resume-run`、
`--plan-only`）。命令预览只显示非默认参数的等效 CLI 调用。

**Dry run (preflight)** 是 Confirm 前的强制步骤：有离线输入时以
`dry_run=True` 运行适配器（解析 report 并显示计划，不写入任何内容）；
纯 accession 请求以 `plan_only=True` 运行（缺失 include 的下载分组，
不下载、不写 `workflow_runs` 行）。预览列出解析到的来源、assembly 记录数、
下载计划与已归档而跳过的条目。预检失败（非法 accession、未知 include 类型、
输入不可读）会显示在预览区并保持 Confirm 禁用；干净预检之后的任何表单改动
同样会重新锁定 Confirm。数值字段在 worker 启动前校验。

Confirm 在后台 worker 中真实运行适配器，并显示实时计时状态行。运行期间点击
**Cancel**（或按 `esc`）为协作式取消：它设置一个 `threading.Event`，下载器在
每个批次/数据块边界检查它，核心以与 SIGINT 相同的中断记账中止——run 行记录为
`interrupted`（退出码 130），把该 run id 填入 resume 字段即可续跑。取消后对话框
保持打开且控件恢复可编辑；失败时错误内联显示；成功时通知报告 assembly 记录数与
run id。

## 表格导入

该对话框（通过 Home 界面的 *Import table* 按钮打开）对应 `operon import
table`。选择六张可导入表之一（`organisms`、`samples`、`runs`、
`assemblies`、`annotations`、`accessions`）与模式：

- *Generate template* 写出带该表列名的空 `.csv`/`.xlsx` 模板（XLSX 模板
  附带 schema 指南页），等价于 `--template`。
- *Import file* 像 CLI 的预览表（key、action、changed fields）一样预览已有
  `.csv`/`.xlsx` 文件。*Preview import* 按钮是 Confirm 前的强制预检：预览不
  写入任何内容，对表、模式或文件路径的任何修改都会重新锁定 Confirm。Confirm
  应用与 CLI 相同的计划——插入的行进入 `METADATA_VALIDATED`，每个插入或修改的
  字段都会以源文件为 evidence 记入 `changes`，与 `--yes` 完全一致。

当会修改已有行时，请显式选择 on-conflict 策略；留空则复现 CLI 的
"existing rows would change" 错误而不是写入。运行中的导入是单条短事务：不可
中断，对话框会拒绝关闭直至完成。

## Publish 界面

**Release 标签页。** 上方是已有 release 表（版本、创建时间、profile、
接受/排除计数），下方是构建表单：版本、profile 选择器、"copy files" 复选框
（对应 `--copy-files`）与链接方式（copy/hardlink）。**Preview**（切换
profile 时也会触发）运行只读的核心查询 `release_files_for`/
`release_exclusions_for`，显示成员数量与总字节数以及排除表（实体、有效判定、
排除原因、reason codes）——正是该 release 将发布的内容。**Create release**
先显示等价 CLI 命令并要求确认，然后在后台 worker 中通过
`operon.release.create_release` 构建；版本重复或存在未评估/过期实体时内联
报错。

**Export 标签页。** 过滤表单（实体类型、实体 id、文件 id、文件角色、格式、状态、
判定 + profile——判定过滤缺少 profile 时内联报错，与 CLI 一致）、链接方式
（copy/hardlink/symlink）、`include_qc` 复选框以及输出目录。**Preview**
在不写入任何内容的情况下统计匹配文件数量与总字节数（使用与导出本身相同的
`_select_files` 选择逻辑）。**Run export** 显示等价 CLI 命令并要求确认，然后
通过 `operon.export.export_files` 物化导出；输出目录已存在且非空时，在打开
对话框之前即被拒绝。

实体 ID 和文件 ID 均可输入逗号分隔的列表；空白和空项会被忽略。仅提供文件
ID 即可构成有效筛选条件，并按 CLI 的规则与其他过滤条件组合。Preview 与
实际导出使用相同的 ID，确认对话框为每个文件 ID 显示一个 `--file-id` 参数。

## Coverage 界面

上半部分列出已导入的 NCBI Taxonomy 快照（`taxonomy list` 数据）与已编译的
reference set（`taxonomy reference-sets` 数据），并提供 **Import taxonomy…** 按钮，
通过 `operon taxonomy import` 同一核心归档并导入 NCBI taxonomy 包（版本与字节都相同时复用已有快照；
运行中的导入无法从 TUI 中断），以及 **Compile reference set…** 按钮，
通过 `operon taxonomy compile` 同一核心将 taxonomy_coverage profile
对照 READY 快照冻结为 reference set（profile/快照/字节都相同时复用已有
reference set；运行中的编译无法从 TUI 中断）。**Generate report** 表单选择
reference set 与范围——项目元数据或冻结的 release（release 范围会额外显示
release 选择器）——并显示等价的 `operon report coverage` 命令以供确认。输入
相同时复用已缓存的不可变报告；当某个 rank 低于阈值时，结果为带各 rank 覆盖度
与报告路径的警告通知（CLI 将同一结果映射为退出码 1），而不是错误对话框。

**Reports** 表格列出每个 `reports/coverage/COV_*` 目录及其溯源摘要
（reference set、范围、判定、创建时间）。选中某行会将该报告的 TSV——
summary、targets、missing、observations、excluded——渲染为标签页表格
（使用标准库解析；过大的表格截断至 500 行）。

解析时检查每行的字段数是否与表头一致，包括显示上限以外的行。空白表头或格式错误会
显示带文件名和行号的内联错误，不会导致查看器崩溃。修复文件后重新选择报告
即可加载；该校验不会修改报告文件。

## Config 界面

Config 界面以结构化表单编辑两个带版本的配置文件，表单值由后端重新组合为
合法 YAML——没有自由文本编辑器，因此保存绝不可能产生语法非法的文件。
表单未建模的键（`value_by`、`source`、`unknown`、参数 spec
细节等）会**逐字保留**，以暗淡的只读提示显示，绝不被悄悄丢弃。

**保存即新版本。** 每次保存都写入*新版本*：`version` 字段递增
（取当前文件、该名称的已记录快照以及编辑器曾加载的既有文件中的最高版本 + 1；
没有已知历史的新名称为 `1`），
并记录一条内容寻址快照——使用的
规范化文档与 CLI 记录的完全一致，因此 TUI 保存与随后对相同内容执行的
`operon evaluate` / `operon analyze` 映射到同一快照行。保存与当前既有文件相同的内容是
空操作：版本不递增，也不记录快照。每次保存都在对话框中确认，并显示其效果
（"writes `config/profiles/<name>.yaml` as version N + records snapshot"）；
校验错误以内联方式显示且不改动文件（失败的写入会回滚到原文件字节）。
配置通过原子替换发布。回滚范围也包含数据库打开、快照插入或提交失败及可处理的中断：
恢复既有文件的原始字节（包括换行符），或删除本次新建的配置文件。

删除或重命名配置文件不会清除其已记录的版本历史：重建原名称时会接着历史
最高版本递增。尚无快照的文件，其已读取版本也会由编辑器保留。
若文件被外部替换为旧版本，下次内容变更也按此规则确定新版本。
确认对话框使用相同的版本计算方式。

**历史与恢复。** History 对话框列出已记录的快照（快照 id、版本、sha256
前缀、记录时间、使用计数），与 `operon profiles history` /
`operon recipes history` 一致。*View* 将快照文档以 YAML 只读渲染；*Restore*
把快照载入编辑器——随后保存会创建**下一个**版本。快照绝不会被原地覆盖。

**运行分类（Run classify）。** 分类 profile 保存后，*Run classify* 按钮
（位于 *New profile* / *History* 旁，仅对已保存的分类 profile 可用）打开一个
与 `operon classify-sequences --profile` 对应的对话框：显示该 profile 的目标
文件与 source/rule 数量，Confirm 调用同一个核心入口（一个事务、一行 run 记录）。
运行结束后对话框保持打开并展示 CLI 自己的报告——labeled/unlabeled 数量、label
表、labels written/removed 与 run id——同时把按钮改名为 *Run again*，因此对内容
未变的 profile 与输入重跑会显示 0 变更。CLI 会打印的两条警告（忽略的已完成
作业、没有注册序列的目标文件）也会以黄色显示在对话框内。

**QC Profiles 标签页。** 左侧：`config/profiles/` 中的全部 `kind: qc`、
`kind: sequence_classification` 与 `kind: taxonomy_coverage` profile（名称 +
版本；分类类与 coverage 类带标签）。右侧：按所选 profile 自身的 `kind` 切换
（不是合并）到对应编辑器——qc、分类、coverage 三个编辑器互斥显示。*New profile*
提示输入名称**与 kind**，并从该 kind 的最小骨架开始。qc 编辑器包含：
description、五个 `applies_to` 复选框、只读版本提示，以及两个规则小节
（required / warnings）；每条规则是一行 metric、operator（覆盖规则引擎全部
操作符的 Select）、value、code 输入加删除按钮，"add rule" 按小节追加行。
看似数字的值会存为数字。

**分类 profile**（`kind: sequence_classification`）。编辑器对应
`classify.py` 的语法：`applies_to` 是 `entity_type` + `file_role` 一对输入；
*sources*（名称 → analysis、filter 条件列表，以及含 field / direction /
`Value=rank` 映射 / default 的 `best_by` 条目）；*rules*（label 加三者之一：
带 `when` 条件列表的 source、`absent: true`、或 `default: true`）。条件行提供
核心的完整操作符集——`>=`、`<=`、`>`、`<`、`==`、`!=`、`in`、`not_in`、
`between`、`exists` 以及大小写不敏感的 `like`——每行可以是平铺条件、一个
`any of` 组，或一个 `not` 取反。规则行的 source 从已声明的 source 名称中选择；
标记为 `default` 或 `absent` 的规则会隐藏它不应携带的字段。看似数字的操作数
存为数字；`best_by` 仅在文件原本就有 `direction`、或输入偏离核心默认值
（`asc`）时才写出该键；*Save profile* 走与 qc 编辑器相同的版本 + 快照 +
回滚机制，因此之后的 `operon classify-sequences` 消费的正是这里保存的快照。
手工表单无法表示的结构（条件嵌套深于一层 `any:`/`not:`，或 source/rule 不是
映射）会以**只读**方式打开：编辑器说明原因、禁用保存、绝不改写文件——请直接
编辑 YAML。表单未建模的键在每一层（document、source、rule、condition 与
`best_by` 条目）都原样保留。

**Coverage profile**（`kind: taxonomy_coverage`）。编辑器对应
`taxonomy.py` 的扁平 coverage 语法：taxonomy source（`NCBI`）、root TaxIDs、
family/genus 目标 rank、extinct / 排除子树 / 名称正则过滤器，以及每个勾选
rank 一个最低覆盖百分比。结构超出表单的 profile（例如 thresholds 含有非目标
rank 的键）以**只读**打开：界面给出理由、*Save profile* 禁用、文件绝不被表单
改写。未建模的键（含可选 `name`）在文档层与分节层逐字保留。保存记录的内容
寻址快照与后续 `operon taxonomy compile` 消费的一致。

**Tools & Recipes 标签页。** 工具表（名称、可执行文件、启动方式）加
*Check tools* 按钮——等价于 `operon tools-check`，在后台 worker 中运行并逐行
实时更新（检测到的版本为绿色，`MISSING` 为红色），结束时给出汇总通知；单个
工具损坏不会影响整批。下方是 recipe 表（名称、版本、工具、实体类型、文件
角色、格式）；选中某个 recipe 打开其编辑器：description、entity type
（Select，留空 = `*`）、file role 或 file role prefix（二者互斥——同时设置
会被内联拒绝）、format、输入/输出产物类型（Select：file/directory，留空 =
键不存在）、database、database version、environment policy（Select：
`ignore`/`warn`/`strict`，留空 = 核心默认值 `warn`）、输出
子目录与后缀输入框，`arguments` 为每行一个参数的文本框（`${input}` 等占位符
保持可见），运行时 `parameters` 为 `name=default` 行（其余 spec 键保留），
result parser Select（`none`、`blast_tabular`、`hmmer_tblout`、`hmmer_domtblout`、
`rpsbproc_tabular`、`busco_json`）、`result_glob`、HMMER 模式 Select
（`hmmsearch`/`hmmscan`，留空 = 键不存在），
`result_columns` / `hit_metric_columns` / `numeric_columns` 为逗号分隔输入框，
解析器列映射输入框（`query_column`、`subject_column`、`qstart_column`、
`qend_column`、`sstart_column`、`send_column`、`evalue_column`、
`bitscore_column`、`pident_column`），以及
`max_hits_per_query`。清空此可选限制会在保存时移除该键，并恢复核心默认值
（5），并非不限制命中数量。原本没有该键时，继续留空仍为空操作。选中 recipe
后，*Run analysis* 按钮打开预填该 recipe 的分析对话框（见下文）。

> **注意（tools.yaml 格式）：** 从 TUI 保存 recipe 会以规范化的 YAML 格式重写
> `config/tools.yaml`，并丢弃手写注释。内容不会丢失：每个保存的版本都逐字
> 保存在 `recipe_snapshots` 表中（`operon recipes history` /
> `operon recipes show`）。手工编辑该文件仍然完全受支持——TUI 是带审计的
> 替代途径。
