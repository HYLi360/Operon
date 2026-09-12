# 仓库协作规范

## 项目原则

五条核心设计不变量统一在[架构总览](../architecture/overview.md)中说明；改动代码时必须保持不变。

## 仓库结构

- `operon/`：Python 包；CLI 入口为 `operon/cli.py` 和 `operon/__main__.py`。
- `operon/adapters/`：外部来源适配器，目前包括 NCBI Datasets。
- `operon/qc/`：所有 QC 功能的家——流式序列文件解析器、内置 QC，以及比对 QC（`alignment.py`）。`parsers.py` 是纯 Python 行为参考；`_parsers.pyx` 是生产使用的 Cython 实现。两者必须通过 parity 回归测试保持指标和错误文本一致。
- `operon/execution.py`：`local`、`slurm` 和 `ssh` 执行后端。
- `operon/remotes.py`：SFTP 镜像、push/pull 和远程 URL 下载。
- `tests/`：`unit/`、`integration/`、`regression/`、`compatibility/` 测试。
- `docs/`：用户、架构和运维文档。

## 协作约定

- 以 `pyproject.toml` 为唯一依赖事实来源。新增运行时依赖必须获得明确授权，并放入范围最小的 optional extra。
- 代码、注释、docstring 和提交信息使用英文；用户文档同时维护中文和英文版本。
- 标题使用 `Operon`；正文中的命令行工具写作 `` `operon` ``。
- 不得在 QC 代码中硬编码阈值。
- 不得静默覆盖归档文件；相同字节幂等，同实体同角色不同字节必须抛出 `ConflictError`。
- `curate`、强制 `set-state` 等人工修改必须写入 `changes`。
- 修改数据库迁移或 NCBI adapter schema 升级路径前，先阅读[数据库兼容代码清单](../operations/database-compatibility.md)。
- 项目许可证为 AGPL-3.0-or-later。
