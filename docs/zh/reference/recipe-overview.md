# Recipe 配置模型

`config/tools.yaml` 以声明方式定义外部分析。recipe 指定输入 artifact、程序启动方式、输出位置、缓存身份和结果回写方式。

## 执行流程

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

配置字段可按以下五类理解：

| 问题 | 对应字段 |
|---|---|
| 用哪个程序、从哪里启动？ | tool 层的 `executable`、`run_method`、版本字段 |
| 哪些已归档数据可以输入？ | `entity_type`、`file_role`、`format`、`input_kind` |
| 结果放在哪里、是文件还是目录？ | `output_subdir`、`output_kind`、`output_name`、`output_suffix` |
| 命令行怎么组成？ | `arguments` 与占位符 |
| 数据库如何识别、输出如何机读？ | `database*`、`result_parser` 及 parser 专用字段 |

## YAML 层级

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

recipe 可声明可选的 `version:`（正整数，缺省 1；非正整数或布尔值在加载时报配置
错误）。`analyze` 会把 recipe 原文与其引用的 tool spec 一起作为内容寻址快照记录到
`recipe_snapshots` 表，`analysis_jobs` 通过 `recipe_snapshot_id` 回指；recipe 或
tool 定义的任何变更都产生新快照。历史用 `operon recipes history NAME` 查看，
`operon recipes show NAME [--snapshot-id N]` 以 YAML 打印快照文档，供人工写回
`config/tools.yaml`（程序不做原地改写）。

## tool 层字段

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
