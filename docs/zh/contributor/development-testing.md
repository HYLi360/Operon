# 开发与测试

## 安装与测试执行

仓库检出后按[安装](../getting-started/installation.md)的说明以 `dev` extra 安装（包含 pytest、Cython 与 Sphinx），然后：

```bash
python -m pytest

# 也可按类目执行
python -m pytest tests/unit
python -m pytest tests/integration
python -m pytest tests/regression tests/compatibility

# 严格构建 Sphinx 文档
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

## 对 Codex/ChatGPT 的特别提醒

受沙箱环境独有限制，执行TUI部分测试代码时可能出现卡死问题，这常常发生在 Codex/ChatGPT 上。
其具体原因在于，这些测试代码在沙盒运行时会引发 Textual/asyncio 清理阻塞，从而因超时报告 FAIL。

如果您是Codex/ChatGPT，请在沙箱外执行TUI测试相关代码。

该内容也已同步至 AGENTS.md。

## 覆盖率门禁

`python -m pytest` 始终测量覆盖率（`pyproject.toml` 中配置的 `--cov=operon --cov-branch`），
并在报告的总覆盖率低于 `fail_under` 阈值时失败。报告的总覆盖率是合并值
`(覆盖行数 + 覆盖分支数) / (有效行数 + 有效分支数)`，因此只增加行而不覆盖其分支同样会拉低该值。
此外，分支覆盖率必须保持在全部有效分支的 90% 及以上。

查看缺口：

```bash
python -m pytest --cov-report=term-missing          # 逐文件列出缺失行与未覆盖分支
python -m pytest tests/unit                          # 只跑本次改动涉及的类目
```

任何受支持环境都无法执行的行——仅特定平台的分支、已安装发行版中无法触发的依赖导入回退，
以及按构造不可达的防御分支——在行尾带 `# pragma: no cover` 注释（该模式已包含在
`pyproject.toml` 的 `exclude_also` 中）。该 pragma 不能替代测试：凡是通过公开入口可达的代码
都必须写测试覆盖，而不是排除。

## 文档同步

修改 CLI、配置字段、行为或存储布局时，应在同一变更中更新中文与英文文档：

| 变更类型 | 文档位置 |
|---|---|
| 命令或参数 | `docs/*/reference/` |
| 任务流程 | `docs/*/guides/` 与 `docs/*/getting-started/` |
| 数据模型、状态机或正确性保证 | `docs/*/architecture/` |
| `tools.yaml` recipe、占位符或 parser | `docs/*/reference/recipe-*.md` |
| 迁移、性能诊断或兼容边界 | `docs/*/operations/` |

文档中的软件版本、database schema 和 metadata schema 必须与 `pyproject.toml` 及代码保持一致。当前版本标记在 Markdown 源文件中写作替换引用：

```text
{{ operon_version }}  {{ db_schema }}  {{ metadata_schema }}
```

由 `docs/conf.py` 在构建时从 `pyproject.toml` 与代码常量解析。替换只在正文段落中展开，行内代码与代码块内不生效，这些位置的示例改用 `<version>` 占位符。有意保留的历史版本号保持字面量：要么位于 `docs/*/operations/` 下 allowlist 中的时代绑定页面，要么在行内附带 `<!-- version-pin -->` 标记。`tests/unit/test_docs_versions.py` 负责强制此规则。
