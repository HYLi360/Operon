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
- 执行前先打印一行提示，包含新的 run ID、两个日志路径和 follow 提示，例如
  `run <run_id>: logs logs/<run_id>.stdout.log / logs/<run_id>.stderr.log;
  watch: operon workflow show <run_id> --follow`。
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

1. 从 files manifest 中选取匹配 `entity_type + file_role + format` 的文件或目录输入
   （recipe 也可以改用 `file_role_prefix` 对 `file_role` 做纯字符前缀匹配，见
   [Recipe 字段参考](recipe-fields.md)）；
2. 按 `input_kind` 重新校验文件 SHA-256 或目录内容树哈希；
3. 探测并记录外部程序版本；
4. 校验 recipe `parameters` 中声明的 `--param NAME=VALUE`，再渲染参数；除 `${input}`、`${output}`、`${database}`、`${threads}` 外，还支持
   `${input_parent}`、`${input_name}`、`${input_stem}`、`${output_parent}`、
   `${output_name}`、`${output_stem}`、`${file_id}`、`${file_role}`、`${entity_type}`、`${entity_id}`；
   以及声明后的 `${<parameter>}`；运行参数同时进入输出命名和缓存指纹；
5. 命中 `analysis_jobs` 完成缓存时直接跳过，除非 `--force`；精确指纹未命中但存在
   输入相同、输出哈希验证一致的旧完成结果时，收养该结果（状态 `adopted`）而非重算；
6. 按 `output_kind: file|directory` 校验输出存在/非空并计算内容哈希；
7. 解析结果写入 `analysis_hits`/`analysis_results`，并同步汇总指标到 `qc_results`；
   带坐标的 parser 还会把每条解析出的命中以结构化行写入 `analysis_alignments`
   （不受 `max_hits_per_query` 截断）。

结果 parser 支持 `blast_tabular`、`hmmer_tblout`、`hmmer_domtblout`、`rpsbproc_tabular`、
`busco_json` 和 `none`。`busco_json` 从目录的 `result_glob` 中选择唯一 specific JSON
summary，写入 BUSCO 完整率、单拷贝/重复、碎片化、缺失、marker 数和 lineage 等指标。

recipe 也可以用 `commands` 命令链代替单命令形式的 `arguments`（两者互斥）。渲染后的各步
按顺序通过同一个后端执行，共享一条 `analysis_jobs` 记录；第一个非零退出的步骤中止整条链
并使 job 失败，错误形如 `step N/M failed: ...`。每一步有独立日志
`logs/<run_id>.step<N>.stdout.log` / `.stderr.log`，每步的 argv 与退出码记录在该次运行的
`execution_details.steps` 中。中间产物放在确定性的 `${work_dir}` 暂存目录（输出旁的
`<output_name>.work`），运行前删除重建、结束或失败后移除；`--keep-partial` 会保留它。
只有最后一步需要产出 `${output}`。渲染后的 `commands` 参与参数指纹。字段契约见
[Recipe 字段参考](recipe-fields.md) 的"命令链"一节，完整示例见
[结果解析器与示例](recipe-parsers-examples.md) 的 `rpsblast_cdd`。

非 `--dry-run` 执行时，`analyze` 会按处理进度逐文件打印进度行，形如
`[i/N] file_id: running|done|failed`；dry-run 保持安静，只打印计划表格。

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

默认 recipe：`blastn_nt`、`blastp_nr`、`hmmsearch_pfam`、`busco_autolineage`、
`busco_lineage` 与命令链形式的 `rpsblast_cdd`（可自行增删）。
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
  在 CLI 中恢复旧版本是 print-only 流程：把输出人工写回 `config/tools.yaml`，CLI
  不做原地改写，以避免丢失注释。带审计的替代途径是 TUI 的 Config 界面
  （Tools & Recipes 标签页）：其 History 对话框把快照载入 recipe 编辑器，保存即
  创建下一个版本并记录新快照（注意：TUI 保存会规范化文件格式并丢弃手写注释）。

## profiles

```bash
operon profiles history [NAME]
operon profiles show NAME [--snapshot-id N]
```

查看 evaluate 与 classify-sequences 时记录到 `qc_profiles` 的 profile 快照：

- `history`：不带 NAME 时按 profile 汇总（快照数与最近记录时间）；带 NAME 时列出
  该 profile 的快照历史（snapshot_id/version/sha256 前缀/recorded_at/关联
  decisions 数）。
- `show`：把快照文档以 YAML 形式打印（缺省最新一条）。同样是 print-only：在 CLI 中
  恢复时需人工写回 `config/profiles/`，程序不做原地改写。TUI 的 Config 界面
  （QC Profiles 标签页）提供带审计的替代途径：把快照恢复到 profile 编辑器并保存为
  下一个版本，同时记录新快照。

## report analysis

```bash
operon report analysis [--analysis NAME] [--entity-type TYPE] [--entity-id ID] \
  [--hits [--format {text,tsv,json}] [--out PATH] \
          [--query-id ID] [--subject-id ID] [--evalue-max VALUE]] \
  [--limit N] [--include-retired]
```

