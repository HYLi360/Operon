# Recipe 字段参考

## 输入选择

### 4.1 选择字段

| 字段 | 默认值 | 含义 |
|---|---|---|
| `entity_type` | 空 | 限定 `assembly`、`annotation`、`organism` 等实体类型 |
| `file_role` | 空 | 必须与 manifest 中的 `files.file_role` 精确匹配 |
| `format` | 空 | 必须与 manifest 中的 `files.format` 精确匹配 |
| `input_kind` | `format: directory` 时为 `directory`，否则为 `file` | 运行时要求输入路径的实际类型 |

选择字段与运行时对象类型是两个独立概念：

- `file_role` 和 `format` 回答“从 manifest 选哪一条记录”；
- `input_kind` 回答“该记录指向的路径在文件系统中是什么”。

普通 protein FASTA：

```yaml
entity_type: annotation
file_role: protein_fasta
format: fasta
input_kind: file
```

目录输入：

```yaml
entity_type: organism
file_role: other
format: directory
input_kind: directory
```

目录需要先作为 artifact 归档：

```bash
operon --project . ingest \
  --source proteome_set/ \
  --entity-type organism \
  --entity-id ORG_000001 \
  --role other \
  --format directory
```

目录哈希由相对路径、空目录、文件大小与内容以及符号链接目标确定，不依赖 mtime、属主
或权限。目录中任一文件内容、名字或结构发生变化，运行前的 manifest 哈希复核都会失败。

### 4.2 `analyze` 的额外过滤

recipe 决定基础候选集合；命令行还可进一步收窄：

```bash
operon --project . analyze \
  --analysis example_analysis \
  --entity-type annotation \
  --entity-id ANN_000001 \
  --limit 1
```

命令行过滤不会扩大 recipe 允许的输入范围。例如 recipe 声明
`entity_type: annotation` 时，传入 assembly ID 不会强行把 assembly 作为输入。

## 输出与命名

每个输入的输出根路径由四部分组成：

```text
<project>/analysis/<output_subdir>/<entity_id>/<artifact_name>
```

相关字段：

| 字段 | 默认值 | 含义 |
|---|---|---|
| `output_subdir` | recipe 名 | `analysis/` 下的一级目录 |
| `output_kind` | `file` | 输出必须是 `file` 或 `directory` |
| `output_suffix` | 文件输出为 `.tsv`；目录输出为空 | 只用于默认 artifact 名称 |
| `output_name` | 空 | 可选的单层名称模板；设置后覆盖默认命名公式 |
| `parameters` | 空 mapping | recipe 明确允许从 CLI 传入的运行时参数；未声明参数会被拒绝 |

没有 `output_name` 时：

```text
artifact_name = <file_id>.<file_role><output_suffix>
```

例如：

```yaml
output_subdir: blastn_nt
output_kind: file
output_suffix: .blastn.tsv
```

对 `FIL_000001` 的 `genome_fasta` 输入得到：

```text
analysis/blastn_nt/ASM_000001/FIL_000001.genome_fasta.blastn.tsv
```

设置 `output_name` 后，默认公式和 `output_suffix` 都不再负责最终名称：

```yaml
output_subdir: busco
output_kind: directory
output_name: ${file_id}.busco
```

得到：

```text
analysis/busco/ANN_000001/FIL_000003.busco/
```

`output_name` 必须渲染成一个安全的路径组件，不能是空字符串、`.`、`..`、绝对路径或
带 `/` 的嵌套路径。需要分层时用 `output_subdir` 与系统自动加入的 `entity_id` 层级。

外部程序必须创建恰好由 recipe 计算出的 `${output}`。文件必须非空；目录必须存在、
类型正确且包含结果。未命中缓存的新运行会先安全删除这个精确目标 artifact，避免上一次
残留文件被误判为本次成功，但不会删除 `analysis/` 中其他路径。

## 参数与占位符

### 6.1 声明安全的运行时参数

当一个 recipe 需要在每次运行时选择一个值（例如 BUSCO lineage），不要允许任意参数
直接追加到命令末尾。recipe 必须先通过 `parameters` 声明名字、是否必需和约束：

```yaml
parameters:
  lineage_dataset:
    description: BUSCO lineage dataset name
    required: true
    pattern: '[A-Za-z0-9][A-Za-z0-9_.-]*'
```

支持的参数约束：

| 字段 | 含义 |
|---|---|
| `description` | 人类可读说明 |
| `required` | 未提供且没有 default 时是否报错 |
| `default` | 可选默认值 |
| `pattern` | 整个值必须匹配的正则表达式 |
| `choices` | 可选的允许值列表 |

