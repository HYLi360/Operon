# 系统发育分析流程

本指南以基因超家族分析为例，演示基于 Operon 项目的完整系统发育研究闭环：先构建超家族框架树，再参照参考序列做亚族划分并精修各亚族树，随后用 OrthoFinder/ASTRAL 推断物种树，用 TreeShrink 筛查异常序列，最后用 Notung 做基因树-物种树协调。

## 分工边界

`operon` 刻意不做工作流引擎。多步编排——依赖图、并行、重试——交给 snakemake/nextflow 这类工作流管理器（小规模研究也可以逐步调用 `run-external`，同样能记录 provenance）。`operon` 在闭环中负责的是数据准入与发布：

1. **导出**：把选定序列以带校验和的输入包形式实体化出项目；
2. **外部计算**：工作流管理器消费该输入包，在项目外产出进化树、比对和报告；
3. **收养**：把关键中间产物与最终结果以一等 manifest 文件身份回库，并记录 `file_lineage` 谱系边；
4. **发布**：对收养产物做评估，冻结进不可变 release。

这正是 [扩展边界](../architecture/extensibility.md) 所述的 export/adopt 契约；下游工作流应通过该契约读写，而不是直接读写数据库。

## 第一步：导出选定序列

`export` 按实体、角色、格式、状态或 QC 判定选择 manifest 文件，逐文件对照 manifest 验证 SHA-256，并实体化到 `data/<entity_type>/<entity_id>/` 目录布局下，同时生成 `manifest.tsv`、`qc.tsv`、`checksums.sha256` 和 `provenance.json`。只导出当前通过某 QC profile 的 annotation 蛋白 FASTA：

```bash
operon export --output analysis/external/superfamily_round1 \
  --entity-type annotation \
  --file-role protein_fasta \
  --decision PASS --profile default
```

`--decision` 需搭配 `--profile`，匹配 `current_decisions` 中的有效判定（PASS、PASS_WITH_WARNINGS 等），因此进入工作流的输入恰好是 QC 放行的子集。全部选择条件见 [export 参考](../reference/cli-decisions-reports.md)。

## 第二步：运行外部流程

把工作流管理器指向导出包——`manifest.tsv` 就是机器可读的输入清单。典型的 snakemake 配置读取该清单，依次执行超家族框架树构建、参考引导的亚族划分、各亚族精细树、OrthoFinder/ASTRAL 物种树推断、TreeShrink 筛查和 Notung 协调，产物统一写到某个工作目录（如 `analysis/external/superfamily_round1/results/`）。

对于单个长时步骤，也可以不借助工作流管理器而直接记录 provenance：

```bash
operon run-external --step astral_species_tree \
  --tool astral \
  --input analysis/external/superfamily_round1/results/gene_trees.tre \
  --expected-output analysis/external/superfamily_round1/results/astral.species_tree.nwk \
  --command 'astral -i analysis/external/superfamily_round1/results/gene_trees.tre -o analysis/external/superfamily_round1/results/astral.species_tree.nwk'
```

`run-external` 在执行前会打印新的 run ID、两个日志路径和 `watch: operon workflow show <run_id> --follow` 提示；`--follow` 可实时流式查看日志，退出码、资源用量和 expected-output 校验照常落入 `workflow_runs`。

## 第三步：把中间产物与结果收养回库

流程结束后，用批量 adopt 清单把值得保留的产物——关键中间产物（框架树、亚族指派表）与最终结果（物种树、TreeShrink 输出、Notung 协调结果）——重新注册回数据库。TSV 形式需要包含 `path`、`entity_type`、`entity_id`、`role`、`derived_from` 的表头行；`format`、`compression` 与 `workflow_run_id` 列可选（省略时自动探测）。`derived_from` 填写逗号分隔的、必须已注册的 file_id；相对路径按项目根目录解析：

