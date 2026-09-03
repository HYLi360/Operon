# Workflow 历史命令

`operon workflow` 对 `workflow_runs` 中的记录提供只读检索与终端打印。使用者无需手写
SQL；命令不会修改项目，也不会因查询而追加新的 workflow 记录。

## 列出 workflow 运行

```bash
operon workflow list \
  [--from TIME] [--to TIME] \
  [--run-id WF_ID] \
  [--step STEP ...] [--status STATUS ...] \
  [--entity-type TYPE] [--entity-id ID] \
  [--parent-run-id WF_ID] [--resumes-run-id WF_ID] \
  [--tool NAME] [--executor NAME] \
  [--limit N] [--offset N] [--oldest-first] \
  [--format {table,json,jsonl}]
```

不加过滤条件时，默认打印最新 50 条。终端表格把最常用于诊断的字段放在一起：换算为本机
时区的开始时间、状态、step、实体、耗时和完整 run ID。为把常规行控制在约 120 列，过长的
step 或实体标签只会在终端表格中缩短；`show`、JSON 与 JSONL 不会截断字段，也不会替换
数据库中保存的原始时间戳。

`--from` 包含起点，`--to` 不包含终点；两者都按 `started_at` 过滤，因此两个相邻区间不会
在边界处重复返回同一运行。时间值使用 ISO-8601 日期或时间戳。不带时区偏移的日期/时间按
本机时区解释；也接受 `Z` 和显式偏移，并按绝对时刻比较。

`--step` 与 `--status` 都是精确匹配且可重复；同一选项的多个值按 OR 组合，不同过滤条件
按 AND 组合。`--parent-run-id` 用于查找某个父 workflow 下的 item run，
`--resumes-run-id` 用于查找显式恢复某次旧运行的新尝试。

默认从新到旧排序；`--oldest-first` 改为从旧到新。`--limit` 默认 50，`--offset` 用于分页，
`--limit 0` 取消条数限制。JSON 输出一个数组，JSONL 每行输出一个对象。若
`execution_details` 中保存的是合法 JSON，两个机器格式都会将其还原为对象或数组；旧版
纯文本仍保持字符串。

示例：

```bash
# 查询本机日历中某一天开始的失败或中断运行
operon workflow list \
  --from 2026-09-01 --to 2026-09-02 \
  --status failed --status interrupted

# 按时间顺序查看某个 assembly 的全部 QC 运行
operon workflow list --step qc \
  --entity-type assembly --entity-id ASM_000123 \
  --oldest-first --limit 0

# 以 JSONL 输出经 Slurm 提交的运行
operon workflow list --executor slurm --format jsonl

# 查看某次 batch/import workflow 的子运行
operon workflow list --parent-run-id WF_20260901_120000+0800_abcd1234
```

没有匹配行时，table 输出 `no workflow runs matched`，JSON 输出 `[]`，JSONL 不输出任何
行；三种情况都正常返回成功。

## 查看单次 workflow 运行

```bash
operon workflow show WF_ID [--format {text,json}]
```

文本输出依次分为身份与关联、时间与资源、执行信息、产物与日志路径、结果、执行详情。
长命令和错误信息会随当前终端宽度换行，hash 与路径保持可复制。`--format json` 返回全部
`workflow_runs` 列，不做终端缩短，并还原合法的 `execution_details` JSON。

run ID 不存在时属于校验错误，退出码为 2。

## Provenance 边界与当前限制

项目内 SQLite 数据库是唯一可写事实来源；上述命令查询其中的 `workflow_runs` 表。
`logs/workflow.jsonl` 继续作为 append-only、机器可读的 provenance 日志及备份/导出材料；
这些只读命令不会编辑或重建它。

当前接口只覆盖 workflow run。`changes` 中的字段修改、`entity_lifecycle_events` 中的直接
生命周期事件及其他领域历史仍通过各自专用命令或只读 SQL 查看；目前没有统一的跨表事件
时间线。全文检索、实时 follow 和交互式 TUI 也明确延后，待后端事件模型和运维接口成熟后
再作为整体 UX 项目建设；当前只提供稳定、可脚本化的 CLI 输出。
