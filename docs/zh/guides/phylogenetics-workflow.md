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

流程结束后，用批量 adopt 清单把值得保留的产物——关键中间产物（框架树、各亚族比对）与最终结果（物种树、TreeShrink 输出、Notung 协调结果）——重新注册回数据库。TSV 形式需要包含 `path`、`entity_type`、`entity_id`、`role`、`derived_from` 的表头行；`format`、`compression` 与 `workflow_run_id` 列可选（省略时自动探测）。`derived_from` 填写逗号分隔的、必须已注册的 file_id；相对路径按项目根目录解析：

```text
path	entity_type	entity_id	role	format	compression	derived_from
analysis/external/superfamily_round1/results/framework.treefile	organism	ORG_000001	superfamily_framework_tree	other	none	FIL_000012
analysis/external/superfamily_round1/results/subfamilies/OG000102.aln.faa	organism	ORG_000001	subfamily_alignment	fasta	none	FIL_000012,FIL_000013
analysis/external/superfamily_round1/results/astral.species_tree.nwk	organism	ORG_000001	species_tree	other	none	FIL_000021
analysis/external/superfamily_round1/results/treeshrink/	organism	ORG_000001	treeshrink_output	directory	none	FIL_000021
analysis/external/superfamily_round1/results/notung/	organism	ORG_000001	notung_reconciliation	directory	none	FIL_000021,FIL_000022
```

```bash
operon adopt --from-manifest adopt_manifest.tsv
```

role 由工作流自由命名。每个产物实体化到 `analysis/adopted/<entity_id>/` 下（树文件通常探测为 `other`，比对为 `fasta`；目录产物按 `format: directory`、`compression: none` 收养），继承 ingest 的幂等/冲突不变量，并写入指回输入的 `file_lineage` 谱系边。整批在同一事务中提交；失败时只删除本批新建的产物。

## 第四步：以收养产物为输入做级联分析

收养后的文件是普通 manifest 成员，因此 `analyze` recipe 可以通过 `entity_type + file_role + format` 把它选为输入。例如用一个声明了 `entity_type: organism`、`file_role: subfamily_alignment`、`format: fasta` 的 recipe 对所有收养来的亚族比对做结构域扫描，或对收养的 FASTA 直接 `operon qc --file-id FIL_...`。结果与针对原始文件的分析一样流入 `analysis_results`/`analysis_alignments`/`qc_results`，因此每一轮流程都可以建立在上一轮收养产物之上。

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
