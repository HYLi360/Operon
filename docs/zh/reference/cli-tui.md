# 终端界面（`operon tui`）

`operon tui` 打开项目的交互式终端用户界面（TUI）。TUI 第一阶段是严格只读的：
它从不写入 `operon.sqlite`，从不追加 workflow 记录，也从不触碰已归档文件。
每个面板都使用自己独立的短生命周期只读数据库连接，因此在 CLI 命令对同一项目
执行写操作时，保持 TUI 打开也是安全的。

## 安装

TUI 是基于 [Textual](https://textual.textualize.io/) 的可选功能。安装对应 extra：

```bash
pip install 'operon[tui]'
```

冻结的独立构建版本不打包 Textual；在该版本中 `operon tui` 会打印上述安装提示
并以退出码 2 结束。

## 用法

```bash
operon [--project PATH] tui
```

项目通过全局 `--project` 选项选择，与其他命令完全一致。

## 界面

左侧边栏（或数字键）在四个界面之间切换：

| 界面 | 按键 | 内容 |
|------|------|------|
| Home | `1` | 项目标识、各类实体计数、文件数量与总大小、判定分布、最新 release、最近 10 条 workflow 运行记录，以及"Attention needed"（需要关注）小节（failed/interrupted 运行、当前判定为 REVIEW/FAIL 的实体、状态不健康的文件）。 |
| Entities | `2` | 层级树（organisms → samples → runs 与 assemblies → annotations），并显示每个实体的当前状态。选中节点时显示其元数据字段、accession、状态及关联文件。按 `t` 可纳入已逻辑退休的实体（以暗淡/删除线显示）。 |
| Files | `3` | 可过滤的文件清单表格（子串过滤加状态选择器）。移动光标即可查看完整文件记录及其 `file_locations` 驻留列表。状态带有颜色标记：已验证为绿色，`REMOTE_ONLY` 为蓝色，`MISSING`/`CHECKSUM_FAILED` 为红色。 |
| Runs | `4` | Workflow 运行监控，数据源与 `operon workflow list` 使用相同的只读查询，支持状态/step/entity/数量上限过滤。表格每 2 秒自动刷新，运行中的任务实时更新。在某一行按 `enter` 查看完整运行记录（与 `operon workflow show` 相同的小节）；按 `esc` 返回。 |

全局按键：`1`–`4` 切换界面，`r` 刷新当前界面，`?` 显示按键帮助，`q` 退出。
