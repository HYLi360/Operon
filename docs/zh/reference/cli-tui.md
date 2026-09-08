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

## 界面

左侧边栏（或数字键）在八个界面之间切换：

| 界面 | 按键 | 内容 |
|------|------|------|
| Home | `1` | 项目标识、各类实体计数、文件数量与总大小、判定分布、最新 release、最近 10 条 workflow 运行记录，以及"Attention needed"（需要关注）小节（failed/interrupted 运行、当前判定为 REVIEW/FAIL 的实体、状态不健康的文件）。按 `i` 键打开导入向导（见下文）。 |
| Entities | `2` | 层级树（organisms → samples → runs 与 assemblies → annotations），并显示每个实体的当前状态。选中节点时显示其元数据字段、accession、状态、关联文件，以及最新的内置 QC 与外部分析（如 BUSCO/QUAST）指标。已逻辑退休的实体默认显示（暗淡加删除线）；按 `t` 可隐藏它们。按 `x` 打开生命周期对话框（见下文）。 |
| Files | `3` | 可过滤的文件清单表格（子串过滤加状态选择器）。移动光标即可查看完整文件记录及其 `file_locations` 驻留列表。状态带有颜色标记：已验证为绿色，`REMOTE_ONLY` 为蓝色，`MISSING`/`CHECKSUM_FAILED` 为红色。按 `i`/`v`/`q` 分别进行归档、校验与 QC（见下文）。 |
| Tasks | `4` | Workflow 运行监控（指处理任务，而非测序 run），数据源与 `operon workflow list` 使用相同的只读查询，支持状态/step/entity/数量上限过滤。表格在进入时加载、按 `r` 手动刷新（无后台轮询），光标与滚动位置在刷新间保持不变。在某一行按 `enter` 查看完整运行记录（与 `operon workflow show` 相同的小节）；按 `esc` 返回。 |
| Decisions | `5` | 来自 `current_decisions` 视图的当前判定（有效判定 = 存在人工裁定时的裁定值，标记 `✎curated`），支持 profile/判定/文本过滤。按 `e` 评估，按 `c` 裁定选中行（见下文）。 |
| Config | `6` | 项目配置文件的结构化、基于控件的编辑器（不提供自由文本 YAML 编辑）：**QC Profiles** 与 **Tools & Recipes**。详见下文。 |
| Publish | `7` | 不可变 release 构建器与选择性导出构建器（两个标签页），写入前均提供只读预览。详见下文。 |
| Coverage | `8` | 已导入的 NCBI Taxonomy 快照、已编译的 reference set、覆盖度报告生成（`operon report coverage`），以及已有 `reports/coverage/COV_*` 报告的浏览器。详见下文。 |

全局按键：`1`–`8` 切换界面，`r` 刷新当前界面，`i` 打开数据集导入向导
（焦点位于 Files 界面时除外，此时 `i` 为归档 ingest），`?` 显示按键帮助，
`ctrl+q` 退出。

## 写操作

每个对话框都会显示等价的 CLI 命令，并随表单输入实时同步；记录的审计与溯源
信息与该命令完全一致。

| 按键 | 界面 | 操作 | 等价命令 |
|------|------|------|----------|
| `e` | Decisions | 在选定的 profile 下评估全部实体或选中行所属实体；完成后提示"N decisions evaluated"。 | `operon evaluate --profile … [--entity-type … --entity-id …]` |
| `c` | Decisions | 裁定选中的判定：选择新判定、reviewer（预填 `$USER`）、必填的 reason、可选的 evidence。校验错误（实体已退休、无自动判定）以内联方式显示，对话框不关闭。 | `operon curate --entity-type … --entity-id … --profile … --decision … --reviewer … --reason …` |
| `x` | Entities | 退休（或对已退休实体恢复）选中实体。对话框先加载只读的影响计划（受影响的实体/文件/引用及物理变更——逻辑退休恒为零），当计划报告无变更时禁用确认按钮；RETIRE 必须选择 reason code。 | `operon retire\|restore <id> --reason … [--reason-code …] --apply --yes` |
| `i` | Files | 将文件（本地路径或 `sftp://`/`remote://` URL）归档到 `raw/`，表单根据选中行预填。format/compression 留空时自动检测。校验和冲突（同一实体+角色的字节不同）以红色内联显示，绝不覆盖。 | `operon ingest --source … --entity-type … --entity-id … --role …` |
| `v` | Files | 校验选中文件，或在"verify all N files?"确认后校验全部文件。失败项（`MISSING`、`CHECKSUM_FAILED` 等）会在错误对话框中列出。 | `operon verify [--file-id …]` |
| `q` | Files | 对选中文件或全部文件运行内置 QC，带实时进度条（"k/n · 当前 file_id"）。完成通知与 CLI 文本一致（"QC complete: ok/total file(s) passed built-in stages"）；失败项在错误对话框中列出。Cancel 在文件之间协作式地停止批处理——已完成文件的结果保留。 | `operon qc [--file-id …]` |
| `i` | 全局 | 打开数据集导入向导（也可通过 Home 按钮；在 Files 界面 `i` 仍为归档 ingest）。 | `operon import dataset` |
| — | Publish | 在成员/排除预览之后创建不可变 release；版本重复时内联报错。 | `operon release --version … --profile … [--copy-files\|--link hardlink]` |
| — | Publish | 在数量/字节预览之后执行选择性导出；输出目录非空时内联报错。 | `operon export --output … [--entity-type … --entity-id … --file-role … --format … --state … --decision … --profile …] [--link …] [--no-qc]` |
| — | Coverage | 生成分类覆盖度报告；低于 profile 阈值的结果是警告通知（FAIL），而不是崩溃。 | `operon report coverage --reference-set … [--release …]` |

