# 文档构建与 Read the Docs 发布

## 文档结构

文档使用 Sphinx、MyST Parser 和 Read the Docs 主题构建，正文继续使用 Markdown：

- `docs/zh/`：中文文档；
- `docs/en/`：英文文档；
- `docs/index.md`：中立的语言选择入口；
- `docs/conf.py`：共用的 Sphinx 配置；
- `.readthedocs.yaml`：Read the Docs 构建环境与安装步骤。

中英文目录应保持相同的相对文件路径。页面侧栏中的语言链接据此跳转到另一语言的同一页面；新增、移动或删除页面时，必须同时更新两棵目录及相应 `toctree`。

## 本地严格构建

在仓库根目录使用项目虚拟环境：

```bash
.venv/bin/python -m pip install -e '.[docs]'
.venv/bin/sphinx-build -W --keep-going -b html docs docs/_build/html
```

`-W` 将警告视为错误；`--keep-going` 会在一次运行中列出尽可能多的问题。生成目录 `docs/_build/` 已被 Git 忽略。提交文档变更前必须保证该命令无警告通过。

## 接入 Read the Docs

1. 在 Read the Docs 中导入 GitHub 仓库 `HYLi360/Operon`。
2. 保持配置文件路径为仓库根目录下的 `.readthedocs.yaml`。
3. 选择需要发布的默认分支并触发首次构建。
4. 构建完成后检查根页面、`/zh/`、`/en/`，以及任意深层页面的语言切换链接。
5. 在 Read the Docs 的版本设置中只启用需要公开的分支或标签。

依赖从 `pyproject.toml` 的 `docs` optional extra 安装。RTD 使用 Python 3.12，并在 Sphinx 警告出现时使构建失败，因此其结果与本地严格构建及 CI 门禁一致。

当前方案在一个 RTD 项目中发布现有的两棵完整 Markdown 文档树，不要求把正文改写成 gettext PO 文件。如果未来需要让 RTD 顶部语言菜单、各语言独立搜索索引或不同翻译版本生命周期由平台原生管理，可以在 RTD 中创建关联的 translation project；迁移时仍可复用当前中英文正文和相对路径约定。
