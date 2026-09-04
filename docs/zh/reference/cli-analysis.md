# 外部分析命令

## run-external

```bash
operon run-external \
  --step STEP --command 'CMD ARGS' \
  [--entity-type TYPE] [--entity-id ID] \
  [--parameter-set PS] [--tool NAME] [--input PATH ...] [--threads N] \
  [--expected-output PATH ...] \
  [--cwd DIR] [--timeout SECONDS] [--backend {local,slurm,ssh}]
```

- 命令用 shlex 解析，不经过 shell。
- 记录退出码、stdout/stderr 文件、起止时间到 `workflow_runs` 与 `logs/workflow.jsonl`；
  `workflow_runs` 同时填充 `duration_seconds`（墙钟秒数）与执行后端采集到的
  `max_rss_mb`/`avg_rss_mb`/`cpu_seconds` 资源使用列（采集不到时留 NULL，不影响
  运行判定；各后端采集方式见
  [外部分析执行模型](../architecture/external-analysis.md)）。
- 仅当退出码为 0 且所有 `--expected-output` 非空时才判定成功。
- `--tool NAME` 引用 `config/tools.yaml` 中已配置的工具：命中时自动探测版本并记录
  `tool_version` 与 `tool_version_raw`；探测失败降级为 warning，不阻断运行。
- `--input PATH`（可重复）声明输入文件/目录：逐文件计算 SHA-256，组合哈希写入
  `input_sha256`，完整清单进入 `execution_details`。使用 SSH 后端且 `remote_root`
  非空时，声明的输入还会在执行前上传到远端项目镜像中的对应路径；这些输入解析后
  必须位于本地项目根目录内。
- `--threads N` 记录并向执行后端申请线程数。
- `--backend` 覆盖 `project.yaml` 的 `execution.backend`，可选 `local`（默认，
  本地子进程）、`slurm`（本地 Slurm 集群提交）或 `ssh`（在 SSH 远程主机上
  执行）。配置与前提见 [How-to 操作手册](../guides/index.md)第 9 节。

## tools-check

```bash
operon tools-check
```

读取 `config/tools.yaml`，逐个执行 `version_args` 并用 `version_pattern` 提取版本。
程序缺失时显示 `ERROR` 与配置建议，不修改数据库；任一程序不可用时返回退出码 1。

## analyze

```bash
operon analyze --analysis NAME   [--param NAME=VALUE ...]   [--entity-type TYPE] [--entity-id ID]   [--threads N] [--limit N] [--dry-run] [--force] [--keep-partial] [--backend {local,slurm,ssh}]
```

按 recipe 自动完成：

1. 从 files manifest 中选取匹配 `entity_type + file_role + format` 的文件或目录输入；
2. 按 `input_kind` 重新校验文件 SHA-256 或目录内容树哈希；
3. 探测并记录外部程序版本；
4. 校验 recipe `parameters` 中声明的 `--param NAME=VALUE`，再渲染参数；除 `${input}`、`${output}`、`${database}`、`${threads}` 外，还支持
   `${input_parent}`、`${input_name}`、`${input_stem}`、`${output_parent}`、
   `${output_name}`、`${output_stem}`、`${file_id}`、`${file_role}`、`${entity_type}`、`${entity_id}`；
   以及声明后的 `${<parameter>}`；运行参数同时进入输出命名和缓存指纹；
5. 命中 `analysis_jobs` 完成缓存时直接跳过，除非 `--force`；精确指纹未命中但存在
   输入相同、输出哈希验证一致的旧完成结果时，收养该结果（状态 `adopted`）而非重算；
6. 按 `output_kind: file|directory` 校验输出存在/非空并计算内容哈希；
7. 解析结果写入 `analysis_hits`/`analysis_results`，并同步汇总指标到 `qc_results`。

结果 parser 支持 `blast_tabular`、`hmmer_tblout`、`busco_json` 和 `none`。
`busco_json` 从目录的 `result_glob` 中选择唯一 specific JSON summary，写入 BUSCO
完整率、单拷贝/重复、碎片化、缺失、marker 数和 lineage 等指标。

`--backend` 覆盖 `project.yaml` 的 `execution.backend`，可选 `local`（默认）、
`slurm`（本地 Slurm 集群提交）或 `ssh`（在 SSH 远程主机上执行）；工具版本探测
也经同一后端执行。配置、前提与日志位置见 [How-to 操作手册](../guides/index.md)第 9 节。
若 SSH 配置了 `storage_remote`，本地缺失但状态为 `REMOTE_ONLY` 的候选输入会先严格
验证远端清单和实际内容，再在远端原位使用。

