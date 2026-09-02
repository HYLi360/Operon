# 仓库协作规范

## 项目原则

1. SQLite 中的结构化元数据是唯一事实来源。
2. 原始数据不可变，派生数据必须可重建。
3. 文件身份为 `file_id + sha256 + size_bytes`，路径不构成身份。
4. QC 代码只计算指标；阈值保存在 `config/profiles/` 下的版本化 YAML profile 中。
5. 所有处理必须以显式、幂等的状态机运行，并记录机器可读 provenance。

## 仓库结构

- `operon/`：Python 包；CLI 入口为 `operon/cli.py` 和 `operon/__main__.py`。
- `operon/adapters/`：外部来源适配器，目前包括 NCBI Datasets。
- `operon/qc_module/`：流式解析器和内置 QC。`parsers.py` 是纯 Python 行为参考；`_parsers.pyx` 是生产使用的 Cython 实现。两者必须通过 parity 回归测试保持指标和错误文本一致。
- `operon/execution.py`：`local`、`slurm` 和 `ssh` 执行后端。
- `operon/remotes.py`：SFTP 镜像、push/pull 和远程 URL 下载。
- `tests/`：`unit/`、`integration/`、`regression/`、`compatibility/` 测试。
- `docs/`：用户、架构和运维文档。
- `build/release/v<version>/`：生成的独立应用发布目录。

## 协作约定

- 以 `pyproject.toml` 为唯一依赖事实来源。新增运行时依赖必须获得明确授权，并放入范围最小的 optional extra。
- 代码、注释、docstring 和提交信息使用英文；用户文档同时维护中文和英文版本。
- 标题使用 `Operon`；正文中的命令行工具写作 `` `operon` ``。
- 不得在 QC 代码中硬编码阈值。
- 不得静默覆盖归档文件；相同字节幂等，同实体同角色不同字节必须抛出 `ConflictError`。
- `curate`、强制 `set-state` 等人工修改必须写入 `changes`。
- 修改数据库迁移或 NCBI adapter schema 升级路径前，先阅读[数据库兼容代码清单](../operations/database-compatibility.md)。
- 项目许可证为 AGPL-3.0-or-later。
