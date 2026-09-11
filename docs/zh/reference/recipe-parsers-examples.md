# 结果解析器与示例

## 结果解析器

`result_parser` 控制成功的输出怎样进入 SQLite：

| parser | 预期输出 | 主要回写 |
|---|---|---|
| `none` | 任意已验证 artifact | 只保存作业与输出 provenance，不解析业务指标 |
| `blast_tabular` | tab-separated 文件 | top hits、query/hit 汇总、best e-value，以及全量结构化比对行 |
| `hmmer_tblout` | HMMER `--tblout` 文件 | query-target 的 e-value/score 与汇总 |
| `hmmer_domtblout` | HMMER `--domtblout` 文件 | per-domain 的 i-Evalue/score 命中与全量结构化比对行 |
| `rpsbproc_tabular` | rpsbproc 表格报告 | per-domain 的 e-value/bitscore 命中与全量结构化比对行 |
| `busco_json` | BUSCO 输出目录或 JSON 文件 | 完整率、单拷贝/重复、碎片/缺失、lineage 与版本元数据 |

所有汇总 metric 写入 `analysis_results`，并以 `qc_stage: analysis:<recipe>` 同步到
`qc_results`；因此它们会自然出现在 `report qc` 宽表导出中，也可以直接被 QC profile 使用。
top hits 另外写入 `analysis_hits`（EAV 行，每个 query 截断到 `max_hits_per_query`）。
带坐标的 parser（`blast_tabular`、`hmmer_domtblout`、`rpsbproc_tabular`）还会把每条解析出的命中以结构化行写入
`analysis_alignments`——query/subject ID、命中排名、query/subject 区间、e-value、bitscore
与 identity 百分比各有独立列，未映射的列保存在 `extra_json`；该表不受
`max_hits_per_query` 截断，`report analysis --hits` 读取的就是它。

### 10.1 `blast_tabular`

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

当输出包含 BLAST 坐标列时会被自动识别，每条命中行同时以结构化形式写入
`analysis_alignments`：

```yaml
arguments:
  - -outfmt
  - "6 qseqid sseqid pident length qstart qend sstart send evalue bitscore"
result_columns: [qseqid, sseqid, pident, length, qstart, qend, sstart, send, evalue, bitscore]
```

常用名 `qstart`/`qend`/`sstart`/`send`/`evalue`/`bitscore`/`pident` 会被默认识别；工具
使用其他表头时用 `qstart_column`/`qend_column`/`sstart_column`/`send_column`/
`evalue_column`/`bitscore_column`/`pident_column` 显式覆盖。比对行不受
`max_hits_per_query` 影响，始终全量写入。

以 rpsblast 为例，显式声明全部列映射的完整 recipe：

```yaml
tools:
  rpsblast:
    executable: rpsblast
    run_method: "conda run --no-capture-output -n blast"
    version_args: ["-version"]
    version_pattern: 'rpsblast:\s*([^\s]+)'
    recipes:
      rpsblast_cdd:
        description: Annotation proteins against the CDD database
        entity_type: annotation
        file_role: protein_fasta
        format: fasta
        database: /data/db/cdd/Cdd
        database_version: "3.21"
        output_subdir: rpsblast_cdd
        output_suffix: .rpsblast.tsv
        arguments:
          - -db
          - ${database}
          - -query
          - ${input}
          - -out
          - ${output}
          - -outfmt
          - "6 qseqid sseqid pident length qstart qend sstart send evalue bitscore"
          - -num_threads
          - ${threads}
        result_parser: blast_tabular
        result_columns: [qseqid, sseqid, pident, length, qstart, qend, sstart, send, evalue, bitscore]
        hit_metric_columns: [pident, length, evalue, bitscore]
        query_column: qseqid
        subject_column: sseqid
        qstart_column: qstart
        qend_column: qend
        sstart_column: sstart
        send_column: send
        evalue_column: evalue
        bitscore_column: bitscore
        pident_column: pident
        max_hits_per_query: 5
```

如果 rpsblast 的结果改由 `rpsbproc` 后处理（推荐的 CDD 流程），应使用 `commands` 命令链
配合 `rpsbproc_tabular` parser——见下文。

### 10.2 `hmmer_tblout`

该 parser 按标准 HMMER tblout 读取 target、query、full-sequence E-value 和 score，忽略
注释行，并按输入顺序保留每个 query 的前 `max_hits_per_query` 个 target。tblout 不含比对
坐标，因此该 parser 不会写 `analysis_alignments` 行。

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

### 10.3 `hmmer_domtblout`