```text
path	entity_type	entity_id	role	format	compression	derived_from
analysis/external/superfamily_round1/results/framework.treefile	organism	ORG_000001	superfamily_framework_tree	other	none	FIL_000012
analysis/external/superfamily_round1/results/subfamily_assignments.tsv	organism	ORG_000001	subfamily_assignments	other	none	FIL_000021
analysis/external/superfamily_round1/results/astral.species_tree.nwk	organism	ORG_000001	species_tree	other	none	FIL_000021
analysis/external/superfamily_round1/results/treeshrink/	organism	ORG_000001	treeshrink_output	directory	none	FIL_000021
analysis/external/superfamily_round1/results/notung/	organism	ORG_000001	notung_reconciliation	directory	none	FIL_000021,FIL_000022
```

```bash
operon adopt --from-manifest adopt_manifest.tsv
```

role 由工作流自由命名。每个产物实体化到 `analysis/adopted/<entity_id>/` 下（树文件通常探测为 `other`，比对为 `fasta`；目录产物按 `format: directory`、`compression: none` 收养），继承 ingest 的幂等/冲突不变量，并写入指回输入的 `file_lineage` 谱系边。整批在同一事务中提交；失败时只删除本批新建的产物。各亚族序列集这类数据决定数量的单元**不**在这里逐条手写收养——下一步改用 fanout 从已收养的指派表扇出。

## 第四步：扇出数据决定的单元，再做级联分析

亚族划分是整条流程中形状由数据决定的环节：只有外部 assign-subfamilies 步骤读完已收养的框架树之后，亚族数量才知道。不要逐亚族手写 adopt 清单条目。让外部步骤产出干净的两列指派 TSV（`unit`、`seqid`——诸如丢弃未解析序列这类项目专属的过滤在那里完成），按第三步收养该 TSV，然后用 `operon fanout` 准入这些单元：

```bash
# 先看计划：不写文件，也不写运行记录。
operon fanout --assignments-file FIL_000022 \
  --source-file FIL_000012 \
  --entity-type organism --entity-id ORG_000001 \
  --role-prefix subfamily_alignment --dry-run

operon fanout --assignments-file FIL_000022 \
  --source-file FIL_000012 \
  --entity-type organism --entity-id ORG_000001 \
  --role-prefix subfamily_alignment
```

