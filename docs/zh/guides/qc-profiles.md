# QC profile

## 创建 QC profile

在 `config/profiles/` 下添加 YAML 文件，例如 `phylogenomics_v1.yaml`：

```yaml
kind: qc
version: 1
description: 系统发育基因组学准入规则
applies_to: [assembly]
required:
  - metric: sha256_match
    operator: "=="
    value: 1
    code: SHA256_MISMATCH
  - metric: parseable
    operator: "=="
    value: 1
    code: FORMAT_INVALID
  - metric: busco_complete_percent
    operator: ">="
    value: 90
    code: LOW_BUSCO_COMPLETENESS
  - metric: contamination_percent
    operator: "<="
    value: 3
    code: HIGH_CONTAMINATION
warnings:
  - metric: busco_duplicated_percent
    operator: ">"
    value: 20
    code: HIGH_BUSCO_DUPLICATION
```

支持的运算符：`>=`、`<=`、`>`、`<`、`==`、`!=`、`between`（需 `min`/`max`）、`in`/`not_in`（需 `values`）、`exists`。比较在边界上是闭区间，`in`/`not_in` 把指标值当字符串比较。注意两个 profile 校验缺口：缺 `min`/`max` 的 `between` 规则或缺 `values` 的 `in`/`not_in` 规则不会在加载 profile 时被拒绝——前者在评估时以 Python traceback 形式暴露，后者会静默地对空值集求值。

手工编辑 YAML 文件完全受支持。带审计的替代途径是 TUI 的 Config 界面
（`operon tui`，按键 `6`，QC Profiles 标签页）：结构化表单（description、
`applies_to` 复选框、规则行）会校验组合后的文档、递增 `version`、写回文件，
并记录与 `operon evaluate` 相同的内容寻址快照——内置快照历史与"恢复为新版本"
功能。

运行：

```bash
operon evaluate --profile phylogenomics_v1
operon report decisions --profile phylogenomics_v1
```

每次 evaluate 都会保存 profile 内容快照，并追加 decision 历史。

### 12.1 按分类器指标选择门限：`value_by`

当一个数值指标的合理门限取决于另一个指标时，可用 `value_by`。绿色植物 BUSCO
auto-lineage 是典型场景：整个 Viridiplantae 不适合使用同一个 lineage，也不应要求用户
逐物种查询 taxonomy 后手工选择；先让 BUSCO 自动选择 lineage，再让 profile 根据实际
`busco_lineage_dataset` 选择完整率门限：

```yaml
kind: qc
version: 1
description: BUSCO 6.1.0 / odb12.2 auto-lineage gates for Viridiplantae
applies_to: [annotation]

required:
  - metric: busco_complete_percent
    operator: ">="
    value_by:
      metric: busco_lineage_dataset
      values:
        eudicotyledons_odb12.2: 70
        poales_odb12.2: 80
        fabales_odb12.2: 75
        lamiales_odb12.2: 70
        embryophyta_odb12.2: 70
        liliopsida_odb12.2: 75
        brassicales_odb12.2: 80
        solanales_odb12.2: 75
        malpighiales_odb12.2: 75
        rosaceae_odb12.2: 85
        chlorophyceae_odb12.2: 60
        viridiplantae_odb12.2: 65
        rosales_odb12.2: 90
        trebouxiophyceae_odb12.2: 80
        chlorophyta_odb12.2: 85
      unknown: warning
    source:
      qc_stage: analysis:busco_autolineage
    code: BUSCO_COMPLETENESS_FAIL
    unknown_code: BUSCO_LINEAGE_UNCONFIGURED
```

`value_by.metric` 和被判定的 `metric` 从同一个来源读取。selector 的字符串值命中
`values` 后，所选数值临时成为普通 `value`，再执行原有 operator。

未知 selector 的策略：

| `unknown` | required rule 的行为 |
|---|---|
| `warning` | 不判 required 失败，但产生 warning；适合 BUSCO 新增 lineage |
| `fail` | required 失败 |
| `ignore` | 跳过该规则，decision 不受影响，但会把忽略 code 持久化到 reason_codes（不静默） |

warning rule 主要使用 `warning` 或 `ignore`；其他策略不会把 warning 提升为 required
失败。缺省（不写 `unknown`）按缺少可用门限处理，最终 `NOT_EVALUATED`，避免遇到
未配置类别时静默放行。`ignore` 的缺省 code 为 `{SELECTOR}_IGNORED`，可用
`unknown_code` 覆盖。

### 12.2 用 `source.qc_stage` 固定指标来源

同一实体可以同时拥有 auto-lineage 和多个固定-lineage BUSCO 结果。正式判定不能依赖
“同名指标里最后写入哪一条”，因此规则可显式限定来源：

```yaml
source:
  qc_stage: analysis:busco_autolineage
```

如果该 stage 没有 required metric，结果为缺少指标/`NOT_EVALUATED`；不会回退到其他
stage 的同名结果。固定 lineage 也可以作为 profile 来源，例如：

```yaml
source:
  qc_stage: analysis:busco_lineage:lineage_dataset=fabales_odb12.2
```

### 12.3 内置绿色植物 BUSCO profile

新项目会生成：

```text
config/profiles/annotation_busco_viridiplantae_odb12_v1.yaml
```

它明确绑定 `analysis:busco_autolineage`，包含四类判定：

1. lineage-specific complete 下限：低于下限 `FAIL`；
2. complete 未达到建议 PASS 线：`PASS_WITH_WARNINGS`；
3. fragmented 超过 lineage 经验高位：`BUSCO_FRAGMENTED_HIGH`；
4. duplicated 超过 lineage 经验高位：`BUSCO_DUPLICATION_REVIEW`，只复核、不直接 FAIL。