运行时使用可重复的 `--param NAME=VALUE`：

```bash
operon analyze --analysis busco_lineage \
  --param lineage_dataset=fabales_odb12.2
```

运行参数可以作为 `${lineage_dataset}` 一样的占位符用于 `arguments` 和
`output_name`。参数值会进入参数指纹；未声明、缺少必需值、不符合 pattern/choices、
重复传值或仍有未解析占位符时，命令在启动外部程序前失败。

带运行时参数的 recipe 不使用跨指纹“输出收养”：只有完全相同的参数指纹才能命中
缓存。这样不会把某个 lineage 的现有输出当成另一个 lineage 的等价结果。

`arguments` 是参数数组，不是 shell 命令字符串。每个 YAML list item 对应一个 argv：

```yaml
arguments:
  - --input
  - ${input}
  - --label
  - "sample with spaces"
```

这里 `sample with spaces` 是一个参数，不会再次按空格拆分。反过来，下面写法也是一个
参数，不会自动拆成 `--cpu` 与 `24`：

```yaml
# 错误，除非目标程序真的要求单个含空格参数
- "--cpu ${threads}"
```

### 6.2 arguments 中可用的占位符

| 占位符 | 渲染内容 |
|---|---|
| `${input}` | 输入文件或目录的绝对路径 |
| `${input_parent}` | 输入父目录的绝对路径 |
| `${input_name}` | 输入 artifact 的 basename，包含扩展名 |
| `${input_stem}` | `Path.stem` 意义上的输入 stem，只移除最后一个 suffix |
| `${output}` | 计算出的输出文件或目录绝对路径 |
| `${output_parent}` | 输出 artifact 的父目录绝对路径 |
| `${output_name}` | 输出 artifact 的 basename |
| `${output_stem}` | `Path.stem` 意义上的输出 stem |
| `${database}` | 解析后的数据库或共享缓存绝对路径；未配置时为空字符串 |
| `${threads}` | CLI `--threads` 或项目默认线程数 |
| `${file_id}` | 当前输入的稳定文件 ID |
| `${file_role}` | 当前 manifest 文件角色 |
| `${entity_type}` | 当前实体类型 |
| `${entity_id}` | 当前实体 ID |
| `${<parameter>}` | recipe 在 `parameters` 中声明并解析后的运行时参数 |

占位符可以嵌入一个参数：

```yaml
- --prefix=${file_id}
```

但不会执行 shell 的环境变量、`~`、glob 或命令替换。路径本身不需要人工加 shell 引号，
因为 `operon` 直接传递 argv 数组。

### 6.3 `output_name` 中可用的占位符

`output_name` 在完整输出路径建立之前先渲染，因此只支持：

```text
${file_id}
${file_role}
${entity_type}
${entity_id}
${input_name}
${input_stem}
${<parameter>}
```

它不能引用 `${output}`、`${output_parent}` 或 `${output_name}` 本身。无法识别的占位符会
在配置校验阶段报错。

## 数据库与缓存目录

| 字段 | 默认值 | 含义 |
|---|---|---|
| `database` | 空 | 数据库文件、数据库目录或工具共享下载目录；相对路径按项目根解析 |
| `database_version` | 空 | 人类可读的逻辑版本，同时参与数据库缓存身份 |
| `database_checksum` | 空 | 可选的显式 SHA-256 身份，适合冻结的大型数据库 |
| `database_mode` | `reference` | `reference` 或 `mutable_cache` |

### 7.1 `reference`

适合分析期间不应改变的数据库：

```yaml
database: /data/db/Pfam-A.hmm
database_version: "37.0"
database_mode: reference
```

单文件默认按内容 SHA-256 识别；目录默认使用包含相对路径、大小和 mtime 的快速目录
指纹。对严格可复现的大型目录库，建议显式提供发布方校验值：

```yaml
database_checksum: 0123456789abcdef...
```

### 7.2 `mutable_cache`

适合 BUSCO 这类运行时会逐步下载 lineage 的共享目录：

```yaml
database: resources/busco_downloads
database_version: odb12
database_mode: mutable_cache
```

该目录在实际运行前自动创建。身份由路径、明确的 `database_version` 和可选 checksum
决定，不会因为后来又下载了另一个 lineage 就让所有旧 BUSCO 作业失去缓存。
`mutable_cache` 必须配置非空的 `database_version`。

如果目标是严格冻结与离线复现，应预先下载指定 lineage，把 BUSCO 改为
`--lineage_dataset ... --offline`，再使用 `reference` 模式并维护版本或 checksum。

### 7.3 SSH 远程数据库

