# 开发与测试

## 开发与测试

```bash
python -m pip install -e '.[dev]'
python -m pytest

# 也可按类目执行
python -m pytest tests/unit
python -m pytest tests/integration
python -m pytest tests/regression tests/compatibility

# 严格构建 Sphinx 文档
python -m pip install -e '.[docs]'
sphinx-build -W --keep-going -b html docs docs/_build/html
```

pytest 测试按 `unit`、`integration`、`regression`、`compatibility` 四类组织，覆盖：
Python 3.10 语法与运行时门禁、schema 校验与受控词汇、metadata round-trip 与事务
回滚、稳定 ID、默认副本隔离、query 只读约束、file-aware QC 身份、profile/decision
历史、gzip FASTA 识别、assembly/annotation QC、规则判定、幂等 ingest 与冲突保护、
checksum 篡改检测、demo 端到端流水线与 release 校验、NCBI Datasets adapter、
BLAST/HMMER/BUSCO 封装执行、目录 artifact、JSON summary、conda run 前缀解析、
缓存命中/强制重跑、结果回写与输入篡改拒绝。
taxonomy coverage 集成测试还覆盖 taxonomy 原包身份冲突、profile 类型/内容冲突、
排除规则、secondary TaxID、分母/报告幂等，以及活动 metadata 修改不影响 release
冻结口径。

## 文档同步

修改 CLI、配置字段、行为或存储布局时，应在同一变更中更新中文与英文文档：

| 变更类型 | 文档位置 |
|---|---|
| 命令或参数 | `docs/*/reference/` |
| 任务流程 | `docs/*/guides/` 与 `docs/*/getting-started/` |
| 数据模型、状态机或正确性保证 | `docs/*/architecture/` |
| `tools.yaml` recipe、占位符或 parser | `docs/*/reference/recipe-*.md` |
| 迁移、性能诊断或兼容边界 | `docs/*/operations/` |

文档中的软件版本、database schema 和 metadata schema 必须与 `pyproject.toml` 及代码保持一致。