门限来自 2026-08-27 对 532 个绿色植物 annotation 的 BUSCO 6.1.0/odb12.2 分布分析，
是当前研究集合的经验 profile，不是 BUSCO 官方通用标准。升级 BUSCO/OrthoDB、改变物种
范围或研究用途时，应复制为新的版本化 profile 并重新估计，不能静默修改旧 profile。

运行：

```bash
operon evaluate \
  --profile annotation_busco_viridiplantae_odb12_v1 \
  --entity-type annotation
operon report decisions \
  --profile annotation_busco_viridiplantae_odb12_v1
```

旧项目的 `operon init` 配置不会被自动覆盖；需要从新项目模板复制该 profile，或按本文
示例在原项目 `config/profiles/` 中创建同名版本化 YAML。

## 序列分类 profile

`kind: sequence_classification` 的 profile 给单条序列打标签，而不是对实体做判定。
存储在 `analysis_alignments` 中的比对命中是观测数据，判定由 profile 做出。用
`operon classify-sequences --profile NAME` 运行（见
[命令参考](../reference/cli-analysis.md)）；标签写入 `sequence_labels` 并带完整审计。
所有阈值都写在这份 YAML 里——绝不在代码中。

```yaml
kind: sequence_classification
version: 1
description: 由 CDD 核心命中加 Pfam 补救命中构成的 bHLH 分级
applies_to:
  entity_type: annotation
  file_role: protein_fasta
sources:
  core:
    analysis: rpsbproc_cdd
    # 核心命中：CDD cl00081 或 bhlh/bhlh_* 短名……
    filter:
      - any:
          - {field: subject_id, operator: "==", value: cl00081}
          - {field: short_name, operator: like, value: bhlh}
          - {field: short_name, operator: like, value: bhlh_%}
      # ……但 bhlh-myc_n 短名永远不算核心命中。
      - not: {field: short_name, operator: "==", value: bhlh-myc_n}
    best_by:
      - {field: hit_type, rank: {Specific: 0, Motif: 1, Partial: 2}}
      - {field: incomplete, rank: {"-": 0, NC: 1}}
      - {field: evalue, direction: asc}
      - {field: bitscore, direction: desc}
      - {field: span, direction: desc}
  rescue:
    analysis: hmmsearch_pf00010
    filter:
      - {field: subject_id, operator: "==", value: PF00010}
    best_by:
      - {field: evalue, direction: asc}
rules:
  - label: A
    source: core
    when:
      - {field: hit_type, operator: "==", value: Specific}
      - {field: incomplete, operator: "==", value: "-"}
      - {field: span, operator: ">=", value: 40}
  - label: B
    source: core
    when:
      - {field: span, operator: ">=", value: 30}
      - {field: incomplete, operator: "!=", value: NC}
  - label: R
    source: rescue
    when:
      - {field: i_evalue, operator: "<=", value: 1e-5}
      - {field: span, operator: ">=", value: 30}
  - {label: U, source: core, absent: true}
  - {label: C, default: true}
```

语法：

- `applies_to` 是含 `entity_type` 与 `file_role` 的映射（注意与 `kind: qc`
  profile 的列表形式不同）；它选定目标 manifest 文件。被取代（superseded）与
  有效退役的实体会被排除。
- `sources` 声明规则可引用的命名命中来源。每个来源包含：
  - `analysis`：命中来自哪个 analysis 的 `analysis_alignments` 行；只有每个目标
    文件最新一个 `completed` 作业的行参与判定。
  - `filter`：哪些行算命中（条件列表，按 AND 组合；空列表表示所有行都算）。
  - `best_by`：有序的 best-hit 排序；每个 seqid 幸存的第一行就是规则所见的最佳
    命中。每项是一个 `field` 加 `direction: asc|desc`（默认 `asc`），或一个把
    取值映射为名次的 `rank` 映射（未列出的取值排在所有已列出取值之后；条目级
    `default` 可显式指定它们的名次）。不写 `best_by` 时按 `hit_rank` 升序。
- `rules` 按顺序求值，首条命中生效。每条规则带一个 `label`，再加三者之一：
  非空的 `when` 条件列表（对该来源的最佳命中求值）、`absent: true`（该来源对此
  序列完全没有命中行——如上例中的 U 级），或 `default: true`（兜底；不接受
  `source`/`when`/`absent`）。没有命中任何规则的序列保持无标签。
- 条件——`filter` 与 `when` 共用同一语法——是含 `field` 与 `operator` 的映射：
  `>=`、`<=`、`>`、`<`、`==`、`!=`（数值比较，等值比较有字符串回退）、
  `in`/`not_in`（配 `values` 列表）、`between`（配 `min`/`max`）、`exists`，以及
  `like`（大小写不敏感的 SQL LIKE 模式，`%` 与 `_` 为通配符）。条件也可以是
  `any: [...]`（组内按 OR 组合）或 `not: {...}`（对单个条件取反）；顶层列表始终
  按 AND 组合。
- 字段先解析到比对列（`subject_id`、`hit_rank`、`query_start`、`query_end`、
  `evalue`、`bitscore`、`percent_identity`……），再解析到派生字段 `span`
  （`query_end - query_start + 1`）与 `seqid`（query id 取第一个空白前的部分），
  最后解析到命中行 `extra_json` 的键——`hit_type`、`incomplete`、`short_name`、
  `i_evalue` 等 parser 特有字段。字段缺失时条件永不成立。

以相同 profile 内容与相同输入重跑是空操作；修改 profile 后会重新判定受影响的
序列，并在 `changes` 中逐条审计每次标签变更。
