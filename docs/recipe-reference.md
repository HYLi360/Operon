# 外部分析 recipe 配置参考

`operon` 的 recipe 不是一段可以随意拼接的 shell 命令。它是一份声明：指定哪类已归档
artifact 可以作为输入、外部程序如何运行、输出 artifact 应放在哪里、怎样判断缓存是否
仍然有效，以及如何把结果同步回 SQLite。

如果只想运行现有 recipe，通常只需修改程序环境和数据库路径，然后执行
`tools-check` 与 `analyze --dry-run`。只有接入新工具时，才需要阅读本文的完整字段参考。

## 1. 先记住这条执行链

一次 `analyze` 运行依次完成：

1. 用 `entity_type + file_role + format` 在 `files` manifest 中选择输入；
2. 用 `input_kind` 检查它在文件系统中究竟应当是文件还是目录，并复核内容哈希；
3. 探测外部工具版本，解析数据库路径并计算数据库身份；
4. 计算输出 artifact 的唯一目标路径；
5. 将 `${...}` 占位符渲染成参数数组；
6. 用输入、参数、工具版本和数据库身份查找已完成缓存；
7. 未命中缓存时运行外部程序，并验证文件或目录输出存在且非空；
8. 计算输出内容哈希，通过 result parser 写入 `analysis_results`、`analysis_hits` 和
   `qc_results`。

可以把各组字段理解为下面五个问题：

| 问题 | 对应字段 |
|---|---|
| 用哪个程序、从哪里启动？ | tool 层的 `executable`、`run_method`、版本字段 |
| 哪些已归档数据可以输入？ | `entity_type`、`file_role`、`format`、`input_kind` |
| 结果放在哪里、是文件还是目录？ | `output_subdir`、`output_kind`、`output_name`、`output_suffix` |
| 命令行怎么组成？ | `arguments` 与占位符 |
| 数据库如何识别、输出如何机读？ | `database*`、`result_parser` 及 parser 专用字段 |

## 2. YAML 层级

`config/tools.yaml` 有三层：全局启动默认值、tool、recipe。

```yaml
version: 1

conda:
  bin: conda
  run_args:
    - run
    - --no-capture-output

tools:
  example_tool:                 # tool 名；写入 provenance
    description: Example tool
    executable: example
    run_method: "conda run --no-capture-output -n example"
    version_args: ["--version"]
    version_pattern: 'example\s+([^\s]+)'

    recipes:
      example_analysis:         # analysis 名；传给 analyze --analysis
        description: Example recipe
        entity_type: annotation
        file_role: protein_fasta
        format: fasta
        input_kind: file
        output_kind: file
        output_suffix: .example.tsv
        arguments:
          - --input
          - ${input}
          - --output
          - ${output}
          - --threads
          - ${threads}
        result_parser: none
```

同一个 tool 可以包含多个 recipe，例如同一个 `blastp` 可分别使用不同数据库、参数和
结果上限。recipe 名在整个配置中应保持唯一，否则查找时只会使用最先遇到的同名项。

## 3. tool 层字段

| 字段 | 必需性与默认值 | 含义 |
|---|---|---|
| `description` | 可选 | 人类可读说明 |
| `executable` | 可选，默认使用 tool 名 | 最终执行的程序名或绝对路径 |
| `run_method` | 可选，默认直接执行 | 放在 `executable` 前的启动前缀；可写字符串或结构化 mapping |
| `version_args` | 建议配置 | 追加到程序后的版本探测参数列表 |
| `version_pattern` | 建议配置 | 从 stdout 与 stderr 合并文本中提取版本的正则；第一个捕获组是版本 |
| `recipes` | 必需 | 该工具提供的 recipe mapping |

最简单的直接启动方式：

```yaml
executable: blastn
run_method: ""
```

字符串启动前缀使用 shell 风格引号拆分，但执行时不经过 shell：

```yaml
executable: busco
run_method: "mamba run -n busco_6.1.0"
```

