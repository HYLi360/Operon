# PyPI 发布

PyPI 分发名为 `OperonDBS`，因为 `operon` 分发名已属于另一个无关项目。这不会改变
导入包名或命令名：用户安装 `OperonDBS`，在 Python 中导入 `operon`，并执行
`operon` 命令。

PEP 517 构建依赖仅包含 setuptools 与 Cython。

## GitHub Actions 发布链路

`.github/workflows/publish.yml` 只在 GitHub Release 正式发布时运行。它先验证 release
tag 必须严格等于 `v<project.version>`，再构建：

- 一份源码分发包；
- CPython 3.10-3.14 的 manylinux x86-64 wheel；
- CPython 3.10-3.14 的 macOS Intel wheel；
- CPython 3.10-3.14 的 macOS Apple Silicon wheel。

`verify-release` job 会先于其他所有 job 运行，且它们都依赖它：它检出 release tag，
   并断言"该提交的 `test` workflow 结论为 `success`"以及
   `scripts/release-preflight.sh --ci --tag <tag>` 通过。因此，tag 落在 CI 为红（或压根
   被取消、没跑完）的提交上时，什么都不会被构建，更不会被上传。

每个 wheel 都会先安装到隔离测试环境，导入编译后的 parser 并调用 CLI；源码分发包
通过 Twine 检查。最终发布 job 必须等待全部构建成功，并通过 PyPI Trusted
Publishing 认证，不保存长期 API token。

在 PyPI 中配置 trusted publisher 时，owner 填 `HYLi360`，repository 填 `Operon`，
workflow 填 `publish.yml`，environment 填 `pypi`。GitHub environment 名称必须完全
一致；可以为该 environment 增加人工审批规则，在最终上传前保留一道确认。

## 发布步骤

1. 更新 `[project].version`；如有变化，同时更新代码中的 `SCHEMA_VERSION` /
   `METADATA_SCHEMA_VERSION`。文档版本标记通过 `docs/conf_common.py` 的
   `myst_substitutions` 从这些唯一来源渲染，无需手工批量替换；
   `tests/unit/test_docs_versions.py` 会拒绝 Markdown 源文件中硬编码的当前版本。
2. 运行 `scripts/release-preflight.sh` —— 一条命令、一个退出码，就是发布闸门：
   全量 pytest 套件、两棵树语言的严格文档构建、`pyproject.toml` 版本与已安装元数据的
   一致性、缺陷登记库（凡是 `fixed_in` 为本版本的记录都必须 `verified`、带
   `fix_commit`，且该提交包含在当前树中），以及"你即将打 tag 的那个提交"的跨版本矩阵
   证据（`--run-matrix` 会把矩阵跑在闸门里；`--tag vX.Y.Z` 还会断言 create 出来的 tag
   是附注、已 GPG 签名、名为 `v<version>` 且指向 `HEAD`）。发布不再依赖谁记得住这些
   单个命令。
3. 提交发布状态，并让 `v<project.version>` tag 指向该提交；不要复用仍指向旧软件包
   元数据的 tag。
4. 等待该 tag 对应提交的 `test` workflow 全部通过。若该 run 缺失、被取消或为红，
   发布链路会拒绝构建与上传；先用 `gh run rerun <run-id>` 给它补一轮，再发布。
5. 从该 tag 创建 GitHub Release；如仍需检查发布说明可先保存为 draft，确认后发布。
6. 在 PyPI 的 `OperonDBS` 项目页核对文件与元数据。

PyPI 同一版本的文件不可覆盖。如果错误内容已经发布，应递增项目版本，而不是尝试替换。