需要结构化的 per-domain 命中时，建议改用 `--domtblout` 与 `hmmer_domtblout` parser。每
条非注释行对应一个 domain：query 为 HMM profile 名，subject 为目标序列，单 domain 的
i-Evalue 与 domain score 记录为 `evalue`/`bitscore`，比对坐标进入
`query_start`/`query_end`；HMM 与 envelope 坐标保存在 `extra_json`，subject 坐标因
domtblout 不含而保持 NULL。EAV hits 仍受 `max_hits_per_query` 限制，而
`analysis_alignments` 保留全部 domain 行。

```yaml
hmmsearch_pfam_domains:
  description: Annotation proteins against Pfam-A.hmm (per-domain hits)
  entity_type: annotation
  file_role: protein_fasta
  format: fasta
  database: /path/to/Pfam-A.hmm
  database_version: ""
  output_subdir: hmmsearch_pfam_domains
  output_suffix: .hmmsearch.domtblout
  arguments:
    - --domtblout
    - ${output}
    - --cpu
    - ${threads}
    - ${database}
    - ${input}
  result_parser: hmmer_domtblout
  max_hits_per_query: 5
```

### 10.4 `busco_json`

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

### 10.5 `rpsbproc_tabular`

NCBI 的 CDD 流程让 `rpsblast` 输出 ASN.1 归档（`-outfmt 11`），再由 `rpsbproc` 渲染成
表格报告。报告按 `DATA`/`SESSION`/`QUERY`/`DOMAINS`/`ENDDATA` 块组织；`DOMAINS` 块中
每行是一条 domain，共 12 列：

```text
session  query-id  hit-type  PSSM-ID  from  to  E-Value  bitscore  accession  short-name  incomplete  superfamily-PSSM-ID
```

parser 把 domain 行归入其所在的 `QUERY` 块，以 `(session, query-id)` 为键——因为
`Query_N` 编号跨 session 重复；键重复或行格式错误都会报错。`SITES`/`MOTIFS` 块以及
`ENDDATA` 之后的内容被忽略。对每条 domain 行：

- `query_id` 取 QUERY 的 definition line（与输入 FASTA 标题对应），`subject_id` 取
  accession；
- `from`/`to` 成为 `query_start`/`query_end`；e-value 与 bitscore 按数值解析；
- `hit_type`、`pssm_id`、`short_name`、`incomplete`、`superfamily_pssm`、`session` 与
  `rps_query_id` 保存在 `extra_json` 中。

EAV hits 照常按 `max_hits_per_query` 截断，`analysis_alignments` 保留全部 domain 行。
没有任何 domain 的 query 不产生行——下游筛选通过对 `sequences` 表左连接识别"无命中"
（见 [外部分析命令](cli-analysis.md) 中的 `select-sequences`）。该 parser 无需列声明：

```yaml
result_parser: rpsbproc_tabular
max_hits_per_query: 5
```

默认 `tools.yaml` 模板附带完整的 `rpsblast_cdd` recipe，用 `commands` 命令链把两个程序
耦合起来（字段契约见 [Recipe 字段参考](recipe-fields.md)）：

```yaml
tools:
  rpsblast:
    description: NCBI RPS-BLAST against CDD, post-processed by rpsbproc
    executable: rpsblast
    run_method: "conda run --no-capture-output -n blast"
    version_args: ["-version"]
    version_pattern: 'rpsblast:\s*([^\s]+)'
    recipes:
      rpsblast_cdd:
        description: Annotation proteins against CDD via rpsblast + rpsbproc
        entity_type: annotation
        file_role: protein_fasta
        format: fasta
        database: /path/to/cdd/Cdd
        database_version: ""
        output_subdir: rpsblast_cdd
        output_suffix: .rpsbproc.tsv
        commands:
          - arguments:
              - rpsblast
              - -query
              - ${input}
              - -db
              - ${database}
              - -out
              - ${work_dir}/hits.asn
              - -outfmt
              - "11"
              - -evalue
              - "0.001"
              - -num_threads
              - ${threads}
          - arguments:
              - rpsbproc
              - -i
              - ${work_dir}/hits.asn
              - -o
              - ${output}
              - -e
              - "0.001"
              - -m
              - rep
            version_args: [-version]
            version_pattern: 'rpsbproc:\s*([^\s]+)'
        result_parser: rpsbproc_tabular
        max_hits_per_query: 5
```

第 1 步把 ASN.1 归档写入本次运行的 `${work_dir}`，第 2 步把它渲染为 `${output}`；暂存
目录在运行结束后清理。第 1 步继承顶层 `rpsblast` tool 的版本，第 2 步则在 step provenance
中独立记录 `rpsbproc -version` 的结果。rpsbproc 的参数在不同构建间有差异，请按本地安装的
rpsbproc 版本调整第二个命令块。