实际参数数组相当于：

```text
mamba | run | -n | busco_6.1.0 | busco | <rendered arguments...>
```

因此 `run_method` 中不能依赖管道、重定向、`$VAR`、通配符扩展或命令替换。需要这些
行为时，应制作一个明确的 wrapper executable，让 wrapper 自己管理其内部行为。

结构化 conda/mamba 写法更容易区分环境名和参数：

```yaml
run_method:
  mode: conda
  bin: mamba
  env: busco_6.1.0
  args: [run, --no-capture-output]
```

支持的结构化 `mode`：

| mode | 字段 | 行为 |
|---|---|---|
| `conda` | `env` 必需；`bin`、`args` 可选 | 生成 `<bin> <args> -n <env> <executable>` |
| `prefix` | `prefix` 列表 | 将列表原样放在 executable 前，可用于容器启动器 |
| `path` | 无 | 不增加前缀，直接运行 executable |

`tools-check` 会逐个运行版本命令：

```bash
operon --project . tools-check
```

版本字符串参与缓存身份。程序升级后，即使其余参数完全相同，也不会错误复用旧版本
生成的结果。

## 4. 输入选择与 artifact 类型

### 4.1 选择字段

| 字段 | 默认值 | 含义 |
|---|---|---|
| `entity_type` | 空 | 限定 `assembly`、`annotation`、`organism` 等实体类型 |
| `file_role` | 空 | 必须与 manifest 中的 `files.file_role` 精确匹配 |
| `format` | 空 | 必须与 manifest 中的 `files.format` 精确匹配 |
| `input_kind` | `format: directory` 时为 `directory`，否则为 `file` | 运行时要求输入路径的实际类型 |

这两组概念不要混在一起：

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

## 5. 输出 artifact 与命名规则

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

## 6. 参数与占位符

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

### 6.1 arguments 中可用的占位符

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

占位符可以嵌入一个参数：

```yaml
- --prefix=${file_id}
```

但不会执行 shell 的环境变量、`~`、glob 或命令替换。路径本身不需要人工加 shell 引号，
因为 `operon` 直接传递 argv 数组。

### 6.2 `output_name` 中可用的占位符

`output_name` 在完整输出路径建立之前先渲染，因此只支持：

```text
${file_id}
${file_role}
${entity_type}
${entity_id}
${input_name}
${input_stem}
```

它不能引用 `${output}`、`${output_parent}` 或 `${output_name}` 本身。无法识别的占位符会
在配置校验阶段报错。

## 7. 数据库与共享下载缓存

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

## 8. 缓存到底比较什么

一次已完成分析只有在以下身份全部相同时才会复用：

```text
analysis name
+ file_id
+ 输入内容哈希
+ 渲染后的 arguments
+ threads
+ tool version
+ parser/output 相关 recipe 设置
+ database identity
```

命中数据库记录后，`operon` 还会检查输出 artifact 仍然存在，并重新计算文件或目录
哈希与已记录值比较。输出被删除或修改时，旧作业会标记为 `superseded` 并重新执行。

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

## 9. result parser

`result_parser` 控制成功的输出怎样进入 SQLite：

| parser | 预期输出 | 主要回写 |
|---|---|---|
| `none` | 任意已验证 artifact | 只保存作业与输出 provenance，不解析业务指标 |
| `blast_tabular` | tab-separated 文件 | top hits、query/hit 汇总、best e-value |
| `hmmer_tblout` | HMMER `--tblout` 文件 | query-target 的 e-value/score 与汇总 |
| `busco_json` | BUSCO 输出目录或 JSON 文件 | 完整率、单拷贝/重复、碎片/缺失、lineage 与版本元数据 |

所有汇总 metric 写入 `analysis_results`，并以 `qc_stage: analysis:<recipe>` 同步到
`qc_results`；因此它们会自然出现在 `qc-table` 宽表中，也可以直接被 QC profile 使用。
top hits 另外写入 `analysis_hits`。

