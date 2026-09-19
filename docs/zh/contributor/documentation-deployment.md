# 文档构建与 Read the Docs 发布

## 文档布局

文档使用 Sphinx、MyST Parser 与 Read the Docs 主题构建，同时保留 Markdown 源文件。每种语言既是独立的 Sphinx 工程，也是独立的 Read the Docs 项目：

| 源目录 | Sphinx 配置 | Read the Docs 项目 |
| --- | --- | --- |
| `docs/en/` | `docs/en/conf.py` | `operonproject`（父项目），英文 |
| `docs/zh/` | `docs/zh/conf.py` | `operonproject-zh`（翻译项目），中文（中国） |

- `docs/conf_common.py`：两个项目共享的全部设置——版本替换、MyST 选项、主题选项，以及下文所述的语言守卫。语言的 `conf.py` 只声明自己的语言、标题后缀与 Read the Docs 项目名；
- `docs/en/.readthedocs.yaml`：父项目（英文）的构建配置；
- `docs/zh/.readthedocs.yaml`：翻译项目（中文）的构建配置。Read the Docs 不接受此处使用其它文件名，因此语言由所在目录标识，每个项目在 `Build configuration file` 中填写完整路径。仓库根目录下的构建配置不会被任何项目发布；
- `docs/requirements.txt`：两个项目共用的依赖文件（`pyproject.toml` 的 `docs` extra）；
- `docs/locales/`：用于以 gettext 而非第二棵 Markdown 树来维护某种语言的目录，使用方式见其中的 README。

Read the Docs 会在项目 `Build configuration file` 设置所指定文件的所在目录中运行 Sphinx，因此 `docs/en/` 与 `docs/zh/` 同时也是 Sphinx 的源目录。这正是拆分的目的：每种语言的页面都与自己的 `conf.py` 放在一起，每个项目只构建一种语言，并各自拥有搜索索引、带语言标记的 HTML 与 URL 命名空间。

中英文文档树必须保持相同的相对路径，`tests/unit/test_docs_projects.py` 会在两者不一致时失败。新增、移动或删除页面时，请同时更新两棵树及对应的 `toctree` 条目。

## 严格本地构建

在仓库根目录、使用项目虚拟环境构建两个项目：

```bash
.venv/bin/python -m pip install -e '.[docs]'
.venv/bin/sphinx-build -W --keep-going -b html docs/en docs/_build/en/html
.venv/bin/sphinx-build -W --keep-going -b html docs/zh docs/_build/zh/html
```

`-W` 将警告视为错误，`--keep-going` 则在一次运行中尽可能多地报告问题。提交文档改动前，两条命令都必须无警告完成；CI 的 `docs` 作业运行的就是这两条命令。生成的 `docs/_build/` 目录已被 Git 忽略。

## 接入 Read the Docs

1. 在 Read the Docs 中导入 GitHub 仓库 `HYLi360/Operon`。
2. 父项目 `operonproject`：`Build configuration file` 设为 `docs/en/.readthedocs.yaml`，语言保持英文，选择要发布的默认分支并触发首次构建。
3. 用同一仓库创建翻译项目，命名 `operonproject-zh`，语言设为 `Chinese (China)`，`Build configuration file` 设为 `docs/zh/.readthedocs.yaml`。
4. 在父项目的 Translations 页面添加 `operonproject-zh`。此后 Read the Docs 会带着语言前缀在父项目的域名下提供该翻译，并在语言选择器中列出它。
5. 两个项目构建完成后，检查域名根路径（会跳转到父项目的语言）、带语言前缀的翻译地址、侧边栏的语言选择器，以及两种语言各一个深层页面。
6. 在 Read the Docs 的版本设置中只开放需要公开的分支或标签。

依赖来自 `pyproject.toml` 的 `docs` optional extra。Read the Docs 使用 Python 3.12，并在出现 Sphinx 警告时让构建失败，与本地严格构建及 CI 门禁一致。

语言选择器由 Read the Docs 主题渲染、由 Read the Docs Addons 浮层填充，因此只有在项目至少关联了一个翻译时才会出现。此前的语言选择入口页、单一的共享 `docs/conf.py` 与手写的侧边栏语言切换链接都已移除：切换语言会进入对方项目的首页，这是平台原生行为。发布路径同样发生变化——过去由单一英文项目在 `/en/latest/` 提供语言选择入口、两棵树分别位于 `/en/latest/en/` 与 `/en/latest/zh/`；现在两个互链项目把英文放在 `/en/latest/`、把中文放在 `/zh-cn/latest/`。若旧链接必须继续可用，请在 Admin -> Redirects 中添加重定向规则。

### 语言不一致会中断构建

Read the Docs 会以 `-D language=<语言>` 把项目语言传给 Sphinx，该参数覆盖配置中的声明，因此仪表盘设置才是发布结果的权威来源。共享配置会比较二者并中止不一致的构建，同时指出需要修正的项目名。若没有这道守卫，一个语言仍为默认值的中文项目会静默发布英文页面。请把仪表盘语言设为与文档树一致，或把项目指向它真正要构建的那棵树对应的配置文件。

## 新增语言

1. 新增 `docs/<语言>/`，页面与其它树保持相同相对路径，并添加调用 `apply_shared_settings(globals(), language=..., title_suffix=..., rtd_project=...)` 的 `conf.py`。
2. 新增 `docs/<语言>/.readthedocs.yaml`（Read the Docs 不接受其它文件名），并在 `tests/unit/test_docs_projects.py` 中登记该语言。
3. 创建 Read the Docs 项目，设置其语言与 `Build configuration file`，并加入父项目的 Translations 页面。

若某种语言改用 gettext 目录而非第二棵 Markdown 树来维护，可在其 `conf.py` 中声明 `language = "auto"`，构建时语言将取自 Read the Docs 而非声明值。目录结构与提取方式见 `docs/locales/README.md`。
