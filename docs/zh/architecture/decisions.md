# 决策记录

本页索引约束 Operon 及其生态的各类决策：核心不变式、扩展面的边界、插件可以依赖的契约，以及让这些结论保持成立的闸门。它存在的理由与缺陷登记表相同——**对外承诺必须能在版本控制里被查到，而不是只在一份计划里被论证过**。

由此有两条规则：

- **承诺写在 `docs/` 里。** 任何其他项目、插件、发行版或运维方可能依赖的表述——契约、不变式、版本政策、兼容窗口、交互契约——**必须**写在本文档树中。计划草稿、里程碑记录与推演过程放在仓库之外；它们对维护者有用，但**不是**对任何外部读者的依据。
- **每条记录都要写明它的执法者。** 有测试、闸门或 CLI 表面的，记录直接指向它；没有执法机制的，记录要如实说明；仍属提议的，必须标注为提议。

记录按领域分组。本页不覆盖 `AGENTS.md`；后者把同样的不变式表述为项目要求。

## 核心不变式

| 编号 | 决策 | 执法者 |
|------|------|--------|
| INV-1 | `operon.sqlite` 中的结构化元数据是唯一真相；大型序列文件从不进入数据库。 | schema 校验，`operon/schema.py` |
| INV-2 | 原始数据不可变；派生数据可重建。 | 字节不一致即 `ConflictError`，`operon/files.py` |
| INV-3 | 文件身份是 `file_id + sha256 + size_bytes`，永远不是路径。 | 清单归档与校验，`operon/files.py` |
| INV-4 | QC 工具只测量指标；判定只来自版本化的 YAML profile。 | `operon/profiles.py`、`operon/rules.py` |
| INV-5 | 每一步处理都是显式、幂等的状态机迁移，并留下机器可读的 provenance。 | `operon/workflow.py`，`changes`/`workflow_runs` |

## 扩展边界

| 编号 | 决策 | 状态 | 出处 |
|------|------|------|------|
| EXT-1 | 分析插件是**独立发行的 CLI**，经由配方（`operon analyze`）或 `operon run-external` 以子进程方式调起。Operon 不是它的运行时依赖，插件也不得 import Operon。 | 生效 | [扩展边界](extensibility.md)、[外部分析执行模型](external-analysis.md) |
| EXT-2 | 为什么不做进程内钩子：执行从设计上就是"子进程 + 跨后端"（local / Slurm / SSH）并共用一套 provenance 契约；进程内钩子只能在控制机本机运行，会与 provenance、缓存指纹与环境比对脱钩。entry-point 钩子是**暂缓**，不是否定。 | 生效 | [扩展边界](extensibility.md) |
| EXT-3 | entry-point / `operon.api` 设计**暂缓**。立项判据：≥2 个外部插件确有进程内需求，或需要自定义 result parser / executor 后端。 | 暂缓 | 本页 |
| EXT-4 | 插件可以额外产出事件 JSONL，用 `operon import-events` 回载：`metric` 事件进 QC 结果，`artifact` 事件成为 `adopt` 清单草稿。未知事件类型跳过并计数；未知 `schema_version` 报错且不写盘。 | 待实现 | 本页 |
| EXT-5 | 事件承载的是**事实，而非判定**：插件可以报告它测量到或产出了什么，永远不能报告状态、决策或 QC 结论——那些只来自版本化 profile（见 INV-4）。 | 生效（由 INV-4 推出） | 本页 |
| EXT-6 | 可视化插件只读 `report view` bundle（带 `bundle_schema_version` 与各成员校验和的只读目录），永不写项目数据库。 | 生效 | [插件契约](../reference/plugin-contract.md)；`tests/regression/test_view_bundle.py` |
| EXT-7 | 插件发行版继承 Operon 当前的开发者与许可证；发行名与托管位置待定。 | 生效 | 本页 |

## 契约与闸门

| 编号 | 决策 | 执法者 |
|------|------|--------|
| GATE-1 | CLI 先行：任何能力先落在 CLI/核心，TUI 永不走在前头。parity 注册表 `operon/tui/parity.py` 把每条命令登记为 `implemented`、`cli-only`（附理由）或 `planned`（附里程碑）。 | `tests/unit/test_tui_cli_parity.py`；CI 导出 `OPERON_PARITY_STRICT=1`，再次出现的 `planned` 条目会直接让构建失败 |
| GATE-2 | TUI 的每次写入都是"预览 → 等价 CLI 命令 → 显式确认"，调用与 CLI 相同的核心函数，并留下相同的 `changes`/`workflow_runs` provenance。 | `operon/tui/actions.py`、TUI 屏幕测试 |
| GATE-3 | 手动覆盖（`curate`、强制 `set-state`）一律记入 `changes` 审计表。 | `operon/lifecycle.py`、审计测试 |
| GATE-4 | 覆盖率与分支覆盖率门禁、Codacy 政策与贡献流程。 | [开发与测试](../contributor/development-testing.md) |
| GATE-5 | 版本号有单一来源（`pyproject.toml`、`operon/database.py`、`operon/schema.py`），且**不得**在文档里写字面值——使用 `{{ operon_version }}`、`{{ db_schema }}`、`{{ metadata_schema }}` 替换引用。 | `tests/unit/test_docs_versions.py` |
| GATE-6 | 缺陷先登记再修复，一次提交修一个缺陷，且每条 `fixed`/`verified` 记录至少列出一条带 `@pytest.mark.bug("ODR-XXXX")` 的回归测试。 | `scripts/defects.sh`、`tests/unit/test_defect_registry.py` |
| GATE-7 | TUI 与机读出口的性能预算。 | [性能预算](../operations/performance-budgets.md)——政策已定，基线待测 |
| GATE-8 | TUI 改动不得引入 Textual 竞态缺陷：断言 UI 状态前必须谓词等待；框架级 guard 的修理走最小覆盖；改动后本地以随机序至少跑 3 次 TUI 测试模块。 | [开发与测试](../contributor/development-testing.md)；缺陷 ODR-0027、ODR-0050、ODR-0051、ODR-0055、ODR-0056、ODR-0057 |

## 开放问题

记录在此的目的是让它们**在 `docs/` 里被回答**，而不是只在计划草稿里。在这些行被提升为决策之前，外部消费者不得依赖其中任何一种结果。

- **第三方只读访问 SQLite**——是否承诺一份文档化的只读表/列面（bundle 之外），以及若承诺，schema 抬升时配套多长的迁移窗口。
- **bundle 演进政策**——`bundle_schema_version` 的兼容承诺细则（现行假设：只增字段；破坏性变更抬主版本并附迁移说明）。
- **事件 schema 的归属**——事件 schema 内嵌在插件契约中，还是单独成文。

## 第二阶段文件契约

[插件契约](../reference/plugin-contract.md)明确版本拒绝、bundle 演进、hits 导入和预设冲突。执法者随第二阶段命令与契约测试落地；EXT-4、EXT-6 在命令落地前保持 planned。事件 Schema 作为独立包资源发布，直读 SQLite 兼容仍开放。