`fanout` 把每个被指派的 seqid 对声明的 `--source-file` 的 `sequences` 注册表解析（解析不到的 seqid 是指名道姓的硬错误；一个 seqid 出现在多个来源中需收窄 `--source-file` 消歧），随后逐单元物化一个 FASTA 到 `analysis/derived/ORG_000001/` 下，并以 `subfamily_alignment:SF01`、`subfamily_alignment:SF02`…… 的 role 注册，同时写入指回源 FASTA 与指派 TSV 的 `file_lineage` 谱系边。输入不变时重跑是空操作（`reused`）；同一单元 role 下字节不同则抛 `ConflictError`。见 [fanout 参考](../reference/cli-decisions-reports.md#fanout)。

由于单元数量由数据决定，下游 recipe 声明 role 前缀而不是精确 role：

```yaml
# config/tools.yaml
tools:
  iqtree:
    executable: iqtree2
    version_args: ["--version"]
    version_pattern: '([0-9][^\s]*)'
    recipes:
      iqtree_subfamily:
        entity_type: organism
        file_role_prefix: "subfamily_alignment:"
        format: fasta
        output_suffix: .treefile
        arguments: [-s, ${input}, -T, ${threads}, --prefix, ${output_stem}]
        result_parser: none
```

完整字段契约见 [Recipe 字段参考](../reference/recipe-fields.md)；随后：

```bash
operon analyze --analysis iqtree_subfamily
```

`analyze` 对每个单元文件跑一个作业，串行或经配置的后端执行；缓存、收养与审计与任何其他输入完全一致。再往上的编排——重试、并行，以及针对单元产物的 TreeShrink/Notung 批处理——照旧归工作流管理器。

收养与扇出的文件在其他方面都是普通 manifest 成员：带精确 role 的收养文件仍按 `entity_type + file_role + format` 被选中，FASTA 可以用 `operon qc --file-id FIL_...` 重新 QC，结果与针对原始文件的分析一样流入 `analysis_results`/`analysis_alignments`/`qc_results`，因此每一轮流程都可以建立在上一轮的产物之上。

## 第五步：评估并发布

收养产物参与标准质量门槛。对有内置解析器的格式运行 `operon qc`（外部指标则走 `run-external` + `import-qc`），随后 `operon evaluate --profile <name>`，最后：

```bash
operon release --version v1.0 --profile default
```

只有有效判定放行的实体进入 release，其余写入 `exclusions.tsv`。由于收养的中间产物携带谱系边，release 始终可以回溯解释到最初的原始输入。

## 第六步：用 SQL 审计谱系

`file_lineage` 记录 `derived_file_id -> input_file_id` 谱系边，谱系图可直接查询：

```bash
# 收养的物种树由哪些输入产生？
operon query "SELECT l.input_file_id, f.file_role, f.entity_id
              FROM file_lineage l JOIN files f ON f.file_id = l.input_file_id
              WHERE l.derived_file_id = 'FIL_000021'"

# 从某个导出的蛋白 FASTA 派生出了哪些文件：
operon query "SELECT l.derived_file_id, f.file_role
              FROM file_lineage l JOIN files f ON f.file_id = l.derived_file_id
              WHERE l.input_file_id = 'FIL_000012'"
```

## 第七步：定位序列与比对命中

新增的两张表让序列级问题不必重新解析文件即可回答：

- `sequences` 为每条 FASTA 记录保存一行（`file_id`、`entity_type`、`entity_id`、`seqid`、`length`），由内置 QC 自动填充。查询某个 seqid 属于哪个 assembly：

```bash
operon query "SELECT entity_type, entity_id, file_id, length
              FROM sequences WHERE seqid = 'NW_012345678.1'"

# 或者让 show 在 ID 不是实体时回退到 sequences 表：
operon show NW_012345678.1
```

- `analysis_alignments` 保存已完成作业解析出的全部比对命中——query/subject ID、排名、query/subject 区间、e-value、bitscore、identity 百分比——不受 `max_hits_per_query` 截断。列出结构域扫描中某 query 的全部命中区间：

```bash
operon query "SELECT subject_id, hit_rank, query_start, query_end, evalue, bitscore
              FROM analysis_alignments
              WHERE analysis_name = 'hmmsearch_pfam_domains'
                AND query_id = 'PF00046'
              ORDER BY hit_rank"
```

不写 SQL 也可以通过 `operon report analysis --hits --query-id PF00046` 查看同样的行，并用 `--format tsv|json` 与 `--out` 供下游消费。

## 域聚焦变体：扫描、筛选、提取、定年

以域为中心的研究——例如由单个 CDD/Pfam 结构域定义的基因超家族——可以把前几轮留在
项目内部完成，再交给工作流管理器：

1. **扫描**：对蛋白 FASTA 运行 `rpsblast_cdd` recipe（rpsblast + rpsbproc 命令链，由
   `rpsbproc_tabular` 解析）；每条 domain 行带着 hit type、accession 与 short name 进入
   `analysis_alignments`。
2. **筛选**：`operon select-sequences --analysis rpsblast_cdd --hit-type Specific --evalue-max 1e-5 ...`
   把每个蛋白组划分为含域与不含域两个子集。完整性用 e-value 阈值控制，绝不用 max hits
   截断——被截断的命中列表会静默破坏"无命中"补集的判定（见 [外部分析](external-analysis.md)）。
3. **提取**：`operon extract-domains --analysis rpsblast_cdd --flank 5 --min-length 30 ...`
   写出带侧翼的域 FASTA 与逐区间 manifest；adopt 两个产物，后续 recipe 与 export 才能
   选它们。
4. **外部比对与建树**：adopt 后的域 FASTA 按上文第二、三步的方式交给外部比对与建树
   流程。建树前可在任意机器上用 `operon alignment-qc --alignment ... --outdir ...`
   度量比对（逐序列与逐列指标），再把比对与树 adopt 回库。
5. **物种树定年**：`operon timetree calibrations --taxa A,B,C --out calibrations.tsv`
   从 TimeTree 编制 MCMCTree 标定先验（每次查询都会打印必须引用的 Kumar et al. 2022
   文献）；把 TSV 与定年输入一起 adopt。更严格的审阅约束流程用
   `operon timetree fetch` 把逐对原始证据冻结为不可变快照，再用
   `operon timetree calibrate` 只把经批准、带理由的软界标定编译到有根树上。见
   [timetree](../reference/cli-analysis.md#timetree)。