## BUSCO 示例

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
`${file_id}.busco` 可以避免默认的 `<file_id>.protein_fasta` 名称；`operon` 也会在
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
operon --project . report analysis \
  --analysis busco_autolineage \
  --entity-id ANN_000001
```

### 11.1 显式 lineage recipe 与结果共存

`busco_lineage` 使用声明式运行参数接受 `--lineage_dataset`：

```yaml
busco_lineage:
  description: BUSCO protein mode with an explicitly selected lineage
  entity_type: annotation
  file_role: protein_fasta
  format: fasta
  input_kind: file
  parameters:
    lineage_dataset:
      required: true
      pattern: '[A-Za-z0-9][A-Za-z0-9_.-]*'
  database: resources/busco_downloads
  database_version: odb12
  database_mode: mutable_cache
  output_subdir: busco_lineage
  output_kind: directory
  output_name: ${file_id}.${lineage_dataset}.busco
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
    - --lineage_dataset
    - ${lineage_dataset}
    - -c
    - ${threads}
    - --opt-out-run-stats
    - --tar
  result_parser: busco_json
  result_glob: short_summary.specific.*.json
```

运行示例：

```bash
operon analyze --analysis busco_lineage \
  --entity-id ANN_000001 \
  --param lineage_dataset=fabales_odb12.2
operon analyze --analysis busco_lineage \
  --entity-id ANN_000001 \
  --param lineage_dataset=eudicotyledons_odb12.2
```

两次结果不会“以最新覆盖旧值”，而是分别保存：

```text
analysis/busco_lineage/ANN_000001/FIL_000003.fabales_odb12.2.busco/
analysis/busco_lineage/ANN_000001/FIL_000003.eudicotyledons_odb12.2.busco/
```

它们拥有不同参数指纹、output artifact、`analysis_jobs`/`analysis_results` 行和 QC stage：

```text
analysis:busco_lineage:lineage_dataset=fabales_odb12.2
analysis:busco_lineage:lineage_dataset=eudicotyledons_odb12.2
```

`report analysis --analysis busco_lineage` 展示全部仍为 `completed` 的参数变体；同一精确
参数被 `--force` 重跑时，旧 job 标记为 `superseded`，新 job 成为该变体的有效结果。

`qc_results` 长表是事实来源，能够完整表达共存结果。`qc_results.wide.tsv` 只适合浏览和
探索性统计：同名 metric 必须折叠成一列，因此会显示最近的一个值。正式 QC profile 不应
依赖这种隐式“最新值”，而应使用 `source.qc_stage` 指明要采用 auto-lineage 还是某个
固定-lineage stage。

对覆盖整个绿色植物的研究范围，推荐把 `busco_autolineage` 作为全库统一 QC 输入；固定
lineage recipe 用于某个分类子集的复核、同标尺比较或异常诊断，而不是要求整个项目使用
同一个 lineage。

## 目录输入与输出

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

## 接入新工具

接入新工具时应按最小可运行配置逐步增加字段。按下面顺序通常最容易排错：

1. 先确定一条真实 manifest 输入记录的 `entity_type`、`file_role`、`format`；
2. 配置 tool 启动和版本探测，直到 `tools-check` 成功；
3. 先用 `result_parser: none`，只让工具正确产生一个文件或目录 artifact；
4. 用 `output_name` 明确目录型程序的根目录，确认 `${output}` 与工具实际创建的位置一致；
5. 加入数据库路径和版本策略；
6. 用 `analyze --dry-run --limit 1` 检查完整命令；
7. 对一个小输入实际运行，检查 stdout、stderr、`analysis_jobs` 和输出结构；
8. 最后启用 parser，核对 `report analysis` 与 `report qc`；
9. 再扩大到全部候选实体。

## 常见错误

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

## 发布前检查清单

保存新 recipe 前逐项确认：

- 输入选择字段与 manifest 的实际值完全一致；
- `input_kind` 和 `output_kind` 与文件系统对象类型一致；
- 外部程序最终创建的位置恰好等于 `${output}`；
- 每个 `arguments` list item 就是一个独立 argv；
- 可增长下载目录使用 `mutable_cache`，冻结参考库使用 `reference`；
- tool version、数据库逻辑版本和 parser 已明确；
- `tools-check` 通过；
- `analyze --dry-run --limit 1` 的完整命令符合预期；
- 单个小输入实跑并核对 `report analysis` 与 `report qc` 后，再批量运行。