当 SSH 使用非空 `remote_root` 时，`${database}` 中位于本地项目根下的路径会映射到
远端 root；项目根之外的绝对路径保持原样。`operon` 不会把大型参考库随每个任务上传：

- `reference` 必须由管理员预先放到远端目标路径，并配置 `database_checksum`；运行前
  检查路径存在。显式 checksum 与 SSH 主机/root 一起进入数据库缓存身份；
- `mutable_cache` 必须有 `database_version`，目标目录不存在时通过 SFTP 创建；
- 本地存在同名数据库不代表远端已经部署，反之亦然；缺失会在提交分析前明确报错；
- 不同 SSH 主机/root 不共享分析缓存身份，避免在内容位置不明时跨集群复用结果。

这里的 `database_checksum` 是 recipe 对冻结数据库发布身份的显式声明。对于需要逐字节
审计的参考库，应在部署阶段另外执行发布方校验或生成 Operon 可复核的清单；运行期不会
为每个候选输入反复遍历数 TB 数据库。

## 缓存身份

一次已完成分析只有在以下身份全部相同时才会复用：

```text
analysis name
+ file_id
+ 输入内容哈希
+ 渲染后的 arguments
+ 解析后的运行时参数
+ threads
+ tool version
+ parser/output 相关 recipe 设置
+ database identity
```

命中数据库记录后，`operon` 还会检查输出 artifact 仍然存在，并重新计算文件或目录
哈希与已记录值比较。输出被删除或修改时，旧作业会标记为 `superseded` 并重新执行。

精确身份未命中时的第二级续跑（输出验证收养）：如果同一 `(analysis, file_id)` 存在
一条旧的 `completed` 作业，其输入内容哈希与当前一致，且其记录的输出 artifact 仍在
磁盘上、逐字节哈希与记录相同，`operon` 不会重算，而是把该输出收养进当前指纹——
以当前参数指纹/数据库身份插入一条指向同一输出的新 `completed` 行（关联原
`workflow_run_id`），在 `changes` 审计表记录收养原因，并把该文件标记为 `adopted`。
这覆盖了软件升级导致指纹公式变化、recipe 改名等场景。输出被修改或输入内容变化时
不收养，照常重算。收养只针对有验证过的输出的完成结果；`--force` 语义不变，始终
重算。dry-run 输出中 status 列为 `cached`/`adoptable`/`planned`，分别表示命中
完成缓存、会走收养路径、将实际执行；`--force` 下原本命中的缓存也显示为 `planned`。
声明了运行时参数的 recipe 禁用第二级收养，只允许精确缓存命中。

`--force` 只表示忽略一个本来有效的 completed cache。它会保留历史作业记录、将旧记录
标为 `superseded`，删除精确的旧输出目标，然后创建新作业。它不能修复错误参数、错误
输出名或外部程序自身的失败。

先用 dry-run 检查选择、命令与缓存最安全：

```bash
operon --project . analyze \
  --analysis busco_autolineage \
  --entity-id ANN_000001 \
  --threads 24 \
  --dry-run
```

## Slurm 资源覆盖

当项目使用 Slurm 执行后端（`project.yaml` 的 `execution.backend: slurm`，或命令行
`--backend slurm`）时，所有 recipe 默认共享 `execution.slurm` 的资源设置。单个
recipe 可以用 `slurm:` mapping 覆盖其中的同名字段（空值与空字符串不会覆盖），
例如给 BUSCO 单独调整内存与时限：

```yaml
tools:
  busco:
    executable: busco
    run_method: ""
    version_args: ["--version"]
    version_pattern: 'BUSCO\s+([^\s]+)'
    recipes:
      busco_autolineage:
        # ... 其余字段不变 ...
        slurm:
          mem_gb: 64
          time: "72:00:00"
```

可覆盖字段与 `execution.slurm` 一致：

| 字段 | 默认值 | 含义 |
|---|---|---|
| `partition` | 空 | Slurm 分区；空表示不写 `--partition` |
| `time` | `24:00:00` | 作业时限 |
| `mem_gb` | `0` | 内存上限（GB）；`0` 表示不写 `--mem` |
| `extra_sbatch` | `[]` | 追加的 `#SBATCH` 行，如 `["--gres=gpu:1"]` |
| `setup_commands` | `[]` | 插入在命令前的行，如 `["module load blast/2.15"]` |
| `poll_interval` | `15` | `squeue` 轮询间隔（秒）；本地与远端 Slurm 都完整遵守该值（仅下限 0.1 秒） |

未列出的字段继承 `execution.slurm`。线程数始终来自 `--threads`（映射为
`--cpus-per-task`），不在 recipe 覆盖范围内。
