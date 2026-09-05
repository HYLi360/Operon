# PyPI 发布

PyPI 分发名为 `OperonDBS`，因为 `operon` 分发名已属于另一个无关项目。这不会改变
导入包名或命令名：用户安装 `OperonDBS`，在 Python 中导入 `operon`，并执行
`operon` 命令。

PyPI 发布与独立应用发布相互独立。PEP 517 构建依赖只包含 setuptools 和 Cython；
cx_Freeze 留在可选 `build` extra 中，仅由 `python tools/build.py` 使用。

## GitHub Actions 发布链路

`.github/workflows/publish.yml` 只在 GitHub Release 正式发布时运行。它先验证 release
tag 必须严格等于 `v<project.version>`，再构建：

- 一份源码分发包；
- CPython 3.10-3.14 的 manylinux x86-64 wheel；
- CPython 3.10-3.14 的 macOS Intel wheel；
- CPython 3.10-3.14 的 macOS Apple Silicon wheel。

每个 wheel 都会先安装到隔离测试环境，导入编译后的 parser 并调用 CLI；源码分发包
通过 Twine 检查。最终发布 job 必须等待全部构建成功，并通过 PyPI Trusted
Publishing 认证，不保存长期 API token。

在 PyPI 中配置 trusted publisher 时，owner 填 `HYLi360`，repository 填 `Operon`，
workflow 填 `publish.yml`，environment 填 `pypi`。GitHub environment 名称必须完全
一致；可以为该 environment 增加人工审批规则，在最终上传前保留一道确认。

## 发布步骤

1. 更新 `[project].version`；如有变化，同时更新代码中的 `SCHEMA_VERSION` /
   `METADATA_SCHEMA_VERSION`。文档版本标记通过 `docs/conf.py` 的
   `myst_substitutions` 从这些唯一来源渲染，无需手工批量替换；
   `tests/unit/test_docs_versions.py` 会拒绝 Markdown 源文件中硬编码的当前版本。
2. 运行完整 pytest 套件与严格文档构建。
3. 提交发布状态，并让 `v<project.version>` tag 指向该提交；不要复用仍指向旧软件包
   元数据的 tag。
4. 等待该 tag 对应提交的 `deploy` workflow 全部通过。
5. 从该 tag 创建 GitHub Release；如仍需检查发布说明可先保存为 draft，确认后发布。
6. 在 PyPI 的 `OperonDBS` 项目页核对文件与元数据。

PyPI 同一版本的文件不可覆盖。如果错误内容已经发布，应递增项目版本，而不是尝试替换。