`--dry-run` 只列出计划不执行：表格的 status 列为 `cached`（命中完成缓存）、
`adoptable`（将收养已验证的旧输出）或 `planned`（将实际执行），output 列为
计划输出路径，tool_version 为探测到的版本。

`--param` 只能设置 recipe 明确声明的参数。缺少 required 参数、未知参数、重复参数或不
满足 recipe 的 `pattern`/`choices` 时返回配置错误。默认 `busco_lineage` 用法：

```bash
operon analyze --analysis busco_lineage \
  --param lineage_dataset=fabales_odb12.2
```

`report analysis` 显示所有仍为 `completed` 的参数变体；不会只保留同 recipe 的最新一条。

中断与优雅停机：运行期间收到 Ctrl+C（SIGINT）或 SIGTERM 时，`analyze` 会优雅停机——

- 当前步骤的作业进程被完整终止：本地后端按进程组（含孙进程）先 SIGTERM 后
  SIGKILL；`slurm` 后端对排队/运行中的作业执行 `scancel`；`ssh` 后端终止远端
  `setsid` 进程组或对远端 Slurm 作业执行 `scancel`；
- 当前文件的 `analysis_jobs` 行被置为 `interrupted`（不会污染完成缓存），其半成品
  输出被删除（stdout/stderr 日志保留用于排查；加 `--keep-partial` 可保留半成品输出）；
- 批次不再处理后续文件，进程以退出码 130 退出；重跑同一命令即可从未完成的文件
  继续（`interrupted` 行不参与缓存命中）；
- 清理期间再次发送信号会立即强制退出（退出码 128+signum）。

若进程被 SIGKILL 等无法捕获的方式杀死，残留的 `RUNNING` 行会在下一次 `analyze`
启动时被清扫为 `interrupted`。

每个待处理文件在实际执行前都会把当前 recipe 及其引用的 tool spec 快照记录到
`recipe_snapshots`（内容寻址去重），`analysis_jobs.recipe_snapshot_id` 回指该快照；
缓存命中同样记录当前配置的快照，续跑收养的作业继承原作业的快照 id。详见下文
`recipes` 命令与 [外部分析执行模型](../architecture/external-analysis.md)。

默认 recipe：`blastn_nt`、`blastp_nr`、`hmmsearch_pfam`、`busco_autolineage`（可自行增删）。
`config/tools.yaml` 的完整字段和执行语义见 [Recipe 配置参考](recipe-overview.md)。

## recipes

```bash
operon recipes list
operon recipes history NAME
operon recipes show NAME [--snapshot-id N]
```

- `list`：列出 `config/tools.yaml` 中配置的全部 recipe（name/version/tool/
  entity_type/file_role/format）。
- `history`：列出该 recipe 已记录的快照历史（snapshot_id/version/sha256 前缀/
  recorded_at/关联 `analysis_jobs` 数）。
- `show`：把快照文档以 YAML 形式打印（缺省最新一条，或按 `--snapshot-id` 指定）。
  恢复旧版本是 print-only 流程：把输出人工写回 `config/tools.yaml`，程序不做原地
  改写，以避免丢失注释。

## profiles

```bash
operon profiles history [NAME]
operon profiles show NAME [--snapshot-id N]
```

查看 evaluate 时记录到 `qc_profiles` 的 profile 快照：

- `history`：不带 NAME 时按 profile 汇总（快照数与最近记录时间）；带 NAME 时列出
  该 profile 的快照历史（snapshot_id/version/sha256 前缀/recorded_at/关联
  decisions 数）。
- `show`：把快照文档以 YAML 形式打印（缺省最新一条）。同样是 print-only：恢复时
  人工写回 `config/profiles/`，程序不做原地改写。

## report analysis

```bash
operon report analysis [--analysis NAME] [--entity-type TYPE] [--entity-id ID] \
  [--hits] [--limit N] [--include-retired]
```

- 默认显示 `analysis_results` 汇总指标。
- `--hits` 显示 `analysis_hits` 中的 top hits。
- `--limit` 默认 20。
- 默认排除有效退役实体；`--include-retired` 显示历史结果。
