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

支持的运算符：`>=`、`<=`、`>`、`<`、`==`、`!=`、`between`（需 `min`/`max`）、`in`/`not_in`（需 `values`）、`exists`。

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
