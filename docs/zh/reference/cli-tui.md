# 终端界面（`operon tui`）

`operon tui` 打开项目的交互式终端用户界面（TUI）。读取操作使用短生命周期的只读
数据库连接，因此在 CLI 命令对同一项目执行写操作时，保持 TUI 打开也是安全的。
写操作（第二阶段）调用与 CLI **完全相同的核心函数**，因此审计行（`changes`）、
workflow 溯源记录（`workflow_runs`）及语义与等价命令完全一致。每个写操作遵循
相同的流程：表单或计划预览 → 对话框中显示等价的 CLI 命令 → 显式确认（Confirm）
→ 在后台 worker 中执行变更 → 成功通知并刷新面板，或显示内联错误信息（对话框
保持打开）。

## 安装

TUI 是基于 [Textual](https://textual.textualize.io/) 的可选功能。安装对应 extra：

```bash
pip install 'OperonDBS[tui]'
```

冻结的独立构建版本不打包 Textual；在该版本中 `operon tui` 会打印上述安装提示
并以退出码 2 结束。

## 用法

```bash
operon [--project PATH] tui
```

项目通过全局 `--project` 选项选择，与其他命令完全一致。

## 界面

左侧边栏（或数字键）在六个界面之间切换：

| 界面 | 按键 | 内容 |
|------|------|------|
| Home | `1` | 项目标识、各类实体计数、文件数量与总大小、判定分布、最新 release、最近 10 条 workflow 运行记录，以及"Attention needed"（需要关注）小节（failed/interrupted 运行、当前判定为 REVIEW/FAIL 的实体、状态不健康的文件）。 |
| Entities | `2` | 层级树（organisms → samples → runs 与 assemblies → annotations），并显示每个实体的当前状态。选中节点时显示其元数据字段、accession、状态、关联文件，以及最新的内置 QC 与外部分析（如 BUSCO/QUAST）指标。已逻辑退休的实体默认显示（暗淡加删除线）；按 `t` 可隐藏它们。按 `x` 打开生命周期对话框（见下文）。 |
| Files | `3` | 可过滤的文件清单表格（子串过滤加状态选择器）。移动光标即可查看完整文件记录及其 `file_locations` 驻留列表。状态带有颜色标记：已验证为绿色，`REMOTE_ONLY` 为蓝色，`MISSING`/`CHECKSUM_FAILED` 为红色。按 `i`/`v`/`q` 分别进行归档、校验与 QC（见下文）。 |
| Tasks | `4` | Workflow 运行监控（指处理任务，而非测序 run），数据源与 `operon workflow list` 使用相同的只读查询，支持状态/step/entity/数量上限过滤。表格每 2 秒自动刷新，运行中的任务实时更新，光标与滚动位置在刷新间保持不变。在某一行按 `enter` 查看完整运行记录（与 `operon workflow show` 相同的小节）；按 `esc` 返回。 |
| Decisions | `5` | 来自 `current_decisions` 视图的当前判定（有效判定 = 存在人工裁定时的裁定值，标记 `✎curated`），支持 profile/判定/文本过滤。按 `e` 评估，按 `c` 裁定选中行（见下文）。 |
| Config | `6` | 项目配置文件的结构化、基于控件的编辑器（不提供自由文本 YAML 编辑）：**QC Profiles** 与 **Tools & Recipes**。详见下文。 |

全局按键：`1`–`6` 切换界面，`r` 刷新当前界面，`?` 显示按键帮助，`q` 退出
（当 Files 表格获得焦点时，`q` 改为启动 QC 运行——将焦点移至别处或使用侧边栏
离开）。

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

以上所有操作都会追加与 CLI 相同的 `changes` 审计行和 `workflow_runs` 溯源
记录，因此在报告与导出中，通过 TUI 执行的操作与命令行操作无法区分。

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
