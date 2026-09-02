# NCBI Taxonomy 覆盖率

本页说明如何以显式版本的 NCBI Taxonomy 为分母，审计当前元数据或不可变 release 的科/属覆盖率。

```text
NCBI taxonomy_report.jsonl / Datasets package / taxdump archive
        │ operon taxonomy import --version <显式版本>
        ▼
不可变原包 + taxonomy_snapshots/nodes/aliases
        │
coverage YAML profile ── operon taxonomy compile
        │                         │
        │                         ▼
        │             taxonomy/reference_sets/
        │             <profile>@<taxonomy_version>.tsv
        │                         │
        └─────────────────────────┤
                                  ▼
                      operon report coverage
                         ├── metadata 口径
                         └── release 冻结口径
```

导入、编译和报告都是显式命令。导入原件进入文件 manifest，按
`file_id + sha256 + size_bytes` 识别；编译动作进入 `changes` 审计表，完整运行信息进入
`workflow_runs` 与 `logs/workflow.jsonl`。

## 准备 coverage profile

`operon init` 会在 `config/profiles/coverage_viridiplantae_v1.yaml` 生成一个示例。
它是模板而不是适用于所有研究的通用标准；正式使用前应核对 clade、排除规则和阈值，
并用反映研究口径的新文件名保存。QC 与 coverage profile 共用目录，但由必填的
`kind` 字段严格区分。

```yaml
kind: taxonomy_coverage
version: 1
name: coverage_viridiplantae_v1
description: NCBI Viridiplantae family/genus coverage

taxonomy:
  source: NCBI

scope:
  root_taxids: [33090]       # 可声明一个或多个 clade 根

targets:
  ranks: [family, genus]     # 当前只支持 family / genus，可只选其一

filters:
  exclude_extinct: true
  exclude_subtrees: []       # TaxID 及其全部后代从分母、分子中排除
  exclude_name_patterns:
    - '(?i)^unclassified(?:\s|$)'
    - '(?i)environmental samples$'

thresholds:
  family:
    min_coverage_percent: 80
  genus:
    min_coverage_percent: 80
```

约束如下：

- `taxonomy.source` 必须为 `NCBI`；
- `root_taxids` 和 `exclude_subtrees` 必须是整数 TaxID；
- `targets.ranks` 只能是互不重复的 `family`、`genus`；
- `thresholds` 必须与目标层级完全对应，数值范围为 0–100；
- 名称排除项是 Python 正则表达式，profile 编译时会先校验；
- 阈值只从 YAML 读取，代码中没有 family/genus 覆盖率默认门槛。

`exclude_extinct: true` 需要 taxonomy 快照对所有节点提供明确的 extinct 布尔语义。
NCBI Datasets taxonomy JSON 中省略的布尔值按其 schema 默认值 `false` 处理；传统
taxdump 的 `nodes.dmp` 没有 extinct 字段，因此使用 taxdump 时 compile 会拒绝假装
已经执行该规则。此时必须显式改为 `false`，并用 `exclude_subtrees`/名称正则表达式
声明项目认可的化石排除口径，或改用包含 extinct 标注的 Datasets taxonomy report。

排除规则同时作用于编译后的分母和样本 TaxID 的谱系投影，避免一个被明确排除的
environmental、unclassified 或 extinct 观察反过来覆盖其上级目标。

## 导入 NCBI Taxonomy

```bash
operon taxonomy import \
  --input /data/ncbi_taxonomy/2026-08-01/taxonomy_report.jsonl \
  --version 2026-08-01

operon taxonomy list
```

`--input` 接受 NCBI Datasets 的 `taxonomy_report.jsonl`、包含该成员的 ZIP/tar，或
官方 taxdump ZIP/tar（至少包含 `nodes.dmp` 与 `names.dmp`；若存在
`merged.dmp`/`delnodes.dmp` 也会导入 current/deleted TaxID 映射）。`--version` 是
项目采用的不可变版本标签，必须显式给出；建议使用 NCBI
下载日期或发布标识，不要写 `latest`。

导入后：

- 原始输入按 SHA-256 保存在 `raw/metadata/ncbi_taxonomy/`；
- `files` 中记录 `taxonomy_snapshot` / `taxonomy_package` manifest 行；
- `taxonomy_snapshots` 记录来源、版本、SHA-256、大小、节点数和状态；
- `taxonomy_nodes` 保存 TaxID、父节点、rank、学名和 extinct 标志；
- `taxonomy_aliases` 保存 secondary/merged TaxID 到 current TaxID 的映射。

同一 `--version` 与相同字节重复导入会复用已有快照；同一版本标签对应不同字节会以
冲突退出，不能静默改写历史 taxonomy 身份。

## 编译不可变分母

```bash
operon taxonomy compile \
  --profile coverage_viridiplantae_v1 \
  --taxonomy-version 2026-08-01

operon taxonomy reference-sets
```

输出身份为 `<profile文件名>@<taxonomy_version>`，例如：

```text
taxonomy/reference_sets/
├── coverage_viridiplantae_v1@2026-08-01.tsv
└── coverage_viridiplantae_v1@2026-08-01.provenance.json
```

TSV 每行是一个应覆盖的分类单元，固定三列：

```text
rank    taxid    scientific_name
family  12345    Exampleaceae
genus   67890    Examplea
```

行按 `family`、`genus`、TaxID 确定性排序。数据库与 provenance sidecar 记录 taxonomy
版本及原包 SHA-256、profile 完整文档及 SHA-256、TSV SHA-256/大小、每个 rank 的行数、
编译器版本和 workflow run ID。