### 9.1 `blast_tabular`

至少需要声明两列：

```yaml
result_parser: blast_tabular
result_columns:
  - qseqid
  - sseqid
  - pident
  - length
  - evalue
  - bitscore
hit_metric_columns: [pident, length, evalue, bitscore]
numeric_columns: [pident, length, evalue, bitscore]
query_column: qseqid
subject_column: sseqid
max_hits_per_query: 5
```

`result_columns` 必须与外部程序实际列顺序完全一致。默认用前两列作为 query/subject，
其余列作为 hit metric；上述专用字段可以覆盖。输入顺序决定 hit rank，因此应让工具按
希望保留的优先级输出。

### 9.2 `hmmer_tblout`

该 parser 按标准 HMMER tblout 读取 target、query、full-sequence E-value 和 score，忽略
注释行，并按输入顺序保留每个 query 的前 `max_hits_per_query` 个 target。

```yaml
arguments:
  - --tblout
  - ${output}
  - --cpu
  - ${threads}
  - ${database}
  - ${input}
result_parser: hmmer_tblout
max_hits_per_query: 5
```

### 9.3 `busco_json`

BUSCO 通常使用目录输出：

```yaml
result_parser: busco_json
result_glob: short_summary*.json
```

`result_glob` 必须保持在输出目录内，不能是绝对路径或含 `..`。如果只命中一个 JSON，
直接使用；如果 generic 与 specific summary 同时存在，优先唯一的
`short_summary.specific.*.json`；多个 specific summary 仍然匹配时拒绝猜测，应收窄 glob。

至少要求 JSON 中存在 `results.Complete percentage` 和 `results.n_markers`。解析结果包括：

- complete、single-copy、duplicated、fragmented、missing 的百分比与数量；
- marker 数、domain 与 one-line summary；
- lineage 名称、创建日期、BUSCO 数与物种数；
- datasets/OrthoDB/dataset 版本、NCBI taxid 与 BUSCO 软件版本。

## 10. 完整 BUSCO recipe

```yaml
tools:
  busco:
    description: Benchmarking Universal Single-Copy Ortholog assessment
    executable: busco
    run_method: "mamba run -n busco_6.1.0"
    version_args: ["--version"]
    version_pattern: 'BUSCO\s+([^\s]+)'

    recipes:
      busco_autolineage:
        description: BUSCO protein mode with automatic lineage selection
        entity_type: annotation
        file_role: protein_fasta
        format: fasta
        input_kind: file

        database: resources/busco_downloads
        database_version: odb12
        database_mode: mutable_cache

        output_subdir: busco
        output_kind: directory
        output_name: ${file_id}.busco

        arguments:
          - -m
          - protein
          - -i
          - ${input}
          - -o
          - ${output_name}
          - --out_path
          - ${output_parent}
          - --download_path
          - ${database}
          - -c
          - ${threads}
          - --auto-lineage
          - --opt-out-run-stats
          - --tar

        result_parser: busco_json
        result_glob: short_summary*.json
```

BUSCO 的 `-o` 是短 run name，不是输入路径，也不应传完整 `${output}`；`--out_path` 才是
父目录。因此分别使用 `${output_name}` 与 `${output_parent}`。

另外，BUSCO auto-lineage 使用的 SEPP 会错误地在完整输出路径上执行
`replace("fasta", "jplace")`。输出路径任何一层都不要含小写 `fasta`。使用
`${file_id}.busco` 可以避免默认的 `<file_id>.protein_fasta.busco` 名称；`operon` 也会在
启动 auto-lineage 前检查并拒绝这种危险路径。

运行：

```bash
operon --project . tools-check
operon --project . analyze \
  --analysis busco_autolineage \
  --entity-id ANN_000001 \
  --threads 24 \
  --dry-run
operon --project . analyze \
  --analysis busco_autolineage \
  --entity-id ANN_000001 \
  --threads 24
operon --project . analysis-results \
  --analysis busco_autolineage \
  --entity-id ANN_000001
```