- 默认显示 `analysis_results` 汇总指标。
- `--hits` 显示 `completed` 作业存储在 `analysis_alignments` 中的结构化比对命中行，列为
  `analysis_name`、`entity_type`、`entity_id`、`query_id`、`subject_id`、`hit_rank`、
  `query_start`、`query_end`、`subject_start`、`subject_end`、`evalue`、`bitscore`、
  `percent_identity`。这些行是完整的解析命中集合，而不是被 `max_hits_per_query` 截断的
  EAV 视图。
- `--format` 选择 `--hits` 的输出形式：对齐的 `text` 表格（默认）、带表头的 `tsv` 或
  `json` 数组。`--out PATH` 把所选格式原子写入文件而不是 stdout。
- `--query-id`、`--subject-id`、`--evalue-max` 过滤 `--hits` 行（e-value 过滤保留
  `evalue <= VALUE` 的行）。`--format`/`--out`/`--query-id`/`--subject-id`/`--evalue-max`
  都必须配合 `--hits` 使用，单独传入属于校验错误。
- `--limit` 默认 20。
- 默认排除有效退役实体；`--include-retired` 显示历史结果。

## extract-domains

```bash
operon extract-domains --file-id FIL_... \
  (--analysis NAME | --regions-tsv TSV) \
  [--flank N] [--min-length N] [--best-only | --all-regions] \
  [--subject-like PATTERN] [--evalue-max E] \
  --out FASTA [--manifest TSV]
```

- 从 `analysis_alignments`（只统计 `completed` 作业）中存储的 query 区间切割一个 manifest
  FASTA 的对应子序列。`--analysis` 指定提供区间的已完成分析；`--subject-like`（SQL LIKE，
  同时匹配 `subject_id` 与 `extra_json` 中的 `short_name`）与 `--evalue-max` 进一步收窄。
  也可以用 `--regions-tsv` 提供外部坐标（列为 `seqid,start,end`，可选 `subject`、`evalue`），
  以支持非 analysis 来源的区间。
- `--flank`（默认 5）把每个区间向两端扩展并在序列边界截断；`--min-length`（默认 30）在
  扩展前丢弃更短的区间。
- `--best-only`（默认）每个 query 只保留 e-value 最优的一个区间，header 保持原 seqid；
  `--all-regions` 每个区间输出一条记录，header 为 `<seqid>|region:<start>-<end>`，使用
  扩展并截断后的坐标。
- 输出 FASTA 原子写入。`--manifest` 记录全部候选区间——包括被排除的及其原因
  （`missing_coordinates`、`below_min_length`、`seqid_not_in_fasta`、
  `region_outside_sequence`）——含来源文件、analysis、subject、原坐标与截取后坐标、长度和
  e-value。
- 源文件为 `REMOTE_ONLY` 时会报出可操作错误：先用 `operon pull` 取回再重跑。每次运行向
  `workflow_runs` 写入一条含完整命令行的 `extract-domains` 步骤；产物通过 `operon adopt`
  重新登记进 manifest（见 [外部分析](../guides/external-analysis.md)）。

## select-sequences

```bash
operon select-sequences --file-id FIL_... \
  [--analysis NAME ...] [--subject-like PATTERN] [--evalue-max E] \
  [--min-span N] [--hit-type TYPE] [--entity-type TYPE] [--entity-id ID] \
  [--require-hit | --require-no-hit] \
  --out FASTA [--manifest TSV]
```

- 输出一个 manifest FASTA 中序列在 `analysis_alignments`（只统计 `completed` 作业）有（或
  没有）匹配命中的子集。重复的 `--analysis` 之间是 OR（任一 analysis 命中即计入）；
  `--subject-like`、`--evalue-max`、`--min-span`、`--hit-type`（与 `extra_json` 中
  `hit_type` 精确匹配）之间是 AND。一个条件都不给属于错误。
- `--require-hit`（默认）保留至少有一条匹配命中的序列；`--require-no-hit` 保留其补集，
  以 `sequences` 表中的 seqid 全集做差集（该表无此文件的行时回退为扫描 FASTA 本身）。
- `--entity-type`/`--entity-id` 限制只统计哪些作业的比对，支持按分类群分批的策略。
- `--manifest` 列出文件中每条序列的入选状态与依据（`matched_analysis`、`best_evalue`、
  `best_subject`、`hit_count`）。
- 向 `workflow_runs` 写入 `select-sequences` 步骤；子集通过 `operon adopt` 重新登记进
  manifest。

## classify-sequences

```bash
operon classify-sequences --profile NAME
```

根据已存储的比对命中为 profile 目标文件中的每条序列打标签，每条被打标的序列向
`sequence_labels` 写入一行。`--profile` 从 `config/profiles/<name>.yaml` 解析，
且必须声明 `kind: sequence_classification`；完整的 YAML 语法见
[序列分类 profile](../guides/qc-profiles.md)。
阈值从不硬编码在引擎里——全部落在版本化的 profile 中。

- 目标文件是匹配 profile 的 `applies_to.entity_type` + `applies_to.file_role` 的
  manifest 文件；被取代（superseded）与有效退役的实体会被排除。对每个文件，
  `sequences` 表中登记的每个 seqid 都会与 `analysis_alignments` 中该文件各来源
  analysis 最新一个 `completed` 作业的命中进行比对判定。