相同 profile、taxonomy 和结果重复执行是幂等复用，不新增审计记录。已存在的
`<profile>@<taxonomy_version>` 若 profile 内容、taxonomy 快照或 TSV 字节不同则拒绝
覆盖。要改变分母，必须新建 profile 名称（通常升为 `_v2`）或导入新的 taxonomy
版本，再显式执行 compile；新编译动作会写入 `changes`。

## 生成覆盖率报告

### 5.1 metadata 口径（默认）

```bash
operon report coverage \
  --reference-set coverage_viridiplantae_v1@2026-08-01
```

该口径直接审计 `organisms` 表，回答“这个库登记采样了什么”。只有
`taxonomy_source=NCBI` 且具有可解析 TaxID 的 organism 进入谱系投影；secondary TaxID
会按冻结 taxonomy 的 alias 表映射到 current TaxID。GTDB、缺失 TaxID、在快照中未知或
被 profile 排除的观察会进入排除清单，不计入分子。

### 5.2 release 口径

```bash
operon report coverage \
  --reference-set coverage_viridiplantae_v1@2026-08-01 \
  --release 2026.08
```

该口径从 `release_members` 确认成员身份和文件 checksum，再读取 release 目录中冻结的
`organisms.tsv`、`samples.tsv`、`runs.tsv`、`assemblies.tsv`、`annotations.tsv`，沿成员
实体关系回溯到 organism。它回答“这个已发布数据集覆盖了什么”。报告不读取当前活动
库中的 organism TaxID 来替换 release 快照，因此 release 创建后修改元数据不会改变
该 release 的历史覆盖率。release 创建时保存的 metadata SHA-256 也会在计算前复核；
快照被改动、缺失或无法与成员关系对应时会拒绝报告，而不会把篡改后的内容视作同一
release 的新口径。早于这一 checksum 契约创建的开发期 release 需要重新创建后才能
用于 release-scope coverage。

### 5.3 公式与输出

每个目标 rank 独立计算：

```text
numerator   = 至少被一个纳入范围 organism 覆盖的快照 TaxID 数
denominator = 分母快照中该 rank 的 TaxID 数
coverage    = numerator / denominator × 100%
```

同一科/属采样多个 organism 仍只给该目标计数一次；`organism_count` 作为诊断信息另行
输出。报告目录由全部输入身份的 SHA-256 决定：

```text
reports/coverage/COV_<input-hash>/
├── coverage_summary.tsv                # 各 rank 分子、分母、百分比、阈值、PASS/FAIL
├── coverage_targets.tsv                # 完整目标及 COVERED/MISSING、organism_count
├── coverage_missing.tsv                # 后续采样用的缺失清单
├── coverage_observations.tsv           # 纳入观察、alias 映射和 family/genus 投影
├── coverage_excluded_observations.tsv  # 未纳入观察及原因码
└── provenance.json                     # 全部冻结身份、算法版本和结果摘要
```

所有配置 rank 达到 YAML 阈值时退出码为 0；报告已成功生成但至少一个 rank 未达标时
退出码为 1；配置、身份、checksum、冲突等领域错误返回 2。未达标不是计算失败，报告
仍会完整落盘并进入 `coverage_reports` / `coverage_report_metrics` 历史。

输入身份包括 reference-set SHA-256、profile SHA-256、口径、release 版本（若有）和
范围成员哈希。完全相同的输入重复报告会校验既有文件并复用；活动 metadata 或 release
成员身份改变后会产生新的报告 ID，而不是覆盖旧报告。

## 解释缺失与排除清单

`coverage_missing.tsv` 是“分母中没有任何合格观察覆盖的目标”，适合直接转成后续采样
候选。它与 `coverage_excluded_observations.tsv` 不同：后者是库中存在、但因 TaxID 或
口径问题未进入分子的 organism，例如：

- `UNSUPPORTED_TAXONOMY_SOURCE`：GTDB 或其他非 NCBI 来源；
- `MISSING_TAXID`：没有 TaxID；
- taxonomy 快照中没有该 TaxID，也没有可用 secondary alias；
- `EXCLUDED_EXTINCT`、名称正则或 excluded subtree 命中；
- `MISSING_TARGET_RANK`：谱系中没有 profile 所要求的 family/genus 层级；
- `OUTSIDE_REFERENCE_SCOPE`：有相应 rank，但 TaxID 不在冻结 reference set 中。

报告不会用 scientific name 猜 TaxID。应修复 `organisms` 元数据或换用包含正确 alias
的 taxonomy 快照，再生成一个新身份的报告。

## 已知局限

- 当前只支持 NCBI Taxonomy，不支持 GTDB，也不提供 NCBI↔GTDB 自动 crosswalk；
- taxonomy 数据由用户显式取得并导入，`operon` 不隐式下载或跟随 `latest`；
- 传统 taxdump 不携带 extinct 布尔标注；为防止排除规则被静默弱化，
  `exclude_extinct: true` 与这种快照组合会在 compile 时被拒绝；
- family/genus 覆盖衡量的是采样广度，不代表组装质量、注释完整度、系统发育代表性或
  各 clade 的均衡性；这些问题仍需 QC profile 和专门分析；
- NCBI rank、名称、merged TaxID 与 extinct 标注会随版本变化，版本间数字不能在没有
  说明分母快照的情况下直接比较；
- release 口径只统计 release 成员可沿冻结元数据关系追溯到的 organism；不完整关系会
  明确排除，不会按文件名或名称推断。
