# 应用程序发布

## 应用发布文件结构

这条链路与发布到 PyPI 的 `OperonDBS` 软件包相互独立。PyPI wheel 与 sdist 不调用、
也不依赖 cx_Freeze；项目仅保留 cx_Freeze 来构建可选的独立应用目录。完整独立应用发布
只有一个入口：

```bash
python -m pip install -e '.[build]'
python tools/build.py
```

`build` extra 包含 cx_Freeze、Cython、Sphinx/MyST/RTD 主题，以及 Python 3.10 读取
`pyproject.toml` 所需的条件依赖 `tomli`；Python 3.11 及以上直接使用标准库
`tomllib`，不会安装 `tomli`。Paramiko 与 Textual 是标准运行时依赖，因此安装该
extra 时也会存在。Python 3.10 和 3.11+ 使用相同的构建命令。

`tools/build.py` 依次重建必需的 Cython parser、严格构建双语 Sphinx HTML、解析
`pyproject.toml` 中唯一的应用版本号、收集冻结运行时依赖与渲染文档资产的许可证、
生成对应源码 sdist、调用 cx_Freeze、组装并验证最终目录。任一步失败都不会发布目标版本；
已存在的同版本目录默认拒绝覆盖，只有显式
传入 `--force` 才会替换。不要直接调用 cx_Freeze 制作正式发布包，因为那会跳过许可证、
对应源码和最终 smoke test。

发布内容固定落在版本化目录：

Linux 构建机还需要系统命令 `patchelf`；它是 cx_Freeze 处理 ELF 依赖的构建期工具，
不属于 `operon` 的 Python 运行时依赖。缺少时 cx_Freeze 会在 `build_exe` 阶段直接停止。

```text
build/release/v<version>/
├── operon                  # 命令行可执行文件；Windows 为 operon.exe
├── lib/                    # Python 运行时、operon 包与第三方依赖
├── LICENSE                 # Operon 自身的许可证（AGPL-3.0-or-later）
├── licenses/               # THIRD_PARTY_NOTICES.md 与各第三方依赖的许可证全文
├── source/
│   └── operondbs-<version>.tar.gz # 对应本次二进制的完整项目源码 sdist
├── frozen_application_license.txt  # cx_Freeze 自动附带的冻结引导代码许可证
└── share/doc/operon/
    ├── README.md           # 英文项目说明
    ├── README_ZH.md        # 中文项目说明
    └── html/               # 可直接浏览的双语 Sphinx HTML 站点
```

目录名和源码包名中的版本均动态读取 `[project].version`；代码中不维护第二份版本号。
安装后的 `operon.__version__` 通过分发元数据读取同一值。源码包由 `MANIFEST.in`
控制，包含 Python/Cython 源码、构建脚本、测试、双语 Sphinx 文档及其 RTD 配置和许可证，
排除 `.so`、`.pyd`、
生成的 C 文件、缓存以及本地运行数据。

应用发布包不会直接复制仓库的 `docs/` 源目录。Sphinx 在临时 staging 目录中以
`-W --keep-going` 严格构建，只有完整的 HTML 结果会复制到 `share/doc/operon/html/`；
Markdown、`conf.py`、RTD 配置和模板则保存在 `source/` 下的对应源码 sdist 中。
正式 HTML 关闭源码副本和“查看源码”链接，doctree 增量缓存保存在独立临时目录且构建后
删除；因此本地 `docs/_build/`、pickle 缓存和重复 Markdown 都不会污染正式发布包。

运行时版本读取依赖 `importlib.metadata`，后者会通过标准库 `email` 解析分发元数据。
cx_Freeze 的静态分析不能稳定发现这一间接导入，因此 `[tool.cxfreeze.build_exe].packages`
显式包含整个 `email` 包；不要将其视为未使用模块删除，否则冻结程序可能在启动阶段因
缺少 `email.header` 而退出。

PyPI 分发名（`OperonDBS`）与导入包名（`operon`）不同，因此 cx_Freeze 无法推断该包
对应哪一份分发元数据。发布构建器会在版本 smoke test 前显式把已安装的
`operondbs-<version>.dist-info` 目录复制到冻结库中；否则独立可执行程序中的
`importlib.metadata.version("OperonDBS")` 会回退为 `0+unknown`。

应用发布目录与 `operon release` 生成的数据集快照是两个不同概念：前者交付程序，
后者交付经过筛选并可校验的数据。