以上所有操作都会追加与 CLI 相同的 `changes` 审计行和 `workflow_runs` 溯源
记录，因此在报告与导出中，通过 TUI 执行的操作与命令行操作无法区分。

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

最后的 **Summary** 页面渲染由 questionary 向导自身的 `_summary`/`_warnings`
辅助函数产生的计划与警告，提供非线性的 "Edit <section>" 跳转，并且仅在显式
点击 **Execute import** 后执行。提交走与 CLI 向导相同的单事务
`import_wizard._commit`——数据源注册、实体行、`entity_state`、`changes` 审计、
文件归档（失败时回滚已暂存文件）以及运行日志完全一致。成功后通知列出创建的
实体 ID 与文件数量；错误内联显示，不会留下写入一半的数据。

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

**Export 标签页。** 过滤表单（实体类型、实体 id、文件角色、格式、状态、
判定 + profile——判定过滤缺少 profile 时内联报错，与 CLI 一致）、链接方式
（copy/hardlink/symlink）、`include_qc` 复选框以及输出目录。**Preview**
在不写入任何内容的情况下统计匹配文件数量与总字节数（使用与导出本身相同的
`_select_files` 选择逻辑）。**Run export** 显示等价 CLI 命令并要求确认，然后
通过 `operon.export.export_files` 物化导出；输出目录已存在且非空时，在打开
对话框之前即被拒绝。

## Coverage 界面

上半部分列出已导入的 NCBI Taxonomy 快照（`taxonomy list` 数据）与已编译的
reference set（`taxonomy reference-sets` 数据）。**Generate report** 表单选择
reference set 与范围——项目元数据或冻结的 release（release 范围会额外显示
release 选择器）——并显示等价的 `operon report coverage` 命令以供确认。输入
相同时复用已缓存的不可变报告；当某个 rank 低于阈值时，结果为带各 rank 覆盖度
与报告路径的警告通知（CLI 将同一结果映射为退出码 1），而不是错误对话框。

**Reports** 表格列出每个 `reports/coverage/COV_*` 目录及其溯源摘要
（reference set、范围、判定、创建时间）。选中某行会将该报告的 TSV——
summary、targets、missing、observations、excluded——渲染为标签页表格
（使用标准库解析；过大的表格截断至 500 行）。

## Config 界面

Config 界面以结构化表单编辑两个带版本的配置文件，表单值由后端重新组合为
合法 YAML——没有自由文本编辑器，因此保存绝不可能产生语法非法的文件。
表单未建模的键（`value_by`、`source`、`unknown`、`result_glob`、参数 spec
细节等）会**逐字保留**，以暗淡的只读提示显示，绝不被悄悄丢弃。

**保存即新版本。** 每次保存都写入*新版本*：`version` 字段递增
（旧版本 + 1；新 profile/recipe 为 `1`），并记录一条内容寻址快照——使用的
规范化文档与 CLI 记录的完全一致，因此 TUI 保存与随后对相同内容执行的
`operon evaluate` / `operon analyze` 映射到同一快照行。保存未修改的内容是
空操作：版本不递增，也不记录快照。每次保存都在对话框中确认，并显示其效果
（"writes `config/profiles/<name>.yaml` as version N + records snapshot"）；
校验错误以内联方式显示且不改动文件（失败的写入会回滚到原文件字节）。
配置通过原子替换发布。回滚范围也包含数据库打开、快照插入或提交失败及可处理的中断：
恢复既有文件的原始字节（包括换行符），或删除本次新建的配置文件。

**历史与恢复。** History 对话框列出已记录的快照（快照 id、版本、sha256
前缀、记录时间、使用计数），与 `operon profiles history` /
`operon recipes history` 一致。*View* 将快照文档以 YAML 只读渲染；*Restore*
把快照载入编辑器——随后保存会创建**下一个**版本。快照绝不会被原地覆盖。

**QC Profiles 标签页。** 左侧：`config/profiles/` 中的 `kind: qc` profile
（名称 + 版本）。右侧：编辑器——description、五个 `applies_to` 复选框、
只读版本提示，以及两个规则小节（required / warnings）；每条规则是一行
metric、operator（覆盖规则引擎全部操作符的 Select）、value、code 输入加
删除按钮，"add rule" 按小节追加行。*New profile* 提示输入名称并从最小骨架
开始。看似数字的值会存为数字。`taxonomy_coverage` profile 不在此处编辑。

**Tools & Recipes 标签页。** 工具表（名称、可执行文件、启动方式）加
*Check tools* 按钮——等价于 `operon tools-check`，在后台 worker 中运行并逐行
实时更新（检测到的版本为绿色，`MISSING` 为红色），结束时给出汇总通知；单个
工具损坏不会影响整批。下方是 recipe 表（名称、版本、工具、实体类型、文件
角色、格式）；选中某个 recipe 打开其编辑器：description、entity type
（Select，留空 = `*`）、file role、format、database、database version、输出
子目录与后缀输入框，`arguments` 为每行一个参数的文本框（`${input}` 等占位符
保持可见），运行时 `parameters` 为 `name=default` 行（其余 spec 键保留），
result parser Select（`none`、`blast_tabular`、`hmmer_tblout`、`busco_json`），
`result_columns` / `hit_metric_columns` 为逗号分隔输入框，以及
`max_hits_per_query`。

> **注意（tools.yaml 格式）：** 从 TUI 保存 recipe 会以规范化的 YAML 格式重写
> `config/tools.yaml`，并丢弃手写注释。内容不会丢失：每个保存的版本都逐字
> 保存在 `recipe_snapshots` 表中（`operon recipes history` /
> `operon recipes show`）。手工编辑该文件仍然完全受支持——TUI 是带审计的
> 替代途径。
