# 外部分析命令

## run-external

```bash
operon run-external \
  --step STEP --command 'CMD ARGS' \
  [--entity-type TYPE] [--entity-id ID] \
  [--parameter-set PS] [--expected-output PATH ...] \
  [--cwd DIR] [--timeout SECONDS] [--backend {local,slurm,ssh}]
```

- 命令用 shlex 解析，不经过 shell。
- 记录退出码、stdout/stderr 文件、起止时间到 `workflow_runs` 与 `logs/workflow.jsonl`。
- 仅当退出码为 0 且所有 `--expected-output` 非空时才判定成功。
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

默认 recipe：`blastn_nt`、`blastp_nr`、`hmmsearch_pfam`、`busco_autolineage`（可自行增删）。
`config/tools.yaml` 的完整字段和执行语义见 [Recipe 配置参考](recipe-overview.md)。

## report analysis

```bash
operon report analysis [--analysis NAME] [--entity-type TYPE] [--entity-id ID] \
  [--hits] [--limit N] [--include-retired]
```

- 默认显示 `analysis_results` 汇总指标。
- `--hits` 显示 `analysis_hits` 中的 top hits。
- `--limit` 默认 20。
- 默认排除有效退役实体；`--include-retired` 显示历史结果。