- 规则按顺序求值，首条命中生效；没有被任何规则（也没有 `default`）命中的序列
  保持无标签。每个标签把判定依据（规则序号、来源、job/alignment id、观测值）记入
  `details_json`。
- 幂等且带审计：以相同 profile 内容与相同输入重跑不做任何修改，也不追加
  `changes` 行；profile 变更后会改写受影响的标签、逐条审计（对象类型
  `sequence_label`），并删除不再适用的标签。内容寻址的 profile 快照与 `evaluate`
  一样记录进 `qc_profiles`（可用 `operon profiles history`/`show` 查看）。
- 每次运行写入一行 `workflow_runs`（step 为 `classify-sequences`），
  `execution_details` 含各标签计数，并打印逐标签汇总表。
- 退出码：成功（包括空操作的幂等重跑）为 0；校验错误（未知 profile、profile
  `kind` 不符、profile 格式非法）为 2。

```bash
operon classify-sequences --profile bhlh
```

## timetree

```bash
operon timetree taxon --name NAME
operon timetree pairwise (--taxon NAME | --taxon-id N) ...
operon timetree mrca (--taxon NAME ... | --taxa A,B,C) [--taxon-id N ...]
operon timetree timeline --taxon NAME
operon timetree calibrations (--taxa A,B,C | --taxon NAME ...) [--pairs] [--out TSV]
operon timetree fetch --pairs TSV --output DIR
operon timetree calibrate --snapshot DIR --tree FILE --taxa TSV --constraints TSV --output DIR
```

`timetree` 命令组查询 TimeTree REST API 获取分化时间证据，并编制 MCMCTree 标定先验。
任何 TimeTree 数据的使用都必须引用 Kumar et al. 2022（Mol Biol Evol，
<https://doi.org/10.1093/molbev/msac174>）；每次查询都会打印该引用。

- `taxon` 把学名解析为 TimeTree/NCBI 分类候选（`taxon_id`、`scientific_name`、`rank`）。
  `pairwise` 报告恰好两个分类单元的分化时间摘要；`mrca` 报告 N 个分类单元 MRCA 的摘要；
  `timeline` 列出一个分类单元回溯到 last universal ancestor 的节点时间表。分类单元用
  重复的 `--taxon NAME` / `--taxon-id N` 给出（`mrca` 与 `calibrations` 也接受逗号分隔的
  `--taxa`）；名称多解时绝不自动选择——错误会列出候选并要求改用 `--taxon-id`。所有查询
  子命令接受 `--format text|json`（默认 `text`）与绕过缓存的 `--refresh`。
- 响应缓存在项目内 `adapters_cache/timetree/<sha256(url)>.json`，保存请求 URL、获取时间与
  原始 body，保证重放查询可审计。TimeTree 的使用条款禁止镜像或再分发其数据库，因此只按
  实际查询缓存，且请求保持串行并在每次真实请求间停顿。每次查询向 `workflow_runs` 记录
  一条 `timetree:<subcommand>` 步骤。
- `calibrations` 编制 MCMCTree 标定先验表：默认对整组做一次 MRCA 查询，`--pairs` 则逐对
  查询。十列：`node_label`、`taxa`、`taxon_ids`、`age_median`、`ci_low`、`ci_high`、
  `study_count`、`source`、`queried_at`、`cache_file`。`--out` 额外写出 TSV；需要纳入版本
  管理时用 `operon adopt` 归档进项目。
- `fetch` 与 `calibrate` 与项目无关，只操作显式文件路径：`fetch` 把选定 NCBI 分类单元对
  的摘要与逐研究证据下载为一个新的不可变快照目录（原始响应加带校验的清单，绝不覆盖
  已有运行）；`calibrate` 把此类快照中经审阅的软界标定编译到一棵有根、严格二分的物种树
  上（每条约束都要求 `approved=yes` 与理由；摘要置信区间是证据，绝不能当作化石界标）。

## environments

```bash
operon environments list
operon environments show ENVIRONMENT_ID
operon environments export ENVIRONMENT_ID [--format {explicit,yaml}]
```

这些命令以只读方式打开数据库。`list` 输出 ID、捕获时间，以及每条记录的一行渲染摘要
（发行版、CPU、内存、GPU、conda 环境名与包数、`capture_status`；文档中缺失的字段省略）；
`show` 输出存储的 JSON，包括系统/硬件/包指纹及捕获限制。`export` 向标准输出打印重建规范，不读取或修改当前环境。
未知 ID 或不完整 Conda 清单会报错。旧环境记录可以查看，但缺少完整包清单时不能导出。

默认的 `explicit` 格式锁定安装包 URL 和 SHA-256（缺失时回退 MD5），仅覆盖兼容平台上的
Conda 管理安装包。YAML 包含包名/版本/build 约束及 channel，不含原始 prefix，需要重新求解依赖。
pip/本地修改和激活脚本不在两种导出的恢复范围内。参阅[外部分析指南](../guides/external-analysis.md)中的重建流程。