## 11. 目录输入与目录输出示例

下面的示例假设 wrapper 接收一个目录，创建一个非空结果目录。对原生只接受单文件的
程序，不应仅靠把 `input_kind` 改成 `directory` 来假装支持目录；应让 wrapper 明确遍历
目录并定义失败语义。

```yaml
tools:
  directory_tool:
    executable: directory-wrapper
    run_method: ""
    version_args: ["--version"]
    version_pattern: 'directory-wrapper\s+([^\s]+)'
    recipes:
      directory_roundtrip:
        entity_type: organism
        file_role: other
        format: directory
        input_kind: directory
        output_subdir: directory_roundtrip
        output_kind: directory
        output_name: ${file_id}.results
        arguments:
          - --input-dir
          - ${input}
          - --output-dir
          - ${output}
          - --threads
          - ${threads}
        result_parser: none
```

## 12. 接入新工具的推荐顺序

不要一次把所有字段都填满。按下面顺序通常最容易排错：

1. 先确定一条真实 manifest 输入记录的 `entity_type`、`file_role`、`format`；
2. 配置 tool 启动和版本探测，直到 `tools-check` 成功；
3. 先用 `result_parser: none`，只让工具正确产生一个文件或目录 artifact；
4. 用 `output_name` 明确目录型程序的根目录，确认 `${output}` 与工具实际创建的位置一致；
5. 加入数据库路径和版本策略；
6. 用 `analyze --dry-run --limit 1` 检查完整命令；
7. 对一个小输入实际运行，检查 stdout、stderr、`analysis_jobs` 和输出结构；
8. 最后启用 parser，核对 `analysis-results` 与 `qc-table`；
9. 再扩大到全部候选实体。

## 13. 常见错误速查

| 现象 | 常见原因 | 处理 |
|---|---|---|
| `no candidate files` | manifest 的 role/format/entity 与 recipe 不完全一致 | 查 `files.tsv` 或数据库中的实际值 |
| 输入 checksum mismatch | raw 文件或目录归档后被修改 | 恢复原内容，或按新版本重新归档而不是覆盖 raw |
| 输出 missing or empty | 工具创建的位置与 `${output}` 不一致 | dry-run 后对照工具的 output/run-name 语义 |
| 目录程序被当成文件 | 缺少 `output_kind: directory` | 明确 artifact 类型 |
| 修改参数后没有复用缓存 | 这是预期行为，渲染参数参与身份 | 检查 dry-run 的命令差异 |
| 数据库每次增长都使缓存失效 | 把下载区误设为 `reference` | 对共享下载区使用带版本的 `mutable_cache` |
| parser 没有找到文件 | `result_glob` 相对于输出目录写错 | 从 `${output}` 根目录检查真实相对位置 |
| 多个 BUSCO specific JSON 冲突 | glob 覆盖了多个 lineage summary | 将 `result_glob` 收窄到最终 summary |
| BUSCO/SEPP 出现 `protein_jplace` 路径 | 输出路径含 `fasta` | 使用 `output_name: ${file_id}.busco`，并检查父目录 |
| 加 `--force` 仍失败 | 不是缓存问题，而是命令、输出或工具错误 | 先修 recipe；`--force` 只控制 completed cache |

## 14. 最小检查清单

保存新 recipe 前逐项确认：

- 输入选择字段与 manifest 的实际值完全一致；
- `input_kind` 和 `output_kind` 与文件系统对象类型一致；
- 外部程序最终创建的位置恰好等于 `${output}`；
- 每个 `arguments` list item 就是一个独立 argv；
- 可增长下载目录使用 `mutable_cache`，冻结参考库使用 `reference`；
- tool version、数据库逻辑版本和 parser 已明确；
- `tools-check` 通过；
- `analyze --dry-run --limit 1` 的完整命令符合预期；
- 单个小输入实跑并核对 `analysis-results` 与 `qc-table` 后，再批量运行。
